"""Name lookup is a read, codes stay out of Chat, and a stop stays in-thread."""

from __future__ import annotations

import asyncio
import logging
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_thread import bind_job_chat_thread
from robie_job_engine.chat_turn_control import (
    ALREADY_FINISHED_REPLY,
    NOTHING_RUNNING_REPLY,
    clear_agent_stop,
)
from robie_job_engine.client_name_lookup import (
    bind_named_client,
    client_name_from_lookup,
)
from robie_job_engine.models import JobStatus
from robie_job_engine.playwright_write_guard import install_playwright_write_guards
from robie_job_engine.store import JobStore
from robie_job_engine.user_reply import STUCK_LINE, format_user_reply
from test_round10_reply_lifecycle import (
    FRESH,
    SPACE,
    THREAD,
    _Event,
    _adapter_module,
    _chat,
    _outbound_text,
)

ASK = "whats the GL policy number and carrier for buster brown"
LEAK = (
    "ROBIE_BLOCKED: PLAYWRIGHT_BLOCKED: EZLYNX_WRITE_SCOPE_REFUSED: "
    "bound Job has no explicit applicant_id input#applicantSearch"
)


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


class NameLookupTests(unittest.TestCase):
    def test_name_lookup_searches_binds_and_answers_without_scope_refusal(self):
        name = client_name_from_lookup(ASK)
        self.assertEqual(name, "buster brown")
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "hermes.google_chat_task",
                {"text": ASK, "conversation_id": SPACE, "requested_by": "Carlo"},
            )
            self.assertFalse(str((job.get("payload") or {}).get("applicant_id") or ""))
            home = _Page("https://app.ezlynx.com/web/home")
            search = _Box("input#applicantSearch", home)

            class Locator:
                fill = _Box.fill

            scope = {"Locator": Locator}
            install_playwright_write_guards(scope)
            env = os.environ.copy()
            env.pop("ROBIE_EZLYNX_WRITE_APPLICANT_ID", None)
            with patch.dict(os.environ, env, clear=True):
                Locator.fill(search, name)
            self.assertEqual(search.fills, [name])
            answer = bind_named_client(
                store,
                job["id"],
                name,
                [
                    {
                        "applicant_id": "88001122",
                        "name": "Buster Brown",
                        "policy_number": "GL-100",
                        "carrier": "Harbor",
                        "line": "GL",
                    }
                ],
            )
            bound = store.get_job(job["id"])["payload"]["applicant_id"]
            self.assertEqual(bound, "88001122")
            self.assertIn("GL-100", answer)
            self.assertIn("Harbor", answer)
            self.assertNotIn("EZLYNX_WRITE_SCOPE_REFUSED", answer)
            self.assertNotIn("applicant_id", answer)
            self.assertNotIn("ROBIE_BLOCKED", answer)
            account = _Page("https://app.ezlynx.com/web/account/88001122/policies")
            write = _Box("#effectiveDate", account)
            with patch.dict(os.environ, env, clear=True):
                with self.assertRaisesRegex(RuntimeError, "EZLYNX_WRITE_SCOPE_REFUSED"):
                    Locator.fill(write, "2026-10-01")
            self.assertEqual(write.fills, [])
            question = bind_named_client(
                store,
                job["id"],
                name,
                [
                    {"applicant_id": "88001122", "name": "Buster Brown"},
                    {"applicant_id": "88001123", "name": "Buster Brown"},
                ],
            )
            self.assertIn("I found more than one Buster Brown.", question)
            self.assertIn("account 88001122", question)
            self.assertIn("account 88001123", question)
            self.assertTrue(question.endswith("Which one should I use?"))
            self.assertNotIn("applicant_id", question)
            self.assertNotIn("ROBIE_BLOCKED", question)


class OutboundCodeTests(unittest.TestCase):
    def test_internal_codes_and_selectors_become_one_plain_line(self):
        with self.assertLogs("robie.health", level="INFO") as logs:
            cleaned = format_user_reply(LEAK)
        self.assertEqual(cleaned, STUCK_LINE)
        for banned in (
            "ROBIE_BLOCKED",
            "PLAYWRIGHT_BLOCKED",
            "EZLYNX_WRITE_SCOPE_REFUSED",
            "applicant_id",
            "applicantSearch",
            "#",
        ):
            self.assertNotIn(banned, cleaned)
        self.assertTrue(any("EZLYNX_WRITE_SCOPE_REFUSED" in line for line in logs.output))
        self.assertTrue(any("applicantSearch" in line for line in logs.output))
        self.assertEqual(
            format_user_reply("Which Buster Brown did you mean?"),
            "Which Buster Brown did you mean?",
        )
        mixed = format_user_reply(
            "Which Buster Brown did you mean?\n" + LEAK
        )
        self.assertEqual(mixed, "Which Buster Brown did you mean?")
        self.assertNotIn("PLAYWRIGHT", mixed)
        stuck = format_user_reply(
            "I got stuck (EZLYNX_WRITE_SCOPE_REFUSED: bound Job has no explicit "
            "applicant_id) and I will not guess the next click.\n"
            "Reply in this Chat thread with RETRY after the page is corrected."
        )
        self.assertNotIn("EZLYNX_", stuck)
        self.assertNotIn("applicant_id", stuck)
        self.assertNotIn("ROBIE_", stuck)


class StopPlacementTests(unittest.TestCase):
    def test_top_level_stop_posts_into_the_running_jobs_thread(self):
        adapter = _adapter_module()
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "hermes.google_chat_task",
                {
                    "text": "Write a short essay about a certificate of insurance.",
                    "conversation_id": SPACE,
                    "requested_by": "Carlo",
                },
            )
            store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
            bind_job_chat_thread(store, job["id"], THREAD)
            chat = _chat(db)
            chat._gateway_turns[(SPACE, "")] = {"job_id": job["id"], "task": None}

            async def _noop(*args, **kwargs):
                del args, kwargs

            chat._terminate_running_agent = _noop
            try:
                with patch.object(adapter, "ROBIE_JOB_DB", db):
                    asyncio.run(chat._apply_chat_stop(_Event(FRESH, "@Robie /stop")))
            finally:
                clear_agent_stop(job["id"])
            self.assertEqual(
                store.get_job(job["id"])["status"],
                JobStatus.CANCELLED.value,
            )
            self.assertEqual(_outbound_text(chat), ["Stopped. That job is cancelled."])
            posted = chat._chat_api.messages.calls[0]
            self.assertEqual(
                ((posted.get("body") or {}).get("thread") or {}).get("name"),
                THREAD,
            )
            self.assertNotEqual(
                ((posted.get("body") or {}).get("thread") or {}).get("name"),
                FRESH,
            )
            self.assertNotIn(ALREADY_FINISHED_REPLY, _outbound_text(chat))

    def test_idle_stop_logs_one_info_line(self):
        adapter = _adapter_module()
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            chat = _chat(db)
            records: list[str] = []
            handler = logging.Handler()
            handler.setLevel(logging.INFO)
            handler.emit = lambda record: records.append(record.getMessage())
            logger = logging.getLogger("gateway.platforms.google_chat")
            previous = logger.level
            logger.setLevel(logging.INFO)
            logger.addHandler(handler)
            try:
                with patch.object(adapter, "ROBIE_JOB_DB", db):
                    asyncio.run(chat._apply_chat_stop(_Event(None, "@Robie /stop")))
            finally:
                logger.removeHandler(handler)
                logger.setLevel(previous)
            self.assertEqual(_outbound_text(chat), [NOTHING_RUNNING_REPLY])
            self.assertTrue(
                any("idle stop" in line and NOTHING_RUNNING_REPLY in line for line in records)
            )
