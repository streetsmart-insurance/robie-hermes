import tempfile
import unittest
from pathlib import Path

from robie_job_engine.chat_guard import guard_chat_response, open_chat_job
from robie_job_engine.store import JobStore


class ChatGuardTests(unittest.TestCase):
    def test_chat_response_is_checkpointed_and_unverified(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(db, "spaces/s/messages/m1", "move it")
            response = guard_chat_response(db, job_id, "Done")
            self.assertIn("UNVERIFIED", response)
            self.assertEqual(JobStore(db).get_job(job_id)["status"], "UNVERIFIED")

    def test_explicit_continue_reopens_same_unverified_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            first_id = open_chat_job(
                db,
                "spaces/s/messages/m1",
                "upload the renewal quote",
                conversation_id="spaces/s",
                requested_by="Carlo",
            )
            guard_chat_response(db, first_id, "I clicked upload")
            continued_id = open_chat_job(
                db,
                "spaces/s/messages/m2",
                "Continue this job and verify the upload",
                conversation_id="spaces/s",
                requested_by="Carlo",
            )
            self.assertEqual(continued_id, first_id)
            self.assertEqual(JobStore(db).get_job(first_id)["status"], "VERIFYING")
            self.assertIsNotNone(
                JobStore(db).get_checkpoint(first_id, "continuation:spaces/s/messages/m2")
            )

    def test_missing_chat_attachment_fails_closed_before_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(
                db,
                "spaces/s/messages/attachment-missing",
                "upload the attached renewal document",
                attachments=[],
                expected_attachment_count=1,
                conversation_id="spaces/s",
            )
            job = JobStore(db).get_job(job_id)
            self.assertEqual(job["status"], "FAILED")
            self.assertIn("staged 0", job["last_error"])
            prompt = __import__(
                "robie_job_engine.chat_guard", fromlist=["build_chat_execution_text"]
            ).build_chat_execution_text(db, job_id, "upload it")
            self.assertIn("Do not attempt this Job", prompt)


if __name__ == "__main__":
    unittest.main()
