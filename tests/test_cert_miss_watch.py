"""Tests for cert_miss_watch: the EPHE-class health check.

Carlo's rule: a check that has never failed on purpose isn't trusted.
These tests prove the probe stays quiet on a clean ledger AND fires on
a seeded miss whose policy number exists in the applicant index.
"""

import csv
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from robie_job_engine import cert_miss_watch as mw  # noqa: E402
from robie_job_engine.cert_sweep import miss_note, open_retry_db  # noqa: E402


def _make_index_csv(path):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["applicant_id", "account_name", "dba", "email_primary",
                    "email_business", "phone_cell", "phone_home",
                    "phone_work", "policy_numbers"])
        w.writerow([199205654, "EPHE LLC", "EPHE LLC",
                    "sinancanlv@yahoo.com", "sinancanlv@yahoo.com",
                    "7025012909", "", "", "9300216995"])
        w.writerow([111, "Other Corp", "", "", "", "", "", "", "B01053720"])
    return path


def _db_with(directory):
    db_path = os.path.join(directory, mw.DB_FILENAME)
    conn = open_retry_db(db_path)
    conn.close()
    return db_path


def _check(directory, csv_path):
    return mw.check_misses(directory=directory, csv_path=csv_path)


# ---------------------------------------------------------------------------
# Quiet when things are fine
# ---------------------------------------------------------------------------

def test_clean_on_empty_ledger(tmp_path):
    d = str(tmp_path)
    csv_path = _make_index_csv(os.path.join(d, "index.csv"))
    _db_with(d)
    report = _check(d, csv_path)
    assert report["clean"] is True
    assert report["findings"] == []
    assert "error" not in report


def test_clean_when_miss_policy_not_in_index(tmp_path):
    # A held request whose policy is genuinely unknown: still held, but
    # NOT the EPHE class — the probe stays quiet.
    d = str(tmp_path)
    csv_path = _make_index_csv(os.path.join(d, "index.csv"))
    conn = open_retry_db(os.path.join(d, mw.DB_FILENAME))
    miss_note(conn, "Unknown Carrier Request LLC", "g1",
              policy_numbers=["ZZZ-NOT-IN-BOOK"])
    conn.close()
    report = _check(d, csv_path)
    assert report["clean"] is True
    assert report["findings"] == []


def test_clean_when_miss_has_no_policy_at_all(tmp_path):
    d = str(tmp_path)
    csv_path = _make_index_csv(os.path.join(d, "index.csv"))
    conn = open_retry_db(os.path.join(d, mw.DB_FILENAME))
    miss_note(conn, "No Policy Named LLC", "g2")
    conn.close()
    report = _check(d, csv_path)
    assert report["clean"] is True


def test_clean_ignores_resolved_miss(tmp_path):
    # Already resolved via the refresh loop: not an active miss.
    d = str(tmp_path)
    csv_path = _make_index_csv(os.path.join(d, "index.csv"))
    db_path = _db_with(d)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO cert_index_misses (name_key, raw_name, gmail_id,"
        " first_seen_at, last_seen_at, occurrences,"
        " resolved_applicant_id, policy_numbers)"
        " VALUES (?,?,?,?,?,?,?,?)",
        ("ephe llc", "EPHE LLC", "g3", "2026-09-29T10:00:00+00:00",
         mw._utcnow().isoformat(), 1, 199205654, "9300216995"))
    conn.commit()
    conn.close()
    report = _check(d, csv_path)
    assert report["clean"] is True


def test_clean_ignores_stale_miss(tmp_path):
    # Older than the 24h window: outside "reported within a day".
    d = str(tmp_path)
    csv_path = _make_index_csv(os.path.join(d, "index.csv"))
    db_path = _db_with(d)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO cert_index_misses (name_key, raw_name, gmail_id,"
        " first_seen_at, last_seen_at, occurrences, policy_numbers)"
        " VALUES (?,?,?,?,?,?,?)",
        ("ephe llc", "EPHE LLC", "g4", "2026-09-20T10:00:00+00:00",
         "2026-09-20T10:05:00+00:00", 3, "9300216995"))
    conn.commit()
    conn.close()
    report = _check(d, csv_path)
    assert report["clean"] is True
    assert report["misses_checked"] == 0


# ---------------------------------------------------------------------------
# Alerts when broken (the EPHE class, deliberately reproduced)
# ---------------------------------------------------------------------------

def test_fires_on_miss_with_indexed_policy(tmp_path):
    # The exact 2026-09-29 shape: held request, extracted policy
    # 9300216995, and the book carries it under applicant 199205654.
    d = str(tmp_path)
    csv_path = _make_index_csv(os.path.join(d, "index.csv"))
    conn = open_retry_db(os.path.join(d, mw.DB_FILENAME))
    miss_note(conn, "EPHE LLC", "g5", policy_numbers=["9300216995"])
    conn.close()
    report = _check(d, csv_path)
    assert report["clean"] is False
    assert len(report["findings"]) == 1
    f = report["findings"][0]
    assert f["policy_number"] == "9300216995"
    assert f["indexed_applicants"] == [199205654]
    text = mw.render_report(report)
    assert "9300216995" in text and "199205654" in text


def test_probe_error_when_db_missing(tmp_path):
    d = str(tmp_path)
    csv_path = _make_index_csv(os.path.join(d, "index.csv"))
    report = _check(d, csv_path)  # no DB created
    assert report["clean"] is False
    assert "error" in report
    assert mw.main(["--data-dir", d, "--index-csv", csv_path]) == 2


def test_main_exit_codes(tmp_path):
    d = str(tmp_path)
    csv_path = _make_index_csv(os.path.join(d, "index.csv"))
    _db_with(d)
    assert mw.main(["--data-dir", d, "--index-csv", csv_path]) == 0
    conn = open_retry_db(os.path.join(d, mw.DB_FILENAME))
    miss_note(conn, "EPHE LLC", "g6", policy_numbers=["9300216995"])
    conn.close()
    assert mw.main(["--data-dir", d, "--index-csv", csv_path]) == 1


# ---------------------------------------------------------------------------
# Alert dedup: one per episode, quiet while red, recovery on clean
# ---------------------------------------------------------------------------

def _red_report():
    return {
        "at": "2026-09-29T12:00:00+00:00",
        "clean": False,
        "findings": [{
            "raw_name": "EPHE LLC", "gmail_id": "g7",
            "last_seen_at": "2026-09-29T11:00:00+00:00",
            "occurrences": 2, "policy_number": "9300216995",
            "indexed_applicants": [199205654],
        }],
        "misses_checked": 1,
    }


def _green_report():
    return {"at": "2026-09-29T13:00:00+00:00", "clean": True,
            "findings": [], "misses_checked": 1}


def test_alert_dedup_and_recovery(tmp_path):
    d = str(tmp_path)
    posted = []
    poster = lambda text: posted.append(text) or {"posted": True}  # noqa: E731

    first = mw.maybe_alert(_red_report(), directory=d, poster=poster)
    assert first["state"] == "red-alerted"
    assert len(posted) == 1
    assert "9300216995" in posted[0]

    second = mw.maybe_alert(_red_report(), directory=d, poster=poster)
    assert second["state"] == "red-quiet"
    assert len(posted) == 1  # no duplicate alert

    recovered = mw.maybe_alert(_green_report(), directory=d, poster=poster)
    assert recovered["state"] == "green"
    assert len(posted) == 2
    assert "clean again" in posted[1]

    # After recovery, a new red episode alerts again.
    third = mw.maybe_alert(_red_report(), directory=d, poster=poster)
    assert third["state"] == "red-alerted"
    assert len(posted) == 3


def test_alert_never_raises_without_poster(tmp_path):
    d = str(tmp_path)
    outcome = mw.maybe_alert(_red_report(), directory=d, poster=None)
    assert outcome["state"] in ("red-alerted", "red-quiet")
