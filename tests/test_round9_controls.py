"""Round 9: verified claims, note read-back, cross-job notes, Chat sends.

These tests do not import the Google Chat adapter.
"""

from __future__ import annotations

import ast
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_guard import enforce_note_reply_wording, guard_chat_response, open_chat_job
from robie_job_engine.chat_job_controls import (
    explicit_note_repost,
    note_repost_confirmed_by_reply,
    should_bind_waiting_reply,
    waiting_job_to_bind,
)
from robie_job_engine.chat_thread import bind_job_chat_thread
from robie_job_engine.chat_turn_control import fail_cancelled_chat_job
from robie_job_engine.discussion_note_ledger import normalize_note_text
from robie_job_engine.ezlynx_discussions import file_note_to_existing_discussion
from robie_job_engine.jev_client import JevUnavailable
from robie_job_engine.models import JobStatus
from robie_job_engine.recording import RecordingStore
from robie_job_engine.store import JobStore
from robie_job_engine.write_verification_loop import (
    JEV_CHECKPOINT,
    score_confirmed_discussion_note,
)

ROOT = Path(__file__).resolve().parents[1]
THREAD = "spaces/room/threads/waiting"


class _CountClient:
    def __init__(self, *, bodies=None):
        self.posts = 0
        self.note_count = 4
        self.latest = "old-note"
        self.bodies = bodies

    def get_discussions(self, applicant_id):
        return [{"discussionId": "d-1", "title": "follw up 1"}]

    def get_discussion(self, discussion_id):
        payload = {
            "discussionId": discussion_id,
            "title": "follw up 1",
            "noteCount": self.note_count,
            "mostRecentNoteId": self.latest,
        }
        if self.posts and self.bodies is not None:
            payload["notes"] = self.bodies
        return payload

    def append_note(self, discussion_id, text, note_type="Note"):
        self.posts += 1
        self.note_count += 1
        self.latest = f"new-note-{self.posts}"
        return {}


class VerifiedClaimTests(unittest.TestCase):
    def test_posted_and_verified_is_rewritten_when_readback_is_false(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "ezlynx.discussion_note",
                {"text": "add a note", "account_name": "Buster Brown"},
            )
            store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
            store.transition(
                job["id"],
                JobStatus.UNVERIFIED,
                expected={JobStatus.RUNNING},
                error="reply sent",
                release_lease=True,
            )
            store.checkpoint(
                job["id"],
                "discussion_note",
                {
                    "status": "posted, verifying",
                    "discussion_id": "d-1",
                    "discussion_title": "follw up 1",
                    "note_text": "follw up 1",
                    "read_back": False,
                },
            )
            reply = guard_chat_response(db, job["id"], "Posted & verified")
            self.assertIn("couldn't confirm", reply)
            self.assertNotIn("Posted & verified", reply)
            self.assertNotIn("verified", reply.casefold())
            summary = enforce_note_reply_wording(
                db,
                job["id"],
                "Posted & verified\n\nExecution Summary\nWhat happened: the note posted",
            )
            self.assertNotIn("Execution Summary", summary)
            self.assertNotIn("What happened:", summary)
            self.assertIn("couldn't confirm", summary)

    def test_done_line_stays_when_readback_is_true(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "ezlynx.discussion_note",
                {"text": "add a note", "account_name": "Buster Brown"},
            )
            store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
            store.transition(
                job["id"],
                JobStatus.UNVERIFIED,
                expected={JobStatus.RUNNING},
                error="reply sent",
                release_lease=True,
            )
            store.checkpoint(
                job["id"],
                "discussion_note",
                {
                    "status": "filed",
                    "discussion_id": "d-1",
                    "discussion_title": "follw up 1",
                    "note_text": "follw up 1",
                    "read_back": True,
                    "note_id": "new-note",
                },
            )
            store.checkpoint(
                job["id"],
                "discussion_note_readback",
                {"matched": True, "note_id": "new-note"},
            )
            reply = guard_chat_response(db, job["id"], "Posted & verified")
            self.assertIn("Done.", reply)
            self.assertIn("it's there", reply)


class NoteReadbackTests(unittest.TestCase):
    def test_count_and_text_confirm_then_jev_runs(self):
        client = _CountClient(bodies=[{"body": "Hello   there", "noteId": "new-note-1"}])
        with durable_temporary_directory() as tmp:
            ledger = Path(tmp) / "notes.json"
            db = str(Path(tmp) / "jobs.db")
            result = file_note_to_existing_discussion(
                client,
                "220250093",
                "hello there",
                title_hint="follw up 1",
                ledger_path=ledger,
            )
            self.assertEqual(result["status"], "filed")
            self.assertTrue(result["read_back"])
            self.assertEqual(result["note_id"], "new-note-1")
            self.assertEqual(client.posts, 1)
            store = JobStore(db)
            job = store.create_job("ezlynx.discussion_note", {"text": "add a note"})

            class DeadJev:
                def evaluate(self, state, questions):
                    raise JevUnavailable("not configured")

            with patch(
                "robie_job_engine.jev_client.build_jev_client",
                return_value=DeadJev(),
            ):
                score_confirmed_discussion_note(store, job["id"], result)
            self.assertTrue(store.get_checkpoint(job["id"], "discussion_note_readback")["matched"])
            self.assertIn("verdict", store.get_checkpoint(job["id"], JEV_CHECKPOINT))

    def test_text_mismatch_is_not_confirmed(self):
        client = _CountClient(bodies=[{"body": "a different note", "noteId": "new-note-1"}])
        with durable_temporary_directory() as tmp:
            result = file_note_to_existing_discussion(
                client,
                "220250093",
                "hello there",
                title_hint="follw up 1",
                ledger_path=Path(tmp) / "notes.json",
            )
        self.assertEqual(result["status"], "held")
        self.assertFalse(result["read_back"])
        self.assertEqual(client.posts, 1)
        self.assertIn("did not match", result["reason"])


class CrossJobNoteTests(unittest.TestCase):
    def test_same_text_within_a_day_asks_and_posts_only_after_yes(self):
        self.assertEqual(
            normalize_note_text("Hello,   WORLD!"),
            normalize_note_text("hello world"),
        )
        client = _CountClient()
        with durable_temporary_directory() as tmp:
            ledger = Path(tmp) / "notes.json"
            db = str(Path(tmp) / "jobs.db")
            first = file_note_to_existing_discussion(
                client,
                "220250093",
                "Hello, WORLD!",
                title_hint="follw up 1",
                ledger_path=ledger,
            )
            self.assertEqual(first["status"], "filed")
            second = file_note_to_existing_discussion(
                client,
                "220250093",
                "hello world",
                title_hint="follw up 1",
                ledger_path=ledger,
            )
            self.assertEqual(second["status"], "already_posted")
            self.assertFalse(second["read_back"])
            self.assertIn("I already added that note at", second["reason"])
            self.assertIn("ET", second["reason"])
            self.assertIn("Want me to add it again?", second["reason"])
            self.assertEqual(client.posts, 1)
            self.assertFalse(explicit_note_repost("add a different note"))
            self.assertTrue(explicit_note_repost("yes"))
            store = JobStore(db)
            job = store.create_job("ezlynx.discussion_note", {"text": "add the note again"})
            store.checkpoint(job["id"], "discussion_note", second)
            self.assertTrue(note_repost_confirmed_by_reply(store, job["id"], "Yes"))
            again = file_note_to_existing_discussion(
                client,
                "220250093",
                "hello world",
                title_hint="follw up 1",
                ledger_path=ledger,
                allow_repost=True,
            )
            self.assertEqual(again["status"], "filed")
            self.assertTrue(again["read_back"])
            self.assertEqual(client.posts, 2)


class WaitingAndStopTests(unittest.TestCase):
    def test_top_level_message_does_not_answer_a_waiting_job(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            waiting = open_chat_job(
                db,
                "m-wait",
                "can you do a book for me",
                conversation_id="spaces/room",
            )
            bind_job_chat_thread(store, waiting, THREAD)
            self.assertFalse(
                should_bind_waiting_reply(store, store.get_job(waiting), "what does COI stand for?")
            )
            self.assertIsNone(waiting_job_to_bind(store, "what does COI stand for?", None))
            fresh = open_chat_job(
                db,
                "m-coi",
                "what does COI stand for?",
                conversation_id="spaces/room",
            )
            self.assertNotEqual(fresh, waiting)
            self.assertEqual(
                store.get_job(waiting)["status"],
                JobStatus.NEEDS_CLARIFICATION.value,
            )
            bound = open_chat_job(
                db,
                "m-in-thread",
                "Buster Brown's policies",
                conversation_id="spaces/room",
                inbound_thread_id=THREAD,
            )
            self.assertEqual(bound, waiting)

    def test_stop_leaves_the_job_cancelled(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job("hermes.google_chat_task", {"text": "working"})
            store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
            fail_cancelled_chat_job(store, job["id"])
            self.assertEqual(store.get_job(job["id"])["status"], JobStatus.CANCELLED.value)

    def test_stale_recording_without_a_process_is_swept(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            recordings = RecordingStore(db)
            stale = recordings.create("job-stale", Path(tmp) / "stale.webm", Path(tmp) / "stale.stop")
            live = recordings.create("job-live", Path(tmp) / "live.webm", Path(tmp) / "live.stop")
            recordings.update(stale["id"], status="RECORDING", capture_pid=0)
            recordings.update(live["id"], status="RECORDING", capture_pid=os.getpid())
            swept = recordings.sweep_stale_recordings()
            self.assertIn(stale["id"], swept)
            self.assertNotIn(live["id"], swept)
            self.assertEqual(recordings.get(stale["id"])["status"], "FAILED")
            self.assertEqual(recordings.get(live["id"])["status"], "RECORDING")


class ChatSendSanitizerTests(unittest.TestCase):
    def test_every_chat_send_call_site_uses_the_sanitizer(self):
        send_names = {
            "send",
            "send_clarify",
            "edit_message",
            "post_as_chat_app",
            "post_card_as_chat_app",
            "_create_message",
            "_patch_message",
        }
        missing = []
        paths = list((ROOT / "integrations" / "google_chat").glob("*.py"))
        paths.append(ROOT / "robie_job_engine" / "chat_app_post.py")
        for path in paths:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if node.name.startswith("_do_"):
                    continue
                segment = ast.get_source_segment(source, node) or ""
                posts = "messages().create" in segment or "messages().patch" in segment
                if (posts or node.name in send_names) and "format_user_reply" not in segment:
                    missing.append(f"{path.name}:{node.name}")
        self.assertEqual(missing, [])
        adapter = (ROOT / "integrations" / "google_chat" / "adapter.py").read_text(encoding="utf-8")
        send = adapter.split("async def send(", 1)[1].split("async def send_card(", 1)[0]
        self.assertLess(send.index("create_on_job_thread"), send.index("_patch_message"))
        self.assertIn("format_user_reply", send)
        self.assertNotIn("scrub_user_reply", send)
        formatter = (ROOT / "robie_job_engine" / "user_reply.py").read_text(encoding="utf-8")
        self.assertIn("scrub_user_reply", formatter)
