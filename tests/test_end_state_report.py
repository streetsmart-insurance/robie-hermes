"""End-state report and Jev client.

Flag off keeps today's reply. Flag on replaces it with the plain-English
report. Jev is mocked at the HTTP layer. A failed EZLynx readback forces
wrong even when Jev says yes.
"""
from __future__ import annotations

import io
import json
import os
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from durable_temp import durable_temporary_directory

from robie_job_engine.action_gate import format_action_gate_chat_note
from robie_job_engine.chat_guard import _render_chat_terminal
from robie_job_engine.email_guard import _render_email_terminal
from robie_job_engine.end_state_report import (
    escalate_end_state,
    render_job_end_state,
)
from robie_job_engine.jev_client import JevClient, JevUnavailable, load_jev_api_key
from robie_job_engine.models import JobStatus, VerificationEvidence
from robie_job_engine.recording import RecordingManager
from robie_job_engine.store import JobStore


JOB_ID_NOTE = "9d2098e1-aaaa-bbbb-cccc-d1e5f6a7b8c9"
SECRET = "jev-test-key-should-not-leak"


def _jev_body(noul, choice, confidence, model="jev-1.13.0"):
    return {
        "model": model,
        "answers": {
            "satisfied": {"type": "noul", "noul": noul},
            "outcome": {
                "type": "choice",
                "choice": choice,
                "confidence": confidence,
                "probabilities": {choice: confidence},
            },
        },
        "usage": {"input_tokens": 10, "output_tokens": 4},
    }


class _HttpResponse:
    def __init__(self, payload, status=200):
        self._raw = json.dumps(payload).encode("utf-8")
        self.status = status

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _http_error(status):
    return urllib.error.HTTPError(
        "https://api.typesafe.ai/v1/systemone",
        status,
        "no",
        hdrs=None,
        fp=io.BytesIO(b"{}"),
    )


class JevClientTests(unittest.TestCase):
    def _client(self, opener):
        return JevClient(SECRET, opener=opener, retry_sleep=0, timeout=1)

    def test_correct_wrong_unsure_and_low_confidence_from_mocked_http(self):
        cases = [
            (_jev_body(0.95, "completed", 0.92), "correct", 92, False),
            (_jev_body(0.08, "failed", 0.90), "wrong", 90, True),
            (_jev_body(0.60, "partially_completed", 0.80), "unsure", 60, True),
            (_jev_body(0.95, "completed", 0.40), "correct", 40, True),
        ]
        from robie_job_engine.end_state_report import decide

        for payload, verdict, confidence, escalate in cases:
            seen = {}

            def opener(request, timeout=None, payload=payload, seen=seen):
                seen["auth"] = request.get_header("Authorization")
                seen["body"] = json.loads(request.data.decode("utf-8"))
                return _HttpResponse(payload)

            response = self._client(opener).evaluate(
                {"ask": "quote ACME LLC"},
                {"satisfied": {"type": "noul"}},
            )
            decision = decide(response, request_body=seen["body"])
            self.assertEqual(decision.verdict, verdict, payload)
            self.assertEqual(decision.confidence, confidence, payload)
            self.assertEqual(decision.escalate, escalate, payload)
            self.assertEqual(seen["auth"], f"Bearer {SECRET}")
            self.assertNotIn(SECRET, json.dumps(seen["body"]))
            self.assertEqual(seen["body"]["questions"]["satisfied"]["type"], "noul")

    def test_unreachable_retries_once_then_fails_safe(self):
        calls = []

        def opener(request, timeout=None):
            calls.append(1)
            raise urllib.error.URLError("timed out")

        with self.assertRaises(JevUnavailable) as caught:
            self._client(opener).evaluate({"ask": "x"}, {"satisfied": {"type": "noul"}})
        self.assertEqual(len(calls), 2)
        self.assertNotIn(SECRET, str(caught.exception))

    def test_unauthorized_does_not_retry(self):
        calls = []

        def opener(request, timeout=None):
            calls.append(1)
            raise _http_error(401)

        with self.assertRaises(JevUnavailable):
            self._client(opener).evaluate({"ask": "x"}, {"satisfied": {"type": "noul"}})
        self.assertEqual(calls, [1])

    def test_missing_key_does_not_call_http(self):
        def opener(request, timeout=None):
            raise AssertionError("must not call Jev without a key")

        with self.assertRaises(JevUnavailable):
            JevClient("", opener=opener).evaluate({}, {})

    def test_env_key_overrides_secret_manager(self):
        with mock.patch.dict(os.environ, {"JEV_API_KEY": SECRET}):
            with mock.patch(
                "robie_job_engine.staff_jobs_common.read_secret",
                side_effect=AssertionError("secret manager must not be called"),
            ):
                self.assertEqual(load_jev_api_key(), SECRET)

    def test_secret_manager_failure_is_empty(self):
        env = os.environ.copy()
        env.pop("JEV_API_KEY", None)
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch(
                "robie_job_engine.staff_jobs_common.read_secret",
                side_effect=RuntimeError("down"),
            ):
                self.assertEqual(load_jev_api_key(), "")


class EndStateReportTests(unittest.TestCase):
    def setUp(self):
        tmp = durable_temporary_directory()
        self.addCleanup(tmp.cleanup)
        self.db = str(Path(tmp.name) / "jobs.db")
        self.store = JobStore(self.db)
        self.escalations = []

    def _job(self, ask, *, idem="job"):
        return self.store.create_job(
            "hermes.google_chat_task",
            {"request_text": ask},
            idempotency_key=idem,
        )

    def _evidence(self, job_id, *, verified, observed, expected=None, method="EZLYNX_API_DESTINATION_READBACK"):
        self.store.add_evidence(
            job_id,
            verified,
            VerificationEvidence(
                method=method,
                source="ezlynx-api",
                expected=expected or {},
                observed=observed,
                authoritative=True,
                captured_at="2026-09-29T00:00:00+00:00",
            ),
        )

    def _render(self, job, opener, worker=""):
        client = JevClient(SECRET, opener=opener, retry_sleep=0, timeout=1)

        def record(store, job, decision, **kwargs):
            self.escalations.append(decision.verdict)
            return {"posted": True}

        with mock.patch.dict(os.environ, {"ROBIE_END_STATE_REPORT": "1", "ROBIE_ENV": "TEST"}):
            with mock.patch(
                "robie_job_engine.end_state_report.escalate_end_state",
                record,
            ):
                return render_job_end_state(
                    self.store, job, worker, client=client, channel="chat"
                )

    def test_success_report_order_full_job_id_and_no_old_label(self):
        job = self._job("quote ACME LLC", idem="success")
        self._evidence(
            job["id"],
            verified=True,
            observed={
                "account_name": "ACME LLC",
                "applicant_id": "123",
                "premium": "$4,210",
                "carrier": "Hartford",
                "quote_created": True,
                "policy_found": True,
            },
        )

        def opener(request, timeout=None):
            return _HttpResponse(_jev_body(0.95, "completed", 0.92))

        text = self._render(job, opener)
        lines = text.splitlines()
        self.assertEqual(
            lines[0],
            "You asked Robie to quote ACME LLC, and Robie created the quote in EZLynx.",
        )
        self.assertEqual(
            lines[2],
            "End state: Quote created in EZLynx for ACME LLC, applicant id 123, premium $4,210, carrier Hartford.",
        )
        self.assertEqual(
            lines[3],
            "Jev: correct, 92% confidence. The end state matches what was asked.",
        )
        self.assertLess(lines.index("End state: Quote created in EZLynx for ACME LLC, applicant id 123, premium $4,210, carrier Hartford."), lines.index("Jev: correct, 92% confidence. The end state matches what was asked."))
        self.assertLess(lines.index("Jev: correct, 92% confidence. The end state matches what was asked."), lines.index("Details"))
        self.assertEqual(lines[-1], "Ref: job " + job["id"])
        self.assertNotIn("UNVERIFIED", text)
        self.assertNotIn("Worker report (not proof)", text)
        self.assertNotIn(SECRET, text)
        self.assertEqual(self.escalations, [])
        saved = self.store.list_jev_evaluations(job["id"])
        self.assertEqual(saved[0]["verdict"], "correct")
        self.assertEqual(saved[0]["confidence"], 92)
        self.assertEqual(saved[0]["response"]["model"], "jev-1.13.0")
        self.assertNotIn(SECRET, json.dumps(saved[0]["request"]))
        self.assertNotIn(SECRET, json.dumps(self.store.get_checkpoint(job["id"], "jev_score")))

    def test_missing_file_forces_wrong_even_when_jev_says_yes(self):
        job = self._job("file Bond.pdf on the ACME account", idem="missing-file")
        self._evidence(
            job["id"],
            verified=False,
            observed={"documents_missing": ["Bond.pdf"], "policy_found": True},
            expected={"document_names": ["Bond.pdf"]},
        )

        def opener(request, timeout=None):
            return _HttpResponse(_jev_body(0.99, "completed", 0.95))

        text = self._render(job, opener)
        self.assertIn(
            "You asked Robie to file Bond.pdf on the ACME account, and Robie did not finish it.",
            text,
        )
        self.assertIn(
            "End state: Stopped before the file was in EZLynx. Bond.pdf was not in EZLynx.",
            text,
        )
        self.assertIn(
            "Jev: wrong, 100% confidence. A required check failed: Bond.pdf was not in EZLynx. Jev does not override that.",
            text,
        )
        self.assertNotIn("UNVERIFIED", text)
        self.assertNotIn("Worker report (not proof)", text)
        self.assertEqual(text.rstrip().splitlines()[-1], "Ref: job " + job["id"])
        self.assertEqual(self.escalations, ["wrong"])
        saved = self.store.list_jev_evaluations(job["id"])
        self.assertEqual(saved[0]["verdict"], "wrong")
        self.assertTrue(saved[0]["escalate"])

    def test_login_stop_is_wrong_and_has_no_playwright_jargon(self):
        job = self._job("quote Progressive for ACME LLC", idem="login")

        def opener(request, timeout=None):
            return _HttpResponse(_jev_body(0.91, "completed", 0.88))

        text = self._render(
            job,
            opener,
            worker="Playwright stopped at the Progressive login page, 2FA prompt, locator missing.",
        )
        end_line = next(line for line in text.splitlines() if line.startswith("End state:"))
        self.assertEqual(end_line, "End state: Stopped at the login page, 2FA prompt.")
        self.assertNotIn("Playwright", end_line)
        self.assertNotIn("locator", end_line.lower())
        self.assertIn("Jev: wrong, 100% confidence.", text)
        self.assertEqual(self.escalations, ["wrong"])

    def test_unreachable_jev_is_unsure_and_escalates(self):
        job = self._job("quote ACME LLC", idem="down")

        def opener(request, timeout=None):
            raise urllib.error.URLError("down")

        text = self._render(job, opener, worker="Opened the quote screen.")
        self.assertIn("Jev: unsure (Jev unavailable), 0% confidence.", text)
        self.assertIn("Jev could not be reached, so this is not a pass.", text)
        self.assertNotIn("UNVERIFIED", text)
        self.assertEqual(self.escalations, ["unsure"])
        self.assertEqual(text.rstrip().splitlines()[-1], "Ref: job " + job["id"])

    def test_low_confidence_correct_still_escalates(self):
        job = self._job("quote ACME LLC", idem="low")
        self._evidence(
            job["id"],
            verified=True,
            observed={
                "account_name": "ACME LLC",
                "applicant_id": "123",
                "premium": "$4,210",
                "carrier": "Hartford",
                "quote_created": True,
            },
        )

        def opener(request, timeout=None):
            return _HttpResponse(_jev_body(0.95, "completed", 0.40))

        text = self._render(job, opener)
        self.assertIn("Jev: correct, 40% confidence.", text)
        self.assertEqual(self.escalations, ["correct"])

    def test_second_render_does_not_score_or_escalate_again(self):
        job = self._job("quote ACME LLC", idem="once")
        calls = []

        def opener(request, timeout=None):
            calls.append(1)
            return _HttpResponse(_jev_body(0.08, "failed", 0.9))

        first = self._render(job, opener)
        second = self._render(job, opener)
        self.assertEqual(first, second)
        self.assertEqual(calls, [1])
        self.assertEqual(self.escalations, ["wrong"])

    def test_flag_off_keeps_todays_email_and_chat_wording(self):
        email = _render_email_terminal(
            job_id=JOB_ID_NOTE,
            status=JobStatus.UNVERIFIED,
            response="The worker said it was done.",
            summary="",
        )
        self.assertTrue(email.startswith("Not verified."))
        self.assertIn("Worker report (not proof)", email)
        self.assertIn("What happened:", email)
        self.assertTrue(email.rstrip().endswith("Ref: job " + JOB_ID_NOTE))

        job = self._job("quote ACME LLC", idem="flag-off")
        self.store.transition(
            job["id"],
            JobStatus.UNVERIFIED,
            expected={JobStatus.PENDING},
            error="destination verification produced no authoritative evidence",
            release_lease=True,
        )
        chat = _render_chat_terminal(
            self.store,
            self.store.get_job(job["id"]),
            "worker said hi",
            RecordingManager(self.db),
        )
        self.assertTrue(chat.startswith("Not verified."))
        self.assertIn("What happened:", chat)
        self.assertNotIn("Jev:", chat)
        self.assertTrue(chat.rstrip().endswith("Ref: job " + job["id"]))

    def test_production_ignores_the_flag(self):
        with mock.patch.dict(
            os.environ,
            {"ROBIE_END_STATE_REPORT": "1", "ROBIE_ENV": "PRODUCTION"},
        ):
            email = _render_email_terminal(
                job_id=JOB_ID_NOTE,
                status=JobStatus.UNVERIFIED,
                response="The worker said it was done.",
                summary="",
                store=self.store,
            )
        self.assertTrue(email.startswith("Not verified."))
        self.assertIn("Worker report (not proof)", email)
        self.assertNotIn("Jev:", email)

    def test_flag_on_chat_and_email_use_the_new_report(self):
        job = self._job("quote ACME LLC", idem="both-channels")
        self._evidence(
            job["id"],
            verified=True,
            observed={
                "account_name": "ACME LLC",
                "applicant_id": "123",
                "premium": "$4,210",
                "carrier": "Hartford",
                "quote_created": True,
            },
        )

        def opener(request, timeout=None):
            return _HttpResponse(_jev_body(0.95, "completed", 0.92))

        client = JevClient(SECRET, opener=opener, retry_sleep=0, timeout=1)
        with mock.patch.dict(os.environ, {"ROBIE_END_STATE_REPORT": "1", "ROBIE_ENV": "TEST"}):
            with mock.patch(
                "robie_job_engine.end_state_report.build_jev_client",
                return_value=client,
            ):
                with mock.patch(
                    "robie_job_engine.end_state_report.escalate_end_state",
                    return_value={"posted": False},
                ):
                    chat = _render_chat_terminal(
                        self.store,
                        self.store.get_job(job["id"]),
                        "created the quote",
                        RecordingManager(self.db),
                    )
                    email = _render_email_terminal(
                        job_id=job["id"],
                        status=JobStatus.UNVERIFIED,
                        response="created the quote",
                        summary="",
                        store=self.store,
                    )
        self.assertTrue(chat.startswith("You asked Robie to quote ACME LLC"))
        self.assertEqual(chat, email)
        self.assertNotIn("UNVERIFIED", chat)
        self.assertNotIn("Worker report (not proof)", email)
        self.assertTrue(chat.rstrip().endswith("Ref: job " + job["id"]))

    def test_action_gate_refusal_is_unchanged_when_flag_is_on(self):
        with mock.patch.dict(os.environ, {"ROBIE_END_STATE_REPORT": "1", "ROBIE_ENV": "TEST"}):
            text = format_action_gate_chat_note(
                {"id": JOB_ID_NOTE, "last_error": "gated: no test pass"}
            )
        self.assertTrue(text.startswith("Couldn't finish."))
        self.assertIn("What happened:", text)
        self.assertNotIn("Jev:", text)
        self.assertTrue(text.rstrip().endswith("Ref: job " + JOB_ID_NOTE))

    def test_escalation_enters_the_existing_hitl_ladder_once(self):
        job = self._job("quote ACME LLC", idem="hitl")
        from robie_job_engine.end_state_report import decide

        decision = decide(
            None,
            request_body={"state": {}, "questions": {}},
        )
        posted = mock.Mock()
        posted.hitl_posted = True
        with mock.patch("robie_job_engine.hitl_escalation.escalate", return_value=posted) as ladder:
            first = escalate_end_state(self.store, job, decision, channel="chat")
            second = escalate_end_state(self.store, job, decision, channel="chat")
        self.assertTrue(first["posted"])
        self.assertEqual(second["reason"], "already escalated")
        ladder.assert_called_once()
        request = ladder.call_args.args[0]
        self.assertEqual(request.phase, "end_state_report")
        self.assertEqual(request.channel, "chat")


if __name__ == "__main__":
    unittest.main()
