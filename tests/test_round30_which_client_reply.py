"""A which-client reply picks one saved match and the job answers.

The search question lists the accounts. An id, an ordinal, or one
distinguishing word binds that account. Anything else asks once more.
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_job_controls import outbound_is_clarify
from robie_job_engine.client_name_lookup import (
    pending_named_lookup_line,
    prepare_named_client_lookup,
    which_client_question,
)
from robie_job_engine.models import JobStatus
from robie_job_engine.playwright_observability import record_playwright_exec
from robie_job_engine.store import JobStore
from robie_job_engine.user_reply import format_user_reply
from test_round10_reply_lifecycle import SPACE, THREAD, _Event, _adapter_module, _chat, _outbound_text
from test_round24_named_lookup import ASK, ANSWER, _job, _searcher

FIRST = "26356199"
SECOND = "62963499"
THIRD = "143786127"
MATCHES = [
    {
        "applicant_id": FIRST,
        "name": "Buster Brown",
        "address": "12 Oak St, Austin",
        "role": "Insured",
    },
    {
        "applicant_id": SECOND,
        "name": "Buster Brown",
        "address": "4 Pine Rd, Dallas",
        "role": "Insured",
    },
    {
        "applicant_id": THIRD,
        "name": "Buster Brown",
        "address": "9 Elm Ave, Houston",
        "role": "Insured",
    },
]


def _record_reply(store: JobStore, job_id: str, reply: str) -> None:
    job = store.get_job(job_id)
    payload = dict(job.get("payload") or {})
    original = str(payload.get("original_text") or payload.get("text") or ASK)
    payload["original_text"] = original
    payload["clarification_reply"] = reply
    if reply and reply not in str(payload.get("text") or ""):
        payload["text"] = f"{original}\n\nUser reply: {reply}".strip()
    store.update_payload(job_id, payload)
    store.checkpoint(
        job_id,
        "clarification_reply",
        {"text": reply, "message_id": "spaces/ROBY/messages/reply"},
    )
    if JobStatus(job["status"]) == JobStatus.NEEDS_CLARIFICATION:
        store.resume(job_id)


def _run_turn(chat, adapter, db: str, job_id: str, text: str) -> list[str]:
    started: list[str] = []

    async def _ceiling(_self, turn_job, _event):
        started.append(str(turn_job))

    async def _noop(_self, *_args, **_kwargs):
        return None

    event = _Event(THREAD, text)
    with patch.object(adapter, "ROBIE_JOB_DB", db), patch.object(
        type(chat), "_run_gateway_turn_with_ceiling", _ceiling
    ), patch.object(type(chat), "_bind_inbound_job_thread", _noop), patch.object(
        type(chat), "_maintain_generic_chat_job_heartbeat", _noop
    ):
        asyncio.run(chat._run_generic_chat_job(job_id, event))
    return started


class WhichClientReplyTests(unittest.TestCase):
    def _ask(self, store: JobStore, db: str):
        adapter = _adapter_module()
        job_id = _job(store)
        line = prepare_named_client_lookup(store, job_id, searcher=_searcher(MATCHES))
        chat = _chat(db)
        with patch.object(adapter, "ROBIE_JOB_DB", db):
            result = asyncio.run(
                chat.send(SPACE, line or "", metadata={"robie_job_id": job_id})
            )
        self.assertTrue(result.success)
        posted = "\n".join(_outbound_text(chat))
        self.assertIn("I found more than one Buster Brown.", posted)
        self.assertIn("12 Oak St, Austin", posted)
        self.assertIn(f"account {FIRST}", posted)
        self.assertIn(f"account {SECOND}", posted)
        self.assertIn("1. ", posted)
        self.assertTrue(posted.strip().endswith("Which one should I use?"))
        self.assertNotIn("applicant_id", posted)
        self.assertNotIn("ROBIE_BLOCKED", posted)
        self.assertEqual(
            store.get_job(job_id)["status"], JobStatus.NEEDS_CLARIFICATION.value
        )
        return adapter, chat, job_id, posted

    def test_reply_with_id_resumes_and_answers(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            adapter, chat, job_id, question = self._ask(store, db)
            _record_reply(store, job_id, FIRST)
            self.assertEqual(
                store.get_job(job_id)["status"], JobStatus.PENDING.value
            )
            started = _run_turn(chat, adapter, db, job_id, FIRST)
            self.assertEqual(started, [job_id])
            self.assertEqual(_outbound_text(chat), [question])
            payload = store.get_job(job_id)["payload"]
            self.assertEqual(payload["applicant_id"], FIRST)
            note = store.get_checkpoint(job_id, "client_name_search")
            self.assertEqual(note["source"], "search")
            self.assertEqual(note["applicant_ids"], [FIRST])
            self.assertFalse(str(note.get("user_line") or ""))
            self.assertEqual(pending_named_lookup_line(db, job_id), "")
            account = f"https://app.ezlynx.com/web/account/{FIRST}/policies"
            record_playwright_exec(
                f"page.goto('{account}')",
                {"url": account},
                job_id=job_id,
                db_path=db,
                status="ok",
            )
            with patch.object(adapter, "ROBIE_JOB_DB", db):
                answered = asyncio.run(
                    chat.send(SPACE, ANSWER, metadata={"robie_job_id": job_id})
                )
            self.assertTrue(answered.success)
            posted = "\n".join(_outbound_text(chat))
            self.assertIn("GL-100", posted)
            self.assertIn("Harbor", posted)
            self.assertEqual(store.get_job(job_id)["status"], JobStatus.COMPLETE.value)

    def test_reply_two_binds_the_second_match(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            adapter, chat, job_id, _question = self._ask(store, db)
            _record_reply(store, job_id, "2")
            started = _run_turn(chat, adapter, db, job_id, "2")
            self.assertEqual(started, [job_id])
            self.assertEqual(store.get_job(job_id)["payload"]["applicant_id"], SECOND)
            note = store.get_checkpoint(job_id, "client_name_search")
            self.assertEqual(note["applicant_ids"], [SECOND])
            self.assertNotIn(FIRST, note["applicant_ids"])
            self.assertEqual(pending_named_lookup_line(db, job_id), "")

    def test_non_matching_reply_asks_once(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            adapter, chat, job_id, question = self._ask(store, db)
            _record_reply(store, job_id, "nope")
            started = _run_turn(chat, adapter, db, job_id, "nope")
            self.assertEqual(started, [])
            posted = _outbound_text(chat)
            self.assertEqual(len(posted), 2)
            self.assertIn(f"account {FIRST}", posted[1])
            self.assertIn(f"account {THIRD}", posted[1])
            self.assertTrue(posted[1].strip().endswith("Which one should I use?"))
            self.assertNotIn("applicant_id", posted[1])
            payload = store.get_job(job_id)["payload"]
            self.assertFalse(str(payload.get("applicant_id") or ""))
            note = store.get_checkpoint(job_id, "client_name_search")
            self.assertTrue(note.get("reasked"))
            self.assertEqual(note["source"], "several")
            self.assertEqual(
                store.get_job(job_id)["status"], JobStatus.NEEDS_CLARIFICATION.value
            )
            _record_reply(store, job_id, "no thanks")
            again = _run_turn(chat, adapter, db, job_id, "no thanks")
            self.assertEqual(again, [])
            self.assertFalse(
                str(store.get_job(job_id)["payload"].get("applicant_id") or "")
            )
            self.assertEqual(
                store.get_checkpoint(job_id, "client_name_search")["user_line"],
                question,
            )
            self.assertNotEqual(pending_named_lookup_line(db, job_id), "")

    def test_distinguishing_word_binds_that_account(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _job(store)
            prepare_named_client_lookup(store, job_id, searcher=_searcher(MATCHES))
            _record_reply(store, job_id, "the dallas one")
            self.assertEqual(pending_named_lookup_line(db, job_id), "")
            self.assertEqual(store.get_job(job_id)["payload"]["applicant_id"], SECOND)

    def test_saved_candidate_id_binds_without_match_text(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _job(store)
            store.checkpoint(
                job_id,
                "client_name_search",
                {
                    "resolved": True,
                    "source": "several",
                    "applicant_ids": [],
                    "user_line": "I found more than one Buster Brown. Which one should I use?",
                    "name": "buster brown",
                    "candidates": [FIRST, SECOND, THIRD],
                },
            )
            _record_reply(store, job_id, FIRST)
            self.assertEqual(pending_named_lookup_line(db, job_id), "")
            self.assertEqual(store.get_job(job_id)["payload"]["applicant_id"], FIRST)
            self.assertEqual(
                store.get_checkpoint(job_id, "client_name_search")["source"],
                "search",
            )

    def test_stop_cancels_the_question_and_does_not_bind(self):
        adapter = _adapter_module()
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            _adapter, chat, job_id, _question = self._ask(store, db)

            async def _noop(*_args, **_kwargs):
                return None

            chat._terminate_running_agent = _noop
            with patch.object(adapter, "ROBIE_JOB_DB", db):
                asyncio.run(chat._apply_chat_stop(_Event(None, "@Robie /stop")))
            self.assertEqual(
                store.get_job(job_id)["status"], JobStatus.CANCELLED.value
            )
            self.assertIn("Stopped.", "\n".join(_outbound_text(chat)))
            self.assertFalse(
                str(store.get_job(job_id)["payload"].get("applicant_id") or "")
            )

    def test_numbered_list_stays_a_question(self):
        rows = [
            {
                "applicant_id": f"8800112{index}",
                "name": "Buster Brown",
                "address": f"{index} Very Long Street Name, Cityville",
                "role": "Primary Insured",
            }
            for index in range(1, 6)
        ]
        question = which_client_question("buster brown", rows)
        self.assertGreater(len(question.split()), 40)
        self.assertEqual(format_user_reply(question), question)
        self.assertTrue(outbound_is_clarify(question))
        self.assertNotIn("applicant_id", question)
        self.assertNotIn("ROBIE_BLOCKED", question)
        self.assertIn("5. ", question)
        self.assertNotIn("6. ", question)
