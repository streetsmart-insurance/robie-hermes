"""Tests for robie_job_engine/task_verifier.py.

Covers: pending queue dedupe, 40-minute due logic, report matching
(verified / missing / unverified), and the never-silently-pass rule.

No live credentials, no network, no box access — everything is local.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from robie_job_engine.task_verifier import (
    TaskVerificationStore,
    format_missing_alert,
    ingest_phone_watchdog,
    match_task,
    parse_task_report_csv,
    verify_due_tasks,
    PendingTask,
)


def _db():
    d = tempfile.mkdtemp()
    return TaskVerificationStore(os.path.join(d, "pending.db"))


def _iso(dt):
    return dt.isoformat()


class PendingQueueTest(unittest.TestCase):
    def test_record_and_dedupe(self):
        s = _db()
        i1 = s.record_pending(producer="certificates", applicant_id="123",
                             title="Cert request", assignee="SCanales")
        i2 = s.record_pending(producer="certificates", applicant_id="123",
                             title="Cert request", assignee="SCanales",
                             fired_at=None)
        # Different fired_at (now) → different row is fine; same explicit
        # fired_at → deduped to the same id.
        fired = _iso(datetime.now(timezone.utc))
        i3 = s.record_pending(producer="certificates", applicant_id="123",
                             title="Cert request", assignee="SCanales",
                             fired_at=fired)
        i4 = s.record_pending(producer="certificates", applicant_id="123",
                             title="Cert request", assignee="SCanales",
                             fired_at=fired)
        self.assertEqual(i3, i4)
        self.assertGreater(i1, 0)

    def test_due_only_after_40_minutes(self):
        s = _db()
        now = datetime.now(timezone.utc)
        s.record_pending(producer="phone-watchdog", applicant_id="1",
                         title="t", assignee="a",
                         fired_at=_iso(now - timedelta(minutes=10)))
        s.record_pending(producer="phone-watchdog", applicant_id="2",
                         title="t", assignee="a",
                         fired_at=_iso(now - timedelta(minutes=41)))
        due = s.due_for_verification(now)
        self.assertEqual([t.applicant_id for t in due], ["2"])

    def test_resolve(self):
        s = _db()
        i = s.record_pending(producer="certificates", applicant_id="9",
                            title="t", assignee="a",
                            fired_at=_iso(datetime.now(timezone.utc) - timedelta(hours=1)))
        s.resolve(i, "VERIFIED", "matched")
        self.assertEqual(s.counts(), {"VERIFIED": 1})
        self.assertEqual(s.due_for_verification(), [])


class ReportMatchTest(unittest.TestCase):
    def _pending(self, **kw):
        base = dict(id=1, producer="certificates", applicant_id="177412857",
                    title="Certificate request — 139 Trucking",
                    assignee="SCanales",
                    fired_at=_iso(datetime.now(timezone.utc) - timedelta(minutes=50)),
                    status="PENDING")
        base.update(kw)
        return PendingTask(**base)

    def test_match_on_applicant_assignee_title(self):
        rows = parse_task_report_csv(
            b"Task Title,Assignee,Applicant ID,Created\n"
            b"Certificate request - 139 Trucking,Steffany Canales,177412857,"
            + _iso(datetime.now(timezone.utc) - timedelta(minutes=30)).encode() + b"\n"
        )
        hit = match_task(self._pending(), rows)
        self.assertIsNotNone(hit)

    def test_no_match_wrong_applicant(self):
        rows = parse_task_report_csv(
            b"Task Title,Assignee,Applicant ID,Created\n"
            b"Certificate request - 139 Trucking,Steffany Canales,999999999,"
            + _iso(datetime.now(timezone.utc) - timedelta(minutes=30)).encode() + b"\n"
        )
        self.assertIsNone(match_task(self._pending(), rows))

    def test_no_match_created_before_firing(self):
        # A task row that predates the firing cannot be our task.
        rows = parse_task_report_csv(
            b"Task Title,Assignee,Applicant ID,Created\n"
            b"Certificate request - 139 Trucking,Steffany Canales,177412857,"
            + _iso(datetime.now(timezone.utc) - timedelta(hours=2)).encode() + b"\n"
        )
        self.assertIsNone(match_task(self._pending(), rows))

    def test_match_real_activity_detail_columns(self):
        # Regression test (2026-09-27): the production "ROBIE task report CSV"
        # uses Activity Detail column names. A naive substring lookup for
        # "task" hit "Task Assigned To" (returning the assignee as the title)
        # and "created" hit "Note Created by" (returning a name as the
        # timestamp). Exact-match-first must resolve the right columns.
        now = datetime.now(timezone.utc)
        created = _iso(now - timedelta(minutes=30))
        csv = (
            b"Applicant ID,Account Name,Task Assigned To,Activity Type,"
            b"Note Created by,Task Status,Note,Created Date,Task Created Date,"
            b"Task ID\n"
            b"177412857,139 TRUCKING LLC,Steffany Canales,Task Creation Note,"
            b"Carlo Ferrara,Open,Certificate request - 139 Trucking,"
            + created.encode() + b","
            + now.strftime("%Y-%m-%d").encode() + b","
            b"63227816\n"
        )
        rows = parse_task_report_csv(csv)
        hit = match_task(self._pending(), rows)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["task id"], "63227816")


class VerifyDueTest(unittest.TestCase):
    def test_verified_missing_unverified(self):
        s = _db()
        now = datetime.now(timezone.utc)
        old = _iso(now - timedelta(minutes=50))
        s.record_pending(producer="certificates", applicant_id="111",
                         title="Cert A", assignee="SCanales", fired_at=old)
        s.record_pending(producer="phone-watchdog", applicant_id="222",
                         title="Callback B", assignee="Ricardo Aguilar", fired_at=old)

        rows = parse_task_report_csv(
            b"Task Title,Assignee,Applicant ID,Created\n"
            b"Cert A,Steffany Canales,111,"
            + _iso(now - timedelta(minutes=30)).encode() + b"\n"
        )
        out = verify_due_tasks(s, rows, True)
        self.assertEqual(len(out["verified"]), 1)
        self.assertEqual(len(out["missing"]), 1)
        self.assertEqual(out["verified"][0].applicant_id, "111")
        self.assertEqual(out["missing"][0].applicant_id, "222")

    def test_no_report_means_unverified_not_missing(self):
        # Never silently pass, never a false MISSING.
        s = _db()
        old = _iso(datetime.now(timezone.utc) - timedelta(minutes=50))
        s.record_pending(producer="certificates", applicant_id="111",
                         title="Cert A", assignee="SCanales", fired_at=old)
        out = verify_due_tasks(s, None, False)
        self.assertEqual(len(out["unverified"]), 1)
        self.assertEqual(len(out["missing"]), 0)
        self.assertEqual(len(out["verified"]), 0)

    def test_nothing_due_is_quiet(self):
        s = _db()
        out = verify_due_tasks(s, [], True)
        self.assertEqual(out, {"verified": [], "missing": [], "unverified": []})


def _naive_ny(dt_utc: datetime) -> str:
    """Box-local wall clock, the form phone_alerts.processed_at is stored in."""
    local = dt_utc.astimezone(ZoneInfo("America/New_York")).replace(tzinfo=None)
    return local.isoformat(timespec="seconds")


def _phone_db(rows: list[tuple]) -> str:
    path = os.path.join(tempfile.mkdtemp(), "phone_alerts.db")
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE processed_calls (
            message_id TEXT,
            ams_account_id TEXT,
            account_name TEXT,
            assigned_user TEXT,
            processed_at TEXT,
            ezlynx_task_status TEXT
        )"""
    )
    conn.executemany(
        """INSERT INTO processed_calls
           (message_id, ams_account_id, account_name, assigned_user,
            processed_at, ezlynx_task_status)
           VALUES (?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()
    conn.close()
    return path


class PhoneWatchdogIngestTest(unittest.TestCase):
    def test_ingests_delivered_and_sent_to_relay_once(self):
        """Both handoff statuses are queued; other statuses and repeats are not.

        processed_at is naive America/New_York. A row 3 hours old is inside
        the 6-hour window, but its wall-clock string sorts before a UTC
        cutoff of (now - 6h). The tz-aware filter must still ingest it.
        A row 6.5 hours old is inside the coarse SQL buffer and must be
        dropped by that same filter.
        """
        now = datetime.now(timezone.utc)
        recent = _naive_ny(now - timedelta(hours=3))
        just_outside = _naive_ny(now - timedelta(hours=6, minutes=30))
        stale = _naive_ny(now - timedelta(hours=30))
        db = _phone_db([
            ("m-del", "111", "Jayshri Dixit", "Ricardo Aguilar", recent, "delivered"),
            ("m-relay", "222", "Ada Lovelace", "SCanales", recent, "sent_to_relay"),
            ("m-fail", "333", "Skip Fail", "A", recent, "failed"),
            ("m-pend", "444", "Skip Pending", "A", recent, "pending"),
            ("m-err", "555", "Skip Error", "A", recent, "error"),
            ("m-blank", "666", "Skip Blank", "A", recent, ""),
            ("m-sent", "999", "Skip Sent", "A", recent, "sent"),
            ("m-old-del", "777", "Old Delivered", "A", just_outside, "delivered"),
            ("m-stale-relay", "888", "Stale Relay", "A", stale, "sent_to_relay"),
        ])
        store = _db()
        first = ingest_phone_watchdog(store, db, since_hours=6)
        self.assertEqual(first, 2)

        queued = {t.applicant_id: t for t in store.due_for_verification(now)}
        self.assertEqual(set(queued), {"111", "222"})
        self.assertEqual(queued["111"].status, "PENDING")
        self.assertEqual(queued["111"].producer, "phone-watchdog")
        self.assertEqual(queued["111"].title, "[AFTER-HOURS CALLBACK] Jayshri Dixit")
        self.assertEqual(queued["111"].assignee, "Ricardo Aguilar")
        self.assertEqual(queued["222"].status, "PENDING")
        self.assertEqual(queued["222"].title, "[AFTER-HOURS CALLBACK] Ada Lovelace")
        self.assertEqual(store.counts(), {"PENDING": 2})

        # Same rows again: the unique key (producer, applicant, title, fired_at)
        # keeps the queue at one task per handoff.
        second = ingest_phone_watchdog(store, db, since_hours=6)
        self.assertEqual(second, 0)
        self.assertEqual(store.counts(), {"PENDING": 2})
        self.assertEqual(len(store.due_for_verification(now)), 2)

    def test_sent_to_relay_is_not_ezlynx_delivery_proof(self):
        """sent_to_relay only queues the task. The report still has to match."""
        now = datetime.now(timezone.utc)
        recent = _naive_ny(now - timedelta(hours=3))
        db = _phone_db([
            ("m-relay", "222", "Ada Lovelace", "SCanales", recent, "sent_to_relay"),
        ])
        store = _db()
        self.assertEqual(ingest_phone_watchdog(store, db, since_hours=6), 1)

        unmatched = verify_due_tasks(store, [], True)
        self.assertEqual(unmatched["verified"], [])
        self.assertEqual(len(unmatched["missing"]), 1)
        self.assertEqual(unmatched["missing"][0].applicant_id, "222")
        self.assertEqual(store.counts(), {"MISSING": 1})

        matched_store = _db()
        self.assertEqual(ingest_phone_watchdog(matched_store, db, since_hours=6), 1)
        created = _iso(now - timedelta(hours=2, minutes=30))
        rows = parse_task_report_csv(
            b"Task Title,Assignee,Applicant ID,Created\n"
            b"[AFTER-HOURS CALLBACK] Ada Lovelace,Steffany Canales,222,"
            + created.encode() + b"\n"
        )
        matched = verify_due_tasks(matched_store, rows, True)
        self.assertEqual(len(matched["verified"]), 1)
        self.assertEqual(matched["missing"], [])
        self.assertEqual(matched_store.counts(), {"VERIFIED": 1})


class AlertFormatTest(unittest.TestCase):
    def test_missing_alert_names_task(self):
        t = PendingTask(id=1, producer="phone-watchdog", applicant_id="177412857",
                        title="[AFTER-HOURS CALLBACK] Test", assignee="Ricardo Aguilar",
                        fired_at="2026-09-27T11:00:00+00:00", status="MISSING")
        text = format_missing_alert([t])
        self.assertIn("177412857", text)
        self.assertIn("Ricardo Aguilar", text)
        self.assertIn("not found", text)


if __name__ == "__main__":
    unittest.main()
