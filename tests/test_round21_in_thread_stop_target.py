"""An in-thread /stop targets the job that owns the thread.

A job the engine parked on "Which page should I read?" owns its thread
and never had a gateway turn. The space's newer finished job is the
top-level turn. The stop line, the abort, and the delivery record stay
on the parked job. A top-level message after that stop is its own job.
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

from durable_temp import durable_temporary_directory

from robie_job_engine.browser_read import LOCATOR_QUESTION
from robie_job_engine.chat_guard import open_chat_job
from robie_job_engine.chat_thread import bind_job_chat_thread, job_thread_key
from robie_job_engine.chat_turn_control import ALREADY_FINISHED_REPLY, clear_agent_stop
from robie_job_engine.models import JobStatus
from robie_job_engine.store import JobStore
from test_round10_reply_lifecycle import (
    SPACE,
    THREAD,
    _Event,
    _adapter_module,
    _chat,
    _outbound_text,
)

FINISHED_THREAD = "spaces/ROBY/threads/c2"
QUESTION = "Which carriers do we quote for a new auto policy?"


class _Turn:
    def __init__(self) -> None:
        self.cancelled = False

    def done(self) -> bool:
        return False

    def cancel(self) -> None:
        self.cancelled = True


def _engine_parked(store: JobStore) -> str:
    """The job engine parked this read. It has a thread and no gateway turn."""
    job = store.create_job(
        "browser.read",
        {
            "text": "read the dec page",
            "conversation_id": SPACE,
            "requested_by": "Carlo",
        },
    )
    store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
    store.transition(
        job["id"],
        JobStatus.NEEDS_CLARIFICATION,
        expected={JobStatus.RUNNING},
        error="browser read requires a destination locator",
        resume_status=JobStatus.PENDING,
        release_lease=True,
    )
    bind_job_chat_thread(store, job["id"], THREAD)
    store.checkpoint(
        job["id"],
        "clarification",
        {"question": LOCATOR_QUESTION, "asked": True},
    )
    return job["id"]


def _finished_later(store: JobStore) -> str:
    """A newer job that already finished. Its turn is the space's top-level record."""
    job = store.create_job(
        "ezlynx.discussion_note",
        {
            "text": "note the mailing address request on the checkup discussion",
            "conversation_id": SPACE,
            "requested_by": "Carlo",
            "thread_id": FINISHED_THREAD,
        },
    )
    store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
    store.transition(
        job["id"],
        JobStatus.UNVERIFIED,
        expected={JobStatus.RUNNING},
        error="address change is not something Robie can file",
        release_lease=True,
    )
    bind_job_chat_thread(store, job["id"], FINISHED_THREAD)
    return job["id"]


def _arm_space_turn(chat, finished_id: str) -> _Turn:
    task = _Turn()
    chat._active_chat_job[SPACE] = finished_id
    chat._reply_job_by_chat = {SPACE: finished_id}
    chat._gateway_turns[(SPACE, "")] = {
        "job_id": finished_id,
        "task": task,
        "watchdog": task,
    }
    return task


def _stop_records(store: JobStore, job_id: str) -> dict:
    return {
        kind: store.get_checkpoint(job_id, kind)
        for kind in ("cancelled", "agent_abort", "session_stop_keys", "chat_delivery")
    }


class InThreadStopTargetTests(unittest.TestCase):
    def test_engine_parked_stop_records_only_on_that_job(self):
        adapter = _adapter_module()
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            parked = _engine_parked(store)
            finished = _finished_later(store)
            chat = _chat(db)
            task = _arm_space_turn(chat, finished)
            try:
                with patch.object(adapter, "ROBIE_JOB_DB", db):
                    asyncio.run(chat._apply_chat_stop(_Event(THREAD, "@Robie /stop")))
            finally:
                clear_agent_stop(parked)
                clear_agent_stop(finished)
            self.assertEqual(_outbound_text(chat), ["Stopped. That job is cancelled."])
            posted = chat._chat_api.messages.calls[0]
            self.assertEqual(
                (posted.get("body") or {}).get("thread", {}).get("name"),
                THREAD,
            )
            self.assertNotIn(ALREADY_FINISHED_REPLY, _outbound_text(chat))
            self.assertEqual(store.get_job(parked)["status"], JobStatus.CANCELLED.value)
            parked_records = _stop_records(store, parked)
            self.assertIsNotNone(parked_records["cancelled"])
            self.assertEqual(parked_records["agent_abort"]["reason"], "/stop")
            self.assertEqual(parked_records["session_stop_keys"]["reason"], "/stop")
            self.assertIn(
                "Stopped. That job is cancelled.",
                str(parked_records["chat_delivery"]["text"]),
            )
            self.assertTrue(parked_records["chat_delivery"]["posted"])
            self.assertEqual(store.get_job(finished)["status"], JobStatus.UNVERIFIED.value)
            finished_records = _stop_records(store, finished)
            self.assertEqual(
                finished_records,
                {
                    "cancelled": None,
                    "agent_abort": None,
                    "session_stop_keys": None,
                    "chat_delivery": None,
                },
            )
            self.assertFalse(task.cancelled)
            self.assertEqual(
                chat._gateway_turns[(SPACE, "")]["job_id"],
                finished,
            )

    def test_top_level_message_after_that_stop_is_its_own_job(self):
        adapter = _adapter_module()
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            parked = _engine_parked(store)
            finished = _finished_later(store)
            chat = _chat(db)
            chat.config = type("Cfg", (), {"typing_status_text": "Robie is thinking…"})()
            chat._typing_card_inflight = {}
            chat._orphan_typing_messages = {}
            chat._typing_hold = {}
            task = _arm_space_turn(chat, finished)
            try:
                with patch.object(adapter, "ROBIE_JOB_DB", db):
                    asyncio.run(chat._apply_chat_stop(_Event(THREAD, "@Robie /stop")))
                    self.assertNotIn(SPACE, chat._typing_messages)
                    before = len(chat._chat_api.messages.calls)
                    asyncio.run(chat.send_typing(SPACE))
                    self.assertEqual(len(chat._chat_api.messages.calls), before)
                    opened = open_chat_job(
                        db,
                        "spaces/ROBY/messages/later",
                        QUESTION,
                        requested_by="Carlo",
                        conversation_id=SPACE,
                        inbound_thread_id=None,
                    )
                    chat._allow_next_thinking_card(SPACE)
                    chat._active_chat_job[SPACE] = opened
                    asyncio.run(
                        chat.send_typing(SPACE, metadata={"robie_job_id": opened})
                    )
            finally:
                clear_agent_stop(parked)
                clear_agent_stop(finished)
                clear_agent_stop(locals().get("opened"))
            self.assertNotEqual(opened, parked)
            self.assertNotEqual(opened, finished)
            self.assertNotEqual(
                store.get_job(opened)["status"],
                JobStatus.CANCELLED.value,
            )
            card = chat._chat_api.messages.calls[-1]
            self.assertEqual(
                ((card.get("body") or {}).get("thread") or {}).get("threadKey"),
                job_thread_key(opened),
            )
            self.assertTrue(
                str((card.get("body") or {}).get("text") or "")
                .casefold()
                .startswith("robie is")
            )
            self.assertEqual(
                _outbound_text(chat)[0],
                "Stopped. That job is cancelled.",
            )
            self.assertNotIn(ALREADY_FINISHED_REPLY, _outbound_text(chat))
            self.assertEqual(store.get_job(parked)["status"], JobStatus.CANCELLED.value)
            self.assertIn(
                "Stopped. That job is cancelled.",
                str((store.get_checkpoint(parked, "chat_delivery") or {}).get("text")),
            )
            self.assertEqual(
                (store.get_checkpoint(parked, "agent_abort") or {}).get("reason"),
                "/stop",
            )
            self.assertEqual(store.get_job(finished)["status"], JobStatus.UNVERIFIED.value)
            self.assertIsNone(store.get_checkpoint(finished, "agent_abort"))
            self.assertIsNone(store.get_checkpoint(finished, "cancelled"))
            self.assertIsNone(store.get_checkpoint(finished, "chat_delivery"))
            self.assertIsNone(store.get_checkpoint(opened, "agent_abort"))
            self.assertIsNone(store.get_checkpoint(opened, "cancelled"))
            self.assertFalse(task.cancelled)
