"""HITL Chat replies resume the waiting job and apply Coverage A–F.

Same rule as email. A reply in the Robie Chat HITL thread (RETRY or the
amounts) must resume the AWAITING_HUMAN_INPUT hermes.google_chat_task.
No second Chat job. Do not invent omitted letters. Fill goes through
ezlynx_policy_setup, not playwright_exec. Mocks only. No live EZLynx.
No Production. Applicant 220250093 / policy 83669533 only.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_guard import (
    build_chat_execution_text,
    chat_hermes_should_run,
    open_chat_job,
)
from robie_job_engine.chat_hitl import (
    find_parked_chat_hitl_job,
    ingest_chat_hitl_reply,
    run_chat_hitl_coverage_resume,
)
from robie_job_engine.chat_queue import DurableChatEventQueue
from robie_job_engine.email_hitl import (
    apply_hitl_coverage_fill,
    missing_coverage_letters,
    policy_setup_args_from_hitl_payload,
)
from robie_job_engine.hitl import classify_human_reply
from robie_job_engine.hitl_copy import missing_coverage_letters_human_text
from robie_job_engine.models import JobStatus
from robie_job_engine.policy_setup_dispatch import (
    POLICY_SETUP_REQUIRED_KIND,
    parse_coverage_amounts_from_reply,
)
from robie_job_engine.store import JobStore

CARLO_REPLY = (
    "Coverage A $1,200,000; B $120,000; C $500,000; D $500,000; F $10,000"
)
CONVERSATION = "spaces/ROBIE-HITL"


def _job_count(store: JobStore) -> int:
    with store.connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])


def _park_chat_coverage_hitl(
    store: JobStore,
    queue: DurableChatEventQueue,
    *,
    conversation_id: str = CONVERSATION,
) -> dict:
    job = store.create_job(
        "hermes.google_chat_task",
        {
            "worker": "hermes-cua",
            "text": "create homeowners TEST-HO-20260911-E01 on 220250093",
            "request_text": "create homeowners TEST-HO-20260911-E01",
            "conversation_id": conversation_id,
            "policy_number": "TEST-HO-20260911-E01",
            "policy_id": "83669533",
            "applicant_id": "220250093",
            "effective_date": "10/02/2026",
            "expiration_date": "10/02/2027",
        },
        idempotency_key="gchat:orig-chat-hitl",
    )
    store.checkpoint(
        job["id"],
        "action",
        {
            "action": "ezlynx_policy_setup",
            "destination": {
                "policy_number": "TEST-HO-20260911-E01",
                "policy_id": "83669533",
                "applicant_id": "220250093",
            },
        },
    )
    store.checkpoint(
        job["id"],
        POLICY_SETUP_REQUIRED_KIND,
        {
            "policy_number": "TEST-HO-20260911-E01",
            "tool_called": True,
            "setup_complete": False,
        },
    )
    store.transition(
        job["id"],
        JobStatus.RUNNING,
        expected={JobStatus.PENDING},
    )
    queue.link_conversation_job(
        conversation_id=conversation_id,
        job_id=job["id"],
        message_id="msg-orig-chat-hitl",
        event_id="msg-orig-chat-hitl",
        interaction_state={
            "awaiting": "human_input",
            "field_name": "operator_response",
            "accepts_value": True,
        },
    )
    queue.park_direct_human_input(
        conversation_id=conversation_id,
        job_id=job["id"],
        interaction_state={
            "awaiting": "human_input",
            "field_name": "operator_response",
            "checkpoint": "coverage_fill",
            "accepts_value": True,
        },
        error="coverage amounts not on the job; will not invent them",
    )
    return store.get_job(job["id"])


class ClassifyChatCoverageReplyTests(unittest.TestCase):
    def test_amount_blob_is_answer_not_new_intent(self) -> None:
        interaction = {"field_name": "operator_response", "accepts_value": True}
        self.assertEqual(classify_human_reply(CARLO_REPLY, interaction), "ANSWER")
        self.assertEqual(classify_human_reply("RETRY", interaction), "ANSWER")
        self.assertEqual(
            classify_human_reply("Show me pending jobs", interaction),
            "NEW_INTENT",
        )


class FindParkedChatHitlTests(unittest.TestCase):
    def test_same_thread_resolves_parked_chat_job(self) -> None:
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            queue = DurableChatEventQueue(db)
            parked = _park_chat_coverage_hitl(store, queue)
            found = find_parked_chat_hitl_job(store, CONVERSATION, queue=queue)
            self.assertIsNotNone(found)
            self.assertEqual(found["id"], parked["id"])
            self.assertIsNone(
                find_parked_chat_hitl_job(store, "spaces/other", queue=queue)
            )


class ResumeSameChatJobTests(unittest.TestCase):
    def test_amount_reply_resumes_same_job_and_does_not_create_another(self) -> None:
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            queue = DurableChatEventQueue(db)
            parked = _park_chat_coverage_hitl(store, queue)
            before = _job_count(store)
            resumed_id = open_chat_job(
                db,
                "msg-chat-reply-amounts",
                CARLO_REPLY,
                requested_by="Carlo",
                conversation_id=CONVERSATION,
            )
            self.assertEqual(resumed_id, parked["id"])
            self.assertEqual(_job_count(store), before)
            after = store.get_job(parked["id"])
            self.assertTrue(after["payload"].get("hitl_resume"))
            coverage = after["payload"]["human_input_values"]["coverage"]
            self.assertEqual(coverage["dwelling"], "1200000")
            self.assertEqual(coverage["medical_payments"], "10000")
            self.assertNotIn("personal_liability", coverage)
            self.assertEqual(missing_coverage_letters(coverage), ["E"])
            self.assertNotEqual(after["status"], JobStatus.AWAITING_HUMAN_INPUT.value)

    def test_retry_resumes_same_chat_job(self) -> None:
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            queue = DurableChatEventQueue(db)
            parked = _park_chat_coverage_hitl(store, queue)
            before = _job_count(store)
            resumed_id = open_chat_job(
                db,
                "msg-chat-reply-retry",
                "RETRY",
                requested_by="Carlo",
                conversation_id=CONVERSATION,
            )
            self.assertEqual(resumed_id, parked["id"])
            self.assertEqual(_job_count(store), before)
            after = store.get_job(parked["id"])
            self.assertTrue(after["payload"].get("hitl_resume"))
            self.assertEqual(
                after["payload"]["human_input_values"]["operator_response"],
                "RETRY",
            )

    def test_new_chat_thread_still_creates_a_job(self) -> None:
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            queue = DurableChatEventQueue(db)
            parked = _park_chat_coverage_hitl(store, queue)
            before = _job_count(store)
            new_id = open_chat_job(
                db,
                "msg-unrelated-chat",
                "Please file this endorsement on another account",
                requested_by="Carlo",
                conversation_id="spaces/other-thread",
            )
            self.assertIsNotNone(new_id)
            self.assertNotEqual(new_id, parked["id"])
            self.assertEqual(_job_count(store), before + 1)


class ApplyChatHitlAmountsTests(unittest.TestCase):
    def test_resume_invokes_setup_with_reply_amounts_not_playwright(self) -> None:
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            queue = DurableChatEventQueue(db)
            parked = _park_chat_coverage_hitl(store, queue)
            ingest_chat_hitl_reply(
                store,
                job_id=parked["id"],
                message_id="msg-chat-reply-amounts",
                text=CARLO_REPLY,
            )
            captured: list[dict] = []

            def fake_invoke(args):
                captured.append(dict(args))
                return {
                    "success": True,
                    "policy_id": "83669533",
                    "phase_reached": "coverage_fill",
                    "formentry_found": True,
                }

            with (
                patch(
                    "robie_job_engine.policy_setup_dispatch.invoke_policy_setup_tool",
                    side_effect=fake_invoke,
                ),
                patch(
                    "robie_job_engine.chat_app_post.post_hitl_to_originating_thread",
                    return_value=None,
                ),
            ):
                text = run_chat_hitl_coverage_resume(db, parked["id"])
            self.assertIsNotNone(text)
            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0]["policy_number"], "TEST-HO-20260911-E01")
            self.assertEqual(captured[0]["dwelling"], "1200000")
            self.assertEqual(captured[0]["medical_payments"], "10000")
            self.assertNotIn("personal_liability", captured[0])
            folded = text.casefold()
            self.assertIn("i still need the coverage e", folded)
            self.assertIn("will not invent it", folded)
            self.assertIn("reply in this chat thread", folded)
            self.assertNotIn("reply to this email", folded)
            self.assertNotIn("playwright_blocked", folded)
            self.assertNotIn("policy_setup_order", folded)
            self.assertFalse(chat_hermes_should_run(db, parked["id"]))
            args = policy_setup_args_from_hitl_payload(
                store.get_job(parked["id"])["payload"]
            )
            self.assertEqual(args["dwelling"], "1200000")
            self.assertNotIn("personal_liability", args)

    def test_build_execution_text_applies_fill_when_tool_already_ran(self) -> None:
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            queue = DurableChatEventQueue(db)
            parked = _park_chat_coverage_hitl(store, queue)
            ingest_chat_hitl_reply(
                store,
                job_id=parked["id"],
                message_id="msg-chat-build",
                text=CARLO_REPLY,
            )
            captured: list[dict] = []

            def fake_invoke(args):
                captured.append(dict(args))
                return {
                    "success": True,
                    "policy_id": "83669533",
                    "formentry_found": True,
                }

            with (
                patch(
                    "robie_job_engine.hermes_tool_visibility.register_policy_setup_callable",
                    return_value=lambda args: {"success": True},
                ),
                patch(
                    "robie_job_engine.policy_setup_dispatch.invoke_policy_setup_tool",
                    side_effect=fake_invoke,
                ),
                patch(
                    "robie_job_engine.chat_app_post.post_hitl_to_originating_thread",
                    return_value=None,
                ),
            ):
                execution = build_chat_execution_text(db, parked["id"], CARLO_REPLY)
            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0]["dwelling"], "1200000")
            self.assertNotIn("personal_liability", captured[0])
            self.assertIn("ezlynx_policy_setup", execution.casefold())
            self.assertIn("do not call playwright_exec", execution.casefold())
            self.assertIn("i still need the coverage e", execution.casefold())
            self.assertIn("reply in this chat thread", execution.casefold())


class MissingLetterChatCopyTests(unittest.TestCase):
    def test_chat_asks_only_for_e(self) -> None:
        text = missing_coverage_letters_human_text(
            channel="chat", missing=["E"], have=["A", "B", "C", "D", "F"]
        )
        self.assertIn("I have Coverage A, B, C, D, and F.", text)
        self.assertIn("I still need the Coverage E dollar amount.", text)
        self.assertIn("I will not invent it.", text)
        self.assertIn("Reply in this Chat thread with that number.", text)
        self.assertNotIn("Reply to this email", text)

    def test_apply_uses_chat_copy_for_chat_jobs(self) -> None:
        amounts = parse_coverage_amounts_from_reply(CARLO_REPLY)
        self.assertEqual(missing_coverage_letters(amounts), ["E"])
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            queue = DurableChatEventQueue(db)
            parked = _park_chat_coverage_hitl(store, queue)
            ingest_chat_hitl_reply(
                store,
                job_id=parked["id"],
                message_id="msg-copy",
                text=CARLO_REPLY,
            )
            with patch(
                "robie_job_engine.policy_setup_dispatch.invoke_policy_setup_tool",
                return_value={"success": True, "policy_id": "83669533"},
            ):
                text = apply_hitl_coverage_fill(store, parked["id"])
            self.assertIn("Reply in this Chat thread", text)
            self.assertNotIn("Reply to this email", text)


if __name__ == "__main__":
    unittest.main()
