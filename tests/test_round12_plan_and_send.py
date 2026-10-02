"""Round 12: a refused plan must not end the job or let a late write through.

The live sequence is: the first plan is refused, the job used to go
terminal, and the model kept calling the note tool. These tests drive
that handler, then the Chat send, not the helpers on their own.
"""

from __future__ import annotations

import asyncio
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_guard import open_chat_job, reopen_resumed_generic_chat_job
from robie_job_engine.chat_job_controls import settle_job_when_reply_sent
from robie_job_engine.chat_thread import bind_job_chat_thread
from robie_job_engine.models import JobStatus
from robie_job_engine.recording import RecordingManager
from robie_job_engine.store import JobStore
from robie_job_engine.write_verification_loop import (
    plan_refusal,
    refuse_tool_write,
)
from test_playwright_artifact_fail_closed import (
    _install_hermes_registry_stub,
    _restore_modules,
)
from test_round10_reply_lifecycle import SPACE, THREAD, _adapter_module, _chat

ROOT = Path(__file__).resolve().parents[1]
NOTE_TOOL = ROOT / "deploy" / "hermes" / "tools" / "ezlynx_note_tool.py"

BAD_PLAN = {"write": "", "target": "", "values": ""}
NOTE = "Follow up on the claim"
MODEL_PROSE = (
    "The note has been filed on the existing discussion.\n"
    "Status: Verified posted in EZLynx."
)


def _load_note_tool():
    previous = _install_hermes_registry_stub()
    spec = importlib.util.spec_from_file_location("robie_note_tool_round12", NOTE_TOOL)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    try:
        spec.loader.exec_module(module)
    except Exception:
        _restore_modules(previous)
        raise
    return module, previous


def _running_note_job(store: JobStore) -> str:
    job = store.create_job(
        "ezlynx.discussion_note",
        {
            "text": "add a note to Buster Brown on follw up 1 saying Follow up",
            "account_name": "Buster Brown",
            "conversation_id": SPACE,
        },
    )
    store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
    bind_job_chat_thread(store, job["id"], THREAD)
    return job["id"]


class PlanSchemaTests(unittest.TestCase):
    def test_plain_note_plan_locks_when_write_is_blank_and_values_is_a_string(self):
        schema = (NOTE_TOOL).read_text(encoding="utf-8")
        self.assertIn('"note_text"', schema)
        self.assertIn("Do not send write as an object or values as a string.", schema)
        route = (
            ROOT / "robie_job_engine" / "answer_only.py"
        ).read_text(encoding="utf-8")
        self.assertIn("note_text is the exact note", route)
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _running_note_job(store)
            refused = refuse_tool_write(
                {
                    "applicant_id": "220250093",
                    "note_text": NOTE,
                    "title_hint": "follw up 1",
                    "plan": {
                        "write": "",
                        "target": "follw up 1",
                        "values": NOTE,
                    },
                },
                {"job_id": job_id, "db_path": db},
            )
            self.assertIsNone(refused)
            locked = store.get_checkpoint(job_id, "write_plan")
            self.assertEqual(locked["write"], "discussion note")
            self.assertEqual(locked["target"]["discussion"], "follw up 1")
            self.assertNotIn("policy_number", locked["target"])
            self.assertEqual(locked["values"]["note_text"], NOTE)
            self.assertEqual(store.get_job(job_id)["status"], JobStatus.RUNNING.value)

    def test_plan_refusal_text_does_not_end_the_job(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _running_note_job(store)
            self.assertFalse(
                settle_job_when_reply_sent(db, job_id, plan_refusal("write"))
            )
            self.assertEqual(store.get_job(job_id)["status"], JobStatus.RUNNING.value)


class RefusedPlanSequenceTests(unittest.TestCase):
    def test_refused_plan_keeps_the_job_alive_and_a_terminal_retry_does_not_write(self):
        tool, previous = _load_note_tool()
        try:
            with durable_temporary_directory() as tmp:
                db = str(Path(tmp) / "jobs.db")
                store = JobStore(db)
                job_id = _running_note_job(store)
                with patch(
                    "robie_job_engine.ezlynx_api_only_writes.add_note_to_discussion",
                    side_effect=AssertionError("refused plan must not touch EZLynx"),
                ) as mocked:
                    refused = tool.ezlynx_discussion_note_handler(
                        {
                            "applicant_id": "220250093",
                            "note_text": NOTE,
                            "plan": dict(BAD_PLAN),
                        },
                        job_id=job_id,
                        db_path=db,
                    )
                    self.assertFalse(refused["ok"])
                    self.assertIn("The plan field write is wrong", refused["error"])
                    self.assertEqual(
                        store.get_job(job_id)["status"], JobStatus.RUNNING.value
                    )
                    store.transition(
                        job_id,
                        JobStatus.UNVERIFIED,
                        expected={JobStatus.RUNNING},
                        error="reply sent",
                        release_lease=True,
                    )
                    again = tool.ezlynx_discussion_note_handler(
                        {
                            "applicant_id": "220250093",
                            "note_text": NOTE,
                            "title_hint": "follw up 1",
                            "plan": {
                                "write": "discussion note",
                                "target": {"discussion": "follw up 1"},
                                "values": {"note_text": NOTE},
                            },
                        },
                        job_id=job_id,
                        db_path=db,
                    )
                self.assertFalse(again["ok"])
                self.assertIn("already finished", again["error"])
                mocked.assert_not_called()
                self.assertEqual(
                    store.get_job(job_id)["status"], JobStatus.UNVERIFIED.value
                )
        finally:
            _restore_modules(previous)

    def test_refused_plan_then_a_confirmed_note_posts_one_line_on_the_thread(self):
        tool, previous = _load_note_tool()
        try:
            with durable_temporary_directory() as tmp:
                db = str(Path(tmp) / "jobs.db")
                store = JobStore(db)
                job_id = _running_note_job(store)
                posted: list[tuple] = []

                def poster(space, text, thread, posted_job):
                    posted.append((space, text, thread, posted_job))

                filed = {
                    "status": "filed",
                    "note_id": "note-99",
                    "discussion_id": "disc-1",
                    "discussion_title": "follw up 1",
                    "read_back": True,
                    "applicant_id": "220250093",
                }
                with patch(
                    "robie_job_engine.ezlynx_api_only_writes.add_note_to_discussion",
                    return_value=filed,
                ) as mocked, patch(
                    "robie_job_engine.write_verification_loop._score_with_jev",
                    return_value=type(
                        "Decision",
                        (),
                        {"verdict": "yes", "confidence": 1, "reason": "ok"},
                    )(),
                ):
                    refused = tool.ezlynx_discussion_note_handler(
                        {
                            "applicant_id": "220250093",
                            "note_text": NOTE,
                            "plan": dict(BAD_PLAN),
                        },
                        job_id=job_id,
                        db_path=db,
                        outcome_poster=poster,
                    )
                    filed_result = tool.ezlynx_discussion_note_handler(
                        {
                            "applicant_id": "220250093",
                            "note_text": NOTE,
                            "title_hint": "follw up 1",
                            "plan": {
                                "write": "discussion note",
                                "target": {"discussion": "follw up 1"},
                                "values": {"note_text": NOTE},
                            },
                        },
                        job_id=job_id,
                        db_path=db,
                        outcome_poster=poster,
                    )
                self.assertFalse(refused["ok"])
                self.assertEqual(mocked.call_count, 1)
                self.assertTrue(filed_result.get("read_back"))
                self.assertEqual(store.get_job(job_id)["status"], JobStatus.COMPLETE.value)
                self.assertEqual(len(posted), 1)
                space, text, thread, posted_job = posted[0]
                self.assertEqual(space, SPACE)
                self.assertEqual(thread, THREAD)
                self.assertEqual(posted_job, job_id)
                self.assertEqual(text, 'Added the note to Buster Brown on "follw up 1".')
                self.assertNotIn("\n", text)
                adapter = _adapter_module()
                chat = _chat(db)
                with patch.object(adapter, "ROBIE_JOB_DB", db):
                    dropped = asyncio.run(
                        chat.send(SPACE, MODEL_PROSE, metadata={"thread_id": "spaces/ROBY/threads/top-level-auto"})
                    )
                self.assertTrue(dropped.success)
                self.assertEqual(chat._chat_api.messages.calls, [])
        finally:
            _restore_modules(previous)

    def test_ledger_block_after_a_refused_plan_posts_only_the_question(self):
        tool, previous = _load_note_tool()
        try:
            question = "I already added that note at 3:16 PM ET. Want me to add it again?"
            with durable_temporary_directory() as tmp:
                db = str(Path(tmp) / "jobs.db")
                store = JobStore(db)
                job_id = _running_note_job(store)
                posted: list[tuple] = []

                def poster(space, text, thread, posted_job):
                    posted.append((space, text, thread, posted_job))

                with patch(
                    "robie_job_engine.ezlynx_api_only_writes.add_note_to_discussion",
                    return_value={
                        "status": "already_posted",
                        "note_id": "note-1",
                        "discussion_id": "disc-1",
                        "discussion_title": "follw up 1",
                        "reason": question,
                    },
                ) as mocked:
                    refused = tool.ezlynx_discussion_note_handler(
                        {
                            "applicant_id": "220250093",
                            "note_text": NOTE,
                            "plan": dict(BAD_PLAN),
                        },
                        job_id=job_id,
                        db_path=db,
                        outcome_poster=poster,
                    )
                    blocked = tool.ezlynx_discussion_note_handler(
                        {
                            "applicant_id": "220250093",
                            "note_text": NOTE,
                            "title_hint": "follw up 1",
                            "plan": {
                                "write": "discussion note",
                                "target": {"discussion": "follw up 1"},
                                "values": {"note_text": NOTE},
                            },
                        },
                        job_id=job_id,
                        db_path=db,
                        outcome_poster=poster,
                    )
                self.assertFalse(refused["ok"])
                self.assertIn("plan field", refused["error"])
                self.assertEqual(mocked.call_count, 1)
                self.assertEqual(blocked.get("status"), "already_posted")
                self.assertEqual(
                    store.get_job(job_id)["status"],
                    JobStatus.NEEDS_CLARIFICATION.value,
                )
                self.assertEqual(len(posted), 1)
                space, text, thread, posted_job = posted[0]
                self.assertEqual((space, text, thread, posted_job), (SPACE, question, THREAD, job_id))
                self.assertNotIn("filed on the existing", text)
                adapter = _adapter_module()
                chat = _chat(db)
                with patch.object(adapter, "ROBIE_JOB_DB", db):
                    dropped = asyncio.run(chat.send(SPACE, MODEL_PROSE))
                self.assertTrue(dropped.success)
                self.assertEqual(chat._chat_api.messages.calls, [])
        finally:
            _restore_modules(previous)

    def test_send_with_no_job_id_is_dropped(self):
        adapter = _adapter_module()
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            chat = _chat(db)
            with patch.object(adapter, "ROBIE_JOB_DB", db):
                result = asyncio.run(chat.send(SPACE, MODEL_PROSE))
            self.assertTrue(result.success)
            self.assertEqual(chat._chat_api.messages.calls, [])

    def test_question_only_answer_uses_the_stored_thread(self):
        adapter = _adapter_module()
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            with patch.object(
                RecordingManager, "safe_start", side_effect=AssertionError("recording")
            ):
                job_id = open_chat_job(
                    db,
                    "spaces/ROBY/messages/q",
                    "which carriers do we quote for auto?",
                    conversation_id=SPACE,
                )
            bind_job_chat_thread(store, job_id, THREAD)
            chat = _chat(db)
            chat._question_job_by_chat = {SPACE: job_id}
            with patch.object(adapter, "ROBIE_JOB_DB", db):
                result = asyncio.run(
                    chat.send(
                        SPACE,
                        "Travelers and Progressive.",
                        metadata={"thread_id": "spaces/ROBY/threads/top-level-auto"},
                    )
                )
            self.assertTrue(result.success)
            self.assertEqual(len(chat._chat_api.messages.calls), 1)
            body = chat._chat_api.messages.calls[0]["body"]
            self.assertEqual(body["thread"]["name"], THREAD)
            self.assertIn("Travelers", body["text"])


class QuestionResumeTests(unittest.TestCase):
    def test_in_thread_answer_does_not_record_after_the_exemption_is_overwritten(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            with patch.object(
                RecordingManager, "safe_start", side_effect=AssertionError("recording")
            ):
                job_id = open_chat_job(
                    db,
                    "spaces/ROBY/messages/q",
                    "which carriers do we quote for auto?",
                    conversation_id=SPACE,
                )
                store.transition(
                    job_id,
                    JobStatus.NEEDS_CLARIFICATION,
                    expected={JobStatus.RUNNING},
                    resume_status=JobStatus.RUNNING,
                    release_lease=True,
                )
                bind_job_chat_thread(store, job_id, THREAD)
                store.checkpoint(
                    job_id,
                    "recording_exemption",
                    {"reason": "action is not registered as an executable Skill"},
                )
                payload = dict(store.get_job(job_id)["payload"])
                payload["answer_only"] = False
                payload["text"] = "Travelers"
                payload["request_text"] = "Travelers"
                payload["original_text"] = "Travelers"
                store.update_payload(job_id, payload)
                resumed = open_chat_job(
                    db,
                    "spaces/ROBY/messages/q2",
                    "Travelers",
                    conversation_id=SPACE,
                    inbound_thread_id=THREAD,
                )
                task = store.create_job(
                    "hermes.google_chat_task",
                    {
                        "text": "Travelers",
                        "request_text": "Travelers",
                        "original_text": "Travelers",
                        "answer_only": False,
                        "conversation_id": SPACE,
                    },
                )
                store.transition(
                    task["id"], JobStatus.RUNNING, expected={JobStatus.PENDING}
                )
                store.checkpoint(
                    task["id"], "question_only", {"reason": "question only"}
                )
                store.checkpoint(
                    task["id"],
                    "recording_exemption",
                    {"reason": "action is not registered as an executable Skill"},
                )
                reopen_resumed_generic_chat_job(db, task["id"])
            self.assertEqual(resumed, job_id)
            self.assertTrue(store.get_checkpoint(job_id, "question_only"))


if __name__ == "__main__":
    unittest.main()
