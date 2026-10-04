"""Round 32: one reply, the real action, and the job's own thread.

A duplicate-note question is the only line for that turn. A note that
mentions a quote is still a note. A clarify from the current generation
is posted and keeps the job open. A sign-in block stays in the thread.
"""

from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import patch

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_guard import publish_discussion_note_outcome
from robie_job_engine.chat_thread import bind_job_chat_thread
from robie_job_engine.chat_turn_control import (
    agent_stop_requested,
    clear_agent_stop,
    register_chat_adapter,
    session_is_busy,
)
from robie_job_engine.models import JobStatus
from robie_job_engine.request_routing import classify_request
from robie_job_engine.store import JobStore
from robie_job_engine.turn_finalization import (
    COULD_NOT_FINISH,
    begin_model_generation,
    close_turn_after_visible_line,
    finish_model_generation,
    visible_fallback_line,
)
from robie_job_engine.user_reply import SIGN_IN_QUESTION, format_user_reply
from robie_job_engine.write_verification_loop import prepare_write_plan
from test_round10_reply_lifecycle import SPACE, THREAD, _Event, _adapter_module, _chat

NOTE = (
    "add a note for buster brown: client called back about the round 25 "
    "renewal, wants a quote by mon"
)
CLAIM = "The discussion note has been filed. Note ID 1133664484"
QUESTION = "I already added that note at 6:25 AM ET. Want me to add it again?"
SIGN_IN = (
    "ROBIE_BLOCKED: PLAYWRIGHT_BLOCKED: https://app.ezlynx.com/auth/account/login"
)


def _running(store: JobStore, text: str, *, applicant_id: str = "") -> str:
    payload = {
        "text": text,
        "conversation_id": SPACE,
        "requested_by": "Carlo",
        "client_name": "Buster Brown",
    }
    if applicant_id:
        payload["applicant_id"] = applicant_id
    job = store.create_job("ezlynx.discussion_note", payload)
    store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
    bind_job_chat_thread(store, job["id"], THREAD)
    return job["id"]


class EngineQuestionTests(unittest.TestCase):
    def test_engine_question_suppresses_the_model_claim(self):
        adapter = _adapter_module()
        with durable_temporary_directory() as tmp:
            db = str(__import__("pathlib").Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _running(store, NOTE, applicant_id="26356199")
            store.checkpoint(
                job_id,
                "discussion_note",
                {
                    "status": "already_posted",
                    "reason": QUESTION,
                    "discussion_title": "Renewal",
                    "note_id": "1133664484",
                },
            )
            posted: list[str] = []
            publish_discussion_note_outcome(
                db,
                job_id,
                poster=lambda _space, text, _thread, _job: posted.append(text),
            )
            self.assertEqual(posted, [QUESTION])
            chat = _chat(db)
            chat._active_chat_job[SPACE] = job_id
            os.environ["ROBIE_CURRENT_JOB_ID"] = job_id
            try:
                with patch.object(adapter, "ROBIE_JOB_DB", db):
                    dropped = asyncio.run(
                        chat.send(SPACE, CLAIM, metadata={"robie_job_id": job_id})
                    )
                    edited = asyncio.run(
                        chat.edit_message(SPACE, "spaces/ROBY/messages/bubble", CLAIM)
                    )
                self.assertTrue(dropped.success)
                self.assertIsNone(dropped.message_id)
                self.assertTrue(edited.success)
                self.assertEqual(chat._chat_api.messages.calls, [])
                self.assertNotIn("1133664484", " ".join(posted))
                self.assertEqual(
                    store.get_job(job_id)["status"],
                    JobStatus.NEEDS_CLARIFICATION.value,
                )
            finally:
                finish_model_generation(job_id)
                clear_agent_stop(job_id)
                os.environ.pop("ROBIE_CURRENT_JOB_ID", None)


class ActionNotKeywordsTests(unittest.TestCase):
    def test_quote_inside_the_note_stays_a_note_and_uses_the_bound_id(self):
        with patch.dict(os.environ, {"ROBIE_PLAYGROUND": "1", "ROBIE_ENV": "TEST"}):
            route = classify_request(NOTE)
        self.assertEqual(route.action_type, "ezlynx.discussion_note")
        with patch.dict(os.environ, {"ROBIE_PLAYGROUND": "1", "ROBIE_ENV": "TEST"}):
            self.assertEqual(
                classify_request("Please quote ROBIE Test LLC").action_type,
                "ezlynx.quote",
            )
        with durable_temporary_directory() as tmp:
            db = str(__import__("pathlib").Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _running(store, NOTE, applicant_id="26356199")
            job = store.get_job(job_id)
            plan = prepare_write_plan(
                store,
                job,
                lambda _prompt: (
                    '{"write":"discussion note","target":{"applicant_id":"buster brown",'
                    '"discussion":"Renewal"},"values":{"note_text":"wants a quote by mon"}}'
                ),
            )
            target = dict(plan.get("target") or {})
            self.assertEqual(target.get("applicant_id"), "26356199")
            self.assertNotIn("buster", str(target.get("applicant_id") or "").casefold())
            self.assertEqual(target.get("discussion"), "Renewal")


class CurrentGenerationTests(unittest.TestCase):
    def test_a_clarify_from_this_generation_is_posted_and_a_close_records_one_line(self):
        adapter = _adapter_module()
        with durable_temporary_directory() as tmp:
            db = str(__import__("pathlib").Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _running(store, NOTE, applicant_id="26356199")
            begin_model_generation(job_id)
            try:
                self.assertIsNone(visible_fallback_line(db, job_id))
                close_turn_after_visible_line(db, job_id, COULD_NOT_FINISH)
                self.assertEqual(store.get_job(job_id)["status"], JobStatus.RUNNING.value)
                self.assertFalse(agent_stop_requested(job_id))
                chat = _chat(db)
                chat._active_chat_job[SPACE] = job_id
                os.environ["ROBIE_CURRENT_JOB_ID"] = job_id
                question = "Which discussion should I use?"
                with patch.object(adapter, "ROBIE_JOB_DB", db):
                    sent = asyncio.run(
                        chat.send(
                            SPACE,
                            question,
                            metadata={"robie_job_id": job_id, "thread_id": "spaces/ROBY/threads/top"},
                        )
                    )
                self.assertTrue(sent.success)
                self.assertEqual(len(chat._chat_api.messages.calls), 1)
                body = chat._chat_api.messages.calls[0]["body"]
                self.assertEqual(body["thread"]["name"], THREAD)
                self.assertIn(question, body["text"])
                self.assertEqual(
                    store.get_job(job_id)["status"],
                    JobStatus.NEEDS_CLARIFICATION.value,
                )
                self.assertIsNone(visible_fallback_line(db, job_id))
            finally:
                finish_model_generation(job_id)
                clear_agent_stop(job_id)
                os.environ.pop("ROBIE_CURRENT_JOB_ID", None)

    def test_a_silent_close_posts_exactly_one_line_in_the_thread(self):
        adapter = _adapter_module()
        with durable_temporary_directory() as tmp:
            db = str(__import__("pathlib").Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _running(store, "file the note")
            chat = _chat(db)
            event = _Event(THREAD, "file the note")

            async def _watched():
                return "finished"

            with patch.object(adapter, "ROBIE_JOB_DB", db):
                asyncio.run(
                    chat._finish_open_turn_after_ceiling(
                        _watched(),
                        job_id,
                        event,
                        event.source,
                        lambda: False,
                    )
                )
            calls = chat._chat_api.messages.calls
            self.assertEqual(len(calls), 1)
            body = calls[0]["body"]
            self.assertEqual(body["thread"]["name"], THREAD)
            self.assertIn(COULD_NOT_FINISH, body["text"])
            recorded = store.get_checkpoint(job_id, "chat_outcome_sent") or {}
            self.assertTrue(recorded.get("posted"))
            self.assertIn(COULD_NOT_FINISH, str(recorded.get("text") or ""))
            delivery = store.get_checkpoint(job_id, "chat_delivery") or {}
            self.assertTrue(delivery.get("posted"))
            self.assertEqual(store.get_job(job_id)["status"], JobStatus.UNVERIFIED.value)
            clear_agent_stop(job_id)


class SignInThreadTests(unittest.TestCase):
    def test_sign_in_block_is_filtered_into_the_job_thread(self):
        self.assertEqual(format_user_reply(SIGN_IN), SIGN_IN_QUESTION)
        self.assertNotIn("ROBIE_BLOCKED", format_user_reply(SIGN_IN))
        self.assertNotIn("PLAYWRIGHT_BLOCKED", format_user_reply(SIGN_IN))
        adapter = _adapter_module()
        with durable_temporary_directory() as tmp:
            db = str(__import__("pathlib").Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _running(store, "whats the GL policy number for buster brown")
            chat = _chat(db)
            chat._active_chat_job[SPACE] = job_id
            os.environ["ROBIE_CURRENT_JOB_ID"] = job_id
            try:
                with patch.object(adapter, "ROBIE_JOB_DB", db):
                    sent = asyncio.run(
                        chat.edit_message(
                            SPACE,
                            "spaces/ROBY/messages/bubble",
                            SIGN_IN,
                        )
                    )
                self.assertTrue(sent.success)
                self.assertEqual(len(chat._chat_api.messages.calls), 1)
                body = chat._chat_api.messages.calls[0]["body"]
                self.assertEqual(body["text"], SIGN_IN_QUESTION)
                self.assertEqual(body["thread"]["name"], THREAD)
                self.assertNotIn("ROBIE_BLOCKED", body["text"])
                self.assertNotIn("PLAYWRIGHT_BLOCKED", body["text"])
                self.assertNotIn("/auth/account/login", body["text"])
                self.assertEqual(
                    store.get_job(job_id)["status"],
                    JobStatus.NEEDS_CLARIFICATION.value,
                )
            finally:
                finish_model_generation(job_id)
                clear_agent_stop(job_id)
                os.environ.pop("ROBIE_CURRENT_JOB_ID", None)


class TurnLockTests(unittest.TestCase):
    def test_a_terminal_job_releases_the_turn_lock(self):
        with durable_temporary_directory() as tmp:
            db = str(__import__("pathlib").Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _running(store, "add a note for buster brown")
            chat = _chat(db)

            class _Lease:
                def __init__(self) -> None:
                    self.released = False

                def release(self) -> None:
                    self.released = True

            lease = _Lease()
            key = f"chat:{SPACE}"
            chat.gateway_runner._active_session_leases = {key: lease}
            chat.gateway_runner._sessions = {key: object()}
            chat._gateway_turns[(SPACE, "")] = {
                "job_id": job_id,
                "task": None,
                "watchdog": None,
            }
            chat._active_chat_job[SPACE] = job_id
            register_chat_adapter(chat)
            event = _Event("", "stop")
            self.assertTrue(session_is_busy(chat, event))
            store.transition(
                job_id,
                JobStatus.CANCELLED,
                expected={JobStatus.RUNNING},
                error="Cancelled.",
                release_lease=True,
            )
            self.assertTrue(lease.released)
            self.assertNotIn(key, chat.gateway_runner._active_session_leases)
            self.assertFalse(session_is_busy(chat, event))
            clear_agent_stop(job_id)


if __name__ == "__main__":
    unittest.main()
