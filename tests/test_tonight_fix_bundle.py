"""Regression tests for the live failures from the 2026-09-29 prove.

Questions must answer without an EZLynx readback. A short polite ask must
ask one question instead of investigating the server. Chat and email jobs
must stop at a configurable 10 minute ceiling. /stop must cancel the linked
job. Certificates and address notes must name the purpose-built tools.
The stealth import warning must not become a blocked error. Inbox mail
must run with a small pool, and help mail must go to Carlo.
"""

from __future__ import annotations

import ast
import os
import sqlite3
import subprocess
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from durable_temp import durable_temporary_directory

from robie_job_engine.answer_only import (
    CERT_ROUTE,
    CLARIFICATION_QUESTION,
    POLICY_CHANGE_ROUTE,
    SkipDestinationReadback,
    is_informational_ask,
    is_vague_short_request,
    purpose_built_instructions,
)
from robie_job_engine.chat_guard import (
    build_chat_execution_text,
    chat_hermes_should_run,
    open_chat_job,
)
from robie_job_engine.chat_turn_control import (
    DEFAULT_GATEWAY_MAX_TURN_SECONDS,
    STOPPED_AFTER_TEN_MINUTES,
    _abandon_timed_out_gateway_turn,
    fail_cancelled_chat_job,
    gateway_max_turn_seconds,
    is_stop_command,
)
from robie_job_engine.email_agent_runner import (
    email_agent_timeout_seconds,
    run_scripted_email,
)
from robie_job_engine.email_dispatch import (
    email_job_writes_ezlynx,
    email_worker_concurrency,
    run_email_batch,
)
from robie_job_engine.email_guard import _strip_internal_reasoning
from robie_job_engine.end_state_report import render_job_end_state
from robie_job_engine.hitl_email import CARLO_EMAIL, carlo_hitl_email_sender
from robie_job_engine.hitl_escalation import HitlRequest, escalate
from robie_job_engine.models import JobStatus
from robie_job_engine.request_routing import classify_request
from robie_job_engine.store import JobStore

ROOT = Path(__file__).resolve().parents[1]
QUESTION = "Which carriers do we quote for NJ homeowners?"
VAGUE = "@Robie can you do a book for me"
CERT = (
    "Please issue a certificate of insurance for Buster Brown, "
    "holder Test Holder LLC, applicant 26356199. Draft only."
)
ADDRESS = (
    "Please update the mailing address to 100 Test Mailing Rd and file a note "
    "on the existing Policy Change Request Checkup - Mailing Address update discussion."
)


class QuestionPathTests(unittest.TestCase):
    def test_question_is_answer_only_even_when_playground_sees_the_word_quote(self):
        with mock.patch.dict(os.environ, {"ROBIE_PLAYGROUND": "1"}, clear=False):
            route = classify_request(QUESTION)
        self.assertEqual(route.action_type, "hermes.plain_english")
        self.assertTrue(route.answer_only)
        self.assertTrue(is_informational_ask(QUESTION))

    def test_quote_work_still_routes_when_playground_is_on(self):
        with mock.patch.dict(os.environ, {"ROBIE_PLAYGROUND": "1"}, clear=False):
            self.assertEqual(
                classify_request("Please quote ROBIE Test LLC").action_type,
                "ezlynx.quote",
            )

    def test_reply_is_the_answer_and_reasoning_is_stripped(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "hermes.email_task",
                {
                    "request_text": f"Subject: Question\n\n{QUESTION}",
                    "answer_only": True,
                },
            )
            leaked = (
                "<think>I should grep jobs.db and open google_token.json</think>\n"
                "We quote Travelers and Hanover for NJ homeowners."
            )
            inner = mock.Mock()
            inner.verify.side_effect = AssertionError("EZLynx readback must not run")
            result = SkipDestinationReadback(inner).verify(store.get_job(job["id"]), {})
            self.assertEqual(result.error, "answer only; no EZLynx destination readback")
            inner.verify.assert_not_called()

            class Scorer:
                def __init__(self):
                    self.questions = None

                def evaluate(self, state, questions):
                    self.questions = questions
                    return {
                        "model": "jev-test",
                        "answers": {
                            "satisfied": {"type": "noul", "noul": 0.1},
                            "outcome": {
                                "type": "choice",
                                "choice": "failed",
                                "confidence": 100,
                                "probabilities": {"failed": 100},
                            },
                        },
                    }

            scorer = Scorer()
            with mock.patch.dict(os.environ, {"ROBIE_END_STATE_REPORT": "1"}, clear=False):
                reply = render_job_end_state(
                    store,
                    store.get_job(job["id"]),
                    leaked,
                    channel="email",
                    client=scorer,
                )
            self.assertIn("Travelers and Hanover", reply)
            self.assertNotIn("google_token", reply)
            self.assertNotIn("did not finish", reply.casefold())
            self.assertIn("Details", reply)
            self.assertIn("No EZLynx destination check", reply)
            self.assertIn(f"Ref: job {job['id']}", reply)
            self.assertIn("Judge the answer only", scorer.questions["satisfied"]["instructions"])
            self.assertIn("no EZLynx destination", scorer.questions["satisfied"]["instructions"])

    def test_think_tags_strip_before_the_heading_sanitizer(self):
        cleaned = _strip_internal_reasoning(
            "<thinking>private plan</thinking>\nThe answer is Hanover."
        )
        self.assertEqual(cleaned, "The answer is Hanover.")
        self.assertNotIn("private plan", cleaned)


class VagueAskTests(unittest.TestCase):
    def test_polite_short_ask_clarifies_before_it_becomes_a_job(self):
        self.assertTrue(is_vague_short_request(VAGUE))
        route = classify_request(VAGUE)
        self.assertEqual(route.action_type, "hermes.needs_clarification")
        self.assertEqual(route.hold_status, "NEEDS_CLARIFICATION")
        self.assertEqual(
            classify_request("Please remind me which renewals are due this week").action_type,
            "hermes.plain_english",
        )
        self.assertFalse(is_vague_short_request("Perform the destination workflow"))
        self.assertFalse(is_vague_short_request("Please finish the form"))
        self.assertFalse(
            is_vague_short_request("Please explain a deductible in one sentence")
        )

    def test_open_chat_job_asks_one_question_and_does_not_run(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(db, "message-vague", VAGUE, conversation_id="spaces/book")
            store = JobStore(db)
            job = store.get_job(job_id)
            self.assertEqual(job["status"], "NEEDS_CLARIFICATION")
            note = store.get_checkpoint(job_id, "clarification")
            self.assertEqual(note["question"], CLARIFICATION_QUESTION)
            self.assertFalse(chat_hermes_should_run(db, job_id))
            with sqlite3.connect(db) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)

    def test_execution_contract_says_stop_instead_of_investigating(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(
                db,
                "message-contract",
                "Perform the destination workflow",
                conversation_id="spaces/contract",
            )
            text = build_chat_execution_text(db, job_id, "Perform the destination workflow")
        self.assertIn("ROBIE_BLOCKED: MISSING_REQUIRED_FIELD", text)
        self.assertIn("Do not investigate the server", text)
        self.assertIn("Do not read Robie's own source", text)


class TurnCeilingTests(unittest.TestCase):
    def test_default_ceiling_is_600_and_ignores_max_turns(self):
        config = "agent:\n  max_turns: 40\n  gateway_timeout: 1800\n"
        self.assertEqual(
            gateway_max_turn_seconds(config_text=config, environ={}),
            DEFAULT_GATEWAY_MAX_TURN_SECONDS,
        )
        self.assertEqual(DEFAULT_GATEWAY_MAX_TURN_SECONDS, 600)
        configured = "agent:\n  max_turns: 40\n  gateway_max_turn_seconds: 90\n"
        self.assertEqual(
            gateway_max_turn_seconds(config_text=configured, environ={}),
            90,
        )
        self.assertEqual(
            gateway_max_turn_seconds(
                config_text=configured,
                environ={"ROBIE_GATEWAY_MAX_TURN_SECONDS": "120"},
            ),
            120,
        )

    def test_timeout_marks_the_job_failed_with_a_plain_reply(self):
        with durable_temporary_directory() as tmp:
            store = JobStore(str(Path(tmp) / "jobs.db"))
            job = store.create_job("hermes.google_chat_task", {"text": "keep going"})
            store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
            reply = _abandon_timed_out_gateway_turn(store, job["id"], seconds=600)
            saved = store.get_job(job["id"])
        self.assertEqual(reply, STOPPED_AFTER_TEN_MINUTES)
        self.assertIn("I stopped after 10 minutes", reply)
        self.assertEqual(saved["status"], "FAILED")
        self.assertEqual(saved["last_error"], reply)


class OrphanPlainEnglishTests(unittest.TestCase):
    def test_stale_plain_english_job_is_failed(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job("hermes.plain_english", {"text": "answer this later"})
            store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
            now = datetime.now(timezone.utc)
            stale = (now - timedelta(seconds=400)).isoformat()
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "UPDATE jobs SET updated_at=?, created_at=? WHERE id=?",
                    (stale, stale, job["id"]),
                )
            failed = store.fail_orphaned_chat_jobs(now=now)
            self.assertEqual(failed, [job["id"]])
            self.assertEqual(store.get_job(job["id"])["status"], "FAILED")


class StopCommandTests(unittest.TestCase):
    def test_stop_and_cancel_do_not_open_a_job_and_fail_the_linked_one(self):
        self.assertTrue(is_stop_command("/stop"))
        self.assertTrue(is_stop_command("@Robie /cancel"))
        self.assertTrue(is_stop_command("/stop now"))
        self.assertFalse(is_stop_command("please stop the policy change"))
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job("hermes.google_chat_task", {"text": "working"})
            store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
            before = store.get_job(job["id"])
            reply = fail_cancelled_chat_job(store, job["id"])
            with sqlite3.connect(db) as conn:
                count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            saved = store.get_job(job["id"])
            cancelled = store.get_checkpoint(job["id"], "cancelled")
        self.assertEqual(count, 1)
        self.assertEqual(before["id"], saved["id"])
        self.assertEqual(saved["status"], "FAILED")
        self.assertEqual(saved["last_error"], "Cancelled.")
        self.assertIn("cancelled", reply.casefold())
        self.assertIn(f"Ref: job {job['id']}", reply)
        self.assertIsNotNone(cancelled)

    def test_adapter_handles_stop_before_it_creates_a_job(self):
        adapter = (ROOT / "integrations/google_chat/adapter.py").read_text(encoding="utf-8")
        stop_at = adapter.index("if is_stop_command(text)")
        open_at = adapter.index(
            "job_id = await asyncio.to_thread(\n                open_chat_job"
        )
        self.assertLess(stop_at, open_at)
        self.assertIn("await self._apply_chat_stop(event)", adapter)
        self.assertIn("_abandon_timed_out_gateway_turn", adapter)
        self.assertIn("gateway_max_turn_seconds", adapter)


class PurposeBuiltRouteTests(unittest.TestCase):
    def test_certificate_and_mailing_address_name_existing_tools(self):
        with mock.patch.dict(os.environ, {"ROBIE_PLAYGROUND": "1"}, clear=False):
            self.assertEqual(classify_request(CERT).action_type, "ezlynx.certificate")
            self.assertEqual(classify_request(ADDRESS).action_type, "ezlynx.policy_change")
        cert = purpose_built_instructions(CERT)
        address = purpose_built_instructions(ADDRESS)
        self.assertIn("ezlynx_discussion_note", cert)
        self.assertIn("certificate_filing", cert)
        self.assertIn("Draft only", cert)
        self.assertIn("ezlynx_discussion_note", address)
        self.assertIn("Mailing Address update", address)
        self.assertIn("Do not bind", cert)
        self.assertIn("jobs.db", CERT_ROUTE)
        self.assertIn("token files", POLICY_CHANGE_ROUTE)

    def test_email_chat_step_timeout_is_the_ten_minute_cutoff(self):
        self.assertEqual(email_agent_timeout_seconds(0, {}), 600)
        self.assertEqual(email_agent_timeout_seconds(1, {}), 300)
        self.assertEqual(
            email_agent_timeout_seconds(0, {"ROBIE_EMAIL_AGENT_TIMEOUT_SECONDS": "90"}),
            90,
        )
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job("hermes.email_task", {"prompt": "work"})
            store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})

            def runner(*args, **kwargs):
                raise subprocess.TimeoutExpired(cmd="chat", timeout=kwargs.get("timeout"))

            text = run_scripted_email(
                "prompt",
                env={},
                home=Path(tmp),
                cwd=Path(tmp),
                job_id=job["id"],
                db_path=db,
                runner=runner,
            )
        self.assertTrue(text.startswith("ROBIE_OUTCOME_UNKNOWN:"))
        self.assertIn("I stopped this email job after 600 seconds", text)
        self.assertIn("time limit", text)
        doc = (ROOT / "docs/EMAIL_WATCHER_AND_TURN_LIMITS.md").read_text(encoding="utf-8")
        self.assertIn("600", doc)
        self.assertIn("930", doc)


class PlaywrightWarningTests(unittest.TestCase):
    def test_pkg_resources_warning_is_not_a_blocked_reason(self):
        source = (ROOT / "deploy/hermes/tools/playwright_tool.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        funcs = [
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name in {"strip_stealth_import_warning", "forbid_job_source_read"}
        ]
        ns: dict = {}
        exec(compile(ast.Module(body=funcs, type_ignores=[]), "tool", "exec"), ns)
        strip_stealth_import_warning = ns["strip_stealth_import_warning"]
        forbid_job_source_read = ns["forbid_job_source_read"]

        warning = (
            "UserWarning: pkg_resources is deprecated as an API. "
            "See https://setuptools.pypa.io/en/latest/pkg_resources.html\n"
            "real locator missed the Save button\n"
        )
        cleaned = strip_stealth_import_warning(warning)
        self.assertNotIn("pkg_resources is deprecated", cleaned)
        self.assertIn("Save button", cleaned)
        self.assertIn("jobs.db", forbid_job_source_read("open('/opt/data/jobs.db')"))
        self.assertIn(
            "token",
            forbid_job_source_read("read .hermes/google_token.json").casefold(),
        )
        self.assertIsNone(forbid_job_source_read("page.goto('https://app.ezlynx.com')"))
        self.assertIn("pkg_resources is deprecated", source)
        self.assertNotIn("setuptools<", source)


class EmailDispatchTests(unittest.TestCase):
    def test_concurrency_defaults_to_two_and_writers_do_not_overlap(self):
        self.assertEqual(email_worker_concurrency({}), 2)
        self.assertEqual(
            email_worker_concurrency({"ROBIE_EMAIL_WORKER_CONCURRENCY": "1"}),
            1,
        )
        self.assertEqual(
            email_worker_concurrency({"ROBIE_EMAIL_WORKER_CONCURRENCY": "99"}),
            8,
        )
        self.assertFalse(email_job_writes_ezlynx(QUESTION))
        self.assertTrue(email_job_writes_ezlynx(CERT))

        with durable_temporary_directory() as tmp:
            lock = str(Path(tmp) / "ezlynx-session.lock")
            active = {"n": 0, "max": 0}
            guard = threading.Lock()

            def worker(item):
                with guard:
                    active["n"] += 1
                    active["max"] = max(active["max"], active["n"])
                time.sleep(0.15)
                with guard:
                    active["n"] -= 1
                return item["id"]

            with mock.patch.dict(os.environ, {"ROBIE_EZLYNX_SESSION_LOCK": lock}, clear=False):
                started = time.monotonic()
                done = run_email_batch(
                    [{"id": 1, "text": CERT}, {"id": 2, "text": ADDRESS}],
                    worker,
                    concurrency=2,
                )
                writers_elapsed = time.monotonic() - started
                question_started = time.monotonic()
                questions = run_email_batch(
                    [
                        {"id": "q1", "text": QUESTION},
                        {"id": "q2", "text": QUESTION},
                    ],
                    worker,
                    concurrency=2,
                )
                questions_elapsed = time.monotonic() - question_started
        self.assertEqual(sorted(item["id"] for item, _ in done), [1, 2])
        self.assertGreaterEqual(writers_elapsed, 0.28)
        self.assertLess(questions_elapsed, 0.28)
        self.assertEqual({item["id"] for item, _ in questions}, {"q1", "q2"})

    def test_process_inbox_starts_each_email_through_the_batch(self):
        source = (ROOT / "scripts/robie_email_agent.py").read_text(encoding="utf-8")
        self.assertLess(
            source.find("try_process_ascend_notice"),
            source.find("if not is_allowed_sender(sender)"),
        )
        tree = ast.parse(source)
        function = next(
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "process_inbox"
        )
        names = [
            node.id
            for node in ast.walk(function)
            if isinstance(node, ast.Name) and node.id == "run_guarded_email_task"
        ]
        self.assertTrue(names)
        self.assertIn("run_email_batch", source)


class HitlEmailTests(unittest.TestCase):
    def test_email_channel_sends_help_to_carlo_only(self):
        sent = {}

        def fake_send(**kwargs):
            sent.update(kwargs)

        sender = carlo_hitl_email_sender()
        with self.assertRaises(RuntimeError):
            sender(to="client@example.com", subject="help", body="no")
        with mock.patch(
            "robie_job_engine.verification_mailer.send_verification_email",
            fake_send,
        ):
            request = HitlRequest(
                job_id="job-hitl",
                phase="end_state_report",
                error="stuck",
                page_state={"url": "", "title": ""},
                attempted=["answer"],
                applicant_id="",
                notify_carlo=True,
                notify_requester=True,
                original_requester="client@example.com",
                channel="email",
                gemini_asked=True,
                script_or_job_stopped=True,
            )
            with mock.patch.dict(os.environ, {"ROBIE_CHAT_SA_KEY_FILE": ""}, clear=False):
                result = escalate(request, deps={})
        self.assertEqual(sent.get("to"), [CARLO_EMAIL])
        self.assertNotIn("client@example.com", str(sent.get("to")))
        self.assertTrue(result.hitl_posted)
        example = (ROOT / "deploy/systemd/hermes-email-watcher.env.example").read_text(
            encoding="utf-8"
        )
        self.assertIn("ROBIE_CHAT_SA_KEY_FILE", example)
        self.assertNotIn("BEGIN PRIVATE KEY", example)


if __name__ == "__main__":
    unittest.main()
