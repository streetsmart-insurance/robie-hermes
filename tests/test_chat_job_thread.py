"""Job Chat messages stay in the job thread."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_thread import (
    REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD,
    bind_job_chat_thread,
    create_reply_option,
    inbound_thread_to_bind,
    job_thread_key,
    outbound_thread_spec,
    read_job_chat_thread,
    remember_created_thread,
)
from robie_job_engine.store import JobStore

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "integrations" / "google_chat" / "adapter.py"
THREAD = "spaces/ROBY/threads/clarify"
OTHER = "spaces/ROBY/threads/other"


def _job(store: JobStore, message_id: str = "spaces/ROBY/messages/1") -> dict:
    return store.create_job(
        "hermes.google_chat_task",
        {
            "message_id": message_id,
            "text": "I need a quick hand. The request is missing the task details.",
            "conversation_id": "spaces/ROBY",
        },
    )


class ChatJobThreadTests(unittest.TestCase):
    def test_first_outbound_starts_a_thread_and_stores_the_name(self):
        with durable_temporary_directory() as tmp:
            store = JobStore(str(Path(tmp) / "jobs.db"))
            job = _job(store)
            self.assertIsNone(
                inbound_thread_to_bind(
                    job=job,
                    message_id=job["payload"]["message_id"],
                    session_thread_id=THREAD,
                    raw_thread_name=THREAD,
                    reply_in_existing_thread=False,
                )
            )
            spec = outbound_thread_spec(
                job_id=job["id"],
                stored_thread_name=read_job_chat_thread(store, job["id"]),
                explicit_thread_name=THREAD,
                prefer_job_thread_key=True,
            )
            self.assertEqual(spec, {"threadKey": job_thread_key(job["id"])})
            self.assertEqual(
                create_reply_option(spec), REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD
            )
            stored = remember_created_thread(
                store, job["id"], {"thread": {"name": THREAD}}
            )
            self.assertEqual(stored, THREAD)
            self.assertEqual(read_job_chat_thread(store, job["id"]), THREAD)
            self.assertEqual(store.get_job(job["id"])["payload"]["thread_name"], THREAD)
            again = outbound_thread_spec(
                job_id=job["id"],
                stored_thread_name=read_job_chat_thread(store, job["id"]),
                prefer_job_thread_key=True,
            )
            self.assertEqual(again, {"name": THREAD})
            self.assertEqual(
                create_reply_option(again), REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD
            )

    def test_reply_inside_a_thread_is_where_later_sends_go(self):
        with durable_temporary_directory() as tmp:
            store = JobStore(str(Path(tmp) / "jobs.db"))
            job = _job(store)
            remember_created_thread(
                store, job["id"], {"thread": {"name": THREAD}}
            )
            reply_id = "spaces/ROBY/messages/2"
            bound = inbound_thread_to_bind(
                job=store.get_job(job["id"]),
                message_id=reply_id,
                session_thread_id=THREAD,
                raw_thread_name=THREAD,
                reply_in_existing_thread=True,
            )
            self.assertEqual(bind_job_chat_thread(store, job["id"], bound), THREAD)
            # A follow-up that would otherwise start a new top-level message
            # stays on the thread the user replied in.
            spec = outbound_thread_spec(
                job_id=job["id"],
                stored_thread_name=read_job_chat_thread(store, job["id"]),
                explicit_thread_name=None,
                prefer_job_thread_key=True,
            )
            self.assertEqual(spec, {"name": THREAD})
            self.assertNotIn("threadKey", spec)
            moved = inbound_thread_to_bind(
                job=store.get_job(job["id"]),
                message_id="spaces/ROBY/messages/3",
                session_thread_id=OTHER,
                raw_thread_name=OTHER,
                reply_in_existing_thread=True,
            )
            self.assertEqual(bind_job_chat_thread(store, job["id"], moved), OTHER)
            self.assertEqual(read_job_chat_thread(store, job["id"]), OTHER)
            # A create that falls back to a different thread does not undo the bind.
            self.assertEqual(
                remember_created_thread(
                    store, job["id"], {"thread": {"name": THREAD}}
                ),
                OTHER,
            )

    def test_new_top_level_does_not_steal_a_job_that_already_has_a_thread(self):
        with durable_temporary_directory() as tmp:
            store = JobStore(str(Path(tmp) / "jobs.db"))
            job = _job(store)
            remember_created_thread(
                store, job["id"], {"thread": {"name": THREAD}}
            )
            bound = inbound_thread_to_bind(
                job=store.get_job(job["id"]),
                message_id="spaces/ROBY/messages/9",
                session_thread_id=None,
                raw_thread_name="spaces/ROBY/threads/auto",
                reply_in_existing_thread=False,
            )
            self.assertIsNone(bound)
            spec = outbound_thread_spec(
                job_id=job["id"],
                stored_thread_name=read_job_chat_thread(store, job["id"]),
                prefer_job_thread_key=True,
            )
            self.assertEqual(spec, {"name": THREAD})

    def test_reply_that_opens_a_job_inside_an_existing_thread_binds_it(self):
        with durable_temporary_directory() as tmp:
            store = JobStore(str(Path(tmp) / "jobs.db"))
            job = _job(store, message_id="spaces/ROBY/messages/in-thread")
            bound = inbound_thread_to_bind(
                job=job,
                message_id=job["payload"]["message_id"],
                session_thread_id=THREAD,
                raw_thread_name=THREAD,
                reply_in_existing_thread=True,
            )
            self.assertEqual(bound, THREAD)
            bind_job_chat_thread(store, job["id"], bound)
            spec = outbound_thread_spec(
                job_id=job["id"],
                stored_thread_name=read_job_chat_thread(store, job["id"]),
                prefer_job_thread_key=True,
            )
            self.assertEqual(spec, {"name": THREAD})

    def test_adapter_sends_job_messages_with_the_job_thread(self):
        source = ADAPTER.read_text(encoding="utf-8")
        ast.parse(source)
        self.assertIn(
            "job_id = await asyncio.to_thread(\n                open_chat_job",
            source,
        )
        opened = source.split("async def _open_and_run_chat_job", 1)[1].split(
            "async def _handle_setup_files_command", 1
        )[0]
        self.assertLess(
            opened.index("_bind_inbound_job_thread"),
            opened.index("_run_generic_chat_job"),
        )
        generic = source.split("async def _run_generic_chat_job", 1)[1].split(
            "async def _send_clarification_if_needed", 1
        )[0]
        self.assertLess(
            generic.index("require_message_execution_available"),
            generic.index("_bind_inbound_job_thread"),
        )
        send = source.split("async def send(", 1)[1].split("async def send_card(", 1)[0]
        self.assertIn("_thread_spec_for_outbound", send)
        self.assertIn("thread_spec", send)
        self.assertLess(send.index('get("robie_job_id")'), send.index("conversation_job_for_event"))
        typing = source.split("async def send_typing(", 1)[1].split(
            "async def stop_typing(", 1
        )[0]
        self.assertIn("_thread_spec_for_outbound", typing)
        card = source.split("async def send_card(", 1)[1].split(
            "async def send_clarify(", 1
        )[0]
        self.assertIn("_thread_spec_for_outbound", card)
        create = source.split("async def _create_message(", 1)[1].split(
            "async def send_typing(", 1
        )[0]
        self.assertIn('thread_meta.get("threadKey")', create)
        self.assertIn("REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD", create)
        self.assertIn("remember_created_thread", create)
        self.assertIn("prev_thread_count > 0", source)
        self.assertIn('msg.get("threadReply") is True', source)
        self.assertNotIn('msg.get("threadReply") is not False', source)
        self.assertIn("_reply_in_existing_thread", source)
