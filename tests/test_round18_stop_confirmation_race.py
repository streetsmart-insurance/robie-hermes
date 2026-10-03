"""An in-thread /stop posts its line before the next message can run.

The parked job is cancelled, the stop line is in that job's thread, and a
top-level message that arrives while stop is still cleaning up opens its
own job and gets its own reply. A stopped job left on the space does not
refuse a different job's send.
"""

from __future__ import annotations

import asyncio
import logging
import unittest
from pathlib import Path
from unittest.mock import patch

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_guard import open_chat_job
from robie_job_engine.chat_thread import bind_job_chat_thread
from robie_job_engine.chat_turn_control import clear_agent_stop, request_agent_stop
from robie_job_engine.models import JobStatus
from robie_job_engine.store import JobStore
from test_round10_reply_lifecycle import (
    FRESH,
    SPACE,
    THREAD,
    _Event,
    _adapter_module,
    _chat,
    _outbound_text,
)

ESSAY = "Write a short essay about what a certificate of insurance is."
PARKED_QUESTION = "Which page should I read?"


def _cancelled(store: JobStore, text: str) -> str:
    job = store.create_job(
        "hermes.google_chat_task",
        {"text": text, "conversation_id": SPACE, "requested_by": "Carlo"},
    )
    store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
    store.transition(
        job["id"],
        JobStatus.CANCELLED,
        expected={JobStatus.RUNNING},
        error="Cancelled.",
        release_lease=True,
    )
    request_agent_stop(job["id"])
    return job["id"]


class StopConfirmationRaceTests(unittest.TestCase):
    def test_stop_line_is_posted_before_the_next_message_runs(self):
        adapter = _adapter_module()
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            stale = _cancelled(store, "older finished task")
            parked = store.create_job(
                "hermes.google_chat_task",
                {
                    "text": "read the dec page",
                    "conversation_id": SPACE,
                    "requested_by": "Carlo",
                },
            )
            store.transition(parked["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
            store.transition(
                parked["id"],
                JobStatus.NEEDS_CLARIFICATION,
                expected={JobStatus.RUNNING},
                error="waiting on the user",
                release_lease=True,
            )
            bind_job_chat_thread(store, parked["id"], THREAD)
            store.checkpoint(
                parked["id"],
                "clarification",
                {"question": PARKED_QUESTION, "asked": True},
            )
            chat = _chat(db)
            chat._active_chat_job[SPACE] = stale
            chat._reply_job_by_chat = {SPACE: stale}
            seen: dict[str, str] = {}

            async def _terminate(event, job_id, reason="/stop"):
                del event, reason
                # Stop has already yielded. The confirmation has to be in
                # the parked thread before this following message runs.
                calls = chat._chat_api.messages.calls
                self.assertGreaterEqual(len(calls), 1)
                first = calls[0]
                self.assertIn("Stopped.", str((first.get("body") or {}).get("text") or ""))
                self.assertEqual(
                    (first.get("body") or {}).get("thread", {}).get("name"),
                    THREAD,
                )
                seen["stop_job"] = str(job_id)
                opened = open_chat_job(
                    db,
                    "spaces/ROBY/messages/essay",
                    ESSAY,
                    requested_by="Carlo",
                    conversation_id=SPACE,
                    inbound_thread_id=None,
                )
                seen["essay_job"] = opened
                result = await chat.send(
                    SPACE,
                    "A certificate of insurance shows the coverage.",
                    metadata={
                        "robie_job_id": opened,
                        "thread_id": FRESH,
                        "robie_delivery_kind": "notice",
                    },
                )
                self.assertTrue(result.success)
                self.assertIsNotNone(result.message_id)

            chat._terminate_running_agent = _terminate
            records: list[str] = []
            handler = logging.Handler()
            handler.setLevel(logging.INFO)
            handler.emit = lambda record: records.append(record.getMessage())
            logger = logging.getLogger("gateway.platforms.google_chat")
            previous_level = logger.level
            logger.setLevel(logging.INFO)
            logger.addHandler(handler)
            try:
                with patch.object(adapter, "ROBIE_JOB_DB", db):
                    asyncio.run(chat._apply_chat_stop(_Event(THREAD, "@Robie /stop")))
            finally:
                logger.removeHandler(handler)
                logger.setLevel(previous_level)
                clear_agent_stop(stale)
                clear_agent_stop(parked["id"])
            blob = "\n".join(records)
            self.assertNotIn(
                f"refusing send after stop job={stale}",
                blob,
            )
            self.assertEqual(seen["stop_job"], parked["id"])
            self.assertNotEqual(seen["essay_job"], parked["id"])
            self.assertEqual(store.get_job(parked["id"])["status"], JobStatus.CANCELLED.value)
            self.assertNotEqual(
                store.get_job(seen["essay_job"])["status"],
                JobStatus.CANCELLED.value,
            )
            delivery = store.get_checkpoint(parked["id"], "chat_delivery") or {}
            self.assertIn("Stopped.", str(delivery.get("text") or ""))
            self.assertTrue(delivery.get("posted"))
            texts = _outbound_text(chat)
            self.assertIn("Stopped. That job is cancelled.", texts[0])
            self.assertIn("certificate of insurance", texts[-1])
            self.assertNotIn(stale, texts[0])

    def test_refusing_send_after_stop_does_not_apply_to_a_different_job(self):
        adapter = _adapter_module()
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            stale = _cancelled(store, "already finished")
            live = store.create_job(
                "hermes.google_chat_task",
                {
                    "text": ESSAY,
                    "conversation_id": SPACE,
                    "requested_by": "Carlo",
                },
            )
            store.transition(live["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
            bind_job_chat_thread(store, live["id"], FRESH)
            chat = _chat(db)
            chat._active_chat_job[SPACE] = stale
            chat._reply_job_by_chat = {SPACE: stale}
            chat._gateway_turns[(SPACE, "")] = {"job_id": live["id"], "task": None}
            records: list[str] = []
            handler = logging.Handler()
            handler.setLevel(logging.INFO)
            handler.emit = lambda record: records.append(record.getMessage())
            logger = logging.getLogger("gateway.platforms.google_chat")
            previous_level = logger.level
            logger.setLevel(logging.INFO)
            logger.addHandler(handler)
            try:
                with patch.object(adapter, "ROBIE_JOB_DB", db):
                    result = asyncio.run(
                        chat.send(
                            SPACE,
                            "A certificate of insurance shows the coverage.",
                            metadata={
                            "robie_job_id": live["id"],
                            "thread_id": FRESH,
                            "robie_delivery_kind": "notice",
                        },
                        )
                    )
            finally:
                logger.removeHandler(handler)
                logger.setLevel(previous_level)
                clear_agent_stop(stale)
            blob = "\n".join(records)
            self.assertTrue(result.success)
            self.assertIsNotNone(result.message_id)
            self.assertNotIn("refusing send after stop", blob)
            self.assertIn("certificate of insurance", _outbound_text(chat)[0])
            self.assertEqual(
                chat._chat_api.messages.calls[0]["body"]["thread"]["name"],
                FRESH,
            )
