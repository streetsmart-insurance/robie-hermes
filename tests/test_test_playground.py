"""Chat playground: answers, retry, and EZLynx task types.

The flag is ROBIE_PLAYGROUND=1. It is off by default on Test and
Production. With the flag off, Chat text matches today on both. With the
flag on, the same loosening works on Production.
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_guard import (
    _render_chat_terminal,
    build_chat_execution_text,
    guard_chat_response,
    open_chat_job,
    retry_refusal_reply,
    stop_generic_chat_job_heartbeat,
)
from robie_job_engine.chat_queue import DurableChatEventQueue
from robie_job_engine.engine import LEFTOVER_RETRY_REFUSED, leftover_retry_hold_reason
from robie_job_engine.models import JobStatus, VerificationEvidence
from robie_job_engine.recording import RecordingManager
from robie_job_engine.request_routing import classify_request
from robie_job_engine.runtime_env import playground_enabled
from robie_job_engine.status_format import plain_reason
from robie_job_engine.store import JobStore
from robie_job_engine.worker_contract import classify_chat_close_without_checkpoint


ANSWER = "A deductible is the amount you pay before the policy pays."
QUOTE_ANSWER = "Hartford came back at $4,210."


def _clear_playground(mapping: dict[str, str] | None = None):
    env = {"ROBIE_PLAYGROUND": "", "ROBIE_ENV": ""}
    if mapping:
        env.update(mapping)
    return patch.dict(os.environ, env, clear=False)


def _age_job(db: str, job_id: str, *, status: JobStatus, age: timedelta) -> None:
    stamp = (datetime.now(timezone.utc) - age).isoformat()
    with JobStore(db).connect() as conn:
        conn.execute(
            "UPDATE jobs SET status=?, created_at=?, updated_at=? WHERE id=?",
            (status.value, stamp, stamp, job_id),
        )


def _open_and_stop(db: str, message_id: str, text: str, conversation_id: str) -> str:
    job_id = open_chat_job(
        db, message_id, text, conversation_id=conversation_id
    )
    stop_generic_chat_job_heartbeat(db, job_id)
    return job_id


class PlaygroundFlagTests(unittest.TestCase):
    def test_flag_off_on_test_and_production(self):
        for env in ("", "TEST", "PRODUCTION", "PROD", "LIVE"):
            with self.subTest(env=env):
                with _clear_playground({"ROBIE_PLAYGROUND": "", "ROBIE_ENV": env}):
                    self.assertFalse(playground_enabled())

    def test_flag_on_works_on_test_and_production(self):
        for env in ("TEST", "PRODUCTION", "PROD", "LIVE"):
            with self.subTest(env=env):
                with _clear_playground({"ROBIE_PLAYGROUND": "1", "ROBIE_ENV": env}):
                    self.assertTrue(playground_enabled())


class PlainEnglishCodeTests(unittest.TestCase):
    def test_leaked_codes_become_one_sentence_and_keep_the_code(self):
        cases = {
            "PLAYWRIGHT_SILENT: zero playwright_exec rows; incident 1df9740b":
                "The browser never recorded a step, so Robie stopped instead of guessing.",
            "recording is required but disabled":
                "The screen recording this job needs was turned off, so Robie stopped.",
            "ACTION_GATE_REFUSED: Test has no clean pass":
                "This action is blocked until a clean Test pass is on file.",
            "no structured destination action checkpoint":
                "Robie finished talking, but nothing was checked against the destination.",
        }
        for raw, sentence in cases.items():
            self.assertEqual(plain_reason(raw), sentence)
            self.assertNotIn("PLAYWRIGHT_SILENT", sentence)
            self.assertNotIn("ACTION_GATE_REFUSED", sentence)


class PlaygroundAnswerTests(unittest.TestCase):
    def _job(self, status: JobStatus, error: str, content: str, action_type: str = "hermes.plain_english"):
        tmp = durable_temporary_directory()
        self.addCleanup(tmp.cleanup)
        db = str(Path(tmp.name) / "jobs.db")
        store = JobStore(db)
        created = store.create_job(
            action_type,
            {"text": "Please explain a deductible"},
            idempotency_key=f"playground-{status.value}-{id(self)}-{action_type}",
        )
        store.transition(
            created["id"],
            status,
            expected={JobStatus.PENDING},
            error=error or None,
            release_lease=True,
        )
        job = store.get_job(created["id"])
        return store, job, db, content

    def test_flag_off_matches_on_test_and_production_and_drops_the_answer(self):
        store, job, db, content = self._job(
            JobStatus.UNVERIFIED,
            "no structured destination action checkpoint",
            ANSWER,
        )
        recordings = RecordingManager(db)
        with _clear_playground():
            baseline = _render_chat_terminal(store, job, content, recordings)
        self.assertTrue(baseline.startswith("Not verified."))
        self.assertNotIn(ANSWER, baseline)
        self.assertNotIn("Robie's answer:", baseline)
        for env in ("TEST", "PRODUCTION", "PROD", "LIVE"):
            with self.subTest(env=env):
                with _clear_playground({"ROBIE_PLAYGROUND": "0", "ROBIE_ENV": env}):
                    rendered = _render_chat_terminal(store, job, content, recordings)
                self.assertEqual(rendered, baseline)

    def test_flag_on_production_includes_the_same_answer_as_test(self):
        store, job, db, content = self._job(
            JobStatus.UNVERIFIED,
            "no structured destination action checkpoint",
            ANSWER,
        )
        recordings = RecordingManager(db)
        with _clear_playground({"ROBIE_PLAYGROUND": "1", "ROBIE_ENV": "TEST"}):
            test_reply = _render_chat_terminal(store, job, content, recordings)
        with _clear_playground({"ROBIE_PLAYGROUND": "1", "ROBIE_ENV": "PRODUCTION"}):
            prod_reply = _render_chat_terminal(store, job, content, recordings)
        self.assertEqual(prod_reply, test_reply)
        self.assertTrue(prod_reply.startswith("Answered."))
        self.assertIn(ANSWER, prod_reply)

    def test_plain_question_on_test_is_an_answer(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            with _clear_playground({"ROBIE_PLAYGROUND": "1", "ROBIE_ENV": "TEST"}):
                job_id = _open_and_stop(
                    db,
                    "spaces/play/messages/q1",
                    "Please explain a deductible in one sentence",
                    "spaces/play",
                )
                reply = guard_chat_response(db, job_id, ANSWER)
                decision = classify_chat_close_without_checkpoint(
                    ANSWER,
                    action=None,
                    action_type="hermes.plain_english",
                )
            self.assertEqual(decision.reason, "answered question")
            self.assertEqual(decision.status, "COMPLETE")
            self.assertTrue(reply.startswith("Answered."))
            self.assertIn(ANSWER, reply)
            self.assertNotIn("Not verified.", reply)
            self.assertNotIn("UNVERIFIED", reply)
            self.assertNotIn("no structured destination action checkpoint", reply)
            self.assertNotIn(job_id, reply)
            self.assertEqual(JobStore(db).get_job(job_id)["status"], "COMPLETE")

    def test_flag_off_plain_question_stays_not_verified(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            with _clear_playground():
                job_id = _open_and_stop(
                    db,
                    "spaces/play/messages/q0",
                    "Please explain a deductible in one sentence",
                    "spaces/off",
                )
                reply = guard_chat_response(db, job_id, ANSWER)
                decision = classify_chat_close_without_checkpoint(
                    ANSWER,
                    action=None,
                    action_type="hermes.plain_english",
                )
            self.assertEqual(decision.reason, "generic close without destination evidence")
            self.assertTrue(reply.startswith("Not verified."))
            self.assertNotIn(ANSWER, reply)

    def test_failed_job_on_test_keeps_the_answer_in_details(self):
        store, job, db, content = self._job(
            JobStatus.FAILED,
            "PLAYWRIGHT_SILENT: zero playwright_exec rows",
            "I could not open the account.",
        )
        with _clear_playground({"ROBIE_PLAYGROUND": "1", "ROBIE_ENV": "TEST"}):
            reply = _render_chat_terminal(store, job, content, RecordingManager(db))
        self.assertTrue(reply.startswith("Couldn't finish."))
        self.assertIn(
            "What happened: The browser never recorded a step, so Robie stopped instead of guessing.",
            reply,
        )
        self.assertNotIn("PLAYWRIGHT_SILENT", reply.split("\nDetails")[0])
        self.assertIn("Technical detail: PLAYWRIGHT_SILENT", reply)
        self.assertIn("Robie's answer:\nI could not open the account.", reply)


class PlaygroundRetryTests(unittest.TestCase):
    def _failed(self, db: str, conversation_id: str) -> str:
        with _clear_playground():
            job_id = _open_and_stop(
                db,
                f"message-fail-{conversation_id}",
                "Please finish the EZLynx form",
                conversation_id,
            )
        store = JobStore(db)
        current = JobStatus(store.get_job(job_id)["status"])
        store.transition(
            job_id,
            JobStatus.FAILED,
            expected={current},
            error="boom",
            release_lease=True,
        )
        return job_id

    def test_flag_off_refuses_and_replies_in_plain_english(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = self._failed(db, "spaces/retry-off")
            with _clear_playground():
                retried = open_chat_job(
                    db,
                    "spaces/retry-off/messages/retry",
                    "retry",
                    conversation_id="spaces/retry-off",
                )
                note = retry_refusal_reply(JobStore(db), job_id)
            self.assertEqual(retried, job_id)
            self.assertEqual(JobStore(db).get_job(job_id)["status"], "FAILED")
            self.assertTrue(note.startswith("Not retrying."))
            self.assertIn("already failed", note)
            self.assertNotIn(LEFTOVER_RETRY_REFUSED, note.split("\nDetails")[0])
            self.assertIn(f"Technical detail: {LEFTOVER_RETRY_REFUSED}", note)

    def test_test_playground_reruns_a_recent_failure(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = self._failed(db, "spaces/retry-on")
            with _clear_playground({"ROBIE_PLAYGROUND": "1", "ROBIE_ENV": "TEST"}):
                retried = open_chat_job(
                    db,
                    "spaces/retry-on/messages/retry",
                    "retry",
                    conversation_id="spaces/retry-on",
                )
            job = JobStore(db).get_job(job_id)
            self.assertEqual(retried, job_id)
            self.assertIn(job["status"], {JobStatus.PENDING.value, JobStatus.RUNNING.value})

    def test_playground_refuses_a_failure_older_than_24_hours(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = self._failed(db, "spaces/retry-old")
            _age_job(db, job_id, status=JobStatus.FAILED, age=timedelta(hours=25))
            with _clear_playground({"ROBIE_PLAYGROUND": "1", "ROBIE_ENV": "TEST"}):
                retried = open_chat_job(
                    db,
                    "spaces/retry-old/messages/retry",
                    "retry",
                    conversation_id="spaces/retry-old",
                )
                note = retry_refusal_reply(JobStore(db), job_id)
            self.assertEqual(retried, job_id)
            self.assertEqual(JobStore(db).get_job(job_id)["status"], "FAILED")
            self.assertIn("more than 24 hours old", note)

    def test_flag_off_on_production_refuses_like_test(self):
        for env in ("TEST", "PRODUCTION", "PROD", "LIVE"):
            with self.subTest(env=env):
                with durable_temporary_directory() as tmp:
                    db = str(Path(tmp) / "jobs.db")
                    job_id = self._failed(db, f"spaces/retry-off-{env}")
                    with _clear_playground({"ROBIE_PLAYGROUND": "", "ROBIE_ENV": env}):
                        reason = leftover_retry_hold_reason(JobStore(db).get_job(job_id))
                        retried = open_chat_job(
                            db,
                            f"spaces/retry-off-{env}/messages/retry",
                            "retry",
                            conversation_id=f"spaces/retry-off-{env}",
                        )
                    self.assertIn(LEFTOVER_RETRY_REFUSED, reason or "")
                    self.assertIn("new @robie", (reason or "").casefold())
                    self.assertEqual(retried, job_id)
                    self.assertEqual(JobStore(db).get_job(job_id)["status"], "FAILED")

    def test_flag_on_reruns_a_recent_failure_on_production(self):
        for env in ("PRODUCTION", "PROD", "LIVE"):
            with self.subTest(env=env):
                with durable_temporary_directory() as tmp:
                    db = str(Path(tmp) / "jobs.db")
                    job_id = self._failed(db, f"spaces/retry-on-{env}")
                    with _clear_playground({"ROBIE_PLAYGROUND": "1", "ROBIE_ENV": env}):
                        reason = leftover_retry_hold_reason(JobStore(db).get_job(job_id))
                        retried = open_chat_job(
                            db,
                            f"spaces/retry-on-{env}/messages/retry",
                            "retry",
                            conversation_id=f"spaces/retry-on-{env}",
                        )
                    job = JobStore(db).get_job(job_id)
                    self.assertIsNone(reason)
                    self.assertEqual(retried, job_id)
                    self.assertIn(
                        job["status"],
                        {JobStatus.PENDING.value, JobStatus.RUNNING.value},
                    )

    def test_playground_finds_a_failed_job_after_the_link_is_cleared(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = self._failed(db, "spaces/retry-cleared")
            DurableChatEventQueue(db).deactivate_conversation("spaces/retry-cleared")
            with _clear_playground({"ROBIE_PLAYGROUND": "1", "ROBIE_ENV": "TEST"}):
                retried = open_chat_job(
                    db,
                    "spaces/retry-cleared/messages/retry",
                    "retry",
                    conversation_id="spaces/retry-cleared",
                )
            self.assertEqual(retried, job_id)
            self.assertIn(
                JobStore(db).get_job(job_id)["status"],
                {JobStatus.PENDING.value, JobStatus.RUNNING.value},
            )


class PlaygroundRoutingTests(unittest.TestCase):
    def test_flag_off_keeps_today_classification(self):
        with _clear_playground():
            self.assertEqual(
                classify_request("Please quote ROBIE Test LLC").action_type,
                "hermes.plain_english",
            )
            self.assertEqual(
                classify_request("Please change the liability limit on this policy").action_type,
                "hermes.plain_english",
            )
            self.assertEqual(
                classify_request("Please issue a certificate of insurance for the landlord").action_type,
                "hermes.plain_english",
            )
        for env in ("TEST", "PRODUCTION", "PROD", "LIVE"):
            with self.subTest(env=env):
                with _clear_playground({"ROBIE_PLAYGROUND": "", "ROBIE_ENV": env}):
                    self.assertEqual(
                        classify_request("Please quote ROBIE Test LLC").action_type,
                        "hermes.plain_english",
                    )

    def test_flag_on_production_routes_like_test(self):
        texts = {
            "Please quote ROBIE Test LLC": "ezlynx.quote",
            "Please change the liability limit on this policy": "ezlynx.policy_change",
            "Please issue a certificate of insurance for the landlord": "ezlynx.certificate",
        }
        for env in ("TEST", "PRODUCTION"):
            with _clear_playground({"ROBIE_PLAYGROUND": "1", "ROBIE_ENV": env}):
                for text, action_type in texts.items():
                    self.assertEqual(classify_request(text).action_type, action_type)

    def test_playground_routes_quote_policy_change_and_certificate(self):
        with _clear_playground({"ROBIE_PLAYGROUND": "1", "ROBIE_ENV": "TEST"}):
            self.assertEqual(
                classify_request("Please quote ROBIE Test LLC").action_type,
                "ezlynx.quote",
            )
            self.assertEqual(
                classify_request("Please change the liability limit on this policy").action_type,
                "ezlynx.policy_change",
            )
            self.assertEqual(
                classify_request("Please issue a certificate of insurance for the landlord").action_type,
                "ezlynx.certificate",
            )
            self.assertEqual(
                classify_request("Finish the commercial auto from the quote").action_type,
                "ezlynx.commercial_auto",
            )
            self.assertEqual(
                classify_request(
                    "Please set up a homeowners policy TEST-HO-100 for applicant 220250093"
                ).action_type,
                "ezlynx.policy_setup",
            )

    def test_quote_runs_with_framing_and_does_not_block_on_readback(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            with _clear_playground({"ROBIE_PLAYGROUND": "1", "ROBIE_ENV": "TEST"}):
                job_id = _open_and_stop(
                    db,
                    "spaces/quote/messages/1",
                    "Please quote ROBIE Test LLC",
                    "spaces/quote",
                )
                self.assertEqual(JobStore(db).get_job(job_id)["action_type"], "ezlynx.quote")
                execution = build_chat_execution_text(
                    db, job_id, "Please quote ROBIE Test LLC"
                )
                reply = guard_chat_response(db, job_id, QUOTE_ANSWER)
                store = JobStore(db)
                store.add_evidence(
                    job_id,
                    True,
                    VerificationEvidence(
                        method="EZLYNX_API_DESTINATION_READBACK",
                        source="ezlynx_api",
                        expected={
                            "policy_number": "TEST-Q-1",
                            "applicant_id": "220250093",
                        },
                        observed={"policy_found": True},
                        authoritative=True,
                        captured_at=datetime.now(timezone.utc).isoformat(),
                    ),
                )
                with_readback = _render_chat_terminal(
                    store,
                    store.get_job(job_id),
                    QUOTE_ANSWER,
                    RecordingManager(db),
                )
            self.assertIn("Task: quote request", execution)
            self.assertIn("Do not bind", execution)
            self.assertIn("Do not wait on that readback", execution)
            self.assertIn(QUOTE_ANSWER, reply)
            self.assertIn("Robie's answer:", reply)
            self.assertIn(QUOTE_ANSWER, with_readback)
            self.assertIn("Checked: policy TEST-Q-1 exists on applicant 220250093.", with_readback)


if __name__ == "__main__":
    unittest.main()
