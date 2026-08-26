from __future__ import annotations

import unittest
from pathlib import Path

from durable_temp import durable_temporary_directory

from robie_job_engine.email_guard import run_guarded_email_task
from robie_job_engine.store import JobStore


class EmailGuardTests(unittest.TestCase):
    def test_opaque_hermes_success_is_reported_unverified_and_deduplicated(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            calls = []

            def run(prompt):
                calls.append(prompt)
                return "I moved the document successfully."

            first = run_guarded_email_task(db_path=db, gmail_message_id="m-1", prompt="move it", run_agent=run)
            second = run_guarded_email_task(db_path=db, gmail_message_id="m-1", prompt="move it", run_agent=run)
            self.assertIn("UNVERIFIED", first)
            self.assertIn("must not be treated as COMPLETE", first)
            self.assertIn("UNVERIFIED", second)
            self.assertEqual(len(calls), 1)
            self.assertEqual(JobStore(db).get_job(JobStore(db).create_job("hermes.email_task", {}, idempotency_key="gmail:m-1")["id"])["status"], "UNVERIFIED")


if __name__ == "__main__":
    unittest.main()
