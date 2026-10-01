"""A /stop in a job's thread answers even when that job is already finished.

Google Chat only delivers a group-space message that @mentions the app.
The adapter does not drop a delivered /stop for lack of a mention. A drop
that does happen is one INFO line with no message body.
"""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_thread import bind_job_chat_thread
from robie_job_engine.chat_turn_control import ALREADY_FINISHED_REPLY, NOTHING_RUNNING_REPLY
from robie_job_engine.models import JobStatus
from robie_job_engine.playground_execute import capability_menu
from robie_job_engine.playground_service import handle_playground_chat
from robie_job_engine.store import JobStore
from test_round10_reply_lifecycle import (
    SPACE,
    THREAD,
    _Event,
    _adapter_module,
    _chat,
    _outbound_text,
)

SECRET = "the private note body must not be logged"


def _terminal(store: JobStore, status: JobStatus) -> str:
    job = store.create_job(
        "hermes.google_chat_task",
        {
            "text": f"finished note {status.value}",
            "conversation_id": SPACE,
            "requested_by": "Carlo",
        },
    )
    if status == JobStatus.CANCELLED:
        store.transition(
            job["id"],
            JobStatus.CANCELLED,
            expected={JobStatus.PENDING},
            error="Cancelled.",
            release_lease=True,
        )
    else:
        store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
        store.transition(
            job["id"],
            status,
            expected={JobStatus.RUNNING},
            error="finished",
            release_lease=True,
        )
    bind_job_chat_thread(store, job["id"], THREAD)
    return job["id"]


class ThreadStopTests(unittest.TestCase):
    def test_stop_in_a_finished_jobs_thread_says_it_already_finished(self):
        adapter = _adapter_module()
        for status in (
            JobStatus.UNVERIFIED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        ):
            with self.subTest(status=status.value):
                with durable_temporary_directory() as tmp:
                    db = str(Path(tmp) / "jobs.db")
                    store = JobStore(db)
                    job_id = _terminal(store, status)
                    chat = _chat(db)

                    async def _noop(*args, **kwargs):
                        del args, kwargs

                    chat._terminate_running_agent = _noop
                    with patch.object(adapter, "ROBIE_JOB_DB", db):
                        asyncio.run(chat._apply_chat_stop(_Event(THREAD, "/stop")))
                    self.assertEqual(_outbound_text(chat), [ALREADY_FINISHED_REPLY])
                    self.assertEqual(store.get_job(job_id)["status"], status.value)
                    self.assertIsNone(store.get_checkpoint(job_id, "cancelled"))
                    self.assertIsNone(store.get_checkpoint(job_id, "agent_abort"))

    def test_stop_in_a_running_jobs_thread_still_cancels_it(self):
        adapter = _adapter_module()
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "hermes.google_chat_task",
                {
                    "text": "write the essay",
                    "conversation_id": SPACE,
                    "requested_by": "Carlo",
                },
            )
            store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
            bind_job_chat_thread(store, job["id"], THREAD)
            chat = _chat(db)

            async def _noop(*args, **kwargs):
                del args, kwargs

            chat._terminate_running_agent = _noop
            with patch.object(adapter, "ROBIE_JOB_DB", db):
                asyncio.run(chat._apply_chat_stop(_Event(THREAD, "@Robie /stop")))
            self.assertEqual(store.get_job(job["id"])["status"], JobStatus.CANCELLED.value)
            text = _outbound_text(chat)[0]
            self.assertIn("Stopped.", text)
            self.assertNotEqual(text, ALREADY_FINISHED_REPLY)
            self.assertNotEqual(text, NOTHING_RUNNING_REPLY)

    def test_playground_stop_on_a_finished_chat_job_uses_the_same_line(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _terminal(store, JobStatus.UNVERIFIED)
            before = len(store.list_jobs_by_status(set(JobStatus)))
            env = {
                "ROBIE_PLAYGROUND": "1",
                "ROBIE_PLAYGROUND_SPACE_ID": SPACE,
                "ROBIE_PLAYGROUND_REAL_CLIENTS": "0",
                "ROBIE_PLAYGROUND_LIVE_WRITES": "0",
            }
            with patch.dict(os.environ, env, clear=False):
                replies = handle_playground_chat(
                    db,
                    "/stop",
                    conversation_id=SPACE,
                    thread_id=THREAD,
                    message_id="m-stop-finished",
                    requested_by="Carlo",
                )
            self.assertEqual(replies, [ALREADY_FINISHED_REPLY])
            self.assertEqual(store.get_job(job_id)["status"], JobStatus.UNVERIFIED.value)
            self.assertEqual(len(store.list_jobs_by_status(set(JobStatus))), before)
            self.assertIsNone(store.get_checkpoint(job_id, "cancelled"))

    def test_playground_stop_cancels_a_running_chat_job_in_the_thread(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "hermes.google_chat_task",
                {
                    "text": "still running",
                    "conversation_id": SPACE,
                    "requested_by": "Carlo",
                },
            )
            store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
            bind_job_chat_thread(store, job["id"], THREAD)
            env = {
                "ROBIE_PLAYGROUND": "1",
                "ROBIE_PLAYGROUND_SPACE_ID": SPACE,
                "ROBIE_PLAYGROUND_REAL_CLIENTS": "0",
                "ROBIE_PLAYGROUND_LIVE_WRITES": "0",
            }
            with patch.dict(os.environ, env, clear=False):
                replies = handle_playground_chat(
                    db,
                    "@Robie /stop",
                    conversation_id=SPACE,
                    thread_id=THREAD,
                    message_id="m-stop-running",
                    requested_by="Carlo",
                )
            self.assertEqual(store.get_job(job["id"])["status"], JobStatus.CANCELLED.value)
            self.assertIn("Stopped.", replies[0])
            self.assertNotEqual(replies[0], ALREADY_FINISHED_REPLY)


class InboundDropTests(unittest.TestCase):
    def test_unmentioned_slash_text_is_not_filtered(self):
        adapter = _adapter_module()
        extracted = adapter.GoogleChatAdapter._extract_message_payload(
            {
                "type": "MESSAGE",
                "message": {
                    "text": "/stop",
                    "argumentText": "/stop",
                    "sender": {"type": "HUMAN", "displayName": "Carlo"},
                    "thread": {"name": THREAD},
                },
                "space": {"name": SPACE, "type": "ROOM"},
            }
        )
        self.assertIsNotNone(extracted)
        message, _space, _fmt = extracted
        self.assertEqual(message["text"], "/stop")

    def test_drops_are_one_info_line_without_the_body(self):
        adapter = _adapter_module()
        with self.assertLogs("gateway.platforms.google_chat", level="INFO") as logs:
            unrecognized = adapter.GoogleChatAdapter._extract_message_payload(
                {"mystery": SECRET}
            )
            not_message = adapter.GoogleChatAdapter._extract_message_payload(
                {
                    "type": "ADDED_TO_SPACE",
                    "message": {"text": SECRET, "sender": {"type": "HUMAN"}},
                }
            )
        self.assertIsNone(unrecognized)
        self.assertIsNone(not_message)
        blob = "\n".join(logs.output)
        self.assertIn("dropping inbound event: unrecognized envelope", blob)
        self.assertIn("dropping inbound event: not a message", blob)
        self.assertNotIn(SECRET, blob)
        self.assertEqual(blob.count("dropping inbound event:"), 2)

        chat = _chat(":memory:")
        chat._shutting_down = False

        class _PubSub:
            def __init__(self) -> None:
                self.acked = False
                self.data = json.dumps(
                    {
                        "type": "MESSAGE",
                        "message": {
                            "text": SECRET,
                            "sender": {"type": "BOT", "displayName": "Robie"},
                        },
                        "space": {"name": SPACE},
                    }
                ).encode()
                self.attributes = {}

            def ack(self) -> None:
                self.acked = True

            def nack(self) -> None:
                raise AssertionError("a bot message is acked, not retried")

        message = _PubSub()
        with self.assertLogs("gateway.platforms.google_chat", level="INFO") as bot_logs:
            chat._on_pubsub_message(message)
        bot_blob = "\n".join(bot_logs.output)
        self.assertTrue(message.acked)
        self.assertIn("dropping inbound event: sender is a bot", bot_blob)
        self.assertNotIn(SECRET, bot_blob)


class HelpTextTests(unittest.TestCase):
    def test_menu_says_a_space_stop_must_mention_robie(self):
        menu = capability_menu()
        self.assertIn("@Robie /stop", menu)
        self.assertIn("does not mention me", menu)
        self.assertIn("direct message", menu.casefold())
