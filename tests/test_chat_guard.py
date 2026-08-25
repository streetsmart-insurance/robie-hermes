import unittest
import sqlite3
from pathlib import Path

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_guard import (
    build_chat_execution_text,
    chat_message_requires_job,
    guard_chat_response,
    open_chat_job,
)
from robie_job_engine.store import JobStore
from robie_job_engine.recording import RecordingStore


class ChatGuardTests(unittest.TestCase):
    def test_conversation_only_messages_do_not_create_jobs(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            JobStore(db)
            messages = [
                "Thanks!",
                "How is it going?",
                "Where did we leave off?",
                (
                    "TEST ONLY — show one decision card with Continue and Pause. "
                    "Do not perform any EZLynx action."
                ),
            ]
            for index, text in enumerate(messages):
                self.assertIsNone(open_chat_job(db, f"message-{index}", text))
            with sqlite3.connect(db) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)

    def test_operational_requests_and_attachments_still_fail_closed_into_jobs(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            operational = open_chat_job(
                db,
                "operational",
                "Upload the renewal document and add its note in EZLynx",
            )
            self.assertIsNotNone(operational)
            attached = open_chat_job(
                db,
                "attached",
                "Thanks",
                expected_attachment_count=1,
                attachments=[],
            )
            self.assertIsNotNone(attached)
            self.assertEqual(JobStore(db).get_job(attached)["status"], "FAILED")

    def test_questions_commands_and_approvals_attach_to_active_job(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            first_id = open_chat_job(
                db,
                "message-work",
                "Upload the renewal document in EZLynx",
                conversation_id="spaces/related",
            )
            for index, text in enumerate(("How is it going?", "/jobs", "/skills", "Approved")):
                related = open_chat_job(
                    db,
                    f"message-related-{index}",
                    text,
                    conversation_id="spaces/related",
                )
                self.assertEqual(related, first_id)
            with sqlite3.connect(db) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)

    def test_none_job_passes_prompt_and_response_through(self):
        self.assertFalse(chat_message_requires_job("Thank you"))
        self.assertEqual(build_chat_execution_text("unused.db", None, "Thank you"), "Thank you")
        self.assertEqual(guard_chat_response("unused.db", None, "You're welcome"), "You're welcome")

    def test_chat_response_is_checkpointed_and_unverified(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(db, "spaces/s/messages/m1", "move it")
            response = guard_chat_response(db, job_id, "Done")
            self.assertIn("UNVERIFIED", response)
            self.assertEqual(JobStore(db).get_job(job_id)["status"], "UNVERIFIED")

    def test_chat_response_includes_ready_recording_link(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(db, "spaces/s/messages/m-video", "move it")
            recordings = RecordingStore(db)
            recording = recordings.create(
                job_id,
                Path(tmp) / "job.webm",
                Path(tmp) / "job.stop",
            )
            recordings.update(
                recording["id"],
                status="READY",
                drive_url="https://drive.google.com/file/d/test-recording/view",
                drive_file_id="test-recording",
            )
            response = guard_chat_response(db, job_id, "Done")
            self.assertIn("UNVERIFIED", response)
            self.assertIn("Review this job recording", response)
            self.assertIn("https://drive.google.com/file/d/test-recording/view", response)
            self.assertIn("Reply in this thread", response)

    def test_explicit_continue_reopens_same_unverified_job(self):
        with durable_temporary_directory() as tmp:
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
            # A prose worker response is not a destination action checkpoint,
            # so explicit continuation correctly resumes execution, not verify-only.
            self.assertEqual(JobStore(db).get_job(first_id)["status"], "RUNNING")
            self.assertIsNotNone(
                JobStore(db).get_checkpoint(first_id, "continuation:spaces/s/messages/m2")
            )

    def test_missing_chat_attachment_fails_closed_before_execution(self):
        with durable_temporary_directory() as tmp:
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
