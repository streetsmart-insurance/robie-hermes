"""2026-10-08 false MISSING alert for the Conley phone task.

Zapier created "[CALLBACK REQUIRED] George Conley" (task 63591925) for
applicant 167230246, but the verifier expected
"[AFTER-HOURS CALLBACK] Conley Electric" and could also read an hourly
report emailed before the task existed. Both must not raise MISSING.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from robie_job_engine.task_verifier import (
    PendingTask,
    TaskVerificationStore,
    match_task,
    parse_task_report_csv,
    verify_due_tasks,
)


def _store():
    return TaskVerificationStore(os.path.join(tempfile.mkdtemp(), "p.db"))


def _pending(title, producer="phone-watchdog", fired=None):
    fired = fired or datetime(2026, 10, 8, 22, 6, tzinfo=timezone.utc)
    return PendingTask(id=1, producer=producer, applicant_id="167230246", title=title,
                       assignee="AngieV", fired_at=fired.isoformat(), status="PENDING")


def _rows(title, created="2026-10-08T18:07:00"):
    return parse_task_report_csv(
        ("Task Title,Assignee,Applicant ID,Created\n"
         f"{title},AngieV,167230246,{created}\n").encode()
    )


class TitleMatchTest(unittest.TestCase):
    def test_conley_real_title_matches_rebuilt_title(self):
        p = _pending("[AFTER-HOURS CALLBACK] Conley Electric")
        self.assertIsNotNone(match_task(p, _rows("[CALLBACK REQUIRED] George Conley")))

    def test_prefix_drift_with_same_name(self):
        p = _pending("[AFTER-HOURS CALLBACK] Kenneth Smith")
        self.assertIsNotNone(match_task(p, _rows("[CALLBACK REQUIRED] Kenneth Smith")))

    def test_unrelated_title_still_misses(self):
        p = _pending("[AFTER-HOURS CALLBACK] Conley Electric")
        self.assertIsNone(match_task(p, _rows("Renewal review")))

    def test_certificates_keep_strict_titles(self):
        p = _pending("Cert request ACME", producer="certificates")
        self.assertIsNone(match_task(p, _rows("[CALLBACK REQUIRED] Someone")))

    def test_wrong_applicant_or_assignee_still_misses(self):
        p = _pending("[AFTER-HOURS CALLBACK] Conley Electric")
        rows = parse_task_report_csv(
            b"Task Title,Assignee,Applicant ID,Created\n"
            b"[CALLBACK REQUIRED] George Conley,AngieV,999,2026-10-08T18:07:00\n"
            b"[CALLBACK REQUIRED] George Conley,Jazmin11,167230246,2026-10-08T18:07:00\n"
        )
        self.assertIsNone(match_task(p, rows))


class ReportTimingTest(unittest.TestCase):
    def _due(self, store, minutes_ago=50):
        fired = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        store.record_pending(producer="phone-watchdog", applicant_id="167230246",
                             title="[AFTER-HOURS CALLBACK] Conley Electric",
                             assignee="AngieV", fired_at=fired.isoformat())
        return fired

    def test_report_older_than_task_waits(self):
        s = _store()
        fired = self._due(s)
        out = verify_due_tasks(s, [], True, report_received_at=fired - timedelta(minutes=6))
        self.assertEqual(out, {"verified": [], "missing": [], "unverified": []})
        self.assertEqual(len(s.due_for_verification()), 1)   # still pending

    def test_newer_report_without_task_is_missing(self):
        s = _store()
        fired = self._due(s)
        out = verify_due_tasks(s, [], True, report_received_at=fired + timedelta(minutes=30))
        self.assertEqual(len(out["missing"]), 1)

    def test_waiting_too_long_is_unverified_not_missing(self):
        s = _store()
        fired = self._due(s, minutes_ago=5 * 60)
        out = verify_due_tasks(s, [], True, report_received_at=fired - timedelta(minutes=1))
        self.assertEqual(len(out["unverified"]), 1)
        self.assertEqual(len(out["missing"]), 0)


if __name__ == "__main__":
    unittest.main()
