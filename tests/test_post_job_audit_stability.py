import tempfile
import unittest
from pathlib import Path

from robie_job_engine.models import JobStatus
from robie_job_engine.store import JobStore
from robie_job_engine.post_job_audit import (
    AUDIT_CHECKPOINT,
    audit_terminal_job,
)


class PostJobAuditStabilityTests(unittest.TestCase):
    def test_audit_snapshot_is_stable_across_rereads(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "jobs.db"
            store = JobStore(db_path)
            job = store.create_job(
                action_type="test.action",
                payload={"applicant_id": "12345"},
            )
            job_id = str(job["id"])
            store.transition(
                job_id,
                JobStatus.FAILED,
                error="test error",
            )

            # Run initial audit
            first_audit = audit_terminal_job(db_path, job_id)
            self.assertIn("ROBIE post-job audit", first_audit["chat_message"])
            self.assertEqual(first_audit["verdict"], "FAIL")

            # Update checkpoint with an explicit snapshot state
            first_audit["recording_motion"] = {
                "result": "PASS",
                "reason": "motion detected in initial recording",
            }
            store.checkpoint(job_id, AUDIT_CHECKPOINT, first_audit)

            # Re-read the audit — it must return the fixed snapshot without recomputing/flipping
            second_audit = audit_terminal_job(db_path, job_id)
            self.assertEqual(
                second_audit["recording_motion"]["result"],
                "PASS",
            )
            self.assertEqual(
                second_audit["recording_motion"]["reason"],
                "motion detected in initial recording",
            )
            self.assertEqual(second_audit["verdict"], "FAIL")


if __name__ == "__main__":
    unittest.main()
