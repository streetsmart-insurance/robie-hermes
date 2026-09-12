from __future__ import annotations

import unittest
from pathlib import Path

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_policy import SECURITY_GUARD_STOP_RULE
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
            self.assertIn(SECURITY_GUARD_STOP_RULE, calls[0])
            self.assertIn("NEVER ask a human to lift a security control", calls[0])
            self.assertEqual(JobStore(db).get_job(JobStore(db).create_job("hermes.email_task", {}, idempotency_key="gmail:m-1")["id"])["status"], "UNVERIFIED")


if __name__ == "__main__":
    unittest.main()

class EmailCompletionTests(unittest.TestCase):
    def test_email_worker_carries_durable_context_to_child(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / 'jobs.db')
            calls = []
            run_guarded_email_task(
                db_path=db, gmail_message_id='context', prompt='work',
                run_agent=lambda p: self.fail('context callback should be used'),
                run_agent_with_context=lambda p, j, d: calls.append((j, d)) or 'worker response',
            )
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][1], db)
            self.assertEqual(JobStore(db).get_job(calls[0][0])['status'], 'UNVERIFIED')

    def test_busy_execution_leaves_email_pending_without_running_agent(self):
        from robie_job_engine.email_guard import EmailTaskPending
        from robie_job_engine.runs import IsolatedRunStore
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / 'jobs.db')
            runs = IsolatedRunStore(db)
            active = runs.start(owner='chat-worker', job_id='chat')
            with self.assertRaises(EmailTaskPending):
                run_guarded_email_task(db_path=db, gmail_message_id='queued', prompt='work', run_agent=lambda p: self.fail('must not execute while busy'), verifiers={})
            self.assertEqual(runs.active_run()['id'], active['id'])
