"""Round 14: the lines the battery never saw, and the recorder that kept going.

A declined repeat note still says it was left as is. A confirmed note posts
one line and stops the recording; the model's summary does not. A second yes
does not open another job. A note that only requested an address change is
not COMPLETE. A gateway interrupt is not the answer.
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

from durable_temp import durable_temporary_directory

from robie_job_engine.browser_read import LOCATOR_QUESTION, BoundedBrowserReadWorker
from robie_job_engine.chat_guard import (
    guard_chat_response,
    open_chat_job,
    publish_discussion_note_outcome,
)
from robie_job_engine.chat_job_controls import ALREADY_DONE, LEFT_AS_IS
from robie_job_engine.chat_queue import DurableChatEventQueue
from robie_job_engine.chat_thread import bind_job_chat_thread
from robie_job_engine.engine import JobEngine
from robie_job_engine.models import JobStatus
from robie_job_engine.recording import RecordingStore
from robie_job_engine.store import JobStore
from robie_job_engine.unverified_admin import close_unverified_without_write
from test_round10_reply_lifecycle import (
    SPACE,
    THREAD,
    _Event,
    _adapter_module,
    _chat,
    _outbound_text,
)

SUMMARY = (
    "The note is on the follow-up discussion and the address change was requested "
    "for the CSR. Nothing else was written in EZLynx during this turn, and the "
    "screen was left on the applicant. The follow-up discussion still shows the "
    "request note and no field on the policy was changed."
)


def _running(store: JobStore, action: str, payload: dict) -> str:
    job = store.create_job(action, payload)
    store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
    return job["id"]


def _filed_note(store: JobStore, job_id: str, *, title: str) -> None:
    store.checkpoint(
        job_id,
        "discussion_note",
        {
            "status": "filed",
            "discussion_id": "d-1",
            "discussion_title": title,
            "note_id": "n-1",
            "note_text": "Mailing address 6 to 7",
            "applicant_id": "220250093",
            "read_back": True,
        },
    )
    store.checkpoint(
        job_id,
        "discussion_note_readback",
        {"matched": True, "note_id": "n-1"},
    )
    bind_job_chat_thread(store, job_id, THREAD)


def _stick_recording(db: str, job_id: str) -> str:
    recordings = RecordingStore(db)
    path = Path(db).parent / "clip.webm"
    row = recordings.create(job_id, path, path.with_suffix(".stop"))
    recordings.update(row["id"], status="RECORDING", capture_pid=999_999_999)
    return row["id"]


class LeftAsIsDeliveryTests(unittest.TestCase):
    def test_declined_note_line_is_delivered_after_cancel(self):
        adapter = _adapter_module()
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _running(
                store,
                "ezlynx.discussion_note",
                {
                    "text": "add a note",
                    "conversation_id": SPACE,
                    "account_name": "Buster Brown",
                },
            )
            store.transition(
                job_id,
                JobStatus.NEEDS_CLARIFICATION,
                expected={JobStatus.RUNNING},
                error="waiting on the user",
                resume_status=JobStatus.PENDING,
                release_lease=True,
            )
            store.checkpoint(
                job_id,
                "discussion_note",
                {
                    "status": "already_posted",
                    "discussion_id": "d-1",
                    "discussion_title": "follw up 1",
                    "reason": "Want me to add it again?",
                    "note_text": "Follow up",
                },
            )
            bind_job_chat_thread(store, job_id, THREAD)
            DurableChatEventQueue(db).link_conversation_job(
                conversation_id=SPACE,
                job_id=job_id,
                message_id="spaces/ROBY/messages/ask",
                event_id="spaces/ROBY/messages/ask",
                relation="CREATED",
            )
            opened = open_chat_job(
                db,
                "spaces/ROBY/messages/no",
                "no",
                requested_by="Carlo",
                conversation_id=SPACE,
                inbound_thread_id=THREAD,
            )
            self.assertEqual(opened, job_id)
            self.assertEqual(store.get_job(job_id)["status"], JobStatus.CANCELLED.value)
            chat = _chat(db)
            event = _Event(THREAD, "no")
            with patch.object(adapter, "ROBIE_JOB_DB", db):
                sent = asyncio.run(chat._halt_note_left_as_is(event, job_id))
            self.assertTrue(sent)
            outbound = _outbound_text(chat)
            self.assertTrue(outbound)
            self.assertIn(LEFT_AS_IS, outbound[0])
            self.assertEqual(store.get_job(job_id)["status"], JobStatus.CANCELLED.value)


class NoteOutcomeTests(unittest.TestCase):
    def test_one_line_stops_the_recorder_and_drops_the_summary(self):
        adapter = _adapter_module()
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            older = _running(
                store,
                "hermes.google_chat_task",
                {"text": "older turn", "conversation_id": SPACE},
            )
            job_id = _running(
                store,
                "ezlynx.discussion_note",
                {
                    "text": "add a note to Buster Brown on follw up 1 saying Follow up",
                    "account_name": "Buster Brown",
                    "conversation_id": SPACE,
                },
            )
            _filed_note(store, job_id, title="follw up 1")
            store.checkpoint(
                job_id,
                "discussion_note",
                {
                    "status": "filed",
                    "discussion_id": "d-1",
                    "discussion_title": "follw up 1",
                    "note_id": "n-1",
                    "note_text": "Follow up",
                    "applicant_id": "220250093",
                    "read_back": True,
                },
            )
            recording_id = _stick_recording(db, job_id)
            posted: list[tuple] = []
            line = publish_discussion_note_outcome(
                db,
                job_id,
                poster=lambda *args: posted.append(args),
            )
            self.assertEqual(store.get_job(job_id)["status"], JobStatus.COMPLETE.value)
            self.assertIn("Added the note", line or "")
            self.assertEqual(len(posted), 1)
            self.assertNotEqual(
                RecordingStore(db).get(recording_id)["status"], "RECORDING"
            )
            self.assertGreater(len(SUMMARY), 270)
            chat = _chat(db)
            chat._active_chat_job[SPACE] = job_id
            chat._reply_job_by_chat = {SPACE: job_id}
            chat._gateway_turns[(SPACE, "")] = {"job_id": older, "task": None}
            with patch.object(adapter, "ROBIE_JOB_DB", db):
                dropped = asyncio.run(
                    chat.send(SPACE, SUMMARY, metadata={"robie_job_id": job_id})
                )
                edited = asyncio.run(
                    chat.edit_message(SPACE, "spaces/ROBY/messages/bubble", SUMMARY)
                )
            self.assertTrue(dropped.success)
            self.assertIsNone(dropped.message_id)
            self.assertTrue(edited.success)
            self.assertEqual(chat._chat_api.messages.calls, [])
            self.assertEqual(store.get_job(older)["status"], JobStatus.RUNNING.value)

    def test_second_yes_replies_already_done_without_a_new_job(self):
        adapter = _adapter_module()
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _running(
                store,
                "ezlynx.discussion_note",
                {
                    "text": "add a note to Buster Brown",
                    "account_name": "Buster Brown",
                    "conversation_id": SPACE,
                },
            )
            _filed_note(store, job_id, title="follw up 1")
            store.checkpoint(
                job_id,
                "note_repost_confirmed",
                {"text": "yes", "used": True},
            )
            publish_discussion_note_outcome(db, job_id, poster=lambda *args: None)
            self.assertEqual(store.get_job(job_id)["status"], JobStatus.COMPLETE.value)
            DurableChatEventQueue(db).link_conversation_job(
                conversation_id=SPACE,
                job_id=job_id,
                message_id="spaces/ROBY/messages/yes1",
                event_id="spaces/ROBY/messages/yes1",
                relation="CONTINUATION",
            )
            opened = open_chat_job(
                db,
                "spaces/ROBY/messages/yes2",
                "yes",
                requested_by="Carlo",
                conversation_id=SPACE,
                inbound_thread_id=THREAD,
            )
            self.assertEqual(opened, job_id)
            with store.connect() as conn:
                count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            self.assertEqual(count, 1)
            self.assertEqual(
                store.get_checkpoint(job_id, "note_already_done")["reply"],
                ALREADY_DONE,
            )
            self.assertIsNone(RecordingStore(db).active(job_id))
            chat = _chat(db)
            event = _Event(THREAD, "yes")
            with patch.object(adapter, "ROBIE_JOB_DB", db):
                sent = asyncio.run(chat._halt_note_already_done(event, job_id))
            self.assertTrue(sent)
            self.assertIn(ALREADY_DONE, _outbound_text(chat)[0])
            self.assertEqual(store.get_job(job_id)["status"], JobStatus.COMPLETE.value)

    def test_partial_address_note_is_not_complete(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _running(
                store,
                "ezlynx.policy_change",
                {
                    "text": "Change the mailing address from 6 to 7 and note it",
                    "account_name": "Buster Brown",
                    "conversation_id": SPACE,
                },
            )
            _filed_note(
                store,
                job_id,
                title="Policy Change Request Checkup - Mailing Address update",
            )
            recording_id = _stick_recording(db, job_id)
            posted: list[tuple] = []
            line = publish_discussion_note_outcome(
                db,
                job_id,
                poster=lambda *args: posted.append(args),
            )
            self.assertEqual(store.get_job(job_id)["status"], JobStatus.UNVERIFIED.value)
            self.assertIn("I can't change the address myself yet", line or "")
            self.assertNotIn("Added the note", line or "")
            self.assertTrue(posted)
            self.assertNotEqual(
                RecordingStore(db).get(recording_id)["status"], "RECORDING"
            )
            pure = _running(
                store,
                "ezlynx.discussion_note",
                {
                    "text": "add a note to Buster Brown on follw up 1 saying Follow up",
                    "account_name": "Buster Brown",
                    "conversation_id": SPACE,
                },
            )
            _filed_note(store, pure, title="follw up 1")
            store.checkpoint(
                pure,
                "discussion_note",
                {
                    "status": "filed",
                    "discussion_id": "d-1",
                    "discussion_title": "follw up 1",
                    "note_id": "n-2",
                    "note_text": "+1",
                    "applicant_id": "220250093",
                    "read_back": True,
                },
            )
            pure_line = publish_discussion_note_outcome(
                db, pure, poster=lambda *args: None
            )
            self.assertEqual(store.get_job(pure)["status"], JobStatus.COMPLETE.value)
            self.assertIn("Added the note", pure_line or "")


class GatewayNoticeTests(unittest.TestCase):
    def test_interrupt_notice_is_not_stored_as_the_answer(self):
        adapter = _adapter_module()
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _running(
                store,
                "hermes.plain_english",
                {
                    "text": "what time is it in new york",
                    "request_text": "what time is it in new york",
                    "answer_only": True,
                    "conversation_id": SPACE,
                },
            )
            chat = _chat(db)
            chat._question_job_by_chat = {SPACE: job_id}
            notice = "⚡ Interrupting current task…"
            with patch.object(adapter, "ROBIE_JOB_DB", db):
                dropped = asyncio.run(
                    chat.send(SPACE, notice, metadata={"robie_job_id": job_id})
                )
            self.assertTrue(dropped.success)
            self.assertIsNone(dropped.message_id)
            self.assertEqual(chat._chat_api.messages.calls, [])
            self.assertEqual(store.get_job(job_id)["status"], JobStatus.RUNNING.value)
            self.assertFalse(store.get_checkpoint(job_id, "answer_only_close"))
            direct = guard_chat_response(db, job_id, notice)
            self.assertEqual(direct, "")
            self.assertEqual(store.get_job(job_id)["status"], JobStatus.RUNNING.value)
            answer = "It is 11:05 PM Eastern."
            with patch.object(adapter, "ROBIE_JOB_DB", db):
                sent = asyncio.run(
                    chat.send(SPACE, answer, metadata={"robie_job_id": job_id})
                )
            self.assertTrue(sent.success)
            self.assertIsNotNone(sent.message_id)
            self.assertIn("11:05", _outbound_text(chat)[0])
            self.assertNotIn("Interrupting", _outbound_text(chat)[0])
            self.assertEqual(store.get_job(job_id)["status"], JobStatus.COMPLETE.value)


class LocatorAndAdminTests(unittest.TestCase):
    def test_missing_locator_asks_which_page(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "browser.read",
                {"worker": "browser-read", "locator": {}, "text": "read the dec page"},
            )
            JobEngine(
                store,
                {"browser-read": BoundedBrowserReadWorker(object())},
                {},
            ).run(job["id"])
            parked = store.get_job(job["id"])
            self.assertEqual(parked["status"], JobStatus.NEEDS_CLARIFICATION.value)
            question = store.get_checkpoint(job["id"], "clarification")["question"]
            self.assertEqual(question, LOCATOR_QUESTION)
            reply = guard_chat_response(db, job["id"], "The durable background worker finished.")
            self.assertIn("Which page should I read?", reply)
            self.assertNotIn("destination locator", reply.casefold())

    def test_admin_close_cancels_unverified_jobs_that_never_wrote(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _running(
                store,
                "hermes.google_chat_task",
                {"text": "stuck read", "conversation_id": SPACE},
            )
            store.transition(
                job_id,
                JobStatus.UNVERIFIED,
                expected={JobStatus.RUNNING},
                error="never confirmed",
                release_lease=True,
            )
            closed = close_unverified_without_write(store, job_id)
            self.assertEqual(closed["status"], JobStatus.CANCELLED.value)
            self.assertFalse(store.get_checkpoint(job_id, "admin_close")["wrote"])
            wrote = _running(
                store,
                "ezlynx.discussion_note",
                {"text": "add a note", "conversation_id": SPACE},
            )
            store.checkpoint(
                wrote,
                "discussion_note",
                {"status": "filed", "note_id": "n-9", "note_text": "hi"},
            )
            store.transition(
                wrote,
                JobStatus.UNVERIFIED,
                expected={JobStatus.RUNNING},
                error="not fully confirmed",
                release_lease=True,
            )
            with self.assertRaises(RuntimeError):
                close_unverified_without_write(store, wrote)

    def test_answer_only_job_does_not_start_a_recording(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            opened = open_chat_job(
                db,
                "spaces/ROBY/messages/q",
                "which carriers do we quote for auto?",
                requested_by="Carlo",
                conversation_id=SPACE,
            )
            self.assertTrue(store.get_checkpoint(opened, "question_only"))
            self.assertIsNone(RecordingStore(db).active(opened))
