from __future__ import annotations

import unittest
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.durable_temp import durable_temporary_directory

from robie_job_engine.chat_queue import (
    ChatEventConflict,
    ConversationJobLinkConflict,
    DurableChatEventQueue,
    is_stale_human_input_bind_error,
)
from robie_job_engine.context_policy import JobContextManager
from robie_job_engine.models import JobStatus
from robie_job_engine.store import JobStore


class DurableChatEventQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = durable_temporary_directory()
        self.db = Path(self.temp.name) / "durable" / "jobs.db"
        self.queue = DurableChatEventQueue(self.db)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_exact_redelivery_is_idempotent(self):
        first = self.queue.enqueue(
            event_id="spaces/s/messages/m",
            conversation_id="spaces/s",
            message_id="spaces/s/messages/m",
            payload={"action_type": "ezlynx.submission_audit"},
        )
        second = self.queue.enqueue(
            event_id="spaces/s/messages/m",
            conversation_id="spaces/s",
            message_id="spaces/s/messages/m",
            payload={"action_type": "ezlynx.submission_audit"},
        )
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["event_id"], second["event_id"])

    def test_existing_production_queue_schema_is_upgraded_in_place(self):
        legacy_db = Path(self.temp.name) / "durable" / "pre-phase2.db"
        legacy_db.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(legacy_db) as conn:
            conn.execute(
                """CREATE TABLE chat_event_queue (
                    event_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                    message_id TEXT NOT NULL, payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL, job_id TEXT, state TEXT NOT NULL,
                    lease_owner TEXT, lease_expires_at TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0, last_error TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                )"""
            )
        upgraded = DurableChatEventQueue(legacy_db)
        with upgraded._connect() as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(chat_event_queue)")}
            index_columns = [
                row[2]
                for row in conn.execute("PRAGMA index_info(idx_chat_event_queue_runnable)")
            ]
        self.assertIn("max_attempts", columns)
        self.assertIn("available_at", columns)
        self.assertEqual(
            index_columns,
            ["state", "available_at", "lease_expires_at", "created_at"],
        )
        queued = upgraded.enqueue(
            event_id="post-upgrade",
            conversation_id="spaces/s",
            message_id="message-upgrade",
            payload={"request": "audit"},
        )
        self.assertEqual(queued["max_attempts"], 3)
        self.assertTrue(queued["available_at"])

    def test_changed_redelivery_fails_closed(self):
        self.queue.enqueue(
            event_id="event-1",
            conversation_id="spaces/s",
            message_id="message-1",
            payload={"scope": "read-only"},
        )
        with self.assertRaises(ChatEventConflict):
            self.queue.enqueue(
                event_id="event-1",
                conversation_id="spaces/s",
                message_id="message-1",
                payload={"scope": "write"},
            )

    def test_committed_event_survives_process_reconstruction(self):
        self.queue.enqueue(
            event_id="event-restart",
            conversation_id="spaces/s",
            message_id="message-restart",
            payload={"request": "bounded audit"},
        )
        reconstructed = DurableChatEventQueue(self.db)
        claimed = reconstructed.claim_next("worker-a", lease_seconds=60)
        self.assertEqual(claimed["event_id"], "event-restart")
        self.assertEqual(claimed["state"], "INFLIGHT")
        self.assertEqual(claimed["attempt_count"], 1)
        self.assertEqual(
            reconstructed.complete("event-restart", "worker-a")["state"],
            "COMPLETE",
        )

    def test_expired_lease_is_recovered_once(self):
        self.queue.enqueue(
            event_id="event-lease",
            conversation_id="spaces/s",
            message_id="message-lease",
            payload={"request": "bounded audit"},
        )
        first = self.queue.claim_next("worker-a", lease_seconds=60)
        self.assertIsNotNone(first)
        expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        with self.queue._connect() as conn:
            conn.execute(
                "UPDATE chat_event_queue SET lease_expires_at=? WHERE event_id=?",
                (expired, "event-lease"),
            )
        recovered = self.queue.claim_next("worker-b", lease_seconds=60)
        self.assertEqual(recovered["event_id"], "event-lease")
        self.assertEqual(recovered["lease_owner"], "worker-b")
        self.assertEqual(recovered["attempt_count"], 2)

    def test_hitl_state_survives_restart_and_resumes_same_job_atomically(self):
        store = JobStore(self.db)
        job = store.create_job(
            "ezlynx.submission_audit",
            {"read_only": True, "human_input_values": {}},
            idempotency_key="hitl-restart",
        )
        self.queue.enqueue(
            event_id="event-hitl",
            conversation_id="spaces/dm",
            message_id="message-hitl",
            payload={"job_id": job["id"]},
            job_id=job["id"],
        )
        self.queue.link_conversation_job(
            conversation_id="spaces/dm",
            job_id=job["id"],
            message_id="message-hitl",
            event_id="event-hitl",
        )
        self.queue.claim_next("worker-a", lease_seconds=60)
        store.transition(
            job["id"],
            JobStatus.AWAITING_HUMAN_INPUT,
            expected={JobStatus.PENDING},
            error="MISSING_REQUIRED_FIELD: NAICS code",
            resume_status=JobStatus.PENDING,
            release_lease=True,
        )
        parked = self.queue.await_human_input(
            "event-hitl",
            "worker-a",
            interaction_state={
                "awaiting": "human_input",
                "field_name": "NAICS code",
                "checkpoint": "required_field:NAICS code",
            },
            error="MISSING_REQUIRED_FIELD: NAICS code",
        )
        self.assertEqual(parked["state"], "AWAITING_HUMAN_INPUT")

        restarted = DurableChatEventQueue(self.db)
        context = restarted.active_conversation_job("spaces/dm")
        self.assertEqual(context["job_id"], job["id"])
        self.assertEqual(
            context["interaction_state"]["checkpoint"],
            "required_field:NAICS code",
        )
        resumed = restarted.resume_human_input(
            conversation_id="spaces/dm",
            job_id=job["id"],
            reply_message_id="reply-hitl",
            field_name="NAICS code",
            value="541611",
        )
        self.assertEqual(resumed["state"], "QUEUED")
        updated = store.get_job(job["id"])
        self.assertEqual(updated["status"], JobStatus.PENDING.value)
        self.assertEqual(
            updated["payload"]["human_input_values"]["NAICS code"], "541611"
        )
        checkpoint = store.get_checkpoint(job["id"], "human_input_resume")
        self.assertNotIn("541611", str(checkpoint))
        self.assertEqual(restarted.claim_next("worker-b")["job_id"], job["id"])

    def test_generic_chat_hitl_resume_reopens_unleased_job(self):
        store = JobStore(self.db)
        job = store.create_job(
            "hermes.google_chat_task",
            {
                "worker": "hermes-cua",
                "text": "finish policy 220250093",
                "conversation_id": "spaces/generic-hitl",
                "human_input_values": {},
            },
            idempotency_key="generic-hitl-resume",
        )
        store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
        self.assertIsNone(store.get_job(job["id"])["lease_owner"])
        self.queue.link_conversation_job(
            conversation_id="spaces/generic-hitl",
            job_id=job["id"],
            message_id="message-generic-hitl",
            event_id="event-generic-hitl",
            interaction_state={
                "awaiting": "human_input",
                "field_name": "operator_response",
                "accepts_value": True,
            },
        )
        self.queue.park_direct_human_input(
            conversation_id="spaces/generic-hitl",
            job_id=job["id"],
            interaction_state={
                "awaiting": "human_input",
                "field_name": "operator_response",
                "checkpoint": "playwright_blocked:dropdown",
                "accepts_value": True,
            },
            error="PLAYWRIGHT_BLOCKED: Could not find PENDING-PROGRESSIVE-CA-220250093 in dropdown",
        )
        self.assertEqual(
            store.get_job(job["id"])["status"],
            JobStatus.AWAITING_HUMAN_INPUT.value,
        )
        resumed = self.queue.resume_human_input(
            conversation_id="spaces/generic-hitl",
            job_id=job["id"],
            reply_message_id="reply-generic-hitl",
            field_name="operator_response",
            value="just create a new shell for now as a test case",
        )
        self.assertEqual(resumed["state"], "DIRECT_RESUME")
        updated = store.get_job(job["id"])
        self.assertEqual(updated["status"], JobStatus.RUNNING.value)
        self.assertIsNone(updated["lease_owner"])
        resume_record = store.get_checkpoint_record(job["id"], "human_input_resume")
        progress = store.get_checkpoint(job["id"], "gateway_progress")
        self.assertIsNotNone(progress)
        self.assertGreaterEqual(progress["last_at"], resume_record["created_at"])
        self.assertEqual(progress["source"], "hermes-gateway")
        from robie_job_engine.chat_guard import stop_generic_chat_job_heartbeat

        stop_generic_chat_job_heartbeat(str(self.db), job["id"])

    def test_release_stale_human_input_bind_clears_terminal_job(self):
        store = JobStore(self.db)
        conversation_id = "spaces/AAQAZbLJO78"
        for status in (
            JobStatus.FAILED,
            JobStatus.UNVERIFIED,
            JobStatus.COMPLETE,
        ):
            with self.subTest(status=status.value):
                job = store.create_job(
                    "hermes.google_chat_task",
                    {
                        "worker": "hermes-cua",
                        "text": "finish the commercial auto form",
                        "conversation_id": conversation_id,
                    },
                    idempotency_key=f"stale-hitl-{status.value}",
                )
                if status != JobStatus.COMPLETE:
                    store.transition(
                        job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING}
                    )
                    store.transition(
                        job["id"],
                        status,
                        expected={JobStatus.RUNNING},
                        error="TimeoutError: computer_use no-DISPLAY",
                        release_lease=True,
                    )
                else:
                    with store.transaction() as conn:
                        conn.execute(
                            "UPDATE jobs SET status=? WHERE id=?",
                            (JobStatus.COMPLETE.value, job["id"]),
                        )
                self.queue.link_conversation_job(
                    conversation_id=conversation_id,
                    job_id=job["id"],
                    message_id=f"message-{status.value}",
                    event_id=f"event-{status.value}",
                    interaction_state={
                        "awaiting": "human_input",
                        "field_name": "operator_response",
                        "accepts_value": True,
                    },
                )
                released = self.queue.release_stale_human_input_bind(conversation_id)
                self.assertIsNotNone(released)
                self.assertEqual(released["job_id"], job["id"])
                self.assertEqual(released["job_status"], status.value)
                self.assertEqual(released["released_reason"], "terminal_job")
                self.assertIsNone(self.queue.active_conversation_job(conversation_id))

    def test_release_stale_human_input_bind_keeps_live_hitl(self):
        store = JobStore(self.db)
        job = store.create_job(
            "hermes.google_chat_task",
            {
                "worker": "hermes-cua",
                "text": "finish policy 220250093",
                "conversation_id": "spaces/live-hitl",
                "human_input_values": {},
            },
            idempotency_key="live-hitl-keep",
        )
        store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
        self.queue.link_conversation_job(
            conversation_id="spaces/live-hitl",
            job_id=job["id"],
            message_id="message-live-hitl",
            event_id="event-live-hitl",
            interaction_state={
                "awaiting": "human_input",
                "field_name": "operator_response",
                "accepts_value": True,
            },
        )
        self.queue.park_direct_human_input(
            conversation_id="spaces/live-hitl",
            job_id=job["id"],
            interaction_state={
                "awaiting": "human_input",
                "field_name": "operator_response",
                "checkpoint": "playwright_blocked:dropdown",
                "accepts_value": True,
            },
            error="PLAYWRIGHT_BLOCKED: dropdown",
        )
        self.assertIsNone(self.queue.release_stale_human_input_bind("spaces/live-hitl"))
        context = self.queue.active_conversation_job("spaces/live-hitl")
        self.assertEqual(context["job_id"], job["id"])
        self.assertEqual(context["interaction_state"]["awaiting"], "human_input")
        self.assertEqual(
            store.get_job(job["id"])["status"],
            JobStatus.AWAITING_HUMAN_INPUT.value,
        )

    def test_stale_human_input_bind_error_matches_dead_bind_only(self):
        self.assertTrue(
            is_stale_human_input_bind_error(
                RuntimeError("bound Job is not awaiting human input")
            )
        )
        self.assertTrue(
            is_stale_human_input_bind_error(
                RuntimeError("active human-input correlation is missing")
            )
        )
        self.assertFalse(
            is_stale_human_input_bind_error(
                RuntimeError("parked queue event changed during resume")
            )
        )
        self.assertFalse(is_stale_human_input_bind_error(RuntimeError("exhausted")))

    def test_heartbeat_renews_lease_without_incrementing_attempt(self):
        self.queue.enqueue(
            event_id="event-heartbeat",
            conversation_id="spaces/s",
            message_id="message-heartbeat",
            payload={"request": "long playwright audit"},
        )
        claimed = self.queue.claim_next("worker-a", lease_seconds=1)
        renewed = self.queue.renew_lease(
            "event-heartbeat", "worker-a", lease_seconds=120
        )
        self.assertGreater(renewed["lease_expires_at"], claimed["lease_expires_at"])
        self.assertEqual(renewed["attempt_count"], 1)
        self.assertIsNone(self.queue.claim_next("worker-b", lease_seconds=120))

    def test_transient_worker_failure_is_deferred_and_reclaimed(self):
        self.queue.enqueue(
            event_id="event-retry",
            conversation_id="spaces/s",
            message_id="message-retry",
            payload={"request": "read-only playwright audit"},
            max_attempts=3,
        )
        first = self.queue.claim_next("worker-a", lease_seconds=60)
        due = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        deferred = self.queue.defer(
            "event-retry", "worker-a", available_at=due, error="browser crashed"
        )
        self.assertEqual(deferred["state"], "QUEUED")
        recovered = self.queue.claim_next("worker-b", lease_seconds=60)
        self.assertEqual(recovered["attempt_count"], 2)
        self.assertEqual(recovered["lease_owner"], "worker-b")

    def test_defer_fails_closed_after_maximum_attempts(self):
        self.queue.enqueue(
            event_id="event-exhausted",
            conversation_id="spaces/s",
            message_id="message-exhausted",
            payload={"request": "read-only playwright audit"},
            max_attempts=1,
        )
        self.queue.claim_next("worker-a", lease_seconds=60)
        with self.assertRaisesRegex(RuntimeError, "exhausted"):
            self.queue.defer(
                "event-exhausted",
                "worker-a",
                available_at=datetime.now(timezone.utc).isoformat(),
                error="browser crashed",
            )

    def test_job_binding_is_one_way(self):
        self.queue.enqueue(
            event_id="event-bind",
            conversation_id="spaces/s",
            message_id="message-bind",
            payload={"request": "bounded audit"},
        )
        self.assertEqual(self.queue.bind_job("event-bind", "job-1")["job_id"], "job-1")
        with self.assertRaises(ChatEventConflict):
            self.queue.bind_job("event-bind", "job-2")

    def test_conversation_job_link_survives_restart_and_is_idempotent(self):
        first = self.queue.link_conversation_job(
            conversation_id="spaces/dm",
            job_id="job-1",
            message_id="message-1",
            event_id="event-1",
        )
        reconstructed = DurableChatEventQueue(self.db)
        duplicate = reconstructed.link_conversation_job(
            conversation_id="spaces/dm",
            job_id="job-1",
            message_id="message-1",
            event_id="event-1",
        )
        self.assertFalse(first["duplicate"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(
            reconstructed.active_conversation_job("spaces/dm")["job_id"],
            "job-1",
        )

    def test_legacy_active_context_is_migrated_to_conversation_job_links(self):
        legacy_db = Path(self.temp.name) / "durable" / "legacy-jobs.db"
        jobs = JobStore(legacy_db)
        job = jobs.create_job(
            "browser.read", {"task": "read only"}, idempotency_key="legacy-context"
        )
        JobContextManager(legacy_db).bind_job("spaces/legacy-dm", job["id"])

        migrated = DurableChatEventQueue(legacy_db)
        link = migrated.active_conversation_job("spaces/legacy-dm")
        self.assertEqual(link["job_id"], job["id"])
        self.assertEqual(link["relation"], "CONTINUATION")
        self.assertTrue(link["event_id"].startswith("migration:spaces/legacy-dm:"))

    def test_new_job_deactivates_old_link_but_preserves_history(self):
        self.queue.link_conversation_job(
            conversation_id="spaces/dm", job_id="job-1",
            message_id="message-1", event_id="event-1",
        )
        self.queue.link_conversation_job(
            conversation_id="spaces/dm", job_id="job-2",
            message_id="message-2", event_id="event-2",
        )
        self.assertEqual(self.queue.active_conversation_job("spaces/dm")["job_id"], "job-2")
        with self.queue._connect() as conn:
            rows = conn.execute(
                "SELECT job_id,active FROM conversation_job_links ORDER BY id"
            ).fetchall()
        self.assertEqual([(row["job_id"], row["active"]) for row in rows], [("job-1", 0), ("job-2", 1)])
        self.assertEqual(self.queue.deactivate_conversation("spaces/dm"), 1)
        self.assertIsNone(self.queue.active_conversation_job("spaces/dm"))

    def test_inactivity_expiry_preserves_pending_approval(self):
        jobs = JobStore(self.db)
        stale_job = jobs.create_job(
            "browser.read", {"worker": "browser-read"}, idempotency_key="stale-job"
        )
        approval_job = jobs.create_job(
            "browser.read", {"worker": "browser-read"}, idempotency_key="approval-job"
        )
        self.queue.link_conversation_job(
            conversation_id="spaces/stale",
            job_id=stale_job["id"],
            message_id="message-stale",
            event_id="event-stale",
        )
        self.queue.link_conversation_job(
            conversation_id="spaces/approval",
            job_id=approval_job["id"],
            message_id="message-approval",
            event_id="event-approval",
            relation="APPROVAL",
            pending_decision_id="decision-pending",
            interaction_state={"awaiting": "approval"},
        )
        old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        with self.queue._connect() as conn:
            conn.execute("UPDATE conversation_job_links SET updated_at=?", (old,))
        expired = self.queue.expire_inactive_conversations(inactivity_minutes=120)
        self.assertEqual(expired, ["event-stale"])
        self.assertIsNone(self.queue.active_conversation_job("spaces/stale"))
        self.assertEqual(
            self.queue.active_conversation_job("spaces/approval")["pending_decision_id"],
            "decision-pending",
        )

    def test_conversation_event_cannot_be_rebound(self):
        self.queue.link_conversation_job(
            conversation_id="spaces/dm", job_id="job-1",
            message_id="message-1", event_id="event-1",
        )
        with self.assertRaises(ConversationJobLinkConflict):
            self.queue.link_conversation_job(
                conversation_id="spaces/dm", job_id="job-2",
                message_id="message-1", event_id="event-1",
            )


if __name__ == "__main__":
    unittest.main()

class ChatCapacityDeferralTests(unittest.TestCase):
    def test_more_than_retry_budget_busy_polls_preserve_event_until_execution(self):
        with durable_temporary_directory() as tmp:
            queue=DurableChatEventQueue(Path(tmp)/'jobs.db')
            queue.enqueue(event_id='busy',conversation_id='space',message_id='message',payload={'job_id':'job'})
            for _ in range(10):
                claimed=queue.claim_next('worker')
                self.assertIsNotNone(claimed)
                self.assertEqual(claimed['attempt_count'],1)
                queued=queue.defer_until_available('busy','worker',available_at='2000-01-01T00:00:00+00:00',error='maintenance or capacity')
                self.assertEqual(queued['state'],'QUEUED')
                self.assertEqual(queued['attempt_count'],0)
            claimed=queue.claim_next('worker')
            completed=queue.complete('busy','worker')
            self.assertEqual(completed['state'],'COMPLETE')
            self.assertEqual(completed['attempt_count'],1)
    def test_capacity_deferral_cannot_release_another_workers_lease(self):
        with durable_temporary_directory() as tmp:
            queue=DurableChatEventQueue(Path(tmp)/'jobs.db')
            queue.enqueue(event_id='busy',conversation_id='space',message_id='message',payload={})
            queue.claim_next('owner')
            with self.assertRaises(RuntimeError):
                queue.defer_until_available('busy','other',available_at='2000-01-01T00:00:00+00:00',error='busy')
            self.assertEqual(queue.get('busy')['lease_owner'],'owner')
