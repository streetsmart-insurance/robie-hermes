from __future__ import annotations

import unittest

from durable_temp import durable_temporary_directory
from robie_job_engine.chat_admin import parse_admin_command
from robie_job_engine.chat_queue import DurableChatEventQueue
from robie_job_engine.hitl import classify_human_reply
from robie_job_engine.models import JobStatus
from robie_job_engine.store import JobStore


class DirectMessageHitlIntentE2ETests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = durable_temporary_directory()
        self.addCleanup(self.temp.cleanup)
        self.db = f"{self.temp.name}/jobs.db"
        self.jobs = JobStore(self.db)
        self.queue = DurableChatEventQueue(self.db)

    def _park(self, *, event_id: str, field_name: str) -> dict:
        job = self.jobs.create_job(
            "ezlynx.form_setup",
            {"human_input_values": {}, "company": "Regression Test"},
            idempotency_key=event_id,
        )
        self.queue.enqueue(
            event_id=event_id,
            conversation_id="spaces/direct-message",
            message_id=f"message-{event_id}",
            payload={"job_id": job["id"]},
            job_id=job["id"],
        )
        self.queue.link_conversation_job(
            conversation_id="spaces/direct-message",
            job_id=job["id"],
            message_id=f"message-{event_id}",
            event_id=event_id,
            interaction_state={
                "awaiting": "human_input",
                "field_name": field_name,
                "field_label": field_name,
                "accepts_value": True,
            },
        )
        self.queue.claim_next("worker-test")
        self.jobs.transition(
            job["id"],
            JobStatus.AWAITING_HUMAN_INPUT,
            expected={JobStatus.PENDING},
            error=f"MISSING_REQUIRED_FIELD: {field_name}",
            resume_status=JobStatus.PENDING,
            release_lease=True,
        )
        self.queue.await_human_input(
            event_id,
            "worker-test",
            interaction_state={
                "awaiting": "human_input",
                "field_name": field_name,
                "field_label": field_name,
                "accepts_value": True,
            },
            error=f"MISSING_REQUIRED_FIELD: {field_name}",
        )
        return job

    def test_field_only_reply_resumes_exact_parked_job(self):
        job = self._park(event_id="event-fein", field_name="FEIN")
        context = self.queue.active_conversation_job("spaces/direct-message")
        self.assertEqual(
            classify_human_reply("12-3456789", context["interaction_state"]),
            "ANSWER",
        )
        resumed = self.queue.resume_human_input(
            conversation_id="spaces/direct-message",
            job_id=job["id"],
            reply_message_id="reply-fein",
            field_name="FEIN",
            value="12-3456789",
        )
        self.assertEqual(resumed["job_id"], job["id"])
        self.assertEqual(resumed["state"], "QUEUED")
        self.assertEqual(self.jobs.get_job(job["id"])["status"], "PENDING")

    def test_new_admin_intent_detaches_old_job_and_cannot_contaminate_payload(self):
        old_job = self._park(event_id="event-old", field_name="FEIN")
        context = self.queue.active_conversation_job("spaces/direct-message")
        text = "Show me all my currently scheduled jobs"
        self.assertEqual(
            classify_human_reply(text, context["interaction_state"]),
            "NEW_INTENT",
        )
        self.assertEqual(parse_admin_command(text).name, "schedules")

        self.assertEqual(self.queue.deactivate_conversation("spaces/direct-message"), 1)
        self.assertIsNone(self.queue.active_conversation_job("spaces/direct-message"))
        preserved = self.jobs.get_job(old_job["id"])
        self.assertEqual(preserved["status"], "AWAITING_HUMAN_INPUT")
        self.assertEqual(preserved["payload"]["human_input_values"], {})
        self.assertNotIn(text, str(preserved["payload"]))

        # Administrative status reads intentionally do not create executable
        # Jobs. A subsequent executable request is correlated to a fresh ID.
        new_job = self.jobs.create_job(
            "ezlynx.submission_audit",
            {"read_only": True},
            idempotency_key="event-new-executable",
        )
        self.queue.enqueue(
            event_id="event-new-executable",
            conversation_id="spaces/direct-message",
            message_id="message-new-executable",
            payload={"job_id": new_job["id"]},
            job_id=new_job["id"],
        )
        self.queue.link_conversation_job(
            conversation_id="spaces/direct-message",
            job_id=new_job["id"],
            message_id="message-new-executable",
            event_id="event-new-executable",
        )
        self.assertNotEqual(new_job["id"], old_job["id"])
        self.assertEqual(
            self.queue.active_conversation_job("spaces/direct-message")["job_id"],
            new_job["id"],
        )


if __name__ == "__main__":
    unittest.main()
