"""Tests for robie_job_engine/task_verifier.py.

Covers: pending queue dedupe, 40-minute due logic, report matching
(verified / missing / unverified), and the never-silently-pass rule.

No live credentials, no network, no box access — everything is local.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from robie_job_engine.task_verifier import (
    TaskVerificationStore,
    format_missing_alert,
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
