"""Offline tests for the certificate sweep driver (cert_sweep).

Everything runs against fakes. No Gmail, no EZLynx, no Zapier, no network.

Covered:
- all-new-mail-filed path (real verify_record + real file_record, fake ports)
- ambiguous-match-held path (file step never reached)
- zapier-missing -> UNVERIFIED path (nothing filed, reason recorded)
- missing index CSV -> clean fail-closed
- retry ledger round-trip, parking, and re-drive of UNVERIFIED records
"""

import base64
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from robie_job_engine.cert_applicant_index import (
    AMBIGUOUS, MATCHED, MatchResult, build_index,
)
from robie_job_engine.cert_checkpoint import SqliteDedupeStore
from robie_job_engine.cert_filing import FilingDeps, FilingStore
from robie_job_engine.cert_intake_runner import IntakeRecord
from robie_job_engine.cert_sweep import (
    _sweep_once, build_filing_deps, gmail_query,
    load_applicant_index, main, open_retry_db, retry_clear, retry_note,
    retry_pending, run_sweep,
)
from robie_job_engine.cert_task_registry import TaskRegistry


APPLICANT_ID = 220250093


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def gmail_payload(*, gid="g1",
                  subject="Please issue a certificate for Acme LLC",
                  body="Named Insured: Acme LLC\n"
                       "Certificate Holder: Big Client Inc"):
    return {
        "id": gid,
        "threadId": "t1",
        "payload": {
            "headers": [
                {"name": "From", "value": "Bob <bob@acme.example>"},
                {"name": "Subject", "value": subject},
                {"name": "Message-ID", "value": "<m1@example.com>"},
                {"name": "Date", "value": "Fri, 25 Sep 2026 10:00:00 -0400"},
            ],
            "parts": [
                {"mimeType": "text/plain", "filename": "",
                 "body": {"data": _b64(body)}},
            ],
        },
    }


class FakeGmail:
    """Port shape of CertGmailAdapter; payloads keyed by gmail id."""

    def __init__(self, payloads=None):
        self.payloads = payloads or {}

    def list_message_ids(self, query, page_token, page_size=50):
        return list(self.payloads), None

    def get_full_message(self, gmail_id):
        return self.payloads[gmail_id]

    def get_attachment_bytes(self, gmail_id, attachment_id):
        raise AssertionError("no attachments in these payloads")


class FakeCheckpoint:
    def __init__(self):
        self.seen_keys = set()
        self.marks = []

    def seen(self, key):
        return key in self.seen_keys

    def mark(self, key, meta=None):
        self.seen_keys.add(key)
        self.marks.append((key, meta))


class FakeDiscussions:
    def __init__(self, rows):
        self.rows = rows

    def get_discussions(self, applicant_id):
        return self.rows


class FakeVerifier:
    def __init__(self, discussions=()):
        self._discussions = list(discussions)

    def search_policies(self, policy_number):
        return []

    def get_discussions(self, applicant_id):
        return self._discussions


class FakeNoteWriter:
    def __init__(self):
        self.calls = []

    def __call__(self, applicant_id, note_text, **kw):
        self.calls.append((applicant_id, note_text, kw))
        return {"status": "filed", "note_id": "n1", "discussion_id": "d1",
                "read_back": True}


class FakeDocWriter:
    def __init__(self):
        self.calls = []

    def __call__(self, applicant_id, document_name, file_bytes, **kw):
        self.calls.append((applicant_id, document_name, kw))
        return {"document_id": "doc9", "read_back": True}


class FakeDocSearcher:
    def __call__(self, applicant_id):
        return {"results": []}


class FakeZapier:
    def __init__(self):
        self.created = []

    def get_task_state(self, task_id):
        return "unknown"

    def create_task(self, **kw):
        self.created.append(kw)
        return SimpleNamespace(fired=True, reason="fake-fired")

    def reopen_task(self, **kw):
        raise AssertionError("reopen not expected")


def make_index():
    return build_index(
        [{"account_name": "Acme LLC", "applicant_id": str(APPLICANT_ID),
          "email_primary": "bob@acme.example", "phones": []}],
        source_path="test",
    )


def make_record(**kw):
    facts = SimpleNamespace(
        insured_name="Acme LLC", policy_numbers=[],
        holder_names=["Big Client Inc"], requester_name="Bob",
        requester_email="bob@acme.example",
        requester_is_third_party=False,
        pdf_unreadable=False, pdf_texts=[],
    )
    rec = IntakeRecord(
        gmail_id="g1", thread_id="t1",
        subject="Please issue a certificate for Acme LLC",
        from_header="Bob <bob@acme.example>",
        date="Fri, 25 Sep 2026 10:00:00 -0400",
        facts=facts,
        match=MatchResult(status=MATCHED, applicant_id=APPLICANT_ID),
        attachment_count=0,
    )
    for k, val in kw.items():
        setattr(rec, k, val)
    return rec


def make_deps(tmp_path):
    rows = [{"id": "d1", "title": "Certificate request - Big Client Inc",
             "noteCount": 2}]
    db = os.path.join(str(tmp_path), "sweep.db")
    return FilingDeps(
        discussions_client=FakeDiscussions(rows),
        verifier=FakeVerifier(discussions=rows),
        note_writer=FakeNoteWriter(),
        doc_writer=FakeDocWriter(),
        doc_searcher=FakeDocSearcher(),
        zapier=FakeZapier(),
        registry=TaskRegistry(db),
        store=FilingStore(db),
    )


def sweep_harness(tmp_path, records, **kw):
    """Run _sweep_once with fakes. Returns (summary, ctx)."""
    db_path = os.path.join(str(tmp_path), "sweep.db")
    retry_conn = open_retry_db(db_path)

    def intake_fn(gmail, checkpoint, index, *, query, now):
        return {"records": list(records),
                "stats": {"discovered": len(records),
                          "matched": len(records), "held": 0, "errors": 0}}

    ctx = {"retry_conn": retry_conn, "db_path": db_path,
           "deps": kw.get("deps")}
    summary = _sweep_once(
        gmail=FakeGmail(), checkpoint=FakeCheckpoint(), index=make_index(),
        verifier=None, verifier_note="fake", deps=kw.get("deps"),
        retry_conn=retry_conn,
        query="after:2026/09/20",
        now=datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc),
        intake_fn=intake_fn,
        file_record_fn=kw.get("file_record_fn"),
        deps_error=kw.get("deps_error", ""),
    )
    return summary, ctx


# ---------------------------------------------------------------------------
# Path 1: all new mail filed
# ---------------------------------------------------------------------------

def test_all_new_mail_filed(tmp_path):
    deps = make_deps(tmp_path)
    summary, ctx = sweep_harness(tmp_path, [make_record()], deps=deps)

    assert summary["errors"] == []
    assert len(summary["filed"]) == 1
    assert summary["unverified"] == []
    filed = summary["filed"][0]
    assert filed["gmail_id"] == "g1"
    assert filed["applicant_id"] == APPLICANT_ID
    assert filed["note_id"] == "n1"
    assert filed["discussion_id"] == "d1"
    # The email PDF is always filed first, even with no attachments.
    assert filed["documents"] == ["doc9"]
    assert deps.doc_writer.calls[0][1].startswith("COI request email - ")
    # The review task was fired through the (fake) Zapier path.
    assert len(deps.zapier.created) == 1
    assert deps.zapier.created[0]["applicant_id"] == APPLICANT_ID
    # The production client defaults to Steffany Canales' EZLynx login.
    from robie_job_engine.cert_zapier import ASSIGNEE_SCANALES
    assert ASSIGNEE_SCANALES == "SCanales"
    # Retry ledger is clean after a FILED outcome.
    assert retry_pending(ctx["retry_conn"]) == []
    ctx["retry_conn"].close()


def test_filed_summary_carries_task_action(tmp_path):
    deps = make_deps(tmp_path)
    summary, ctx = sweep_harness(tmp_path, [make_record()], deps=deps)
    assert summary["filed"][0]["task_action"] == "create"
    ctx["retry_conn"].close()


# ---------------------------------------------------------------------------
# Path 2: ambiguous match held — file step never reached
# ---------------------------------------------------------------------------

def test_ambiguous_match_held_never_files(tmp_path):
    deps = make_deps(tmp_path)
    calls = []

    def exploding_file_record(record, verified, d, **kw):
        calls.append(record)
        raise AssertionError("file_record must not run for a held record")

    rec = make_record()
    rec.match = MatchResult(status=AMBIGUOUS, applicant_id=None,
                            candidates=[111, 222])
    rec.held = True
    rec.hold_reason = "applicant name matches 2 records (111, 222)"
    summary, ctx = sweep_harness(tmp_path, [rec], deps=deps,
                                 file_record_fn=exploding_file_record)

    assert calls == []
    assert summary["filed"] == []
    assert len(summary["unverified"]) == 1
    entry = summary["unverified"][0]
    assert entry["gmail_id"] == "g1"
    assert "111" in entry["reason"] and "222" in entry["reason"]
    # Held records land in the retry ledger for a later sweep.
    pending = retry_pending(ctx["retry_conn"])
    assert [p["gmail_id"] for p in pending] == ["g1"]
    ctx["retry_conn"].close()


# ---------------------------------------------------------------------------
# Path 3: Zapier missing -> UNVERIFIED, nothing filed
# ---------------------------------------------------------------------------

def test_zapier_missing_means_unverified_not_filed(tmp_path):
    calls = []

    def exploding_file_record(record, verified, d, **kw):
        calls.append(record)
        raise AssertionError("nothing may be filed without the task path")

    summary, ctx = sweep_harness(
        tmp_path, [make_record()], deps=None,
        deps_error="Zapier trigger script not found at /nope/zap-trigger",
        file_record_fn=exploding_file_record)

    assert calls == []
    assert summary["filed"] == []
    assert len(summary["unverified"]) == 1
    assert "Zapier trigger script not found" in summary["unverified"][0]["reason"]
    assert any("Zapier trigger script not found" in e
               for e in summary["errors"])
    ctx["retry_conn"].close()


def test_build_filing_deps_fails_closed_without_trigger(tmp_path, monkeypatch):
    monkeypatch.setenv("CERT_ZAPIER_TRIGGER", "/nonexistent/zap-trigger")
    with pytest.raises(RuntimeError, match="trigger script not found"):
        build_filing_deps(os.path.join(str(tmp_path), "sweep.db"))


# ---------------------------------------------------------------------------
# Path 4: missing index CSV -> clean fail-closed
# ---------------------------------------------------------------------------

def test_missing_index_csv_fails_closed_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("CERT_APPLICANT_INDEX_PATH", raising=False)
    with pytest.raises(RuntimeError, match="CERT_APPLICANT_INDEX_PATH"):
        load_applicant_index()


def test_missing_index_csv_fails_closed_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("CERT_APPLICANT_INDEX_PATH",
                       "/nonexistent/applicants.csv")
    with pytest.raises(RuntimeError, match="not found"):
        load_applicant_index()


def test_main_exits_2_on_missing_index(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CERT_SWEEP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CERT_APPLICANT_INDEX_PATH", "/nonexistent/x.csv")
    rc = main(["--once"])
    assert rc == 2
    out = json.loads(capsys.readouterr().out)
    assert out["filed"] == [] and out["unverified"] == []
    assert any("CERT_APPLICANT_INDEX_PATH" in e or "not found" in e
               for e in out["errors"])


def test_main_requires_once(capsys):
    rc = main([])
    assert rc == 2
    out = json.loads(capsys.readouterr().out)
    assert "--once is required" in out["errors"][0]


# ---------------------------------------------------------------------------
# Retry ledger
# ---------------------------------------------------------------------------

def test_retry_ledger_roundtrip_and_first_seen_preserved(tmp_path):
    db_path = os.path.join(str(tmp_path), "sweep.db")
    conn = open_retry_db(db_path)
    first = retry_note(conn, "g9", "first reason")
    assert first["attempts"] == 1 and not first["parked"]
    before = retry_pending(conn)[0]["first_seen_at"]
    second = retry_note(conn, "g9", "second reason")
    assert second["attempts"] == 2
    pending = retry_pending(conn)
    assert len(pending) == 1
    assert pending[0]["reason"] == "second reason"
    assert pending[0]["first_seen_at"] == before  # never rewritten
    retry_clear(conn, "g9")
    assert retry_pending(conn) == []
    conn.close()


def test_retry_ledger_parks_after_max_attempts(tmp_path, monkeypatch):
    import robie_job_engine.cert_sweep as sweep_mod
    monkeypatch.setattr(sweep_mod, "RETRY_MAX_ATTEMPTS", 2)
    db_path = os.path.join(str(tmp_path), "sweep.db")
    conn = open_retry_db(db_path)
    retry_note(conn, "g9", "r1")
    retry_note(conn, "g9", "r2")
    entry = retry_note(conn, "g9", "r3")
    assert entry["attempts"] == 3
    assert entry["parked"] is True
    conn.close()


def test_parked_records_are_not_redriven(tmp_path, monkeypatch):
    """Records past the attempt cap stay listed but are not re-driven."""
    import robie_job_engine.cert_sweep as sweep_mod
    monkeypatch.setattr(sweep_mod, "RETRY_MAX_ATTEMPTS", 2)
    db_path = os.path.join(str(tmp_path), "sweep.db")
    retry_conn = open_retry_db(db_path)
    retry_note(retry_conn, "g1", "r1")
    retry_note(retry_conn, "g1", "r2")
    retry_note(retry_conn, "g1", "r3")  # parked

    def intake_fn(gmail, checkpoint, index, *, query, now):
        return {"records": [],
                "stats": {"discovered": 0, "matched": 0, "held": 0,
                          "errors": 0}}

    summary = _sweep_once(
        gmail=FakeGmail(), checkpoint=FakeCheckpoint(), index=make_index(),
        verifier=None, verifier_note="fake", deps=make_deps(tmp_path),
        retry_conn=retry_conn, query="after:2026/09/20",
        now=datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc),
        intake_fn=intake_fn)

    assert summary["stats"]["retried"] == 0
    assert summary["filed"] == [] and summary["unverified"] == []
    retry_conn.close()


def test_sweep_redrives_retry_ledger_record(tmp_path):
    """An UNVERIFIED record is re-driven on the next sweep and, once the
    blocker clears, files cleanly (real re-intake path, real file_record).
    """
    db_path = os.path.join(str(tmp_path), "sweep.db")
    retry_conn = open_retry_db(db_path)
    retry_note(retry_conn, "g1", "previous sweep: zapier down")

    gmail = FakeGmail({"g1": gmail_payload()})

    def intake_fn(g, checkpoint, index, *, query, now):
        return {"records": [],
                "stats": {"discovered": 0, "matched": 0, "held": 0,
                          "errors": 0}}

    deps = make_deps(tmp_path)
    summary = _sweep_once(
        gmail=gmail, checkpoint=FakeCheckpoint(), index=make_index(),
        verifier=None, verifier_note="fake", deps=deps,
        retry_conn=retry_conn, query="after:2026/09/20",
        now=datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc),
        intake_fn=intake_fn)

    assert summary["stats"]["retried"] == 1
    assert len(summary["filed"]) == 1
    assert summary["filed"][0]["gmail_id"] == "g1"
    assert summary["unverified"] == []
    # The real intake path matched and verified through the real code.
    assert len(deps.zapier.created) == 1
    retry_conn.close()


def test_gmail_query_window_is_relative():
    now = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
    assert gmail_query(now=now) == "after:2026/09/20"


def test_load_applicant_index_maps_csv_columns(tmp_path):
    csv_path = tmp_path / "directory.csv"
    csv_path.write_text(
        "applicant_id,account_name,dba,email_primary,email_business,"
        "phone_cell,phone_home,phone_work\n"
        "220250093,Acme LLC,,bob@acme.example,,5551234567,,\n")
    index = load_applicant_index(str(csv_path))
    assert index.row_count == 1
    assert index.by_email.get("bob@acme.example") == APPLICANT_ID
    assert "5551234567" in index.by_phone


def test_run_sweep_uses_real_dedupe_store(tmp_path, monkeypatch):
    """run_sweep wires the production SqliteDedupeStore checkpoint."""
    monkeypatch.setenv("CERT_SWEEP_DATA_DIR", str(tmp_path))
    db_path = os.path.join(str(tmp_path), "cert-sweep.db")

    def intake_fn(gmail, checkpoint, index, *, query, now):
        assert isinstance(checkpoint, SqliteDedupeStore)
        assert checkpoint._path == Path(db_path)
        return {"records": [],
                "stats": {"discovered": 0, "matched": 0, "held": 0,
                          "errors": 0}}

    summary = run_sweep(
        gmail=FakeGmail(), index=make_index(),
        verifier=None, verifier_note="fake",
        deps=make_deps(tmp_path), intake_fn=intake_fn,
        now=datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc))
    assert summary["errors"] == []
    assert summary["data_dir"] == str(tmp_path)
