"""An unrelated top-level question must not reopen an unverified note job.

On hermes-test-01 an ACORD 25 question was routed as a continuation of the
space's latest note job. That job had really written and was UNVERIFIED.
It was moved back to PENDING, the readback ran again, and the gateway then
stopped taking Chat messages.
"""

from __future__ import annotations

import asyncio
import importlib.util
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_guard import open_chat_job
from robie_job_engine.chat_job_controls import (
    REPEAT_NOTE_REFUSAL,
    refuse_repeat_note_post,
)
from robie_job_engine.chat_queue import DurableChatEventQueue
from robie_job_engine.chat_thread import bind_job_chat_thread
from robie_job_engine.models import JobStatus
from robie_job_engine.store import JobStore
from test_playwright_artifact_fail_closed import (
    _install_hermes_registry_stub,
    _restore_modules,
)
from test_round10_reply_lifecycle import SPACE, THREAD, _adapter_module, _chat

ROOT = Path(__file__).resolve().parents[1]
NOTE_TOOL = ROOT / "deploy" / "hermes" / "tools" / "ezlynx_note_tool.py"

ACORD = "What is an ACORD 25 certificate of insurance?"
NOTE = "follw up 1"


def _unverified_note_job(store: JobStore) -> str:
    job = store.create_job(
        "ezlynx.discussion_note",
        {
            "text": "add a note to Buster Brown on follw up 1 saying Follow up",
            "account_name": "Buster Brown",
            "conversation_id": SPACE,
            "requested_by": "Carlo",
        },
    )
    store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
    store.transition(
        job["id"],
        JobStatus.UNVERIFIED,
        expected={JobStatus.RUNNING},
        error="the note was sent, but it could not be told apart",
        release_lease=True,
    )
    store.checkpoint(
        job["id"],
        "discussion_note",
        {
            "status": "held",
            "confirmation": "sent, unconfirmed",
            "wrote": True,
            "discussion_id": "d-1",
            "discussion_title": NOTE,
            "note_text": "Follow up",
            "reason": "The note was sent, but it could not be told apart from another note.",
        },
    )
    store.checkpoint(
        job["id"],
        "action",
        {"write": "discussion_note", "target": {"discussion_id": "d-1"}},
    )
    bind_job_chat_thread(store, job["id"], THREAD)
    return job["id"]


class UnverifiedNoteRoutingTests(unittest.TestCase):
    def test_unrelated_top_level_question_opens_a_new_job(self):
        posts: list[str] = []

        def _append(*_args, **_kwargs):
            posts.append("write")
            raise AssertionError("the note write ran again")

        with durable_temporary_directory() as tmp:
            db = str(__import__("pathlib").Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _unverified_note_job(store)
            DurableChatEventQueue(db).link_conversation_job(
                conversation_id=SPACE,
                job_id=job_id,
                message_id="spaces/ROBY/messages/note",
                event_id="spaces/ROBY/messages/note",
                relation="CREATED",
            )
            opened = open_chat_job(
                db,
                "spaces/ROBY/messages/acord",
                ACORD,
                requested_by="Carlo",
                conversation_id=SPACE,
                inbound_thread_id=None,
            )
            self.assertIsNotNone(opened)
            self.assertNotEqual(opened, job_id)
            self.assertEqual(
                store.get_job(job_id)["status"], JobStatus.UNVERIFIED.value
            )
            self.assertIsNone(store.get_checkpoint(job_id, "continuation:spaces/ROBY/messages/acord"))
            self.assertEqual(
                refuse_repeat_note_post(
                    {"note_text": "something else"},
                    {"job_id": job_id, "db_path": db},
                ),
                REPEAT_NOTE_REFUSAL,
            )
            previous = _install_hermes_registry_stub()
            spec = importlib.util.spec_from_file_location(
                "robie_note_tool_terminal_routing", NOTE_TOOL
            )
            module = importlib.util.module_from_spec(spec)
            assert spec is not None and spec.loader is not None
            spec.loader.exec_module(module)
            try:
                with patch(
                    "robie_job_engine.ezlynx_api_only_writes.add_note_to_discussion",
                    _append,
                ):
                    result = module.ezlynx_discussion_note_handler(
                        {
                            "applicant_id": "1",
                            "note_text": "Follow up",
                            "title_hint": NOTE,
                        },
                        job_id=job_id,
                        db_path=db,
                    )
            finally:
                _restore_modules(previous)
            self.assertEqual(posts, [])
            self.assertFalse(bool((result or {}).get("ok")))
            self.assertEqual(
                store.get_job(job_id)["status"], JobStatus.UNVERIFIED.value
            )
            again = open_chat_job(
                db,
                "spaces/ROBY/messages/next",
                "What time is it in New York?",
                requested_by="Carlo",
                conversation_id=SPACE,
                inbound_thread_id=None,
            )
            self.assertIsNotNone(again)
            self.assertNotEqual(again, job_id)
            self.assertNotEqual(again, opened)
            self.assertEqual(
                store.get_job(job_id)["status"], JobStatus.UNVERIFIED.value
            )

    def test_continue_does_not_reopen_an_unverified_note(self):
        with durable_temporary_directory() as tmp:
            db = str(__import__("pathlib").Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _unverified_note_job(store)
            DurableChatEventQueue(db).link_conversation_job(
                conversation_id=SPACE,
                job_id=job_id,
                message_id="spaces/ROBY/messages/note",
                event_id="spaces/ROBY/messages/note",
                relation="CREATED",
            )
            opened = open_chat_job(
                db,
                "spaces/ROBY/messages/continue",
                "Continue this job and verify the note",
                requested_by="Carlo",
                conversation_id=SPACE,
                inbound_thread_id=THREAD,
            )
            self.assertNotEqual(opened, job_id)
            self.assertEqual(
                store.get_job(job_id)["status"], JobStatus.UNVERIFIED.value
            )

    def test_send_does_not_hold_the_chat_loop(self):
        adapter = _adapter_module()

        def _slow(_db, _job_id, content):
            time.sleep(0.3)
            return content

        with durable_temporary_directory() as tmp:
            db = str(__import__("pathlib").Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "hermes.plain_english",
                {"text": ACORD, "conversation_id": SPACE, "answer_only": True},
            )
            chat = _chat(db)

            async def _run():
                started = time.monotonic()
                task = asyncio.create_task(
                    chat.send(
                        SPACE,
                        "An ACORD 25 is a certificate of liability insurance.",
                        metadata={"robie_job_id": job["id"]},
                    )
                )
                await asyncio.sleep(0)
                yielded = time.monotonic() - started
                await task
                return yielded

            with patch.object(adapter, "ROBIE_JOB_DB", db), patch.object(
                adapter, "guard_chat_response", _slow
            ):
                yielded = asyncio.run(_run())
            self.assertLess(yielded, 0.15)


if __name__ == "__main__":
    unittest.main()
