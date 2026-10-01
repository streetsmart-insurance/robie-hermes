"""Round 13: a repeat note parks, and a stop does not swallow the next answer.

The Test run posted the ledger question and also the model's own paragraph
at the top level, then left the job UNVERIFIED. An in-thread answer on the
next job was refused as stopped because that flag was still set.
"""

from __future__ import annotations

import asyncio
import unittest

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_guard import open_chat_job, publish_discussion_note_outcome
from robie_job_engine.chat_job_controls import (
    LEFT_AS_IS,
    consume_note_repost_allowance,
    note_repost_confirmed_by_reply,
    settle_job_when_reply_sent,
)
from robie_job_engine.chat_queue import DurableChatEventQueue
from robie_job_engine.chat_thread import bind_job_chat_thread
from robie_job_engine.chat_turn_control import (
    agent_stop_requested,
    clear_agent_stop,
    request_agent_stop,
)
from robie_job_engine.models import JobStatus
from robie_job_engine.store import JobStore
from test_round10_reply_lifecycle import SPACE, THREAD, _adapter_module, _chat, _outbound_text
from unittest.mock import patch

QUESTION = "I already added that note at 9:36 PM ET. Want me to add it again?"
MODEL_PROSE = (
    "The note has been added to the existing discussion.\n"
    "It is on follw up 1 for Buster Brown."
)


def _note_job(store: JobStore, status: JobStatus) -> str:
    job = store.create_job(
        "ezlynx.discussion_note",
        {
            "text": "add a note to Buster Brown on follw up 1 saying Follow up",
            "account_name": "Buster Brown",
            "conversation_id": SPACE,
            "requested_by": "Carlo",
        },
    )
    if status != JobStatus.PENDING:
        store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
    if status == JobStatus.UNVERIFIED:
        store.transition(
            job["id"],
            JobStatus.UNVERIFIED,
            expected={JobStatus.RUNNING},
            error="reply sent",
            release_lease=True,
        )
    elif status == JobStatus.NEEDS_CLARIFICATION:
        store.transition(
            job["id"],
            JobStatus.NEEDS_CLARIFICATION,
            expected={JobStatus.RUNNING},
            error="waiting on the user",
            resume_status=JobStatus.PENDING,
            release_lease=True,
        )
    store.checkpoint(
        job["id"],
        "discussion_note",
        {
            "status": "already_posted",
            "discussion_id": "d-1",
            "discussion_title": "follw up 1",
            "reason": QUESTION,
            "note_text": "Follow up",
        },
    )
    bind_job_chat_thread(store, job["id"], THREAD)
    return job["id"]


class RepeatNoteParkTests(unittest.TestCase):
    def test_model_prose_is_dropped_and_the_job_parks(self):
        adapter = _adapter_module()
        with durable_temporary_directory() as tmp:
            db = str(__import__("pathlib").Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _note_job(store, JobStatus.UNVERIFIED)
            posted: list[tuple] = []
            publish_discussion_note_outcome(
                db,
                job_id,
                poster=lambda space, text, thread, posted_job: posted.append(
                    (space, text, thread, posted_job)
                ),
            )
            self.assertEqual(posted, [(SPACE, QUESTION, THREAD, job_id)])
            self.assertEqual(
                store.get_job(job_id)["status"], JobStatus.NEEDS_CLARIFICATION.value
            )
            chat = _chat(db)
            chat._active_chat_job[SPACE] = job_id
            with patch.object(adapter, "ROBIE_JOB_DB", db):
                dropped = asyncio.run(chat.send(SPACE, MODEL_PROSE))
                edited = asyncio.run(
                    chat.edit_message(SPACE, "spaces/ROBY/messages/bubble", MODEL_PROSE)
                )
            self.assertTrue(dropped.success)
            self.assertIsNone(dropped.message_id)
            self.assertTrue(edited.success)
            self.assertEqual(chat._chat_api.messages.calls, [])
            self.assertTrue(settle_job_when_reply_sent(db, job_id, MODEL_PROSE))
            self.assertEqual(
                store.get_job(job_id)["status"], JobStatus.NEEDS_CLARIFICATION.value
            )

    def test_unrelated_message_does_not_revive_an_unverified_dedupe_job(self):
        with durable_temporary_directory() as tmp:
            db = str(__import__("pathlib").Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _note_job(store, JobStatus.UNVERIFIED)
            DurableChatEventQueue(db).link_conversation_job(
                conversation_id=SPACE,
                job_id=job_id,
                message_id="spaces/ROBY/messages/old",
                event_id="spaces/ROBY/messages/old",
                relation="CREATED",
            )
            opened = open_chat_job(
                db,
                "spaces/ROBY/messages/new",
                "what time is it in new york",
                requested_by="Carlo",
                conversation_id=SPACE,
                inbound_thread_id=None,
            )
            self.assertIsNotNone(opened)
            self.assertNotEqual(opened, job_id)
            self.assertEqual(
                store.get_job(job_id)["status"], JobStatus.UNVERIFIED.value
            )
            self.assertFalse(store.get_checkpoint(job_id, "note_repost_confirmed"))

    def test_no_closes_without_a_write_and_a_second_yes_does_not_rearm(self):
        with durable_temporary_directory() as tmp:
            db = str(__import__("pathlib").Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _note_job(store, JobStatus.NEEDS_CLARIFICATION)
            DurableChatEventQueue(db).link_conversation_job(
                conversation_id=SPACE,
                job_id=job_id,
                message_id="spaces/ROBY/messages/ask",
                event_id="spaces/ROBY/messages/ask",
                relation="CREATED",
            )
            self.assertTrue(note_repost_confirmed_by_reply(store, job_id, "yes"))
            self.assertTrue(consume_note_repost_allowance(store, job_id))
            self.assertFalse(note_repost_confirmed_by_reply(store, job_id, "yes"))
            self.assertFalse(consume_note_repost_allowance(store, job_id))
            declined = open_chat_job(
                db,
                "spaces/ROBY/messages/no",
                "no",
                requested_by="Carlo",
                conversation_id=SPACE,
                inbound_thread_id=THREAD,
            )
            self.assertEqual(declined, job_id)
            self.assertEqual(store.get_job(job_id)["status"], JobStatus.CANCELLED.value)
            self.assertEqual(
                store.get_checkpoint(job_id, "note_left_as_is")["reply"], LEFT_AS_IS
            )


class StopLeakTests(unittest.TestCase):
    def test_in_thread_answer_is_delivered_after_a_prior_stop(self):
        adapter = _adapter_module()
        with durable_temporary_directory() as tmp:
            db = str(__import__("pathlib").Path(tmp) / "jobs.db")
            store = JobStore(db)
            stale = store.create_job(
                "hermes.google_chat_task",
                {
                    "text": "old task",
                    "conversation_id": SPACE,
                    "requested_by": "Carlo",
                },
            )
            store.transition(stale["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
            store.transition(
                stale["id"],
                JobStatus.CANCELLED,
                expected={JobStatus.RUNNING},
                error="Cancelled.",
                release_lease=True,
            )
            request_agent_stop(stale["id"])
            waiting = store.create_job(
                "hermes.google_chat_task",
                {
                    "text": "which discussion?",
                    "conversation_id": SPACE,
                    "requested_by": "Carlo",
                },
            )
            store.transition(waiting["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
            store.transition(
                waiting["id"],
                JobStatus.NEEDS_CLARIFICATION,
                expected={JobStatus.RUNNING},
                error="waiting on the user",
                resume_status=JobStatus.PENDING,
                release_lease=True,
            )
            bind_job_chat_thread(store, waiting["id"], THREAD)
            store.checkpoint(
                waiting["id"], "clarification", {"question": "Which discussion?", "asked": True}
            )
            request_agent_stop(waiting["id"])
            self.assertTrue(agent_stop_requested(waiting["id"]))
            resumed = open_chat_job(
                db,
                "spaces/ROBY/messages/answer",
                "the renewal discussion",
                requested_by="Carlo",
                conversation_id=SPACE,
                inbound_thread_id=THREAD,
            )
            self.assertEqual(resumed, waiting["id"])
            self.assertFalse(agent_stop_requested(waiting["id"]))
            self.assertIn(
                store.get_job(waiting["id"])["status"],
                {JobStatus.PENDING.value, JobStatus.RUNNING.value},
            )
            chat = _chat(db)
            chat._active_chat_job[SPACE] = stale["id"]
            chat._gateway_turns[(SPACE, "")] = {"job_id": waiting["id"], "task": None}
            with patch.object(adapter, "ROBIE_JOB_DB", db):
                result = asyncio.run(
                    chat.send(
                        SPACE,
                        "I'll use the renewal discussion.",
                        metadata={"robie_job_id": stale["id"], "thread_id": THREAD},
                    )
                )
            self.assertTrue(result.success)
            self.assertIsNotNone(result.message_id)
            sent = _outbound_text(chat)
            self.assertEqual(len(sent), 1)
            self.assertIn("renewal discussion", sent[0])
            self.assertNotIn("stopped", sent[0].casefold())
            self.assertEqual(
                chat._chat_api.messages.calls[0]["body"]["thread"]["name"], THREAD
            )
            clear_agent_stop(stale["id"])
            clear_agent_stop(waiting["id"])
