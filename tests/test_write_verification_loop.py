"""Chat and email write jobs: plan, then EZLynx API readback, then Jev."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from durable_temp import durable_temporary_directory
from robie_job_engine.email_guard import HermesEmailWorker
from robie_job_engine.store import JobStore
from robie_job_engine.write_verification_loop import (
    PLAN_REQUIRED,
    compare_plan_to_api,
    is_ezlynx_write_job,
    lock_stated_plan,
    parse_model_plan,
    plan_is_locked,
    prepare_write_plan,
    refuse_tool_write,
    write_reply_if_planned,
)

ROOT = Path(__file__).resolve().parents[1]
CLAIM = "I already changed the mailing address in EZLynx."
PLAN = {
    "write": "mailing address",
    "target": {
        "applicant_id": "26356199",
        "policy_number": "HO-100",
        "discussion": "Policy Change Request Checkup - Mailing Address update",
    },
    "values": {"mailingAddress": "100 Test Mailing Rd"},
}


def _yes(noul: float = 0.95) -> dict:
    return {
        "answers": {
            "satisfied": {"type": "noul", "noul": noul},
            "outcome": {
                "type": "choice",
                "choice": "completed",
                "confidence": 0.9,
            },
        }
    }


class _Scorer:
    def __init__(self, response):
        self.response = response
        self.states = []

    def evaluate(self, state, questions):
        self.states.append({"state": state, "questions": questions})
        return self.response


class WriteVerificationLoopTests(unittest.TestCase):
    def setUp(self):
        tmp = durable_temporary_directory()
        self.addCleanup(tmp.cleanup)
        self.db = str(Path(tmp.name) / "jobs.db")
        self.store = JobStore(self.db)

    def _job(self, action="ezlynx.policy_change", text="Change the mailing address"):
        return self.store.create_job(action, {"text": text})

    def test_question_is_not_a_write_and_does_not_read_ezlynx(self):
        job = self._job("hermes.plain_english", "what does COI stand for?")
        self.assertFalse(is_ezlynx_write_job(self.store.get_job(job["id"])))

        def fetch(_plan):
            raise AssertionError("readback must not run for a question")

        scorer = _Scorer(_yes())
        reply = write_reply_if_planned(
            self.store,
            self.store.get_job(job["id"]),
            CLAIM,
            fetch_fn=fetch,
            client=scorer,
        )
        self.assertEqual(reply, "")
        self.assertEqual(scorer.states, [])

    def test_model_plan_locks_before_the_write_and_rejects_a_blank_value(self):
        job = self._job()
        calls = []

        def model(prompt):
            calls.append(prompt)
            return json.dumps(PLAN)

        locked = prepare_write_plan(self.store, self.store.get_job(job["id"]), model)
        self.assertTrue(locked["locked"])
        self.assertEqual(locked["values"]["mailingAddress"], "100 Test Mailing Rd")
        self.assertEqual(len(calls), 1)
        self.assertIn("Do not call a tool", calls[0])
        self.assertIn("Change the mailing address", calls[0])
        again = prepare_write_plan(
            self.store,
            self.store.get_job(job["id"]),
            lambda _prompt: (_ for _ in ()).throw(AssertionError("second plan")),
        )
        self.assertEqual(again["values"], locked["values"])
        self.assertIsNone(parse_model_plan('{"write":"address","target":{},"values":{}}'))
        self.assertIsNone(parse_model_plan("I will update the address."))
        blank = self._job(text="Change a different address")
        with self.assertRaises(ValueError):
            lock_stated_plan(
                self.store,
                blank,
                {"write": "address", "target": {"policy_number": "HO-1"}, "values": {}},
            )

    def test_readback_matches_the_api_and_does_not_ask_a_model(self):
        matched = compare_plan_to_api(
            PLAN,
            {"mailingAddress": "100 Test Mailing Rd"},
        )
        self.assertTrue(matched["passed"])
        self.assertEqual(matched["method"], "EZLYNX_API")
        missed = compare_plan_to_api(PLAN, {"mailingAddress": "9 Old Rd"})
        self.assertFalse(missed["passed"])
        self.assertIn("mailingAddress", missed["failure"])
        unread = compare_plan_to_api(PLAN, {}, error="EZLynx API readback was not run")
        self.assertFalse(unread["passed"])
        self.assertFalse(unread["items"][0]["matched"])

    def test_reply_quotes_the_readback_and_jev_cannot_override_a_miss(self):
        job = self._job()
        lock_stated_plan(self.store, job, PLAN)
        scorer = _Scorer(_yes())
        reply = write_reply_if_planned(
            self.store,
            self.store.get_job(job["id"]),
            CLAIM,
            fetch_fn=lambda _plan: {"mailingAddress": "9 Old Rd"},
            client=scorer,
        )
        self.assertIn("planned 100 Test Mailing Rd", reply)
        self.assertIn("API shows 9 Old Rd", reply)
        self.assertIn("does not match", reply)
        self.assertIn("Jev: wrong", reply)
        self.assertNotIn(CLAIM, reply)
        self.assertNotIn("nothing was filed", reply.casefold())
        self.assertEqual(self.store.get_checkpoint(job["id"], "write_jev_score")["verdict"], "wrong")
        self.assertIn("readback", scorer.states[0]["state"])
        self.assertFalse(scorer.states[0]["state"]["worker_claim_is_proof"])

        passed = _Scorer(_yes(0.1))
        reply = write_reply_if_planned(
            self.store,
            self.store.get_job(job["id"]),
            CLAIM,
            fetch_fn=lambda _plan: {"mailingAddress": "100 Test Mailing Rd"},
            client=passed,
        )
        self.assertIn("matches", reply)
        self.assertNotIn(CLAIM, reply)
        self.assertTrue(self.store.get_checkpoint(job["id"], "write_readback")["passed"])

    def test_write_tool_refuses_until_the_plan_is_locked(self):
        job = self._job()
        refused = refuse_tool_write(
            {"applicant_id": "26356199", "note_text": "Robie was here"},
            {"job_id": job["id"], "db_path": self.db},
        )
        self.assertEqual(refused, PLAN_REQUIRED)
        self.assertFalse(plan_is_locked(self.store, job["id"]))
        allowed = refuse_tool_write(
            {
                "applicant_id": "26356199",
                "note_text": "Robie was here",
                "plan": PLAN,
            },
            {"job_id": job["id"], "db_path": self.db},
        )
        self.assertIsNone(allowed)
        self.assertTrue(plan_is_locked(self.store, job["id"]))
        question = self._job("hermes.plain_english", "what does COI stand for?")
        self.assertIsNone(
            refuse_tool_write(
                {"applicant_id": "26356199", "note_text": "Robie was here"},
                {"job_id": question["id"], "db_path": self.db},
            )
        )

    def test_email_worker_locks_the_plan_before_the_acting_call(self):
        job = self.store.create_job(
            "hermes.email_task",
            {
                "prompt": "Please update the mailing address to 100 Test Mailing Rd",
                "request_text": "Please update the mailing address to 100 Test Mailing Rd",
                "gmail_message_id": "mail-1",
            },
        )
        seen = []

        def actor(prompt):
            seen.append(prompt)
            return "acted"

        worker = HermesEmailWorker(actor, self.store)
        worker.plan_model = lambda _prompt: json.dumps(PLAN)
        result = worker.perform(job, idempotency_key=job["idempotency_key"])
        self.assertEqual(result.detail["response_text"], "acted")
        self.assertEqual(len(seen), 1)
        self.assertIn("Locked plan", seen[0])
        self.assertIn("100 Test Mailing Rd", seen[0])
        self.assertTrue(plan_is_locked(self.store, job["id"]))

    def test_handlers_and_chat_call_the_loop_before_the_write(self):
        note = (ROOT / "deploy/hermes/tools/ezlynx_note_tool.py").read_text(encoding="utf-8")
        document = (ROOT / "deploy/hermes/tools/ezlynx_document_tool.py").read_text(encoding="utf-8")
        setup = (ROOT / "deploy/hermes/tools/policy_setup_tool.py").read_text(encoding="utf-8")
        for source in (note, document, setup):
            stop_at = source.index("refuse_current_tool_call")
            plan_at = source.index("refuse_tool_write")
            self.assertLess(stop_at, plan_at)
        handler = note.split("def ezlynx_discussion_note_handler", 1)[1]
        self.assertLess(handler.index("refuse_tool_write"), handler.index("_file_note("))
        adapter = (ROOT / "integrations/google_chat/adapter.py").read_text(encoding="utf-8")
        generic = adapter.split("async def _run_generic_chat_job", 1)[1].split(
            "\n    async def ", 1
        )[0]
        self.assertLess(
            generic.index("prepare_chat_write_plan"),
            generic.index("_run_gateway_turn_with_ceiling"),
        )
        chat = (ROOT / "robie_job_engine/chat_guard.py").read_text(encoding="utf-8")
        self.assertIn("Before any tool or write, state a plan", chat)
        self.assertIn("write_reply_if_planned", chat)


if __name__ == "__main__":
    unittest.main()
