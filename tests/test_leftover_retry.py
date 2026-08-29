"""Leftover RETRY is refused by the Job Engine, not a handoff policy.

RETRY is allowed only for a fresh AWAITING_HUMAN_INPUT HITL younger than
LIVE_TAB_CLAIM_MAX_AGE. Terminal FAILED / UNVERIFIED leftovers must not
resume. New @robie is the path. No auto-retry.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_guard import open_chat_job, stop_generic_chat_job_heartbeat
from robie_job_engine.engine import (
    LEFTOVER_RETRY_REFUSED,
    JobEngine,
    leftover_retry_hold_reason,
)
from robie_job_engine.models import JobStatus
from robie_job_engine.store import JobStore
from robie_job_engine.tab_cleanup import LIVE_TAB_CLAIM_MAX_AGE


def _age_job(db: str, job_id: str, *, status: JobStatus, age: timedelta) -> None:
    stamp = (datetime.now(timezone.utc) - age).isoformat()
    with JobStore(db).connect() as conn:
        conn.execute(
            "UPDATE jobs SET status=?, created_at=?, updated_at=? WHERE id=?",
            (status.value, stamp, stamp, job_id),
        )


class LeftoverRetryEngineTests(unittest.TestCase):
    def test_terminal_leftover_retry_is_refused(self):
        self.assertEqual(LIVE_TAB_CLAIM_MAX_AGE, timedelta(hours=1))
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(
                db,
                "message-leftover-failed",
                "finish the leftover EZLynx file",
                conversation_id="spaces/leftover-retry",
            )
            stop_generic_chat_job_heartbeat(db, job_id)
            store = JobStore(db)
            current = JobStatus(store.get_job(job_id)["status"])
            store.transition(
                job_id,
                JobStatus.FAILED,
                expected={current},
                error="leftover terminal fixture",
                release_lease=True,
            )
            failed = store.get_job(job_id)
            reason = leftover_retry_hold_reason(failed)
            self.assertIsNotNone(reason)
            self.assertIn(LEFTOVER_RETRY_REFUSED, reason or "")
            self.assertIn("FAILED", reason or "")
            self.assertIn("new @robie", (reason or "").casefold())
            engine = JobEngine(store, {}, {})
            result = engine.request_retry(job_id)
            after = store.get_job(job_id)
            self.assertTrue(result.get("leftover_retry_refused"))
            self.assertEqual(after["status"], JobStatus.FAILED.value)
            self.assertNotEqual(after["status"], JobStatus.RETRY_WAIT.value)
            self.assertNotEqual(after["status"], JobStatus.RUNNING.value)
            self.assertNotEqual(after["status"], JobStatus.PENDING.value)
            checkpoint = store.get_checkpoint(job_id, "leftover_retry")
            self.assertTrue(checkpoint["refused"])
            self.assertFalse(checkpoint["auto_retry"])

    def test_thirty_minute_hitl_retry_is_allowed(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(
                db,
                "message-fresh-hitl",
                "finish the EZLynx commercial auto",
                conversation_id="spaces/fresh-hitl-retry",
            )
            stop_generic_chat_job_heartbeat(db, job_id)
            _age_job(
                db,
                job_id,
                status=JobStatus.AWAITING_HUMAN_INPUT,
                age=timedelta(minutes=30),
            )
            store = JobStore(db)
            parked = store.get_job(job_id)
            self.assertEqual(parked["status"], JobStatus.AWAITING_HUMAN_INPUT.value)
            self.assertIsNone(leftover_retry_hold_reason(parked))
            engine = JobEngine(store, {}, {})
            result = engine.request_retry(job_id)
            self.assertFalse(result.get("leftover_retry_refused"))
            self.assertEqual(result["status"], JobStatus.PENDING.value)
            after = store.get_job(job_id)
            self.assertEqual(after["status"], JobStatus.PENDING.value)
            checkpoint = store.get_checkpoint(job_id, "leftover_retry")
            self.assertFalse(checkpoint["refused"])
            self.assertFalse(checkpoint["auto_retry"])


if __name__ == "__main__":
    unittest.main()
