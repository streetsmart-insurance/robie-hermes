"""A live EZLynx note still tells the user, and a matched note is not a write.

POST returns no note id. The discussion read has no note text. The notes
list is HTTP 405. A stable +1 is one plain line and not COMPLETE. A job
that only remembered an older note can be closed. A job that really posted
cannot.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_guard import publish_discussion_note_outcome
from robie_job_engine.chat_thread import bind_job_chat_thread
from robie_job_engine.discussion_note_ledger import SENT_UNCONFIRMED
from robie_job_engine.models import JobStatus
from robie_job_engine.recording import RecordingStore
from robie_job_engine.store import JobStore
from robie_job_engine.unverified_admin import close_unverified_without_write
from test_discussion_note_readback import DISCUSSION, LiveShapeClient, TITLE
from test_playwright_artifact_fail_closed import (
    _install_hermes_registry_stub,
    _restore_modules,
)
from robie_job_engine import ezlynx_discussions as disc

SPACE = "spaces/ROBY"
THREAD = "spaces/ROBY/threads/job"
APPLICANT = "220250093"
ROOT = Path(__file__).resolve().parents[1]
NOTE_TOOL = ROOT / "deploy" / "hermes" / "tools" / "ezlynx_note_tool.py"


def _running(store: JobStore, action: str, payload: dict) -> str:
    job = store.create_job(action, payload)
    store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
    return job["id"]


def _stick_recording(db: str, job_id: str) -> str:
    recordings = RecordingStore(db)
    path = Path(db).parent / "clip.webm"
    row = recordings.create(job_id, path, path.with_suffix(".stop"))
    recordings.update(row["id"], status="RECORDING", capture_pid=999_999_999)
    return row["id"]


class LiveNoteReplyTests(unittest.TestCase):
    def test_stable_plus_one_posts_one_line_and_stays_unverified(self):
        client = LiveShapeClient()
        with durable_temporary_directory() as tmp:
            ledger = Path(tmp) / "ledger.json"
            filed = disc.file_note_to_existing_discussion(
                client,
                APPLICANT,
                "Follow up. ROBIE was here",
                ledger_path=ledger,
            )
            self.assertEqual(filed["status"], "filed")
            self.assertTrue(filed["read_back"])
            self.assertEqual(filed["note_id"], "701")
            self.assertEqual(client.posts, 1)
            self.assertEqual(client.note_lists, 0)
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _running(
                store,
                "ezlynx.discussion_note",
                {
                    "text": "add a note to Buster Brown on follw up 1 saying Follow up",
                    "conversation_id": SPACE,
                    "account_name": "Buster Brown",
                },
            )
            store.checkpoint(
                job_id,
                "discussion_note",
                {
                    "status": "filed",
                    "discussion_id": filed["discussion_id"],
                    "discussion_title": TITLE,
                    "note_id": filed["note_id"],
                    "note_text": "Follow up. ROBIE was here",
                    "applicant_id": APPLICANT,
                    "read_back": True,
                    "verified_by": "discussion",
                    "wrote": True,
                    "reason": filed["reason"],
                },
            )
            store.checkpoint(
                job_id,
                "discussion_note_readback",
                {"matched": True, "note_id": filed["note_id"], "verified_by": "discussion"},
            )
            bind_job_chat_thread(store, job_id, THREAD)
            recording = _stick_recording(db, job_id)
            sent: list[str] = []
            line = publish_discussion_note_outcome(
                db,
                job_id,
                poster=lambda space, text, thread, posted_job: sent.append(text),
            )
            self.assertEqual(sent, [line])
            self.assertEqual(
                line,
                f'Added the note to Buster Brown on "{TITLE}".',
            )
            self.assertNotIn("couldn't read", line or "")
            self.assertEqual(store.get_job(job_id)["status"], JobStatus.COMPLETE.value)
            self.assertNotEqual(
                RecordingStore(db).get(recording)["status"], "RECORDING"
            )

    def test_unconfirmed_hold_still_says_one_line(self):
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
            store.checkpoint(
                job_id,
                "discussion_note",
                {
                    "status": "held",
                    "discussion_id": DISCUSSION,
                    "discussion_title": TITLE,
                    "note_text": "Follow up",
                    "confirmation": SENT_UNCONFIRMED,
                    "wrote": True,
                    "reason": (
                        "The note was sent, but the discussion still has the same notes. "
                        "It was not sent again."
                    ),
                },
            )
            bind_job_chat_thread(store, job_id, THREAD)
            sent: list[str] = []
            line = publish_discussion_note_outcome(
                db,
                job_id,
                poster=lambda space, text, thread, posted_job: sent.append(text),
            )
            self.assertEqual(len(sent), 1)
            self.assertIn("same notes", line or "")
            self.assertEqual(store.get_job(job_id)["status"], JobStatus.UNVERIFIED.value)

    def test_address_note_stays_the_honest_field_line(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _running(
                store,
                "ezlynx.policy_change",
                {
                    "text": "change the mailing address and add a note",
                    "conversation_id": SPACE,
                    "account_name": "Buster Brown",
                },
            )
            store.checkpoint(
                job_id,
                "discussion_note",
                {
                    "status": "filed",
                    "discussion_id": DISCUSSION,
                    "discussion_title": TITLE,
                    "note_id": "701",
                    "note_text": "Mailing address 6 to 7",
                    "applicant_id": APPLICANT,
                    "read_back": True,
                    "verified_by": "discussion",
                    "wrote": True,
                },
            )
            store.checkpoint(
                job_id,
                "discussion_note_readback",
                {"matched": True, "note_id": "701"},
            )
            bind_job_chat_thread(store, job_id, THREAD)
            sent: list[str] = []
            line = publish_discussion_note_outcome(
                db,
                job_id,
                poster=lambda space, text, thread, posted_job: sent.append(text),
            )
            self.assertEqual(len(sent), 1)
            self.assertIn("can't change the address", line or "")
            self.assertNotIn("Added the note", line or "")
            self.assertEqual(store.get_job(job_id)["status"], JobStatus.UNVERIFIED.value)


class AdminWriteTests(unittest.TestCase):
    def test_a_matched_prior_note_is_not_a_write(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            matched = _running(
                store,
                "ezlynx.discussion_note",
                {"text": "add the note that was already there", "conversation_id": SPACE},
            )
            store.checkpoint(
                matched,
                "discussion_note",
                {
                    "status": "already_posted",
                    "note_id": "older-note",
                    "discussion_id": DISCUSSION,
                    "wrote": False,
                    "reason": "Want me to add it again?",
                },
            )
            store.transition(
                matched,
                JobStatus.UNVERIFIED,
                expected={JobStatus.RUNNING},
                error="waiting",
                release_lease=True,
            )
            closed = close_unverified_without_write(store, matched)
            self.assertEqual(closed["status"], JobStatus.CANCELLED.value)

    def test_a_real_send_is_still_refused(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            wrote = _running(
                store,
                "ezlynx.discussion_note",
                {"text": "add the note that really posted", "conversation_id": SPACE},
            )
            store.checkpoint(
                wrote,
                "ezlynx_note_tool_failed",
                {
                    "error": (
                        "RuntimeError: The note was sent, but it could not be "
                        "told apart from another note."
                    )
                },
            )
            store.transition(
                wrote,
                JobStatus.UNVERIFIED,
                expected={JobStatus.RUNNING},
                error="not confirmed",
                release_lease=True,
            )
            with self.assertRaises(RuntimeError) as refused:
                close_unverified_without_write(store, wrote)
            self.assertIn("wrote something", str(refused.exception))

            counted = _running(
                store,
                "ezlynx.discussion_note",
                {"text": "add a different note", "conversation_id": SPACE},
            )
            store.checkpoint(
                counted,
                "discussion_note",
                {
                    "status": "held",
                    "confirmation": SENT_UNCONFIRMED,
                    "wrote": True,
                    "note_id": "",
                    "reason": "The note was sent, but the latest note did not change.",
                },
            )
            store.transition(
                counted,
                JobStatus.UNVERIFIED,
                expected={JobStatus.RUNNING},
                error="not confirmed",
                release_lease=True,
            )
            with self.assertRaises(RuntimeError):
                close_unverified_without_write(store, counted)


class LiveShapeToolTests(unittest.TestCase):
    def _load(self):
        previous = _install_hermes_registry_stub()
        spec = importlib.util.spec_from_file_location(
            "robie_note_tool_live_shape", NOTE_TOOL
        )
        module = importlib.util.module_from_spec(spec)
        assert spec is not None and spec.loader is not None
        try:
            spec.loader.exec_module(module)
        except Exception:
            _restore_modules(previous)
            raise
        return module, previous

    def test_handler_speaks_once_for_the_live_shape(self):
        tool, previous = self._load()
        client = LiveShapeClient()
        try:
            with durable_temporary_directory() as tmp:
                db = str(Path(tmp) / "jobs.db")
                ledger = Path(tmp) / "ledger.json"
                store = JobStore(db)
                job = store.create_job(
                    "hermes.plain_english",
                    {
                        "text": "add a note to Buster Brown on follw up 1 saying Follow up",
                        "conversation_id": SPACE,
                        "account_name": "Buster Brown",
                    },
                )
                store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
                bind_job_chat_thread(store, job["id"], THREAD)

                def _add(applicant_id, note_text, **kwargs):
                    return disc.file_note_to_existing_discussion(
                        client,
                        applicant_id,
                        note_text,
                        title_hint=kwargs.get("title_hint"),
                        ledger_path=ledger,
                        allow_repost=bool(kwargs.get("allow_repost")),
                    )

                sent: list[str] = []
                with patch(
                    "robie_job_engine.ezlynx_api_only_writes.add_note_to_discussion",
                    side_effect=_add,
                ):
                    result = tool.ezlynx_discussion_note_handler(
                        {
                            "applicant_id": APPLICANT,
                            "note_text": "Follow up",
                            "title_hint": TITLE,
                        },
                        job_id=job["id"],
                        db_path=db,
                        outcome_poster=lambda space, text, thread, posted_job: sent.append(
                            text
                        ),
                    )
                self.assertTrue(result["ok"])
                self.assertEqual(result["status"], "filed")
                self.assertEqual(result["verified_by"], "discussion")
                self.assertTrue(result["read_back"])
                self.assertTrue(result["wrote"])
                self.assertEqual(client.posts, 1)
                self.assertEqual(client.note_lists, 0)
                self.assertEqual(len(sent), 1)
                self.assertEqual(
                    sent[0],
                    f'Added the note to Buster Brown on "{TITLE}".',
                )
                saved = JobStore(db).get_checkpoint(job["id"], "discussion_note")
                self.assertTrue(saved["wrote"])
                self.assertEqual(saved["status"], "filed")
                self.assertEqual(
                    JobStore(db).get_job(job["id"])["status"],
                    JobStatus.COMPLETE.value,
                )
        finally:
            _restore_modules(previous)
