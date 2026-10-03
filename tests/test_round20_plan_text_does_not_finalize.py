"""Plan text that shares an assistant message with a tool call is not the reply.

(c2) on c6d5510 put "Plan for execution…" in the same message as playwright_exec.
The browser refusal left the job open, then that plan text was stored as the
reply and closed the job UNVERIFIED, so the note tool heard that the job
was stopped.
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_guard import guard_chat_response
from robie_job_engine.chat_job_controls import settle_job_when_reply_sent
from robie_job_engine.chat_turn_control import agent_output_blocked, clear_agent_stop
from robie_job_engine.models import JobStatus
from robie_job_engine.store import JobStore
from robie_job_engine.turn_finalization import (
    COULD_NOT_FINISH,
    begin_tool_call_message,
    clear_tool_call_text,
    end_tool_call_message,
    close_turn_after_visible_line,
    finalize_turn_if_still_open,
    note_assistant_text_has_tool_calls,
)
from robie_job_engine.write_verification_loop import lock_stated_plan
from test_round10_reply_lifecycle import SPACE, _adapter_module, _chat, _outbound_text
from test_tonight_fix_bundle import _load_hermes_tool, _restore_modules

PLAN = (
    "Plan for execution: open Buster Brown's overview page, then file the "
    "mailing-address request on the checkup discussion."
)
OVERVIEW = "https://app.ezlynx.com/web/account/220250093/overview"
TITLE = "Policy Change Request Checkup - Mailing Address update"
NOTE = "Please change the mailing address to 100 Test Mailing Rd."


def _address_job(store: JobStore) -> str:
    job = store.create_job(
        "ezlynx.policy_change",
        {
            "text": (
                "Change the mailing address for Buster Brown applicant 220250093 to "
                "100 Test Mailing Rd and note the request"
            ),
            "account_name": "Buster Brown",
            "applicant_id": "220250093",
            "conversation_id": SPACE,
            "requested_by": "Carlo",
        },
    )
    store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
    lock_stated_plan(
        store,
        job,
        {
            "write": "discussion note",
            "target": {"applicant_id": "220250093", "discussion": TITLE},
            "values": {"note_text": NOTE},
        },
    )
    return job["id"]


class PlanTextTests(unittest.TestCase):
    def test_plan_text_plus_refused_browser_then_note_stays_unverified(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _address_job(store)
            playwright, previous, created = _load_hermes_tool(
                "playwright_tool_round20", "playwright_tool.py"
            )
            note, note_previous, note_created = _load_hermes_tool(
                "ezlynx_note_tool_round20", "ezlynx_note_tool.py"
            )
            adapter = _adapter_module()
            chat = _chat(db)
            note_assistant_text_has_tool_calls(job_id, PLAN)
            begin_tool_call_message(job_id)
            try:
                with patch.object(adapter, "ROBIE_JOB_DB", db):
                    planned = asyncio.run(
                        chat.send(SPACE, PLAN, metadata={"robie_job_id": job_id})
                    )
                self.assertTrue(planned.success)
                self.assertIsNone(planned.message_id)
                self.assertEqual(guard_chat_response(db, job_id, PLAN), "")
                self.assertFalse(settle_job_when_reply_sent(db, job_id, PLAN))
                self.assertEqual(store.get_job(job_id)["status"], JobStatus.RUNNING.value)
                self.assertIsNone(store.get_checkpoint(job_id, "worker_response"))
                self.assertIsNone(store.get_checkpoint(job_id, "action"))
                end_tool_call_message(job_id)
                refused = playwright.playwright_exec(
                    f"page.goto('{OVERVIEW}')",
                    job_id=job_id,
                    db_path=db,
                )
                self.assertFalse(refused["ok"])
                with patch.object(adapter, "ROBIE_JOB_DB", db):
                    late_plan = asyncio.run(
                        chat.send(SPACE, PLAN, metadata={"robie_job_id": job_id})
                    )
                self.assertIsNone(late_plan.message_id)
                self.assertEqual(store.get_job(job_id)["status"], JobStatus.RUNNING.value)
                self.assertIsNone(agent_output_blocked(job_id, store))
                posted: list[str] = []
                report = {
                    "status": "filed",
                    "discussion_id": "d-mail",
                    "discussion_title": TITLE,
                    "note_id": "new-note",
                    "note_text": NOTE,
                    "applicant_id": "220250093",
                    "read_back": True,
                    "verified_by": "discussion",
                    "reason": "The note was added to the discussion.",
                    "wrote": True,
                }
                with patch.object(note, "_file_note", return_value=report):
                    result = note.ezlynx_discussion_note_handler(
                        {
                            "applicant_id": "220250093",
                            "note_text": NOTE,
                            "title_hint": TITLE,
                        },
                        job_id=job_id,
                        db_path=db,
                        outcome_poster=lambda *_args: posted.append(_args[1]),
                    )
                self.assertTrue(result["ok"])
                self.assertNotIn("This job was stopped", str(result))
                saved = store.get_checkpoint(job_id, "discussion_note")
                self.assertEqual(saved["status"], "filed")
                self.assertEqual(saved["note_id"], "new-note")
                self.assertEqual(store.get_job(job_id)["status"], JobStatus.UNVERIFIED.value)
                self.assertTrue(posted)
                self.assertIn("I can't change the address myself yet", posted[0])
                self.assertNotIn("Plan for execution", " ".join(posted))
                self.assertEqual(chat._chat_api.messages.calls, [])
            finally:
                clear_agent_stop(job_id)
                clear_tool_call_text(job_id)
                _restore_modules(previous, created)
                _restore_modules(note_previous, note_created)

    def test_plan_text_plus_an_allowed_tool_does_not_finalize(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "hermes.google_chat_task",
                {"text": "Look up the carriers we quote", "conversation_id": SPACE},
            )
            job_id = job["id"]
            store.transition(job_id, JobStatus.RUNNING, expected={JobStatus.PENDING})
            adapter = _adapter_module()
            chat = _chat(db)
            note_assistant_text_has_tool_calls(job_id, PLAN)
            begin_tool_call_message(job_id)
            try:
                with patch.object(adapter, "ROBIE_JOB_DB", db):
                    sent = asyncio.run(
                        chat.send(SPACE, PLAN, metadata={"robie_job_id": job_id})
                    )
                self.assertIsNone(sent.message_id)
                self.assertEqual(guard_chat_response(db, job_id, PLAN), "")
                end_tool_call_message(job_id)
                self.assertEqual(guard_chat_response(db, job_id, PLAN), "")
                self.assertFalse(settle_job_when_reply_sent(db, job_id, PLAN))
                self.assertEqual(store.get_job(job_id)["status"], JobStatus.RUNNING.value)
                self.assertIsNone(store.get_checkpoint(job_id, "worker_response"))
                self.assertIsNone(agent_output_blocked(job_id, store))
            finally:
                clear_tool_call_text(job_id)

    def test_a_silent_turn_records_the_fallback_line(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "hermes.google_chat_task",
                {"text": "Do the thing", "conversation_id": SPACE},
            )
            job_id = job["id"]
            store.transition(job_id, JobStatus.RUNNING, expected={JobStatus.PENDING})
            try:
                line = finalize_turn_if_still_open(db, job_id)
                self.assertEqual(line, COULD_NOT_FINISH)
                self.assertEqual(
                    store.get_job(job_id)["status"], JobStatus.RUNNING.value
                )
                close_turn_after_visible_line(db, job_id, line)
                saved = store.get_checkpoint(job_id, "worker_response")
                self.assertIn(COULD_NOT_FINISH, str(saved.get("response_text") or ""))
                self.assertEqual(store.get_job(job_id)["status"], JobStatus.UNVERIFIED.value)
                self.assertIsNone(finalize_turn_if_still_open(db, job_id))
            finally:
                clear_agent_stop(job_id)


class ThinkingCardTests(unittest.TestCase):
    def test_a_consumed_thinking_card_does_not_block_the_next_turn(self):
        adapter = _adapter_module()
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            chat = _chat(db)
            chat.config = type("Cfg", (), {"typing_status_text": "Robie is thinking…"})()
            chat._typing_card_inflight = {}
            chat._orphan_typing_messages = {}
            chat._typing_hold = {}
            chat._mark_typing_card_consumed(SPACE)
            with patch.object(adapter, "ROBIE_JOB_DB", db):
                asyncio.run(chat.send_typing(SPACE))
            self.assertEqual(
                chat._typing_messages.get(SPACE), adapter._TYPING_CONSUMED_SENTINEL
            )
            self.assertEqual(_outbound_text(chat), [])
            chat._allow_next_thinking_card(SPACE)
            with patch.object(adapter, "ROBIE_JOB_DB", db):
                asyncio.run(chat.send_typing(SPACE))
            self.assertNotEqual(
                chat._typing_messages.get(SPACE), adapter._TYPING_CONSUMED_SENTINEL
            )
            posted = _outbound_text(chat)
            self.assertTrue(posted)
            self.assertTrue(posted[0].casefold().startswith("robie is"))
