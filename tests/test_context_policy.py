from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from robie_job_engine.context_policy import JobContextManager
from robie_job_engine.models import JobStatus
from robie_job_engine.scheduler import run_once
from robie_job_engine.store import JobStore


UTC = timezone.utc


class ContextPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "jobs.db"
        self.jobs = JobStore(self.db)
        self.context = JobContextManager(self.db, inactivity_minutes=120, context_char_budget=2_000)

    def tearDown(self):
        self.temp.cleanup()

    def _bind(self, at: datetime):
        job = self.jobs.create_job("hermes.google_chat_task", {"text": "work account A"})
        self.context.bind_job("dm:carlo", job["id"], now=at, summary={"objective": "work account A"})
        return job

    def test_new_dm_starts_fresh_unless_continuation_is_explicit(self):
        at = datetime(2026, 8, 22, 10, tzinfo=UTC)
        job = self._bind(at)
        fresh = self.context.decide("dm:carlo", "Please check another account", now=at + timedelta(minutes=5))
        self.assertEqual(fresh.action, "NEW")
        resume = self.context.decide("dm:carlo", "Continue the account audit", now=at + timedelta(minutes=6))
        self.assertEqual(resume.active_job_id, job["id"])

    def test_expiration_pauses_job_and_detaches_context_without_deleting_history(self):
        at = datetime(2026, 8, 22, 10, tzinfo=UTC)
        job = self._bind(at)
        decision = self.context.decide("dm:carlo", "hello", now=at + timedelta(hours=3))
        self.assertEqual(decision.action, "NEW")
        self.assertEqual(self.jobs.get_job(job["id"])["status"], JobStatus.PAUSED)
        self.assertEqual(self.context.get("dm:carlo")["context_state"], "EXPIRED")
        self.assertIsNotNone(self.jobs.get_checkpoint(job["id"], "context_archive"))

    def test_account_change_clears_policy_and_submission_ids(self):
        at = datetime(2026, 8, 22, 10, tzinfo=UTC)
        self._bind(at)
        self.context.set_entity_boundary(
            "dm:carlo", account_id="A", policy_id="P-A", submission_id="S-A"
        )
        self.context.set_entity_boundary(
            "dm:carlo", account_id="B", policy_id="P-B", submission_id="S-B"
        )
        current = self.context.get("dm:carlo")
        self.assertEqual(current["account_id"], "B")
        self.assertIsNone(current["policy_id"])
        self.assertIsNone(current["submission_id"])

    def test_compaction_keeps_structured_state_and_rejects_prompt_sprawl(self):
        at = datetime(2026, 8, 22, 10, tzinfo=UTC)
        self._bind(at)
        compacted = self.context.compact("dm:carlo", {
            "objective": "Verify the renewal submission",
            "known": ["Account ID A was read from EZLynx"],
            "assumed": ["Discussion may be Renewal Manual"],
            "duplicate_tool_chatter": "discard me",
            "next_action": "Freshly reload the submission folder",
        })
        self.assertNotIn("duplicate_tool_chatter", compacted)
        prompt = self.context.build_prompt_context(
            "dm:carlo",
            permanent_rules=["Only the Job Engine may mark COMPLETE."],
            relevant_skill="Submission Center upload",
            relevant_sop="Use the exact submission folder.",
            current_tool_result="File list: quote.pdf",
        )
        self.assertIn("ACTIVE JOB SUMMARY", prompt)
        self.assertNotIn("discard me", prompt)
        with self.assertRaises(ValueError):
            self.context.compact("dm:carlo", {"objective": "x" * 3_000})

    def test_scheduler_expires_due_contexts(self):
        old = datetime(2020, 1, 1, tzinfo=UTC)
        job = self._bind(old)
        with patch.dict("os.environ", {"ROBIE_ARTIFACT_ROOT": str(Path(self.temp.name) / "artifacts")}):
            result = run_once(str(self.db))
        self.assertEqual(result["expired_contexts"], 1)
        self.assertEqual(self.jobs.get_job(job["id"])["status"], JobStatus.PAUSED)


if __name__ == "__main__":
    unittest.main()
