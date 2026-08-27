import os
import sqlite3
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_guard import (
    build_chat_execution_text,
    chat_message_requires_job,
    guard_chat_response,
    notify_terminal_chat_job,
    open_chat_job,
    start_generic_chat_job_heartbeat,
    stop_generic_chat_job_heartbeat,
)
from robie_job_engine.scheduler import run_once
from robie_job_engine.chat_queue import DurableChatEventQueue
from robie_job_engine.store import JobStore
from robie_job_engine.recording import RecordingStore
from robie_job_engine.models import JobStatus


class ChatGuardTests(unittest.TestCase):
    def test_zero_attempt_generic_running_job_expires_with_precise_reason(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(
                db,
                "message-orphan",
                "Perform the destination workflow",
                conversation_id="spaces/orphan",
            )
            stop_generic_chat_job_heartbeat(db, job_id)
            old = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "DELETE FROM checkpoints WHERE job_id=? AND kind='gateway_progress'",
                    (job_id,),
                )
                conn.execute("UPDATE jobs SET updated_at=? WHERE id=?", (old, job_id))
            related = open_chat_job(
                db,
                "message-status",
                "What did you do?",
                conversation_id="spaces/orphan",
            )
            self.assertEqual(related, job_id)
            job = JobStore(db).get_job(job_id)
            self.assertEqual(job["status"], "FAILED")
            self.assertIn("execution did not start", job["last_error"])
            self.assertIsNotNone(JobStore(db).get_checkpoint(job_id, "orphan_timeout"))

    def test_generic_chat_heartbeat_writes_to_orphan_watcher_db(self):
        with durable_temporary_directory() as tmp:
            watcher_db = str(Path(tmp) / "jobs.db")
            other_db = str(Path(tmp) / "other-jobs.db")
            JobStore(other_db)
            with unittest.mock.patch.dict(
                os.environ, {"ROBIE_JOB_DB": other_db}, clear=False
            ):
                job_id = open_chat_job(
                    watcher_db,
                    "message-same-db-heartbeat",
                    "Perform the destination workflow",
                    conversation_id="spaces/same-db-heartbeat",
                )
                watcher = JobStore(watcher_db)
                now = datetime.now(timezone.utc)
                started = now - timedelta(seconds=300)
                with sqlite3.connect(watcher_db) as conn:
                    conn.execute(
                        "UPDATE jobs SET created_at=?, updated_at=? WHERE id=?",
                        (started.isoformat(), started.isoformat(), job_id),
                    )
                start_generic_chat_job_heartbeat(watcher_db, job_id, now=now)
                failed = watcher.fail_orphaned_chat_jobs(now=now)
                progress = watcher.get_checkpoint(job_id, "gateway_progress")
            self.assertNotIn(job_id, failed)
            self.assertEqual(progress["source"], "hermes-gateway")
            self.assertEqual(watcher.get_job(job_id)["status"], "RUNNING")
            with sqlite3.connect(other_db) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM checkpoints WHERE kind='gateway_progress'"
                    ).fetchone()[0],
                    0,
                )
            stop_generic_chat_job_heartbeat(watcher_db, job_id)

    def test_in_progress_generic_chat_job_is_not_orphan_failed_at_300s(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = open_chat_job(
                db,
                "message-live-gateway",
                "Perform the destination workflow",
                conversation_id="spaces/live-gateway",
            )
            opened = store.get_job(job_id)
            self.assertEqual(opened["status"], "RUNNING")
            self.assertEqual(opened["attempt_count"], 0)
            self.assertIsNone(opened["lease_owner"])
            now = datetime.now(timezone.utc)
            started = now - timedelta(seconds=300)
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "UPDATE jobs SET created_at=?, updated_at=? WHERE id=?",
                    (started.isoformat(), started.isoformat(), job_id),
                )
            store.heartbeat_generic_chat_job(job_id, now=now)
            failed = store.fail_orphaned_chat_jobs(now=now)
            self.assertNotIn(job_id, failed)
            job = store.get_job(job_id)
            self.assertEqual(job["status"], "RUNNING")
            self.assertIsNone(store.get_checkpoint(job_id, "orphan_timeout"))
            self.assertEqual(
                store.get_checkpoint(job_id, "gateway_progress")["source"],
                "hermes-gateway",
            )
            stop_generic_chat_job_heartbeat(db, job_id)

    def test_abandoned_generic_chat_job_fail_closes_without_gateway_heartbeat(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = open_chat_job(
                db,
                "message-abandoned-gateway",
                "Perform the destination workflow",
                conversation_id="spaces/abandoned-gateway",
            )
            stop_generic_chat_job_heartbeat(db, job_id)
            now = datetime.now(timezone.utc)
            stale = now - timedelta(seconds=301)
            store.heartbeat_generic_chat_job(job_id, now=stale)
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "UPDATE jobs SET created_at=?, updated_at=? WHERE id=?",
                    (stale.isoformat(), stale.isoformat(), job_id),
                )
            failed = store.fail_orphaned_chat_jobs(now=now)
            self.assertEqual(failed, [job_id])
            job = store.get_job(job_id)
            self.assertEqual(job["status"], "FAILED")
            self.assertIn("execution did not start", job["last_error"])
            self.assertIsNotNone(store.get_checkpoint(job_id, "orphan_timeout"))

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
                (
                    "HITL lifecycle validation marker FEIN-B577. "
                    "No operational work is requested."
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
                self.assertIsNone(
                    conn.execute(
                        """SELECT name FROM sqlite_master
                           WHERE type='table' AND name='conversation_contexts'"""
                    ).fetchone()
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM conversation_job_links").fetchone()[0],
                    5,
                )

    def test_none_job_passes_prompt_and_response_through(self):
        self.assertFalse(chat_message_requires_job("Thank you"))
        self.assertEqual(build_chat_execution_text("unused.db", None, "Thank you"), "Thank you")
        self.assertEqual(guard_chat_response("unused.db", None, "You're welcome"), "You're welcome")

    def test_awaiting_human_input_renders_friendly_specific_prompt(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "ezlynx.submission_audit",
                {
                    "requested_by": "Carlo",
                    "client_name": "Example Company",
                },
                idempotency_key="friendly-hitl",
            )
            store.transition(
                job["id"],
                JobStatus.AWAITING_HUMAN_INPUT,
                expected={JobStatus.PENDING},
                error="MISSING_REQUIRED_FIELD: FEIN",
                resume_status=JobStatus.PENDING,
                release_lease=True,
            )
            response = guard_chat_response(db, job["id"], "technical fallback")
            self.assertIn("Hey Carlo, I need a quick hand!", response)
            self.assertIn("Federal Employer Identification Number (FEIN)", response)
            self.assertIn("Example Company", response)
            self.assertIn(f"Job ID: `{job['id'][:8]}`", response)
            self.assertNotIn("AWAITING_HUMAN_INPUT", response)

    def test_structured_direct_blocker_parks_and_resumes_same_job(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(
                db,
                "message-direct-hitl",
                "Please finish the EZLynx form",
                requested_by="Carlo",
                conversation_id="spaces/direct-hitl",
            )
            response = guard_chat_response(
                db,
                job_id,
                "ROBIE_BLOCKED: MISSING_REQUIRED_FIELD: FEIN\nI cannot continue.",
            )
            self.assertIn("Federal Employer Identification Number (FEIN)", response)
            self.assertNotIn("AWAITING_HUMAN_INPUT", response)
            store = JobStore(db)
            self.assertEqual(
                store.get_job(job_id)["status"],
                JobStatus.AWAITING_HUMAN_INPUT.value,
            )
            queue = DurableChatEventQueue(db)
            context = queue.active_conversation_job("spaces/direct-hitl")
            self.assertEqual(context["job_id"], job_id)
            self.assertEqual(context["interaction_state"]["field_name"], "FEIN")

            resumed = queue.resume_human_input(
                conversation_id="spaces/direct-hitl",
                job_id=job_id,
                reply_message_id="message-fein-reply",
                field_name="FEIN",
                value="12-3456789",
            )
            self.assertEqual(resumed["state"], "DIRECT_RESUME")
            continued = open_chat_job(
                db,
                "message-fein-reply",
                "12-3456789",
                requested_by="Carlo",
                conversation_id="spaces/direct-hitl",
            )
            self.assertEqual(continued, job_id)
            updated = store.get_job(job_id)
            self.assertEqual(updated["status"], JobStatus.RUNNING.value)
            self.assertEqual(updated["payload"]["human_input_values"]["FEIN"], "12-3456789")
            stop_generic_chat_job_heartbeat(db, job_id)

    def test_resume_reopens_unleased_generic_chat_job_without_second_open(self):
        """HITL resume must re-claim the generic Job. Chat ack is not a claim.

        Production 6cf6f6ae and da53765b-f2c7-4eef-8f48-72b8dc9d2157 wrote
        human_input_resume, posted "Resuming from the saved checkpoint.",
        then died as orphan_timeout: the generic Google Chat Job was not
        claimed within 300 seconds.
        """
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(
                db,
                "message-unleased-hitl",
                "finish policy 220250093",
                requested_by="Carlo",
                conversation_id="spaces/unleased-hitl",
            )
            store = JobStore(db)
            opened = store.get_job(job_id)
            self.assertEqual(opened["action_type"], "hermes.google_chat_task")
            self.assertEqual(opened["attempt_count"], 0)
            self.assertIsNone(opened["lease_owner"])
            guard_chat_response(
                db,
                job_id,
                (
                    "ROBIE_BLOCKED: PLAYWRIGHT_BLOCKED: "
                    "Could not find PENDING-PROGRESSIVE-CA-220250093 in dropdown"
                ),
            )
            stop_generic_chat_job_heartbeat(db, job_id)
            stale = (datetime.now(timezone.utc) - timedelta(seconds=301)).isoformat()
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "UPDATE checkpoints SET created_at=? WHERE job_id=? AND kind='gateway_progress'",
                    (stale, job_id),
                )
            queue = DurableChatEventQueue(db)
            resumed = queue.resume_human_input(
                conversation_id="spaces/unleased-hitl",
                job_id=job_id,
                reply_message_id="spaces/AAQAZbLJO78/messages/3EwF3i1F19Y.nLNTfFdEYQk",
                field_name="operator_response",
                value="just create a new shell for now as a test case",
            )
            self.assertEqual(resumed["state"], "DIRECT_RESUME")
            updated = store.get_job(job_id)
            self.assertEqual(updated["status"], JobStatus.RUNNING.value)
            self.assertIsNone(updated["lease_owner"])
            self.assertEqual(updated["attempt_count"], 0)
            resume_record = store.get_checkpoint_record(job_id, "human_input_resume")
            progress = store.get_checkpoint(job_id, "gateway_progress")
            progress_record = store.get_checkpoint_record(job_id, "gateway_progress")
            self.assertIsNotNone(resume_record)
            self.assertIsNotNone(progress)
            self.assertIsNotNone(progress_record)
            self.assertGreaterEqual(
                progress["last_at"],
                resume_record["created_at"],
            )
            self.assertGreaterEqual(
                progress_record["created_at"],
                resume_record["created_at"],
            )
            self.assertEqual(progress["source"], "hermes-gateway")
            self.assertNotIn("Resuming from the saved checkpoint", str(progress))
            self.assertNotIn(job_id, store.fail_orphaned_chat_jobs())
            later = datetime.now(timezone.utc) + timedelta(seconds=300)
            store.heartbeat_generic_chat_job(job_id, now=later)
            self.assertNotIn(job_id, store.fail_orphaned_chat_jobs(now=later))
            self.assertIsNone(store.get_checkpoint(job_id, "orphan_timeout"))
            self.assertEqual(store.get_job(job_id)["status"], JobStatus.RUNNING.value)
            execution = build_chat_execution_text(
                db, job_id, "finish policy 220250093"
            )
            self.assertIn("just create a new shell for now as a test case", execution)
            stop_generic_chat_job_heartbeat(db, job_id)

    def test_scheduler_posts_orphan_timeout_failed_status_to_chat(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(
                db,
                "message-silent-orphan",
                "Perform the destination workflow",
                conversation_id="spaces/silent-orphan",
            )
            stop_generic_chat_job_heartbeat(db, job_id)
            stale = (datetime.now(timezone.utc) - timedelta(seconds=301)).isoformat()
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "UPDATE checkpoints SET created_at=? WHERE job_id=? AND kind='gateway_progress'",
                    (stale, job_id),
                )
                conn.execute(
                    "UPDATE jobs SET updated_at=?, created_at=? WHERE id=?",
                    (stale, stale, job_id),
                )
            posted: list[tuple[str, str]] = []

            def _poster(space, text, **_kwargs):
                posted.append((space, text))
                return {"name": "ok"}

            with unittest.mock.patch.dict(
                os.environ,
                {"ROBIE_ARTIFACT_ROOT": str(Path(tmp) / "artifacts")},
                clear=False,
            ), unittest.mock.patch(
                "robie_job_engine.chat_app_post.post_as_chat_app",
                side_effect=_poster,
            ):
                result = run_once(db)
            self.assertEqual(result["orphaned_chat_jobs"], 1)
            job = JobStore(db).get_job(job_id)
            self.assertEqual(job["status"], "FAILED")
            self.assertIn(
                "execution did not start: the generic Google Chat Job was not claimed "
                "within 300 seconds",
                job["last_error"],
            )
            self.assertEqual(len(posted), 1)
            self.assertEqual(posted[0][0], "spaces/silent-orphan")
            self.assertIn("FAILED", posted[0][1])
            self.assertIn(
                "the generic Google Chat Job was not claimed within 300 seconds",
                posted[0][1],
            )
            self.assertNotIn("Resuming from the saved checkpoint", posted[0][1])

    def test_notify_terminal_chat_job_posts_failed_status(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(
                db,
                "message-notify-fail",
                "Perform the destination workflow",
                conversation_id="spaces/notify-fail",
            )
            stop_generic_chat_job_heartbeat(db, job_id)
            JobStore(db).transition(
                job_id,
                JobStatus.FAILED,
                expected={JobStatus.RUNNING},
                error=(
                    "execution did not start: the generic Google Chat Job was not "
                    "claimed within 300 seconds"
                ),
                release_lease=True,
            )
            posted: list[tuple[str, str]] = []
            result = notify_terminal_chat_job(
                db,
                job_id,
                poster=lambda space, text, **_k: posted.append((space, text)),
            )
            self.assertTrue(result["posted"])
            self.assertEqual(posted[0][0], "spaces/notify-fail")
            self.assertIn("FAILED", posted[0][1])
            self.assertIn("not claimed within 300 seconds", posted[0][1])

    def test_free_form_blocker_prose_does_not_solicit_human_input(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(db, "message-prose", "Please finish the form")
            response = guard_chat_response(
                db,
                job_id,
                "I might be blocked and may need a field.",
            )
            self.assertIn("UNVERIFIED", response)
            self.assertEqual(JobStore(db).get_job(job_id)["status"], "UNVERIFIED")

    def test_execution_contract_is_added_without_attachments(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(db, "message-contract", "Please finish the form")
            execution = build_chat_execution_text(db, job_id, "Please finish the form")
            self.assertIn("ROBIE_BLOCKED: MISSING_REQUIRED_FIELD", execution)
            self.assertIn("ROBIE_BLOCKED: PLAYWRIGHT_BLOCKED", execution)
            self.assertIn("ask Gemini for one unique field", execution)
            self.assertIn("HITL Carlo if Gemini is unsure", execution)
            self.assertIn("exact phrase Robie was here", execution)
            self.assertIn("Never write a note on Untitled", execution)
            self.assertIn("Save and Continue Edit", execution)
            self.assertIn("Do not bind", execution)
            self.assertIn("Filling Policy Shell", execution)
            self.assertIn("stuck", execution)
            self.assertIn("no destination-verified evidence", execution)
            self.assertIn("/web/account/<id>/", execution)
            self.assertIn("Do not enumerate Summary, Details, or Index", execution)

    def test_chat_response_is_checkpointed_and_unverified(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(db, "spaces/s/messages/m1", "move it")
            response = guard_chat_response(db, job_id, "Done")
            self.assertIn("UNVERIFIED", response)
            self.assertIn("Reason:", response)
            self.assertIn("no structured destination action checkpoint", response)
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

    def test_corrective_dm_retargets_same_zero_attempt_job_to_submission_audit(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            first_id = open_chat_job(
                db,
                "spaces/s/messages/misrouted",
                "Run the destination workflow now",
                conversation_id="spaces/dm",
                requested_by="Carlo",
            )
            self.assertEqual(
                JobStore(db).get_job(first_id)["action_type"],
                "hermes.google_chat_task",
            )
            continued_id = open_chat_job(
                db,
                "spaces/s/messages/correction",
                (
                    "Correction: run the same read-only Submission Center audit. "
                    "Do not modify records or send emails."
                ),
                conversation_id="spaces/dm",
                requested_by="Carlo",
            )
            self.assertEqual(continued_id, first_id)
            job = JobStore(db).get_job(first_id)
            self.assertEqual(job["action_type"], "ezlynx.submission_audit")
            self.assertEqual(job["status"], "PENDING")
            self.assertEqual(
                job["payload"]["scope"]["assigned_producer"],
                "Streetsmart Insurance",
            )
            self.assertIsNotNone(JobStore(db).get_checkpoint(first_id, "route_correction"))
            with sqlite3.connect(db) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)

    def test_explicit_job_id_correction_cannot_remain_generic(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            first_id = open_chat_job(
                db,
                "spaces/s/messages/original",
                "Run the destination workflow now",
                conversation_id="spaces/dm",
                requested_by="Carlo",
            )
            correction = (
                f"Correction: continue the same read-only Submission Center audit job {first_id}. "
                "Treat this as a continuation, not a new job. Verify All Submissions; "
                "Streetsmart Insurance with My Submissions cleared; exactly 100 mat-row "
                "elements; the live pager total; Status aria-sort=ascending; inspect "
                "through the first closed row. Do not modify records or send emails."
            )
            continued_id = open_chat_job(
                db,
                "spaces/s/messages/explicit-correction",
                correction,
                conversation_id="spaces/dm",
                requested_by="Carlo",
            )
            self.assertEqual(continued_id, first_id)
            job = JobStore(db).get_job(first_id)
            self.assertEqual(job["action_type"], "ezlynx.submission_audit")
            self.assertEqual(job["status"], "PENDING")
            self.assertIsNotNone(JobStore(db).get_checkpoint(first_id, "route_correction"))
            with sqlite3.connect(db) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)

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

    def test_published_complete_job_cannot_be_downgraded(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(db, "immutable-complete", "move it")
            store = JobStore(db)
            # This test isolates the post-publication immutability rule. The
            # normal COMPLETE path and its evidence gate are tested elsewhere.
            with store.transaction() as conn:
                conn.execute(
                    "UPDATE jobs SET status=? WHERE id=?",
                    (JobStatus.COMPLETE.value, job_id),
                )
            store.checkpoint(job_id, "control_center_publication", {"sheet_row": 7})
            with self.assertRaisesRegex(RuntimeError, "immutable"):
                store.fail_unpublished_completion(job_id, "late failure")
            self.assertEqual(store.get_job(job_id)["status"], JobStatus.COMPLETE)


if __name__ == "__main__":
    unittest.main()
