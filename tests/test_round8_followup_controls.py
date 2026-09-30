"""Round 8 follow-ups: one note post, waiting-job routing, cancel, scrub, orphans.

These tests do not import the Google Chat adapter.
"""

from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from durable_temp import durable_temporary_directory

from robie_job_engine.answer_only import scrub_user_reply
from robie_job_engine.chat_guard import _looks_in_progress, open_chat_job
from robie_job_engine.chat_job_controls import (
    CANCEL_POLICY_REFUSAL,
    REPEAT_NOTE_REFUSAL,
    WAITING_EXPIRED_NOTE,
    mark_job_waiting_for_user,
    plausibly_answers_waiting_question,
    settle_job_when_reply_sent,
    should_bind_waiting_reply,
    sweep_dead_running_jobs,
    waiting_job_to_cancel,
)
from robie_job_engine.chat_queue import DurableChatEventQueue
from robie_job_engine.chat_thread import bind_job_chat_thread
from robie_job_engine.chat_turn_control import (
    NOTHING_RUNNING_REPLY,
    busy_session_should_defer,
    fail_cancelled_chat_job,
    stop_reply_line,
)
from robie_job_engine.models import JobStatus
from robie_job_engine.request_routing import classify_request
from robie_job_engine.store import JobStore
from robie_job_engine.write_verification_loop import (
    is_ezlynx_write_job,
    prepare_write_plan,
)

ROOT = Path(__file__).resolve().parents[1]
THREAD = "spaces/room/threads/note-thread"


def _backdate(db: str, job_id: str, seconds: int) -> None:
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE jobs SET created_at=?, updated_at=? WHERE id=?",
            (stamp, stamp, job_id),
        )


class DuplicateNoteTests(unittest.TestCase):
    def test_plain_note_request_is_a_write(self):
        text = "add a note to Buster Brown discussion follw up 1"
        classified = classify_request(text)
        self.assertEqual(classified.action_type, "ezlynx.discussion_note")
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(classified.action_type, {"text": text})
            self.assertTrue(is_ezlynx_write_job(store.get_job(job["id"])))

    def test_second_post_on_the_same_step_is_refused(self):
        import importlib.util

        from test_playwright_artifact_fail_closed import (
            _install_hermes_registry_stub,
            _restore_modules,
        )

        previous = _install_hermes_registry_stub()
        spec = importlib.util.spec_from_file_location(
            "robie_note_tool_repeat",
            ROOT / "deploy/hermes/tools/ezlynx_note_tool.py",
        )
        module = importlib.util.module_from_spec(spec)
        assert spec is not None and spec.loader is not None
        spec.loader.exec_module(module)
        try:
            with durable_temporary_directory() as tmp:
                db = str(Path(tmp) / "jobs.db")
                store = JobStore(db)
                job = store.create_job(
                    "ezlynx.discussion_note",
                    {"text": "add a note to Buster Brown discussion follw up 1"},
                )
                store.checkpoint(
                    job["id"],
                    "write_plan",
                    {
                        "locked": True,
                        "write": "discussion note",
                        "target": {"discussion": "follw up 1"},
                        "values": {"note": "follw up 1"},
                    },
                )
                posted = {
                    "status": "posted, verifying",
                    "note_id": None,
                    "discussion_id": "848144886",
                    "discussion_title": "follw up 1",
                    "read_back": False,
                    "reason": "accepted without a note_id",
                }
                with patch(
                    "robie_job_engine.ezlynx_api_only_writes.add_note_to_discussion",
                    return_value=posted,
                ) as mocked:
                    first = module.ezlynx_discussion_note_handler(
                        {
                            "applicant_id": "26356199",
                            "note_text": "follw up 1",
                            "title_hint": "follw up 1",
                        },
                        job_id=job["id"],
                        db_path=db,
                    )
                    second = module.ezlynx_discussion_note_handler(
                        {
                            "applicant_id": "26356199",
                            "note_text": "follw up 1 again",
                            "title_hint": "follw up 1",
                        },
                        job_id=job["id"],
                        db_path=db,
                    )
                self.assertTrue(first["do_not_repost"])
                self.assertIn("Do not post this note again", first["instruction"])
                self.assertEqual(mocked.call_count, 1)
                self.assertFalse(second["ok"])
                self.assertIn(REPEAT_NOTE_REFUSAL, second["error"])
        finally:
            _restore_modules(previous)

    def test_note_tool_without_a_job_still_files(self):
        import importlib.util

        from test_playwright_artifact_fail_closed import (
            _install_hermes_registry_stub,
            _restore_modules,
        )

        previous = _install_hermes_registry_stub()
        spec = importlib.util.spec_from_file_location(
            "robie_note_tool_no_job",
            ROOT / "deploy/hermes/tools/ezlynx_note_tool.py",
        )
        module = importlib.util.module_from_spec(spec)
        assert spec is not None and spec.loader is not None
        spec.loader.exec_module(module)
        try:
            with patch(
                "robie_job_engine.ezlynx_api_only_writes.add_note_to_discussion",
                return_value={
                    "status": "filed",
                    "note_id": "n1",
                    "discussion_id": "d1",
                    "read_back": True,
                },
            ) as mocked:
                result = module.ezlynx_discussion_note_handler(
                    {"applicant_id": "26356199", "note_text": "Hello"}
                )
            self.assertTrue(result["ok"])
            mocked.assert_called_once()
        finally:
            _restore_modules(previous)


class WaitingJobTests(unittest.TestCase):
    def test_clarify_marks_waiting_so_the_answer_is_not_busy(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "hermes.google_chat_task",
                {"text": "Which coverage amount?"},
            )
            store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
            self.assertTrue(mark_job_waiting_for_user(store, job["id"], "Which one?"))
            self.assertEqual(
                store.get_job(job["id"])["status"],
                JobStatus.NEEDS_CLARIFICATION.value,
            )
            self.assertIsNotNone(store.get_checkpoint(job["id"], "clarification"))

            class Adapter:
                gateway_runner = None

                def __init__(self):
                    self._active_sessions = {"agent:main:google_chat:dm:spaces/room": object()}

            class Source:
                chat_id = "spaces/room"
                thread_id = ""
                user_id = "users/carlo"

            class Event:
                source = Source()

            with patch(
                "robie_job_engine.chat_turn_control.running_chat_job_id",
                return_value=job["id"],
            ), patch(
                "robie_job_engine.chat_turn_control.session_is_busy",
                return_value=True,
            ):
                self.assertFalse(
                    busy_session_should_defer(Adapter(), Event(), db_path=db)
                )

    def test_unrelated_message_does_not_attach_to_a_stale_waiting_job(self):
        essay = "write a 3000-word essay about insurance"
        self.assertFalse(plausibly_answers_waiting_question(essay))
        self.assertTrue(plausibly_answers_waiting_question("3"))
        self.assertTrue(plausibly_answers_waiting_question("Buster Brown's policies"))
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            waiting = open_chat_job(
                db,
                "m-vague",
                "can you do a book for me",
                conversation_id="spaces/room",
            )
            self.assertEqual(
                store.get_job(waiting)["status"],
                JobStatus.NEEDS_CLARIFICATION.value,
            )
            bind_job_chat_thread(store, waiting, THREAD)
            fresh = open_chat_job(
                db,
                "m-essay",
                essay,
                conversation_id="spaces/room",
                inbound_thread_id="spaces/room/threads/other",
            )
            self.assertNotEqual(fresh, waiting)
            self.assertEqual(
                store.get_job(waiting)["status"],
                JobStatus.NEEDS_CLARIFICATION.value,
            )
            in_thread = open_chat_job(
                db,
                "m-in-thread",
                "please write an essay about roofs",
                conversation_id="spaces/room",
                inbound_thread_id=THREAD,
            )
            self.assertEqual(in_thread, waiting)

    def test_only_a_fresh_plausible_reply_binds_without_a_thread(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            waiting = open_chat_job(
                db,
                "m-vague-2",
                "can you do a book for me",
                conversation_id="spaces/only",
            )
            job = store.get_job(waiting)
            self.assertTrue(should_bind_waiting_reply(store, job, "3"))
            _backdate(db, waiting, 16 * 60)
            aged = store.get_job(waiting)
            self.assertFalse(should_bind_waiting_reply(store, aged, "3"))
            expired = open_chat_job(
                db,
                "m-after-expire",
                "3",
                conversation_id="spaces/only",
            )
            self.assertNotEqual(expired, waiting)
            self.assertEqual(store.get_job(waiting)["status"], JobStatus.FAILED.value)
            self.assertEqual(
                store.get_checkpoint(waiting, "waiting_expired")["reply"],
                WAITING_EXPIRED_NOTE,
            )

    def test_stop_cancels_a_waiting_job_and_not_a_finished_one(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            queue = DurableChatEventQueue(db)
            waiting = open_chat_job(
                db,
                "m-wait-stop",
                "can you do a book for me",
                conversation_id="spaces/stop",
            )
            self.assertEqual(waiting_job_to_cancel(db, "spaces/stop"), waiting)
            reply = fail_cancelled_chat_job(store, waiting)
            self.assertEqual(reply, stop_reply_line(waiting))
            self.assertEqual(store.get_job(waiting)["status"], JobStatus.FAILED.value)
            finished = store.create_job(
                "hermes.google_chat_task",
                {"text": "done already"},
            )
            store.transition(finished["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
            store.transition(
                finished["id"],
                JobStatus.FAILED,
                expected={JobStatus.RUNNING},
                error="done",
                release_lease=True,
            )
            queue.link_conversation_job(
                conversation_id="spaces/finished",
                job_id=finished["id"],
                message_id="m-fin",
                event_id="m-fin",
            )
            self.assertIsNone(waiting_job_to_cancel(db, "spaces/finished"))
            self.assertEqual(
                fail_cancelled_chat_job(store, finished["id"]),
                NOTHING_RUNNING_REPLY,
            )
            self.assertIsNone(store.get_checkpoint(finished["id"], "cancelled"))
        adapter = (ROOT / "integrations/google_chat/adapter.py").read_text(encoding="utf-8")
        stop = adapter.split("async def _apply_chat_stop", 1)[1].split(
            "async def _stop_chat_queue_heartbeat", 1
        )[0]
        self.assertNotIn("active_conversation_job", stop)
        self.assertIn("waiting_job_to_cancel", stop)
        send = adapter.split("async def send(", 1)[1].split("async def send_card(", 1)[0]
        branch = send.split('delivery_kind == "stop"', 1)[1].split("elif", 1)[0]
        self.assertIn("NOTHING_RUNNING_REPLY", branch)
        self.assertIn("stop_reply_line", branch)
        self.assertIn("send_clarify", adapter)
        clarify = adapter.split("async def send_clarify", 1)[1].split("async def ", 1)[0]
        self.assertIn("_mark_clarify_waiting", clarify)


class CancelAndScrubTests(unittest.TestCase):
    def test_cancel_policy_is_refused_immediately_and_not_planned(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            blocked = open_chat_job(
                db,
                "m-cancel",
                "cancel the auto policy",
                conversation_id="spaces/cancel",
            )
            job = store.get_job(blocked)
            self.assertEqual(job["status"], JobStatus.FAILED.value)
            self.assertEqual(
                store.get_checkpoint(blocked, "hard_block")["reply"],
                CANCEL_POLICY_REFUSAL,
            )
            self.assertIsNone(store.get_checkpoint(blocked, "clarification"))

            def boom(_prompt: str) -> str:
                raise AssertionError("the plan model must not run")

            planned = prepare_write_plan(store, store.get_job(blocked), boom)
            self.assertTrue(planned.get("skipped"))
            self.assertEqual(planned.get("reason"), "hard blocked")
            nxt = open_chat_job(
                db,
                "m-after-cancel",
                "what does COI stand for?",
                conversation_id="spaces/cancel",
            )
            self.assertIsNotNone(nxt)
            self.assertNotEqual(nxt, blocked)
            self.assertNotEqual(
                store.get_job(nxt)["status"],
                JobStatus.NEEDS_CLARIFICATION.value,
            )

    def test_missing_field_marker_becomes_a_plain_question(self):
        leaked = "ROBIE_BLOCKED: MISSING_REQUIRED_FIELD: task details"
        cleaned = scrub_user_reply(leaked)
        self.assertNotIn("ROBIE_BLOCKED", cleaned)
        self.assertNotIn("MISSING_REQUIRED_FIELD", cleaned)
        self.assertEqual(cleaned, "I need the task details before I can do that.")
        self.assertNotIn("PLAYWRIGHT_BLOCKED", scrub_user_reply("PLAYWRIGHT_BLOCKED: modal"))


class OrphanJobTests(unittest.TestCase):
    def test_long_reply_is_not_an_in_progress_status(self):
        self.assertTrue(_looks_in_progress("still working"))
        essay = "working " * 200
        self.assertFalse(_looks_in_progress(essay))

    def test_reply_sent_closes_a_running_job(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "hermes.google_chat_task",
                {"text": "write a 3000-word essay"},
            )
            store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
            settle_job_when_reply_sent(db, job["id"], "still working")
            self.assertEqual(store.get_job(job["id"])["status"], JobStatus.RUNNING.value)
            settle_job_when_reply_sent(db, job["id"], "working " * 80)
            self.assertEqual(store.get_job(job["id"])["status"], JobStatus.UNVERIFIED.value)

    def test_sweeper_fails_a_dead_running_job_and_keeps_a_heartbeat(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            dead = store.create_job(
                "ezlynx.policy_setup",
                {"text": "3000-word essay"},
            )
            store.transition(dead["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
            live = store.create_job(
                "hermes.google_chat_task",
                {"text": "still going"},
            )
            store.transition(live["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
            now = datetime.now(timezone.utc)
            store.heartbeat_generic_chat_job(live["id"], now=now)
            _backdate(db, dead["id"], 700)
            _backdate(db, live["id"], 700)
            with patch(
                "robie_job_engine.recording.RecordingManager.safe_stop",
                return_value=None,
            ) as stopped:
                failed = sweep_dead_running_jobs(store, older_than_seconds=600, now=now)
            self.assertIn(dead["id"], failed)
            self.assertNotIn(live["id"], failed)
            self.assertEqual(store.get_job(dead["id"])["status"], JobStatus.FAILED.value)
            self.assertEqual(store.get_job(live["id"])["status"], JobStatus.RUNNING.value)
            self.assertIsNotNone(store.get_checkpoint(dead["id"], "dead_running"))
            self.assertTrue(any(call.args[0] == dead["id"] for call in stopped.call_args_list))


if __name__ == "__main__":
    unittest.main()
