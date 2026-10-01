"""A refused browser open is not the reply, and a Chat 429 is recorded.

(c2) opened Buster Brown's overview page with playwright_exec. The guard
refused that read-only navigation. The refusal was stored as the reply and
as an EZLynx write, the job closed, and the note tool then heard that the
job was stopped. The progress line must not reach Chat. A 429 retries, and
the reply is recorded if Chat still will not take it. A stop on one job
does not swallow another job's send.
"""

from __future__ import annotations

import asyncio
import logging
import unittest
from pathlib import Path
from unittest.mock import patch

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_guard import guard_chat_response
from robie_job_engine.chat_turn_control import (
    HAND_DRIVEN_EZLYNX_STOP,
    agent_output_blocked,
    clear_agent_stop,
    request_agent_stop,
)
from robie_job_engine.models import JobStatus
from robie_job_engine.store import JobStore
from robie_job_engine.write_verification_loop import lock_stated_plan
from test_round10_reply_lifecycle import SPACE, THREAD, _adapter_module, _chat
from test_tonight_fix_bundle import _load_hermes_tool, _restore_modules

OVERVIEW = "https://app.ezlynx.com/web/account/220250093/overview"
HONEST = (
    "I noted the request on Policy Change Request Checkup - Mailing Address update; "
    "I can't change the address myself yet, so a CSR needs to make it."
)
TITLE = "Policy Change Request Checkup - Mailing Address update"
NOTE = "Please change the mailing address to 100 Test Mailing Rd."


class _Quota(Exception):
    def __init__(self) -> None:
        super().__init__("Quota limit exceeded")
        self.resp = type("Resp", (), {"status": 429})()


class _FlakyMessages:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.fails_left = 0

    def create(self, **kwargs):
        self.calls.append(kwargs)
        parent = kwargs.get("parent")
        fails = self

        class _Exec:
            def execute(self, http=None):
                del http
                if parent == "spaces/HEALTH":
                    return {"name": "spaces/HEALTH/messages/1"}
                if fails.fails_left > 0:
                    fails.fails_left -= 1
                    raise _Quota()
                body = kwargs.get("body") or {}
                thread = (body.get("thread") or {}).get("name") or THREAD
                return {"name": "spaces/ROBY/messages/out", "thread": {"name": thread}}

        return _Exec()


def _running_address_job(store: JobStore) -> str:
    job = store.create_job(
        "ezlynx.policy_change",
        {
            "text": (
                "Change the mailing address for Buster Brown to "
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
            "target": {
                "applicant_id": "220250093",
                "discussion": TITLE,
            },
            "values": {"note_text": NOTE},
        },
    )
    return job["id"]


def _texts(messages: _FlakyMessages) -> list[str]:
    return [str((call.get("body") or {}).get("text") or "") for call in messages.calls]


class RefusedNavigationTests(unittest.TestCase):
    def test_refused_overview_open_then_note_stays_unverified(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _running_address_job(store)
            playwright, previous, created = _load_hermes_tool(
                "playwright_tool_round19", "playwright_tool.py"
            )
            note, note_previous, note_created = _load_hermes_tool(
                "ezlynx_note_tool_round19", "ezlynx_note_tool.py"
            )
            adapter = _adapter_module()
            chat = _chat(db)
            progress = f"🎭 playwright_exec: page.goto('{OVERVIEW}')"
            try:
                refused = playwright.playwright_exec(
                    f"page.goto('{OVERVIEW}')",
                    job_id=job_id,
                    db_path=db,
                )
                self.assertFalse(refused["ok"])
                self.assertIn("Do not drive EZLynx screens by hand", refused["error"])
                rows = store.list_playwright_exec(job_id)
                self.assertEqual(rows[0]["status"], "refused")
                with patch.object(adapter, "ROBIE_JOB_DB", db):
                    progress_sent = asyncio.run(
                        chat.send(
                            SPACE,
                            progress,
                            metadata={"robie_job_id": job_id},
                        )
                    )
                    refusal_sent = asyncio.run(
                        chat.send(
                            SPACE,
                            refused["error"],
                            metadata={"robie_job_id": job_id},
                        )
                    )
                self.assertTrue(progress_sent.success)
                self.assertIsNone(progress_sent.message_id)
                self.assertTrue(refusal_sent.success)
                self.assertIsNone(refusal_sent.message_id)
                self.assertEqual(chat._chat_api.messages.calls, [])
                direct = guard_chat_response(db, job_id, HAND_DRIVEN_EZLYNX_STOP)
                self.assertEqual(direct, "")
                self.assertEqual(guard_chat_response(db, job_id, progress), "")
                self.assertEqual(store.get_job(job_id)["status"], JobStatus.RUNNING.value)
                self.assertIsNone(store.get_checkpoint(job_id, "worker_response"))
                self.assertIsNone(store.get_checkpoint(job_id, "action"))
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
                self.assertNotIn("🎭", " ".join(posted))
                self.assertNotIn("playwright_exec", " ".join(posted).casefold())
            finally:
                clear_agent_stop(job_id)
                _restore_modules(previous, created)
                _restore_modules(note_previous, note_created)


class QuotaTests(unittest.TestCase):
    def test_429_then_success_posts_and_logs_the_outcome(self):
        adapter = _adapter_module()
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _running_address_job(store)
            chat = _chat(db)
            messages = _FlakyMessages()
            messages.fails_left = 2
            chat._chat_api.messages = messages
            chat._chat_api._spaces._messages = messages

            async def _no_wait(_delay):
                return None

            with (
                patch.object(adapter, "ROBIE_JOB_DB", db),
                patch.object(adapter.asyncio, "sleep", _no_wait),
                self.assertLogs("gateway.platforms.google_chat", level="INFO") as logs,
            ):
                sent = asyncio.run(
                    chat.send(
                        SPACE,
                        HONEST,
                        metadata={
                            "robie_job_id": job_id,
                            "robie_delivery_kind": "notice",
                        },
                    )
                )
        self.assertTrue(sent.success)
        self.assertTrue(any(HONEST in text for text in _texts(messages)))
        blob = "\n".join(logs.output)
        self.assertIn("outcome: ok after", blob)
        self.assertNotIn("reply was not delivered", blob)

    def test_429_to_the_end_records_the_reply(self):
        adapter = _adapter_module()
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _running_address_job(store)
            chat = _chat(db)
            messages = _FlakyMessages()
            messages.fails_left = 9
            chat._chat_api.messages = messages
            chat._chat_api._spaces._messages = messages

            async def _no_wait(_delay):
                return None

            with (
                patch.object(adapter, "ROBIE_JOB_DB", db),
                patch.object(adapter.asyncio, "sleep", _no_wait),
                patch.dict("os.environ", {"ROBIE_HEALTH_CHAT_SPACE": "spaces/HEALTH"}),
                self.assertLogs("gateway.platforms.google_chat", level="INFO") as logs,
            ):
                failed = asyncio.run(
                    chat.send(
                        SPACE,
                        HONEST,
                        metadata={
                            "robie_job_id": job_id,
                            "robie_delivery_kind": "notice",
                        },
                    )
                )
            self.assertFalse(failed.success)
            self.assertTrue(failed.retryable)
            failed_row = store.get_checkpoint(job_id, "chat_delivery_failed")
            self.assertEqual(failed_row["status"], 429)
            self.assertIn("I can't change the address myself yet", failed_row["text"])
            self.assertFalse(failed_row["posted"])
            remembered = store.get_checkpoint(job_id, "worker_response")
            self.assertIn("I can't change the address myself yet", str(remembered))
            blob = "\n".join(logs.output)
            self.assertIn("outcome: failed after 3/3 status=429", blob)
            self.assertIn("reply was not delivered", blob)
            self.assertTrue(
                any(
                    "spaces/HEALTH" == call.get("parent") and "not delivered" in str(call.get("body"))
                    for call in messages.calls
                )
            )
            messages.fails_left = 0
            with (
                patch.object(adapter, "ROBIE_JOB_DB", db),
                patch.object(adapter.asyncio, "sleep", _no_wait),
            ):
                later = asyncio.run(
                    chat.send(
                        SPACE,
                        "Still here.",
                        metadata={
                            "robie_job_id": job_id,
                            "robie_delivery_kind": "notice",
                        },
                    )
                )
            self.assertTrue(later.success)
            posted = _texts(messages)
            self.assertTrue(any("Still here." in text for text in posted))
            self.assertTrue(any(HONEST in text for text in posted))


class StopSuppressionTests(unittest.TestCase):
    def test_stop_on_one_job_does_not_touch_another_jobs_send(self):
        adapter = _adapter_module()
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            finished = store.create_job(
                "hermes.google_chat_task",
                {"text": "older question", "conversation_id": SPACE},
            )
            store.transition(finished["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
            store.transition(
                finished["id"],
                JobStatus.UNVERIFIED,
                expected={JobStatus.RUNNING},
                error="finished",
                release_lease=True,
            )
            request_agent_stop(finished["id"])
            live = _running_address_job(store)
            chat = _chat(db)
            chat._active_chat_job[SPACE] = finished["id"]
            chat._reply_job_by_chat = {SPACE: finished["id"]}
            chat._gateway_turns = {
                (SPACE, THREAD): {"job_id": live, "task": None},
            }

            class _Queue:
                def conversation_job_for_event(self, reply_to):
                    del reply_to
                    return {"job_id": finished["id"]}

            chat._durable_chat_queue = lambda: _Queue()
            records: list[str] = []
            handler = logging.Handler()
            handler.setLevel(logging.INFO)
            handler.emit = lambda record: records.append(record.getMessage())
            logger = logging.getLogger("gateway.platforms.google_chat")
            previous_level = logger.level
            logger.setLevel(logging.INFO)
            logger.addHandler(handler)
            try:
                with patch.object(adapter, "ROBIE_JOB_DB", db):
                    sent = asyncio.run(
                        chat.send(
                            SPACE,
                            "Filing the request note next.",
                            reply_to="spaces/ROBY/messages/old",
                            metadata={"robie_delivery_kind": "notice"},
                        )
                    )
                    named = asyncio.run(
                        chat.send(
                            SPACE,
                            "The live job can still speak.",
                            metadata={
                                "robie_job_id": live,
                                "robie_delivery_kind": "notice",
                            },
                        )
                    )
                self.assertTrue(sent.success)
                self.assertTrue(named.success)
                posted = [
                    str((call.get("body") or {}).get("text") or "")
                    for call in chat._chat_api.messages.calls
                ]
                self.assertTrue(any("Filing the request note next." in text for text in posted))
                self.assertTrue(any("The live job can still speak." in text for text in posted))
                blob = "\n".join(records)
                self.assertNotIn(f"refusing send after stop job={finished['id']}", blob)
                self.assertNotIn(
                    f"dropping send tied to a stopped job job={finished['id']}",
                    blob,
                )
                self.assertNotIn("stopped job", blob.casefold())
                self.assertEqual(store.get_job(live)["status"], JobStatus.RUNNING.value)
            finally:
                logger.removeHandler(handler)
                logger.setLevel(previous_level)
                clear_agent_stop(finished["id"])
                clear_agent_stop(live)


if __name__ == "__main__":
    logging.basicConfig(level="INFO")
    unittest.main()
