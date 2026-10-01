"""A lookup stays open through a clarify, then posts the answer.

Job d03301f3 closed UNVERIFIED on a heartbeat line while the agent was
still running. Job a78ae34c posted a clarify that still contained
ROBIE_BLOCKED. A dashboard search was refused as a write.
"""

from __future__ import annotations

import os
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_guard import (
    _GENERIC_CHAT_HEARTBEATS,
    start_generic_chat_job_heartbeat,
    stop_generic_chat_job_heartbeat,
)
from robie_job_engine.chat_turn_control import (
    agent_output_blocked,
    clear_agent_stop,
    register_chat_adapter,
)
from robie_job_engine.models import JobStatus
from robie_job_engine.playwright_write_guard import install_playwright_write_guards
from robie_job_engine.store import JobStore
from robie_job_engine.user_reply import SIGN_IN_QUESTION, format_user_reply
from test_round10_reply_lifecycle import SPACE, _adapter_module, _chat, _outbound_text

ASK = "whats the GL policy number and carrier for buster brown"
HEARTBEAT = "⏳ Working — 6 min — iteration 21/500, clarify"
SIGN_IN = (
    "ROBIE_BLOCKED: MISSING_REQUIRED_FIELD: please sign in to EZLynx"
)
SIGN_IN_ASK = "Please sign in to EZLynx, then tell me to continue?"
DISCUSSIONS = (
    "ROBIE_BLOCKED: MISSING_REQUIRED_FIELD: which of the 62 'Renewal' discussions"
)
ANSWER = "Buster Brown's GL policy number is GL-100 with Harbor."


class _Lease:
    def __init__(self) -> None:
        self.released = False
        self._stop = threading.Event()
        self.beats = 0

    def run(self) -> None:
        while not self._stop.wait(0.02):
            self.beats += 1

    def release(self) -> None:
        self.released = True
        self._stop.set()


class _Box:
    def __init__(self, selector: str, page) -> None:
        self.selector = selector
        self.page = page
        self.fills: list[str] = []

    def count(self) -> int:
        return 1

    def fill(self, value: str) -> None:
        self.fills.append(value)


class _Page:
    def __init__(self, url: str) -> None:
        self.url = url


class LookupResumeTests(unittest.TestCase):
    def test_clarify_resume_posts_the_final_answer(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "hermes.google_chat_task",
                {"text": ASK, "conversation_id": SPACE, "requested_by": "Carlo"},
            )
            job_id = job["id"]
            store.transition(job_id, JobStatus.RUNNING, expected={JobStatus.PENDING})
            store.checkpoint(
                job_id,
                "action",
                {"destination": {"applicant_id": "26356199"}},
            )
            session_key = "agent:main:google_chat:dm:spaces/ROBY"
            lease = _Lease()
            holder = threading.Thread(target=lease.run, daemon=True)
            holder.start()

            class Runner:
                def __init__(self) -> None:
                    self._active_session_leases = {session_key: lease}

            class Held:
                def __init__(self) -> None:
                    self.gateway_runner = Runner()
                    self._gateway_turns = {
                        (SPACE, "threads/job"): {
                            "job_id": job_id,
                            "task": None,
                            "watchdog": None,
                        }
                    }
                    self._active_chat_job = {SPACE: job_id}
                    self._session_tasks = {}

            held = Held()
            register_chat_adapter(held)
            adapter = _adapter_module()
            chat = _chat(db)
            engine_calls: list[str] = []

            def _refuse_reread(self, rerun_id):
                engine_calls.append(str(rerun_id))
                raise AssertionError("write re-read")

            try:
                start_generic_chat_job_heartbeat(db, job_id, interval_seconds=0.05)
                with patch.object(adapter, "ROBIE_JOB_DB", db), patch(
                    "robie_job_engine.engine.JobEngine.run", _refuse_reread
                ):
                    import asyncio

                    beat = asyncio.run(
                        chat.send(SPACE, HEARTBEAT, metadata={"robie_job_id": job_id})
                    )
                    self.assertIsNone(beat.message_id)
                    self.assertEqual(
                        store.get_job(job_id)["status"], JobStatus.RUNNING.value
                    )
                    self.assertIsNone(agent_output_blocked(job_id, store))
                    self.assertFalse(lease.released)
                    asked = asyncio.run(
                        chat.send(
                            SPACE, SIGN_IN_ASK, metadata={"robie_job_id": job_id}
                        )
                    )
                    self.assertTrue(asked.success)
                    posted = " ".join(_outbound_text(chat))
                    self.assertIn("sign in to EZLynx", posted)
                    self.assertNotIn("ROBIE_BLOCKED", posted)
                    self.assertNotIn("MISSING_REQUIRED_FIELD", posted)
                    self.assertEqual(
                        store.get_job(job_id)["status"],
                        JobStatus.NEEDS_CLARIFICATION.value,
                    )
                    store.transition(
                        job_id,
                        JobStatus.RUNNING,
                        expected={JobStatus.NEEDS_CLARIFICATION},
                    )
                    clear_agent_stop(job_id)
                    start_generic_chat_job_heartbeat(db, job_id, interval_seconds=0.05)
                    again = asyncio.run(
                        chat.send(SPACE, HEARTBEAT, metadata={"robie_job_id": job_id})
                    )
                    self.assertIsNone(again.message_id)
                    self.assertEqual(
                        store.get_job(job_id)["status"], JobStatus.RUNNING.value
                    )
                    self.assertIsNone(agent_output_blocked(job_id, store))
                    from robie_job_engine.playwright_observability import (
                        record_playwright_exec,
                    )

                    payload = dict(store.get_job(job_id).get("payload") or {})
                    payload["applicant_id"] = "26356199"
                    store.update_payload(job_id, payload)
                    store.checkpoint(
                        job_id,
                        "client_name_search",
                        {
                            "resolved": True,
                            "source": "search",
                            "applicant_ids": ["26356199"],
                            "user_line": "",
                        },
                    )
                    record_playwright_exec(
                        "page.goto('https://app.ezlynx.com/web/account/26356199/policies')",
                        {
                            "url": "https://app.ezlynx.com/web/account/26356199/policies"
                        },
                        job_id=job_id,
                        db_path=db,
                        status="ok",
                    )
                    final = asyncio.run(
                        chat.send(SPACE, ANSWER, metadata={"robie_job_id": job_id})
                    )
                self.assertTrue(final.success)
                self.assertIsNotNone(final.message_id)
                posted = "\n".join(_outbound_text(chat))
                self.assertIn("GL-100", posted)
                self.assertIn("Harbor", posted)
                self.assertNotIn("no policy number", posted.casefold())
                self.assertNotIn("This job was stopped", posted)
                closed = store.get_job(job_id)
                self.assertEqual(closed["status"], JobStatus.COMPLETE.value)
                self.assertNotIn(
                    "policy number",
                    str(closed.get("last_error") or "").casefold(),
                )
                self.assertEqual(engine_calls, [])
                self.assertTrue(lease.released)
                self.assertFalse(
                    any(key[1] == job_id for key in _GENERIC_CHAT_HEARTBEATS)
                )
            finally:
                clear_agent_stop(job_id)
                stop_generic_chat_job_heartbeat(db, job_id)
                lease.release()
                holder.join(timeout=1)


class DashboardSearchTests(unittest.TestCase):
    def test_search_on_the_dashboard_is_a_read(self):
        dashboard = _Page("https://app.ezlynx.com/")
        search = _Box("#search", dashboard)
        placeholder = _Box("input[placeholder='Search applicants']", dashboard)

        class Locator:
            fill = _Box.fill

        install_playwright_write_guards({"Locator": Locator})
        env = os.environ.copy()
        env.pop("ROBIE_EZLYNX_WRITE_APPLICANT_ID", None)
        with patch.dict(os.environ, env, clear=True):
            Locator.fill(search, "buster brown")
            Locator.fill(placeholder, "buster brown")
        self.assertEqual(search.fills, ["buster brown"])
        self.assertEqual(placeholder.fills, ["buster brown"])
        form = _Box("#effectiveDate", dashboard)
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(RuntimeError, "EZLYNX_WRITE_SCOPE_REFUSED"):
                Locator.fill(form, "2026-10-01")
        self.assertEqual(form.fills, [])


class ClarifyFilterTests(unittest.TestCase):
    def test_clarify_and_sign_in_are_one_plain_question(self):
        with self.assertLogs("robie.health", level="INFO") as logs:
            question = format_user_reply(DISCUSSIONS)
            sign_in = format_user_reply(SIGN_IN)
        self.assertEqual(question, "Which of the 62 'Renewal' discussions?")
        self.assertEqual(sign_in, SIGN_IN_QUESTION)
        for text in (question, sign_in):
            self.assertNotIn("ROBIE_BLOCKED", text)
            self.assertNotIn("MISSING_REQUIRED_FIELD", text)
            self.assertTrue(text.endswith("?"))
        self.assertIn("MISSING_REQUIRED_FIELD", "\n".join(logs.output))
        adapter = _adapter_module()
        card = adapter._widget_to_chat({"type": "text", "text": DISCUSSIONS})
        shown = card["textParagraph"]["text"]
        self.assertNotIn("ROBIE_BLOCKED", shown)
        self.assertNotIn("MISSING_REQUIRED_FIELD", shown)
        self.assertIn("Renewal", shown)
        self.assertEqual(
            format_user_reply("ROBIE_BLOCKED: MISSING_REQUIRED_FIELD: task details"),
            "I need the task details before I can do that.",
        )


if __name__ == "__main__":
    unittest.main()
