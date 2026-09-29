"""Tests for the certificate sweep today-forward cutoff.

Covers (all offline, fakes only):
- the cutoff constant is exactly 2026-09-27 00:00 America/New_York
- timezone-boundary intake: 23:59:59 ET skipped, 00:00:00 ET processed
- skipped messages are not checkpointed and never ledgered
- the historical-backlog parking migration (date-checked, idempotent,
  dead-message handling, fail-closed on undatable messages)
- the defensive re-drive park for pre-cutoff messages
- the index-miss ledger for current NO_MATCH requests
- the notifier's noteworthy/quiet behavior (never raises)
- the index-refresh dry run, row-ratio gate, and miss coverage
"""

import base64
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from robie_job_engine.cert_applicant_index import build_index
from robie_job_engine.cert_intake_runner import (
    message_internal_ms, run_intake_once,
)
from robie_job_engine.cert_notify import (
    is_noteworthy, notify_summary, noteworthy, render_message,
)
from robie_job_engine.cert_sweep import (
    CUTOFF_ET, CUTOFF_MS, RETRY_MAX_ATTEMPTS, _reintake_message,
    _sweep_once, gmail_query, miss_note, open_retry_db,
    park_pre_cutoff_backlog, retry_note, retry_park, retry_pending,
)


ET = ZoneInfo("America/New_York")


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def gmail_payload(*, gid="g1", internal_ms=None,
                  subject="Please issue a certificate for Acme LLC",
                  from_header="Bob <bob@acme.example>",
                  body="Named Insured: Acme LLC\n"
                       "Certificate Holder: Big Client Inc"):
    payload = {
        "id": gid,
        "threadId": "t1",
        "payload": {
            "headers": [
                {"name": "From", "value": from_header},
                {"name": "Subject", "value": subject},
                {"name": "Message-ID", "value": f"<{gid}@example.com>"},
                {"name": "Date",
                 "value": "Sat, 27 Sep 2026 08:00:00 -0400"},
            ],
            "parts": [
                {"mimeType": "text/plain", "filename": "",
                 "body": {"data": _b64(body)}},
            ],
        },
    }
    if internal_ms is not None:
        payload["internalDate"] = str(internal_ms)
    return payload


class FakeGmail:
    def __init__(self, payloads=None, dead=()):
        self.payloads = payloads or {}
        self.dead = set(dead)

    def list_message_ids(self, query, page_token, page_size=50):
        return list(self.payloads), None

    def get_full_message(self, gmail_id):
        if gmail_id in self.dead:
            raise RuntimeError("Gmail GET failed: 404")
        return self.payloads[gmail_id]


class FakeCheckpoint:
    def __init__(self):
        self.seen_keys = set()

    def seen(self, key):
        return key in self.seen_keys

    def mark(self, key, meta=None):
        self.seen_keys.add(key)


def make_index():
    return build_index([{
        "account_name": "Acme LLC",
        "applicant_id": "220250093",
        "email_primary": "bob@acme.example",
        "phones": [],
    }], source_path="test")


def et_ms(year, month, day, hour, minute=0, second=0):
    return int(datetime(year, month, day, hour, minute, second,
                        tzinfo=ET).timestamp() * 1000)


# ---------------------------------------------------------------------------
# Cutoff constant and query
# ---------------------------------------------------------------------------

def test_cutoff_is_exactly_2026_09_27_midnight_et():
    assert CUTOFF_ET.isoformat() == "2026-09-27T00:00:00-04:00"
    # EDT is UTC-4: the epoch boundary is 2026-09-27T04:00:00Z.
    expected = int(datetime(2026, 9, 27, 4, 0,
                            tzinfo=timezone.utc).timestamp() * 1000)
    assert CUTOFF_MS == expected


def test_gmail_query_keeps_one_slack_day_before_cutoff():
    assert gmail_query() == "after:2026/09/26"


def test_message_internal_ms_reads_gmail_internal_date():
    assert message_internal_ms({"internalDate": "1758945600000"}) == \
        1758945600000
    assert message_internal_ms({}) is None
    assert message_internal_ms(None) is None
    assert message_internal_ms({"internalDate": "bogus"}) is None


# ---------------------------------------------------------------------------
# Timezone-boundary intake
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("when_ms,expected", [
    # 2026-09-26 23:59:59 ET -> before the cutoff -> skipped
    (et_ms(2026, 9, 26, 23, 59, 59), "skipped"),
    # exactly 2026-09-27 00:00:00 ET -> at the cutoff -> processed
    (et_ms(2026, 9, 27, 0, 0, 0), "processed"),
    # 2026-09-27 00:00:01 ET -> after -> processed
    (et_ms(2026, 9, 27, 0, 0, 1), "processed"),
])
def test_cutoff_boundary_is_evaluated_in_et(when_ms, expected):
    gmail = FakeGmail({"g1": gmail_payload(gid="g1", internal_ms=when_ms)})
    store = FakeCheckpoint()
    out = run_intake_once(gmail, store, make_index(), query="after:2026/09/26",
                          cutoff_ms=CUTOFF_MS)
    if expected == "skipped":
        assert out["stats"]["skipped_pre_cutoff"] == 1
        assert out["stats"]["processed"] == 0
        assert out["records"] == []
        # Not checkpointed: a cutoff change could revisit it.
        assert store.seen_keys == set()
    else:
        assert out["stats"]["skipped_pre_cutoff"] == 0
        assert out["stats"]["processed"] == 1
        assert len(out["records"]) == 1
        assert store.seen_keys  # checkpointed (3 dedupe keys per message)


def test_no_cutoff_means_legacy_behavior():
    gmail = FakeGmail({"g1": gmail_payload(
        gid="g1", internal_ms=et_ms(2026, 9, 20, 12))})
    out = run_intake_once(gmail, FakeCheckpoint(), make_index(),
                          query="q", cutoff_ms=None)
    assert out["stats"]["skipped_pre_cutoff"] == 0
    assert len(out["records"]) == 1


def test_missing_internal_date_is_processed_not_skipped():
    # Fail-closed direction: when Gmail gives no date, the message is
    # processed (the query window already bounds discovery).
    gmail = FakeGmail({"g1": gmail_payload(gid="g1", internal_ms=None)})
    out = run_intake_once(gmail, FakeCheckpoint(), make_index(),
                          query="q", cutoff_ms=CUTOFF_MS)
    assert out["stats"]["skipped_pre_cutoff"] == 0
    assert len(out["records"]) == 1


# ---------------------------------------------------------------------------
# Parking migration
# ---------------------------------------------------------------------------

def _seed_retry(tmp_path, rows):
    db_path = str(tmp_path / "cert-sweep.db")
    conn = open_retry_db(db_path)
    for gmail_id, first_seen in rows:
        conn.execute(
            "INSERT INTO cert_sweep_retry (gmail_id, reason, attempts,"
            " first_seen_at, last_attempt_at) VALUES (?,?,?,?,?)",
            (gmail_id, "no applicant in the full-book report", 3,
             first_seen, first_seen))
    conn.commit()
    return conn


def test_migration_parks_only_pre_cutoff_messages(tmp_path):
    old_ms = et_ms(2026, 9, 26, 18)          # pre-cutoff
    new_ms = et_ms(2026, 9, 27, 9, 30)       # post-cutoff (Laney's window)
    gmail = FakeGmail({
        "old1": gmail_payload(gid="old1", internal_ms=old_ms),
        "new1": gmail_payload(gid="new1", internal_ms=new_ms),
    })
    conn = _seed_retry(tmp_path, [("old1", "2026-09-26T20:00:00"),
                                  ("new1", "2026-09-27T13:00:00")])
    result = park_pre_cutoff_backlog(conn, gmail)
    assert result["parked_pre_cutoff"] == 1
    assert result["checked"] == 2

    rows = {r["gmail_id"]: r for r in retry_pending(conn)}
    assert rows["old1"]["attempts"] == RETRY_MAX_ATTEMPTS + 1
    assert rows["old1"]["reason"].startswith("[PARKED")
    assert "2026-09-27" in rows["old1"]["reason"]
    # Post-cutoff row untouched.
    assert rows["new1"]["attempts"] == 3
    assert not rows["new1"]["reason"].startswith("[PARKED")
    conn.close()


def test_migration_is_idempotent(tmp_path):
    old_ms = et_ms(2026, 9, 25, 10)
    gmail = FakeGmail({"old1": gmail_payload(gid="old1",
                                             internal_ms=old_ms)})
    conn = _seed_retry(tmp_path, [("old1", "2026-09-25T12:00:00")])
    first = park_pre_cutoff_backlog(conn, gmail)
    second = park_pre_cutoff_backlog(conn, gmail)
    assert first["parked_pre_cutoff"] == 1
    assert second["parked_pre_cutoff"] == 0
    assert second["checked"] == 0  # parked rows are never re-checked
    conn.close()


def test_migration_leaves_undatable_messages_alone(tmp_path):
    gmail = FakeGmail({"nodate": gmail_payload(gid="nodate",
                                               internal_ms=None)})
    conn = _seed_retry(tmp_path, [("nodate", "2026-09-26T20:00:00")])
    result = park_pre_cutoff_backlog(conn, gmail)
    assert result["parked_pre_cutoff"] == 0
    rows = {r["gmail_id"]: r for r in retry_pending(conn)}
    assert rows["nodate"]["attempts"] == 3  # still active
    conn.close()


def test_migration_parks_dead_messages_after_repeated_failures(tmp_path):
    from robie_job_engine.cert_sweep import FETCH_FAILURES_BEFORE_PARK
    gmail = FakeGmail({}, dead={"gone1"})
    conn = _seed_retry(tmp_path, [("gone1", "2026-09-26T20:00:00")])
    for _ in range(FETCH_FAILURES_BEFORE_PARK - 1):
        result = park_pre_cutoff_backlog(conn, gmail)
        assert result["parked_dead"] == 0
    result = park_pre_cutoff_backlog(conn, gmail)
    assert result["parked_dead"] == 1
    rows = {r["gmail_id"]: r for r in retry_pending(conn)}
    assert rows["gone1"]["attempts"] == RETRY_MAX_ATTEMPTS + 1
    assert "no longer retrievable" in rows["gone1"]["reason"]
    conn.close()


def test_retry_park_keeps_row_and_pins_attempts(tmp_path):
    conn = open_retry_db(str(tmp_path / "cert-sweep.db"))
    retry_note(conn, "g9", "some reason")
    parked = retry_park(conn, "g9", "test parking")
    assert parked["parked"] is True
    assert parked["attempts"] == RETRY_MAX_ATTEMPTS + 1
    rows = retry_pending(conn)
    assert len(rows) == 1  # kept, not deleted
    assert rows[0]["reason"].startswith("[PARKED")
    conn.close()


def test_reintake_returns_none_for_pre_cutoff(tmp_path):
    old_ms = et_ms(2026, 9, 26, 18)
    gmail = FakeGmail({"old1": gmail_payload(gid="old1",
                                             internal_ms=old_ms)})
    assert _reintake_message(gmail, "old1", make_index()) is None


def test_reintake_returns_record_for_post_cutoff():
    new_ms = et_ms(2026, 9, 27, 9, 30)
    gmail = FakeGmail({"new1": gmail_payload(gid="new1",
                                             internal_ms=new_ms)})
    record = _reintake_message(gmail, "new1", make_index())
    assert record is not None
    assert record.gmail_id == "new1"
    assert record.internal_ms == new_ms


# ---------------------------------------------------------------------------
# Sweep-level: cutoff stats, misses, precise hold reasons
# ---------------------------------------------------------------------------

def test_sweep_once_skips_pre_cutoff_and_records_miss(tmp_path):
    old_ms = et_ms(2026, 9, 26, 18)
    new_ms = et_ms(2026, 9, 27, 9, 30)
    gmail = FakeGmail({
        # Pre-cutoff: skipped before intake.
        "old1": gmail_payload(gid="old1", internal_ms=old_ms,
                              body="Named Insured: Old Co\n"),
        # Post-cutoff, unknown insured -> NO_MATCH -> miss recorded.
        # (Sender is the certificate holder's address — holder senders
        # are never searched as applicants, mirroring the real case.)
        "new1": gmail_payload(gid="new1", internal_ms=new_ms,
                              subject="COI request for Laney Express",
                              from_header="Highway App "
                                          "<insurance@certs.highway.com>",
                              body="Named Insured: LANEY EXPRESS TRUCKING LLC\n"
                                   "Certificate Holder: Highway App, Inc."),
    })
    checkpoint = FakeCheckpoint()
    conn = open_retry_db(str(tmp_path / "cert-sweep.db"))
    summary = _sweep_once(
        gmail=gmail, checkpoint=checkpoint, index=make_index(),
        verifier=None, verifier_note="test", deps=None,
        retry_conn=conn, query=gmail_query(),
        now=datetime(2026, 9, 28, 10, 30, tzinfo=timezone.utc))
    assert summary["stats"]["skipped_pre_cutoff"] == 1
    assert summary["stats"]["index_misses"] == 1
    # The NO_MATCH hold reason names the index consulted.
    unverified = summary["unverified"]
    assert len(unverified) == 1
    assert unverified[0]["gmail_id"] == "new1"
    assert "index:" in unverified[0]["reason"]
    assert "miss recorded" in unverified[0]["reason"]
    # Miss ledger has the normalized name.
    misses = conn.execute(
        "SELECT name_key, raw_name, occurrences FROM cert_index_misses"
    ).fetchall()
    assert misses == [("laneyexpresstruckingllc",
                       "LANEY EXPRESS TRUCKING LLC", 1)]
    # The pre-cutoff message never reached the retry ledger.
    pending = {r["gmail_id"] for r in retry_pending(conn)}
    assert "old1" not in pending
    conn.close()


def test_miss_note_dedupes_and_counts_occurrences(tmp_path):
    conn = open_retry_db(str(tmp_path / "cert-sweep.db"))
    miss_note(conn, "Laney Express Trucking LLC", "g1")
    miss_note(conn, "LANEY EXPRESS TRUCKING LLC", "g2")
    assert miss_note(conn, "", "g3") is None
    rows = conn.execute(
        "SELECT name_key, occurrences FROM cert_index_misses").fetchall()
    assert rows == [("laneyexpresstruckingllc", 2)]
    conn.close()


# ---------------------------------------------------------------------------
# Notifier
# ---------------------------------------------------------------------------

def _summary(**overrides):
    base = {"sweep_at": "2026-09-28T10:30:00+00:00", "filed": [],
            "unverified": [], "errors": [],
            "stats": {"filed": 0, "unverified": 0}}
    base.update(overrides)
    return base


def test_notifier_quiet_on_empty_sweep(tmp_path):
    summary = _summary()
    assert not is_noteworthy(noteworthy(summary))
    assert render_message(noteworthy(summary)) is None
    result = notify_summary(summary, data_dir=str(tmp_path),
                            chat_poster=lambda text: {"name": "x"})
    assert result["notified"] is False
    assert not os.path.exists(os.path.join(str(tmp_path),
                                           "notifications.jsonl"))


def test_notifier_reports_filed_and_unverified(tmp_path):
    summary = _summary(
        filed=[{"subject": "COI request for Acme LLC",
                "documents": ["a", "b"], "task_id": "t1",
                "marked_read": True}],
        unverified=[{"subject": "COI request for Laney Express",
                     "reason": "no applicant in the full-book report"}],
        stats={"filed": 1, "unverified": 1})
    parts = noteworthy(summary)
    assert is_noteworthy(parts)
    text = render_message(parts)
    assert "Acme LLC" in text
    assert "Steffany's review task created and confirmed" in text
    assert "Laney Express" in text
    posted = {}

    def poster(t):
        posted["text"] = t
        return {"name": "spaces/x/messages/y"}

    result = notify_summary(summary, data_dir=str(tmp_path),
                            chat_poster=poster)
    assert result["notified"] is True
    assert result["chat"] == {"posted": {"name": "spaces/x/messages/y"}}
    assert "Acme LLC" in posted["text"]
    log_path = os.path.join(str(tmp_path), "notifications.jsonl")
    assert os.path.exists(log_path)
    line = json.loads(open(log_path).read().strip())
    assert line["filed"] == 1 and line["unverified"] == 1


def test_notifier_never_raises_when_chat_is_down(tmp_path):
    summary = _summary(errors=["boom"])
    def poster(t):
        raise RuntimeError("chat outage")
    result = notify_summary(summary, data_dir=str(tmp_path),
                            chat_poster=poster)
    assert result["notified"] is True  # the log line is the durable record
    assert "failed" in result["chat"]


def test_notifier_skips_chat_when_identity_unconfigured(tmp_path):
    summary = _summary(filed=[{"subject": "x"}])
    # chat_poster=None -> default Chat-app path; must not raise even
    # without ROBIE_CHAT_SA_KEY_FILE configured. Fail-soft means either
    # "skipped" (no identity) or "failed" (identity broken) — both are
    # recorded, neither raises.
    result = notify_summary(summary, data_dir=str(tmp_path),
                            chat_poster=None)
    assert result["chat"].get("skipped") or result["chat"].get("failed")
    assert result["notified"] is True  # the log line is the durable record


# ---------------------------------------------------------------------------
# Index refresh
# ---------------------------------------------------------------------------

def _write_live_csv(path, rows):
    import csv as csvmod
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csvmod.DictWriter(fh, fieldnames=[
            "account_name", "applicant_id", "dba", "email_primary",
            "email_business", "phone_cell", "phone_home", "phone_work"])
        w.writeheader()
        for account, app_id in rows:
            w.writerow({"account_name": account, "applicant_id": app_id,
                        "dba": "", "email_primary": "", "email_business": "",
                        "phone_cell": "", "phone_home": "", "phone_work": ""})


def test_refresh_dry_run_reports_diff_and_miss_coverage(tmp_path):
    from robie_job_engine.cert_index_refresh import refresh_report
    live = str(tmp_path / "live.csv")
    _write_live_csv(live, [("Acme LLC", "220250093"),
                           ("Old Corp", "111")])
    source = str(tmp_path / "export.csv")
    with open(source, "w", encoding="utf-8") as fh:
        fh.write("Account Name,Applicant ID,Email - Primary\n"
                 "Acme LLC,220250093,bob@acme.example\n"
                 "LANEY EXPRESS TRUCKING LLC,999001,ops@laney.example\n")
    conn = open_retry_db(str(tmp_path / "cert-sweep.db"))
    miss_note(conn, "LANEY EXPRESS TRUCKING LLC", "g2")
    conn.close()

    report = refresh_report(source, live, data_dir=str(tmp_path),
                            apply=False)
    assert report["applied"] is False
    assert "laneyexpresstruckingllc" in report["names_added"]
    assert "oldcorp" in report["names_removed"]
    assert report["unresolved_misses"] == 1
    covered = report["misses_now_covered"]
    assert len(covered) == 1
    assert covered[0]["applicant_id"] == 999001
    # Dry run: the live CSV is untouched.
    assert "LANEY" not in open(live).read()


def test_refresh_refuses_shrunken_export(tmp_path):
    from robie_job_engine.cert_index_refresh import refresh_report
    live = str(tmp_path / "live.csv")
    _write_live_csv(live, [(f"Corp {i}", str(i)) for i in range(100)])
    source = str(tmp_path / "export.csv")
    with open(source, "w", encoding="utf-8") as fh:
        fh.write("Account Name,Applicant ID\nTiny Co,1\n")
    report = refresh_report(source, live, data_dir=str(tmp_path),
                            apply=True)
    assert report["applied"] is False
    assert "error" in report and "sanity gate" in report["error"]
    assert "Corp 0" in open(live).read()  # untouched


def test_refresh_apply_writes_atomically_with_backup(tmp_path):
    from robie_job_engine.cert_index_refresh import refresh_report
    live = str(tmp_path / "live.csv")
    _write_live_csv(live, [("Acme LLC", "220250093")])
    source = str(tmp_path / "export.csv")
    with open(source, "w", encoding="utf-8") as fh:
        fh.write("Account Name,Applicant ID\nAcme LLC,220250093\n"
                 "New Co,222\n")
    report = refresh_report(source, live, data_dir=str(tmp_path),
                            apply=True, mark_resolved=True)
    assert report["applied"] is True
    assert os.path.isfile(report["backup"])
    assert "New Co" in open(live).read()
