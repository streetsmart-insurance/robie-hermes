"""Production 2026-08-27: a terminal Job left an active HITL bind.

Job 40ccc0d6-c420-4a7b-90a6-35d83bdcc921 FAILED on hermes-poc-01 at
20:40:40 UTC (Playwright TimeoutError / computer_use no-DISPLAY).
conversation_job_links row 45 stayed active=1 for spaces/AAQAZbLJO78
with interaction_state awaiting=human_input and accepts_value=true.

A new @robie at 20:47:03 UTC was classified as a HITL reply, 
resume_human_input raised RuntimeError("bound Job is not awaiting
human input"), and _dispatch_message aborted before open_chat_job.
Pub/Sub never settled.

These tests walk the same Job Engine + Chat queue calls hermes-gateway
uses. They do not change bind_job or money paths.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from tests.durable_temp import durable_temporary_directory

from robie_job_engine.chat_guard import open_chat_job, stop_generic_chat_job_heartbeat
from robie_job_engine.chat_queue import (
    ChatEventConflict,
    DurableChatEventQueue,
    is_stale_human_input_bind_error,
)
from robie_job_engine.hitl import classify_human_reply
from robie_job_engine.models import JobStatus
from robie_job_engine.store import JobStore


SPACE = "spaces/AAQAZbLJO78"
# argumentText after Google Chat strips the @robie mention. Short enough
# that classify_human_reply returns ANSWER when accepts_value is true.
NEW_MENTION = "finish the commercial auto form"


class StaleTerminalHitlBindTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = durable_temporary_directory()
        self.addCleanup(self.temp.cleanup)
        self.db = str(Path(self.temp.name) / "jobs.db")
        self.store = JobStore(self.db)
        self.queue = DurableChatEventQueue(self.db)

    def _failed_job_with_stale_hitl_bind(self) -> dict:
        job = self.store.create_job(
            "hermes.google_chat_task",
            {
                "worker": "hermes-cua",
                "text": "finish the commercial auto form",
                "conversation_id": SPACE,
                "human_input_values": {},
            },
            idempotency_key="prod-40ccc0d6-stale-hitl",
        )
        self.store.transition(
            job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING}
        )
        self.queue.link_conversation_job(
            conversation_id=SPACE,
            job_id=job["id"],
            message_id=f"{SPACE}/messages/old",
            event_id=f"{SPACE}/messages/old",
            interaction_state={
                "awaiting": "human_input",
                "field_name": "operator_response",
                "accepts_value": True,
            },
        )
        self.store.transition(
            job["id"],
            JobStatus.FAILED,
            expected={JobStatus.RUNNING},
            error="TimeoutError: computer_use no-DISPLAY",
            release_lease=True,
        )
        return self.store.get_job(job["id"])

    def test_new_mention_on_failed_bind_opens_a_new_job(self):
        failed = self._failed_job_with_stale_hitl_bind()
        context = self.queue.active_conversation_job(SPACE)
        interaction = dict(context["interaction_state"])
        self.assertEqual(failed["status"], JobStatus.FAILED.value)
        self.assertEqual(context["job_id"], failed["id"])
        self.assertEqual(interaction["awaiting"], "human_input")
        self.assertTrue(interaction["accepts_value"])
        self.assertEqual(classify_human_reply(NEW_MENTION, interaction), "ANSWER")

        with self.assertRaisesRegex(
            RuntimeError, "bound Job is not awaiting human input"
        ):
            self.queue.resume_human_input(
                conversation_id=SPACE,
                job_id=failed["id"],
                reply_message_id=f"{SPACE}/messages/new-robie",
                field_name="operator_response",
                value=NEW_MENTION,
            )

        released = self.queue.release_stale_human_input_bind(SPACE)
        self.assertIsNotNone(released)
        self.assertEqual(released["job_id"], failed["id"])
        self.assertEqual(released["job_status"], JobStatus.FAILED.value)
        self.assertIsNone(self.queue.active_conversation_job(SPACE))

        new_id = open_chat_job(
            self.db,
            f"{SPACE}/messages/new-robie",
            NEW_MENTION,
            requested_by="Carlo",
            conversation_id=SPACE,
        )
        self.assertIsNotNone(new_id)
        self.assertNotEqual(new_id, failed["id"])
        self.assertEqual(self.store.get_job(failed["id"])["status"], JobStatus.FAILED.value)
        self.assertEqual(
            self.queue.active_conversation_job(SPACE)["job_id"], new_id
        )
        created = self.store.get_job(new_id)
        self.assertEqual(created["action_type"], "hermes.google_chat_task")
        self.assertNotEqual(created["status"], JobStatus.FAILED.value)
        stop_generic_chat_job_heartbeat(self.db, new_id)

    def test_unverified_bind_also_opens_a_new_job(self):
        job = self.store.create_job(
            "hermes.google_chat_task",
            {
                "worker": "hermes-cua",
                "text": "finish the form",
                "conversation_id": SPACE,
            },
            idempotency_key="unverified-stale-hitl",
        )
        self.store.transition(
            job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING}
        )
        self.queue.link_conversation_job(
            conversation_id=SPACE,
            job_id=job["id"],
            message_id=f"{SPACE}/messages/unverified",
            event_id=f"{SPACE}/messages/unverified",
            interaction_state={
                "awaiting": "human_input",
                "field_name": "operator_response",
                "accepts_value": True,
            },
        )
        self.store.transition(
            job["id"],
            JobStatus.UNVERIFIED,
            expected={JobStatus.RUNNING},
            error="no structured destination action checkpoint",
            release_lease=True,
        )
        released = self.queue.release_stale_human_input_bind(SPACE)
        self.assertEqual(released["job_status"], JobStatus.UNVERIFIED.value)
        new_id = open_chat_job(
            self.db,
            f"{SPACE}/messages/new-after-unverified",
            NEW_MENTION,
            requested_by="Carlo",
            conversation_id=SPACE,
        )
        self.assertNotEqual(new_id, job["id"])
        self.assertEqual(
            self.store.get_job(job["id"])["status"], JobStatus.UNVERIFIED.value
        )
        stop_generic_chat_job_heartbeat(self.db, new_id)

    def test_live_hitl_reply_still_resumes_and_reopens(self):
        job_id = open_chat_job(
            self.db,
            f"{SPACE}/messages/live-hitl",
            "finish policy 220250093",
            requested_by="Carlo",
            conversation_id=SPACE,
        )
        from robie_job_engine.chat_guard import guard_chat_response

        guard_chat_response(
            self.db,
            job_id,
            (
                "ROBIE_BLOCKED: PLAYWRIGHT_BLOCKED: "
                "Could not find PENDING-PROGRESSIVE-CA-220250093 in dropdown"
            ),
        )
        stop_generic_chat_job_heartbeat(self.db, job_id)
        context = self.queue.active_conversation_job(SPACE)
        self.assertEqual(context["job_id"], job_id)
        self.assertEqual(
            self.store.get_job(job_id)["status"],
            JobStatus.AWAITING_HUMAN_INPUT.value,
        )
        self.assertIsNone(self.queue.release_stale_human_input_bind(SPACE))
        reply = "just create a new shell for now as a test case"
        self.assertEqual(
            classify_human_reply(reply, context["interaction_state"]),
            "ANSWER",
        )
        resumed = self.queue.resume_human_input(
            conversation_id=SPACE,
            job_id=job_id,
            reply_message_id=f"{SPACE}/messages/hitl-reply",
            field_name="operator_response",
            value=reply,
        )
        self.assertEqual(resumed["state"], "DIRECT_RESUME")
        updated = self.store.get_job(job_id)
        self.assertEqual(updated["status"], JobStatus.RUNNING.value)
        self.assertIsNone(updated["lease_owner"])
        resume_record = self.store.get_checkpoint_record(job_id, "human_input_resume")
        progress = self.store.get_checkpoint(job_id, "gateway_progress")
        self.assertIsNotNone(resume_record)
        self.assertIsNotNone(progress)
        self.assertGreaterEqual(progress["last_at"], resume_record["created_at"])
        self.assertEqual(progress["source"], "hermes-gateway")
        self.assertEqual(
            self.queue.active_conversation_job(SPACE)["job_id"], job_id
        )
        stop_generic_chat_job_heartbeat(self.db, job_id)

    def test_dead_bind_runtime_error_falls_through_to_open_chat_job(self):
        failed = self._failed_job_with_stale_hitl_bind()
        try:
            self.queue.resume_human_input(
                conversation_id=SPACE,
                job_id=failed["id"],
                reply_message_id=f"{SPACE}/messages/retry",
                field_name="operator_response",
                value=NEW_MENTION,
            )
        except RuntimeError as exc:
            self.assertTrue(is_stale_human_input_bind_error(exc))
            self.assertEqual(self.queue.deactivate_conversation(SPACE), 1)
        else:
            self.fail("resume_human_input must reject a FAILED bind")
        self.assertIsNone(self.queue.active_conversation_job(SPACE))
        new_id = open_chat_job(
            self.db,
            f"{SPACE}/messages/after-runtime-error",
            NEW_MENTION,
            requested_by="Carlo",
            conversation_id=SPACE,
        )
        self.assertNotEqual(new_id, failed["id"])
        stop_generic_chat_job_heartbeat(self.db, new_id)

    def test_bind_job_still_fails_closed_on_rebind(self):
        self.queue.enqueue(
            event_id="event-bind-money",
            conversation_id=SPACE,
            message_id="message-bind-money",
            payload={"request": "bounded audit"},
        )
        self.assertEqual(
            self.queue.bind_job("event-bind-money", "job-1")["job_id"], "job-1"
        )
        with self.assertRaises(ChatEventConflict):
            self.queue.bind_job("event-bind-money", "job-2")
        self.assertEqual(self.queue.get("event-bind-money")["job_id"], "job-1")


if __name__ == "__main__":
    unittest.main()
