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
import signal
import sqlite3
import subprocess
import sys
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
    rewrite_unproved_address_note,
    strip_answer_verifier_noise,
)
from robie_job_engine.chat_guard import (
    build_chat_execution_text,
    chat_hermes_should_run,
    guard_chat_response,
    open_chat_job,
)
from robie_job_engine.chat_turn_control import (
    BUSY_SESSION_REPLY,
    DEFAULT_GATEWAY_MAX_TURN_SECONDS,
    HAND_DRIVEN_EZLYNX_STOP,
    STOPPED_AFTER_TEN_MINUTES,
    STOPPED_OUTPUT,
    _abandon_timed_out_gateway_turn,
    agent_output_blocked,
    NOTHING_RUNNING_REPLY,
    fail_cancelled_chat_job,
    fresh_turn_history,
    gateway_max_turn_seconds,
    incoming_message_action,
    is_stop_command,
    messages_for_chat_turn,
    resolve_stop_target,
    kill_agent_processes,
    kill_job_recordings,
    record_busy_reply,
    record_chat_delivery,
    record_note_tool_failure,
    refuse_current_tool_call,
    refuse_hand_driven_ezlynx,
    register_agent_process,
    request_agent_stop,
    session_is_busy,
    terminate_gateway_agent,
    watch_turn_ceiling,
)
from robie_job_engine.ezlynx_discussions import discussion_request_headers
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
from robie_job_engine.end_state_report import render_answer_only, render_job_end_state
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
        ceiling = adapter.split("async def _run_gateway_turn_with_ceiling", 1)[1]
        ceiling = ceiling.split("async def _apply_chat_stop", 1)[0]
        self.assertIn("running_agent_task", ceiling)
        self.assertIn("asyncio.create_task(", ceiling)
        self.assertIn("watch_turn_ceiling(", ceiling)
        self.assertNotIn("await asyncio.wait_for", ceiling)
        self.assertNotIn("await watch_turn_ceiling", ceiling)
        self.assertNotIn("await agent", ceiling)
        self.assertNotIn("wait_for(self.handle_message", ceiling)
        self.assertIn('os.getenv("GOOGLE_CHAT_MAX_MESSAGES", "1")', adapter)
        self.assertIn("robie_stop_notice", adapter)
        self.assertIn("agent_output_blocked", adapter)
        self.assertIn("terminate_gateway_agent", adapter)
        self.assertIn("fail_gateway_restart_orphans", adapter)
        control = (ROOT / "robie_job_engine/chat_turn_control.py").read_text(encoding="utf-8")
        watch = control.split("async def watch_turn_ceiling", 1)[1]
        watch = watch.split("def running_agent_task", 1)[0]
        self.assertIn("await asyncio.wait_for(asyncio.shield(agent), timeout=limit)", watch)
        terminate = control.split("async def terminate_gateway_agent", 1)[1]
        self.assertIn("_await_interrupt", terminate)
        self.assertIn("_interrupt_and_clear_session", terminate)
        self.assertIn("_release_session_guard", terminate)
        self.assertIn("kill_job_recordings", terminate)
        awaited = control.split("async def _await_interrupt", 1)[1]
        self.assertIn("inspect.isawaitable(result)", awaited)
        self.assertIn("await result", awaited)
        self.assertIn("cancel_session_processing", control)
        self.assertIn("kill_agent_processes", control)
        self.assertIn("BUSY_SESSION_REPLY", adapter)
        defer_at = adapter.index("BUSY_SESSION_REPLY")
        self.assertLess(stop_at, defer_at)
        self.assertLess(defer_at, open_at)
        between = adapter.split("if is_stop_command(text)", 1)[1].split(
            "job_id = await asyncio.to_thread", 1
        )[0]
        self.assertNotIn("interrupt", between)
        self.assertIn("record_chat_delivery", adapter)
        self.assertIn("robie_delivery_kind", adapter)
        for relative in (
            "deploy/hermes/tools/ezlynx_note_tool.py",
            "deploy/hermes/tools/ezlynx_document_tool.py",
            "deploy/hermes/tools/policy_setup_tool.py",
        ):
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("refuse_current_tool_call", source)
        playwright = (ROOT / "deploy/hermes/tools/playwright_tool.py").read_text(encoding="utf-8")
        self.assertIn("agent_output_blocked", playwright)


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


class ProveFollowUpTests(unittest.TestCase):
    def test_prefixed_questions_use_the_answer_only_route(self):
        questions = (
            "@Robie which carriers do we quote for NJ homeowners?",
            "Can you tell me which carriers we quote for NJ homeowners?",
        )
        with mock.patch.dict(os.environ, {"ROBIE_PLAYGROUND": "1"}, clear=False):
            for text in questions:
                route = classify_request(text)
                self.assertEqual(route.action_type, "hermes.plain_english", text)
                self.assertTrue(route.answer_only, text)
                self.assertTrue(is_informational_ask(text), text)
        with mock.patch.dict(os.environ, {"ROBIE_PLAYGROUND": ""}, clear=False):
            self.assertEqual(
                classify_request("Please change the liability limit on this policy").action_type,
                "hermes.plain_english",
            )

    def test_answer_only_close_is_not_the_general_destination_path(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            with mock.patch.dict(
                os.environ,
                {"ROBIE_PLAYGROUND": "1", "ROBIE_END_STATE_REPORT": ""},
                clear=False,
            ):
                job_id = open_chat_job(
                    db,
                    "spaces/s/messages/question",
                    "@Robie which carriers do we quote for NJ homeowners?",
                    conversation_id="spaces/question",
                )
                reply = guard_chat_response(
                    db,
                    job_id,
                    "We quote Travelers and Hanover for NJ homeowners.",
                )
            job = JobStore(db).get_job(job_id)
        self.assertTrue(job["payload"]["answer_only"])
        self.assertEqual(job["action_type"], "hermes.plain_english")
        self.assertEqual(job["status"], "UNVERIFIED")
        self.assertEqual(job["last_error"], "answer only; no EZLynx destination readback")
        self.assertIn("Travelers and Hanover", reply)
        self.assertNotIn("no structured destination", reply)

    def test_address_change_routes_to_policy_change(self):
        text = "Change the mailing address for Buster Brown to 100 Test Mailing Rd"
        with mock.patch.dict(os.environ, {"ROBIE_PLAYGROUND": ""}, clear=False):
            self.assertEqual(classify_request(text).action_type, "ezlynx.policy_change")
        with mock.patch.dict(os.environ, {"ROBIE_PLAYGROUND": "1"}, clear=False):
            self.assertEqual(classify_request(text).action_type, "ezlynx.policy_change")
            self.assertEqual(
                classify_request("Please change the deductible on this policy").action_type,
                "ezlynx.policy_change",
            )
        self.assertIn("ezlynx_discussion_note", purpose_built_instructions(text))

    def test_restart_fails_running_chat_jobs_with_a_fresh_heartbeat(self):
        with durable_temporary_directory() as tmp:
            store = JobStore(str(Path(tmp) / "jobs.db"))
            job = store.create_job("hermes.plain_english", {"text": "still going"})
            store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
            store.checkpoint(job["id"], "gateway_progress", {"source": "old-process"})
            kept = store.create_job("ezlynx.reassign", {"text": "bounded"})
            store.transition(kept["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
            failed = store.fail_gateway_restart_orphans()
            self.assertEqual(failed, [job["id"]])
            self.assertEqual(store.get_job(job["id"])["status"], "FAILED")
            self.assertIn("restarted", store.get_job(job["id"])["last_error"])
            self.assertEqual(store.get_job(kept["id"])["status"], "RUNNING")

    def test_stop_kills_the_browser_process_and_cancels_the_agent(self):
        proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
        register_agent_process("job-stop", proc.pid)

        class Task:
            def __init__(self):
                self.cancelled = False

            def done(self):
                return self.cancelled

            def cancel(self):
                self.cancelled = True

        task = Task()

        class Adapter:
            def __init__(self):
                self._session_tasks = {}
                self._background_tasks = {task}
                self.calls = []

            def interrupt_session_activity(self, key, chat_id):
                self.calls.append(("interrupt", key, chat_id))

            async def cancel_session_processing(self, key):
                self.calls.append(("cancel", key))

        class Source:
            chat_id = "spaces/1"
            thread_id = "thread"

        class Event:
            source = Source()

        adapter = Adapter()
        try:
            import asyncio

            asyncio.run(
                terminate_gateway_agent(adapter, Event(), "job-stop", reason="/stop")
            )
            proc.wait(timeout=3)
        finally:
            if proc.poll() is None:
                kill_agent_processes("job-stop")
                proc.kill()
                proc.wait(timeout=3)
        self.assertIsNotNone(proc.returncode)
        self.assertNotEqual(proc.returncode, 0)
        self.assertTrue(task.cancelled)
        self.assertIn("cancel", [name for name, *_rest in adapter.calls])
        self.assertIn("interrupt", [name for name, *_rest in adapter.calls])

    def test_note_tool_failure_refuses_hand_driven_ezlynx(self):
        with durable_temporary_directory() as tmp:
            store = JobStore(str(Path(tmp) / "jobs.db"))
            job = store.create_job("hermes.google_chat_task", {"text": "file the note"})
            record_note_tool_failure(store, job["id"], "HTTP 403 error 1010")
            saved = store.get_job(job["id"])
            self.assertEqual(
                refuse_hand_driven_ezlynx(saved, store=store),
                HAND_DRIVEN_EZLYNX_STOP,
            )
            policy = store.create_job(
                "ezlynx.policy_change",
                {"text": "Change the mailing address for Buster Brown"},
            )
            self.assertEqual(refuse_hand_driven_ezlynx(store.get_job(policy["id"])), HAND_DRIVEN_EZLYNX_STOP)
        self.assertIsNone(refuse_hand_driven_ezlynx({"id": "x", "action_type": "browser.read", "payload": {}}))

    def test_discussion_requests_use_a_browser_identity(self):
        api = "https://app.uatezlynx.com/DiscussionApi/v8/discussions/by-applicant"
        token = "https://app.ezlynx.com/auth/connect/token"
        closed = discussion_request_headers(api, port_open=lambda: False)
        self.assertIn("Mozilla", closed["User-Agent"])
        self.assertNotIn("Cookie", closed)
        cookies = [{"name": "sid", "value": "secret-value", "domain": ".uatezlynx.com"}]
        loaded = {"called": False}

        def loader():
            loaded["called"] = True
            return cookies

        headed = discussion_request_headers(api, cookie_loader=loader)
        self.assertTrue(loaded["called"])
        self.assertIn("sid=secret-value", headed["Cookie"])
        self.assertIn("Mozilla", headed["User-Agent"])
        self.assertTrue(headed["Origin"].startswith("https://"))
        token_headers = discussion_request_headers(token, cookie_loader=loader)
        self.assertNotIn("Cookie", token_headers)
        self.assertIn("Mozilla", token_headers["User-Agent"])

        def boom():
            raise AssertionError("CDP must not be opened when the port is closed")

        with mock.patch(
            "robie_job_engine.ezlynx_portal_session.load_cdp_session_cookies",
            boom,
        ):
            again = discussion_request_headers(api, port_open=lambda: False)
        self.assertNotIn("Cookie", again)

    def test_missing_pkg_resources_skips_stealth_immediately(self):
        source = (ROOT / "deploy/hermes/tools/playwright_tool.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        func = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "apply_playwright_stealth"
        )
        ns = {"sys": sys, "warnings": __import__("warnings")}
        exec(compile(ast.Module(body=[func], type_ignores=[]), "tool", "exec"), ns)
        import builtins

        real_import = builtins.__import__

        def blocked(name, *args, **kwargs):
            if name == "playwright_stealth" or str(name).startswith("playwright_stealth"):
                raise ImportError("No module named 'pkg_resources'")
            return real_import(name, *args, **kwargs)

        started = time.monotonic()
        with mock.patch("builtins.__import__", side_effect=blocked):
            result = ns["apply_playwright_stealth"](object(), log=lambda _message: None)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(result["error"], "pkg_resources missing")
        self.assertEqual(result["applied"], 0)
        requirements = (ROOT / "deploy/requirements-test-gateway-playwright.txt").read_text(
            encoding="utf-8"
        )
        self.assertIn("setuptools>=70,<81", requirements)
        self.assertIn("continuing without stealth", source)


def _clear_agent_stop(job_id: str) -> None:
    from robie_job_engine import chat_turn_control as control

    with control._PROC_LOCK:
        control._ABORTED_JOBS.discard(str(job_id))


def _load_hermes_tool(module_name: str, filename: str):
    """Load a Hermes tool without the real tools package."""
    import importlib.util
    import sys
    from types import ModuleType

    tools_pkg = sys.modules.get("tools")
    created_tools = tools_pkg is None
    if created_tools:
        tools_pkg = ModuleType("tools")
    registry_mod = ModuleType("tools.registry")

    def tool_error(message):
        return {"ok": False, "error": message}

    def tool_result(payload):
        return {"ok": True, "result": payload}

    class DummyRegistry:
        def register(self, *args, **kwargs):
            return None

    registry_mod.registry = DummyRegistry()
    registry_mod.tool_error = tool_error
    registry_mod.tool_result = tool_result
    previous = {name: sys.modules.get(name) for name in ("tools", "tools.registry")}
    sys.modules["tools"] = tools_pkg
    sys.modules["tools.registry"] = registry_mod
    tools_pkg.registry = registry_mod
    path = ROOT / "deploy" / "hermes" / "tools" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module, previous, created_tools


def _restore_modules(previous: dict, created_tools: bool) -> None:
    import sys

    for name, module in previous.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module
    if created_tools:
        sys.modules.pop("tools", None)


class StopWhileJobRunsTests(unittest.TestCase):
    def test_stop_is_handled_while_a_long_job_is_still_running(self):
        """One Chat slot. The job handler must return so /stop is not stuck behind it."""
        import asyncio

        class OneSlot:
            def __init__(self):
                self.busy = False
                self.order = []
                self.agent = None
                self.watchdog = None

            async def dispatch(self, kind):
                if self.busy:
                    raise AssertionError(f"{kind} is stuck behind the running job")
                self.busy = True
                try:
                    if kind == "job":
                        await self._start_job()
                    else:
                        self.order.append("stop")
                        if self.agent is not None and not self.agent.done():
                            self.agent.cancel()
                        if self.watchdog is not None and not self.watchdog.done():
                            self.watchdog.cancel()
                finally:
                    self.busy = False

            async def _start_job(self):
                async def long_agent():
                    await asyncio.sleep(30)

                self.agent = asyncio.create_task(long_agent())
                self.ceiling_fired = False

                async def on_timeout():
                    self.ceiling_fired = True

                self.watchdog = asyncio.create_task(
                    watch_turn_ceiling(
                        self.agent,
                        limit=30,
                        on_timeout=on_timeout,
                        stop_requested=lambda: False,
                    )
                )
                self.order.append("job-handler-returned")

        async def scenario():
            slot = OneSlot()
            await asyncio.wait_for(slot.dispatch("job"), timeout=1)
            self.assertEqual(slot.order, ["job-handler-returned"])
            self.assertFalse(slot.agent.done())
            await asyncio.wait_for(slot.dispatch("stop"), timeout=1)
            self.assertEqual(slot.order, ["job-handler-returned", "stop"])
            with self.assertRaises(asyncio.CancelledError):
                await slot.watchdog
            self.assertFalse(slot.ceiling_fired)
            return slot

        asyncio.run(scenario())

    def test_ceiling_fires_from_the_watchdog_after_the_handler_returns(self):
        import asyncio

        job_id = "job-ceiling-watch"

        async def scenario():
            async def long_agent():
                await asyncio.sleep(30)

            agent = asyncio.create_task(long_agent())
            notices = []

            async def on_timeout():
                request_agent_stop(job_id)
                agent.cancel()
                notices.append("ceiling")

            outcome = await watch_turn_ceiling(
                agent,
                limit=0.05,
                on_timeout=on_timeout,
                stop_requested=lambda: False,
            )
            return outcome, notices

        try:
            outcome, notices = asyncio.run(scenario())
            self.assertEqual(outcome, "timeout")
            self.assertEqual(notices, ["ceiling"])
            self.assertEqual(agent_output_blocked(job_id, None), STOPPED_OUTPUT)
            self.assertEqual(refuse_current_tool_call({"job_id": job_id}), STOPPED_OUTPUT)
        finally:
            _clear_agent_stop(job_id)

    def test_interrupt_coroutine_is_awaited(self):
        import asyncio

        seen = []

        class Adapter:
            def __init__(self):
                self._session_tasks = {}
                self._background_tasks = set()

            async def interrupt_session_activity(self, key, chat_id):
                seen.append(("interrupt", key, chat_id))

            async def cancel_session_processing(self, key):
                seen.append(("cancel", key))

        class Source:
            chat_id = "spaces/1"
            thread_id = "thread"

        class Event:
            source = Source()

        try:
            asyncio.run(
                terminate_gateway_agent(Adapter(), Event(), "job-await", reason="/stop")
            )
            self.assertIn(("interrupt", "chat:spaces/1:thread", "spaces/1"), seen)
            self.assertIn(("cancel", "chat:spaces/1:thread"), seen)
            self.assertEqual(agent_output_blocked("job-await", None), STOPPED_OUTPUT)
        finally:
            _clear_agent_stop("job-await")

    def test_stopped_or_timed_out_job_cannot_send_or_call_tools(self):
        import asyncio

        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            running = store.create_job("hermes.google_chat_task", {"text": "working"})
            store.transition(
                running["id"], JobStatus.RUNNING, expected={JobStatus.PENDING}
            )
            self.assertIsNone(agent_output_blocked(running["id"], store))
            self.assertIsNone(
                refuse_current_tool_call({"job_id": running["id"], "db_path": db})
            )

            timed = store.create_job("hermes.google_chat_task", {"text": "too long"})
            store.transition(timed["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
            store.transition(
                timed["id"],
                JobStatus.FAILED,
                expected={JobStatus.RUNNING},
                error="I stopped after 10 minutes.",
            )
            store.checkpoint(timed["id"], "gateway_turn_timeout", {"reason": "limit"})
            cancelled = store.create_job("hermes.google_chat_task", {"text": "stop me"})
            store.transition(
                cancelled["id"], JobStatus.RUNNING, expected={JobStatus.PENDING}
            )
            store.transition(
                cancelled["id"],
                JobStatus.FAILED,
                expected={JobStatus.RUNNING},
                error="Cancelled.",
            )
            store.checkpoint(cancelled["id"], "cancelled", {"by": "/stop"})
            answer = store.create_job(
                "hermes.plain_english", {"text": "Which carriers?"}
            )
            store.transition(
                answer["id"], JobStatus.UNVERIFIED, expected={JobStatus.PENDING}
            )

            self.assertEqual(agent_output_blocked(timed["id"], store), STOPPED_OUTPUT)
            self.assertEqual(
                agent_output_blocked(cancelled["id"], store), STOPPED_OUTPUT
            )
            self.assertIsNone(agent_output_blocked(answer["id"], store))

            request_agent_stop(running["id"])
            try:
                self.assertEqual(
                    refuse_current_tool_call({"job_id": running["id"], "db_path": db}),
                    STOPPED_OUTPUT,
                )
                note, previous, created = _load_hermes_tool(
                    "ezlynx_note_tool_stop_test", "ezlynx_note_tool.py"
                )
                playwright, playwright_previous, playwright_created = _load_hermes_tool(
                    "playwright_tool_stop_test", "playwright_tool.py"
                )
                try:
                    with mock.patch(
                        "robie_job_engine.ezlynx_api_only_writes.add_note_to_discussion",
                        side_effect=AssertionError("stopped job must not file a note"),
                    ):
                        note_result = note.ezlynx_discussion_note_handler(
                            {"applicant_id": "26356199", "note_text": "Robie was here"},
                            job_id=running["id"],
                            db_path=db,
                        )
                    with mock.patch(
                        "subprocess.Popen",
                        side_effect=AssertionError("stopped job must not start a browser"),
                    ):
                        browser_result = playwright.playwright_exec(
                            "page.goto('https://app.ezlynx.com')",
                            job_id=running["id"],
                            db_path=db,
                        )
                finally:
                    _restore_modules(previous, created)
                    _restore_modules(playwright_previous, playwright_created)
            finally:
                _clear_agent_stop(running["id"])

            self.assertFalse(note_result["ok"])
            self.assertIn(STOPPED_OUTPUT, note_result["error"])
            self.assertNotIn("note_id", note_result)
            self.assertFalse(browser_result["ok"])
            self.assertIn("PLAYWRIGHT_BLOCKED", browser_result["error"])
            self.assertIn(STOPPED_OUTPUT, browser_result["error"])

        async def already_stopped():
            calls = []

            async def agent():
                return "posted late"

            task = asyncio.create_task(agent())
            await task

            async def on_timeout():
                calls.append("timeout")

            outcome = await watch_turn_ceiling(
                task,
                limit=5,
                on_timeout=on_timeout,
                stop_requested=lambda: True,
            )
            return outcome, calls

        outcome, calls = asyncio.run(already_stopped())
        self.assertEqual(outcome, "stopped")
        self.assertEqual(calls, [])


class LiveDiscussionApiTests(unittest.TestCase):
    def test_live_mode_uses_prod_host_and_browser_login_and_fails_closed(self):
        from robie_job_engine.ezlynx_api import EzlynxApiConfig, EzlynxApiConfigurationError
        from robie_job_engine.ezlynx_api_only_writes import (
            discussion_api_target,
            load_discussion_api_config,
        )
        from robie_job_engine.ezlynx_discussions import file_note_to_existing_discussion
        from robie_job_engine.ezlynx_write_scope import (
            EzlynxWriteScopeError,
        )
        import robie_job_engine.ezlynx_write_scope as scope

        live = EzlynxApiConfig(
            token_endpoint="https://app.ezlynx.com/auth/connect/token",
            document_base_url="https://app.ezlynx.com/documentapi/",
            client_id="cid",
            client_secret="csecret",
            username="vendor-json-user",
            integration_group_id="159",
            scope="DocumentApi openid",
        )
        uat = EzlynxApiConfig(
            token_endpoint="https://app.uatezlynx.com/auth/connect/token",
            document_base_url="https://app.uatezlynx.com/documentapi/",
            client_id="cid",
            client_secret="csecret",
            username="uat-user",
            integration_group_id="159",
            scope="DocumentApi openid",
        )
        calls = []

        def loader(*args, **kwargs):
            calls.append(kwargs.get("environment"))
            if kwargs.get("environment") == "PRODUCTION":
                return live
            raise AssertionError("UAT load must not run in live mode")

        class Accessor:
            def __init__(self):
                self.refs = []

            def access(self, ref):
                self.refs.append(ref)
                if ref.endswith("ezlynx-username/versions/latest"):
                    return "SSRobie"
                if ref.endswith("ezlynx-password/versions/latest"):
                    return "live-login-secret"
                raise AssertionError(f"unexpected secret {ref}")

        environ = {
            "ROBIE_EZLYNX_DISCUSSION_API": "live",
            "ROBIE_EZLYNX_API_PROD_SECRET": "projects/p/secrets/ezlynx-api-prod/versions/latest",
            "ROBIE_EZLYNX_USERNAME_SECRET": "projects/p/secrets/ezlynx-username/versions/latest",
            "ROBIE_EZLYNX_PASSWORD_SECRET": "projects/p/secrets/ezlynx-password/versions/latest",
            "ROBIE_ENV": "TEST",
        }
        accessor = Accessor()
        with mock.patch(
            "robie_job_engine.ezlynx_api.load_ezlynx_api_config", side_effect=loader
        ):
            config = load_discussion_api_config(accessor=accessor, environ=environ)
        self.assertEqual(calls, ["PRODUCTION"])
        self.assertEqual(config.username, "SSRobie")
        self.assertEqual(config.password, "live-login-secret")
        self.assertIn("app.ezlynx.com", config.discussion_base_url)
        self.assertNotIn("uatezlynx", config.discussion_base_url)
        self.assertNotIn("live-login-secret", repr(config))
        self.assertNotIn("password", repr(config).casefold())

        from urllib import parse

        from robie_job_engine.ezlynx_discussions import DiscussionApiClient

        class FakeResponse:
            def read(self):
                return b'{"access_token":"tok","expires_in":3600}'

        class Urlopen:
            def __init__(self):
                self.calls = []

            def __call__(self, url, *, data=None, headers=None, timeout=None):
                self.calls.append(data)
                return FakeResponse()

        urlopen = Urlopen()
        client = DiscussionApiClient(config, urlopen=urlopen)
        self.assertEqual(client.get_token(), "tok")
        form = parse.parse_qs(urlopen.calls[0].decode("utf-8"))
        self.assertEqual(form["password"], ["live-login-secret"])
        self.assertEqual(form["username"], ["SSRobie"])

        missing = dict(environ)
        missing.pop("ROBIE_EZLYNX_PASSWORD_SECRET")
        calls.clear()
        with mock.patch(
            "robie_job_engine.ezlynx_api.load_ezlynx_api_config", side_effect=loader
        ):
            with self.assertRaises(RuntimeError) as missing_secret:
                load_discussion_api_config(accessor=accessor, environ=missing)
        self.assertIn("Refusing to fall back to UAT", str(missing_secret.exception))
        self.assertNotIn("live-login-secret", str(missing_secret.exception))
        self.assertEqual(calls, ["PRODUCTION"])

        def uat_host(*args, **kwargs):
            calls.append(kwargs.get("environment"))
            return uat

        calls.clear()
        with mock.patch(
            "robie_job_engine.ezlynx_api.load_ezlynx_api_config", side_effect=uat_host
        ):
            with self.assertRaises(RuntimeError) as wrong_host:
                load_discussion_api_config(accessor=accessor, environ=environ)
        self.assertIn("points at UAT", str(wrong_host.exception))
        self.assertNotIn(None, calls)
        self.assertNotIn("TEST", calls)

        def broken(*args, **kwargs):
            calls.append(kwargs.get("environment"))
            raise EzlynxApiConfigurationError("prod secret missing")

        calls.clear()
        with mock.patch(
            "robie_job_engine.ezlynx_api.load_ezlynx_api_config", side_effect=broken
        ):
            with self.assertRaises(RuntimeError) as broken_secret:
                load_discussion_api_config(accessor=accessor, environ=environ)
        self.assertIn("Refusing to fall back to UAT", str(broken_secret.exception))
        self.assertEqual(calls, ["PRODUCTION"])

        self.assertEqual(discussion_api_target({}), "env")
        with self.assertRaises(RuntimeError):
            discussion_api_target({"ROBIE_EZLYNX_DISCUSSION_API": "maybe"})

        class NoteClient:
            def __init__(self):
                self.calls = 0

            def get_discussions(self, applicant_id):
                self.calls += 1
                raise AssertionError("HTTP must not run for a refused applicant")

        note_client = NoteClient()
        with mock.patch.object(
            scope, "ALLOWED_EZLYNX_WRITE_APPLICANT_IDS", frozenset({"26356199"})
        ), mock.patch.object(scope, "_CERT_SWEEP_INDEX_APPLICANT_IDS", None):
            with self.assertRaises(EzlynxWriteScopeError):
                file_note_to_existing_discussion(note_client, "220250093", "Robie was here")
        self.assertEqual(note_client.calls, 0)

        runbook = (ROOT / "docs/TEST_HERMES_OPERATOR_RUNBOOK.md").read_text(encoding="utf-8")
        self.assertIn("ROBIE_EZLYNX_DISCUSSION_API=live", runbook)
        self.assertIn("ROBIE_EZLYNX_WRITE_APPLICANT_IDS=26356199", runbook)
        self.assertIn("does not fall back to UAT", runbook)
        example = (ROOT / "deploy/systemd/robie-ezlynx.env.example").read_text(encoding="utf-8")
        self.assertIn("# ROBIE_EZLYNX_DISCUSSION_API=live", example)

    def test_note_tool_uses_the_newest_enabled_password_not_the_pin(self):
        from types import SimpleNamespace

        from robie_job_engine.ezlynx_api import EzlynxApiConfig
        from robie_job_engine.ezlynx_api_only_writes import load_discussion_api_config

        live = EzlynxApiConfig(
            token_endpoint="https://app.ezlynx.com/auth/connect/token",
            document_base_url="https://app.ezlynx.com/documentapi/",
            client_id="cid",
            client_secret="csecret",
            username="vendor-json-user",
            integration_group_id="159",
            scope="DocumentApi openid",
        )
        password_parent = "projects/p/secrets/ezlynx-password"
        username_parent = "projects/p/secrets/ezlynx-username"
        stale_password = "pw-v7-STALE-DO-NOT-LOG"
        current_password = "pw-v15-CURRENT-DO-NOT-LOG"

        class Client:
            def list_secret_versions(self, request):
                parent = request["parent"]
                if parent == password_parent:
                    return [
                        SimpleNamespace(name=parent + "/versions/7", create_time=7),
                        SimpleNamespace(name=parent + "/versions/15", create_time=15),
                    ]
                return [
                    SimpleNamespace(name=parent + "/versions/3", create_time=3),
                ]

        class Accessor:
            def __init__(self, client):
                self._client = client
                self.refs = []

            def access(self, ref):
                self.refs.append(ref)
                if ref == username_parent + "/versions/3":
                    return "SSRobie"
                if ref == password_parent + "/versions/15":
                    return current_password
                if ref.endswith("/versions/7"):
                    return stale_password
                raise AssertionError(ref)

        environ = {
            "ROBIE_EZLYNX_DISCUSSION_API": "live",
            "ROBIE_EZLYNX_API_PROD_SECRET": "projects/p/secrets/ezlynx-api-prod/versions/latest",
            "ROBIE_EZLYNX_USERNAME_SECRET": username_parent + "/versions/2",
            "ROBIE_EZLYNX_PASSWORD_SECRET": password_parent + "/versions/7",
            "ROBIE_ENV": "TEST",
        }
        accessor = Accessor(Client())
        with mock.patch(
            "robie_job_engine.ezlynx_api.load_ezlynx_api_config", return_value=live
        ), self.assertLogs("robie_job_engine.ezlynx_api_only_writes", level="INFO") as logs:
            config = load_discussion_api_config(accessor=accessor, environ=environ)
        self.assertEqual(config.password, current_password)
        self.assertNotEqual(config.password, stale_password)
        self.assertIn(password_parent + "/versions/15", accessor.refs)
        self.assertNotIn(password_parent + "/versions/7", accessor.refs)
        self.assertIn(username_parent + "/versions/3", accessor.refs)
        logged = "\n".join(logs.output)
        self.assertIn("versions/7", logged)
        self.assertIn("versions/15", logged)
        self.assertNotIn(current_password, logged)
        self.assertNotIn(stale_password, logged)

        class PinnedAccessor:
            def __init__(self):
                self.refs = []

            def access(self, ref):
                self.refs.append(ref)
                if ref.endswith("ezlynx-username/versions/latest"):
                    return "SSRobie"
                if ref.endswith("ezlynx-password/versions/latest"):
                    return "pw-latest-alias-DO-NOT-LOG"
                raise AssertionError(ref)

        pinned_env = dict(environ)
        pinned_env["ROBIE_EZLYNX_USERNAME_SECRET"] = (
            "projects/p/secrets/ezlynx-username/versions/latest"
        )
        pinned_env["ROBIE_EZLYNX_PASSWORD_SECRET"] = password_parent + "/versions/7"
        pinned = PinnedAccessor()
        with mock.patch(
            "robie_job_engine.ezlynx_api.load_ezlynx_api_config", return_value=live
        ), self.assertLogs("robie_job_engine.ezlynx_api_only_writes", level="INFO") as pinned_logs:
            pinned_config = load_discussion_api_config(accessor=pinned, environ=pinned_env)
        self.assertEqual(pinned_config.password, "pw-latest-alias-DO-NOT-LOG")
        self.assertIn(password_parent + "/versions/latest", pinned.refs)
        self.assertNotIn(password_parent + "/versions/7", pinned.refs)
        pinned_text = "\n".join(pinned_logs.output)
        self.assertIn("versions/latest", pinned_text)
        self.assertNotIn("pw-latest-alias-DO-NOT-LOG", pinned_text)
        self.assertNotIn(stale_password, pinned_text)


class ProveSessionReleaseTests(unittest.TestCase):
    def test_stop_ends_the_agent_and_releases_the_session_lease(self):
        import asyncio

        from robie_job_engine.chat_guard import (
            _GENERIC_CHAT_HEARTBEATS,
            start_generic_chat_job_heartbeat,
            stop_generic_chat_job_heartbeat,
        )

        async def scenario(store, job_id):
            agent = {"alive": True, "beats": 0}
            released = {}

            async def loop():
                while agent["alive"]:
                    agent["beats"] += 1
                    await asyncio.sleep(0.05)

            task = asyncio.create_task(loop())
            await asyncio.sleep(0.08)

            class Runner:
                async def _interrupt_and_clear_session(
                    self,
                    key,
                    source,
                    *,
                    interrupt_reason,
                    invalidation_reason,
                    release_running_state=True,
                ):
                    released["awaited"] = True
                    released["key"] = key
                    released["reason"] = interrupt_reason
                    agent["alive"] = False
                    released["ok"] = True

            class Adapter:
                def __init__(self):
                    self._active_sessions = {
                        "google_chat:dm:spaces/1": asyncio.Event()
                    }
                    self._session_tasks = {"google_chat:dm:spaces/1": task}
                    self._pending_messages = {"google_chat:dm:spaces/1": ["held"]}
                    self._background_tasks = {task}
                    self.gateway_runner = Runner()
                    self.interrupts = []

                def _event_session_key(self, event):
                    return "google_chat:dm:spaces/1"

                async def interrupt_session_activity(self, key, chat_id=None):
                    self.interrupts.append(key)

                async def cancel_session_processing(self, key):
                    found = self._session_tasks.pop(key, None)
                    if found is not None and not found.done():
                        found.cancel()

            class Source:
                chat_id = "spaces/1"
                thread_id = ""

            class Event:
                source = Source()

            adapter = Adapter()
            await terminate_gateway_agent(
                adapter, Event(), job_id, reason="/stop", store=store
            )
            return adapter, agent, released, task

        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job("hermes.google_chat_task", {"text": "long job"})
            store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
            start_generic_chat_job_heartbeat(db, job["id"], interval_seconds=30)
            try:
                adapter, agent, released, task = asyncio.run(scenario(store, job["id"]))
            finally:
                stop_generic_chat_job_heartbeat(db, job["id"])
                _clear_agent_stop(job["id"])
            heartbeat_key = (str(Path(db).resolve()), job["id"])
            self.assertNotIn(heartbeat_key, _GENERIC_CHAT_HEARTBEATS)
            self.assertGreater(agent["beats"], 0)
            self.assertFalse(agent["alive"])
            self.assertTrue(task.done())
            self.assertNotIn("google_chat:dm:spaces/1", adapter._active_sessions)
            self.assertNotIn("google_chat:dm:spaces/1", adapter._session_tasks)
            self.assertNotIn("google_chat:dm:spaces/1", adapter._pending_messages)
            self.assertTrue(released.get("ok"))
            self.assertTrue(released.get("awaited"))
            self.assertEqual(released.get("key"), "google_chat:dm:spaces/1")
            self.assertEqual(adapter.interrupts, [])

    def test_stop_cancels_the_real_runner_session_key(self):
        import asyncio

        real = "agent:main:google_chat:dm:spaces/AAQAZbLJO78"
        derived = "chat:spaces/AAQAZbLJO78"
        other = "agent:main:google_chat:dm:spaces/OTHER"

        async def scenario(store, job_id):
            class Agent:
                def __init__(self):
                    self.alive = True
                    self.beats = 0
                    self.tool_calls = 0

                def tool(self):
                    if not self.alive:
                        return "blocked"
                    self.tool_calls += 1
                    return "ran"

                def interrupt(self, reason=None):
                    self.alive = False

            agent = Agent()

            class Turn:
                def __init__(self, running):
                    self.agent = running

            class State:
                def __init__(self, running):
                    self.turn = Turn(running)

            async def loop():
                while agent.alive:
                    agent.beats += 1
                    agent.tool()
                    await asyncio.sleep(0.05)

            task = asyncio.create_task(loop())
            await asyncio.sleep(0.08)

            class Runner:
                def __init__(self):
                    self._sessions = {real: State(agent)}
                    self.calls = []

                def _peek_session_state(self, key):
                    return self._sessions.get(key)

                async def _interrupt_and_clear_session(
                    self,
                    key,
                    source,
                    *,
                    interrupt_reason,
                    invalidation_reason,
                    release_running_state=True,
                ):
                    self.calls.append(key)
                    state = self._sessions.get(key)
                    if state is None:
                        return
                    state.turn.agent.alive = False

                def _drop_turn_slot(self, key):
                    self._sessions.pop(key, None)

            class Adapter:
                def __init__(self):
                    self._active_sessions = {
                        real: asyncio.Event(),
                        other: asyncio.Event(),
                    }
                    self._session_tasks = {real: task}
                    self._pending_messages = {real: ["held"]}
                    self.gateway_runner = Runner()

                def _event_session_key(self, event):
                    return derived

                async def cancel_session_processing(self, key):
                    found = self._session_tasks.pop(key, None)
                    if found is not None and not found.done():
                        found.cancel()

            class Source:
                chat_id = "spaces/AAQAZbLJO78"
                thread_id = ""

            class Event:
                source = Source()

            adapter = Adapter()
            event = Event()
            self.assertTrue(session_is_busy(adapter, event))
            with self.assertLogs("robie.chat_turn", level="WARNING") as logs:
                await terminate_gateway_agent(
                    adapter, event, job_id, reason="/stop", store=store
                )
            beats = agent.beats
            tools = agent.tool_calls
            await asyncio.sleep(0.2)
            first_keys = store.get_checkpoint(job_id, "session_stop_keys")
            await terminate_gateway_agent(
                adapter, event, job_id, reason="/stop", store=store
            )
            await asyncio.sleep(0.2)
            return adapter, agent, task, beats, tools, logs, first_keys

        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job("hermes.google_chat_task", {"text": "long job"})
            store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
            try:
                adapter, agent, task, beats, tools, logs, first_keys = asyncio.run(
                    scenario(store, job["id"])
                )
                self.assertEqual(
                    refuse_current_tool_call({"job_id": job["id"], "db_path": db}),
                    STOPPED_OUTPUT,
                )
            finally:
                _clear_agent_stop(job["id"])
            self.assertEqual(adapter.gateway_runner.calls[0], real)
            self.assertFalse(agent.alive)
            self.assertTrue(task.done())
            self.assertEqual(agent.beats, beats)
            self.assertEqual(agent.tool_calls, tools)
            self.assertGreater(tools, 0)
            self.assertEqual(agent.tool(), "blocked")
            self.assertNotIn(real, adapter._active_sessions)
            self.assertNotIn(real, adapter._session_tasks)
            self.assertNotIn(real, adapter._pending_messages)
            self.assertNotIn(real, adapter.gateway_runner._sessions)
            self.assertIn(other, adapter._active_sessions)
            logged = "\n".join(logs.output)
            self.assertIn(real, logged)
            self.assertIn(derived, logged)
            self.assertEqual(first_keys["resolved"], real)
            self.assertEqual(first_keys["derived"], derived)
            self.assertEqual(adapter.gateway_runner.calls[0], real)

            class Source:
                chat_id = "spaces/AAQAZbLJO78"
                thread_id = ""

            class Fresh:
                source = Source()

            fresh = Fresh()
            self.assertFalse(session_is_busy(adapter, fresh))
            self.assertEqual(
                incoming_message_action(
                    session_busy=session_is_busy(adapter, fresh),
                    is_stop=False,
                ),
                "run",
            )

    def test_stop_ends_a_parked_wait_without_another_message(self):
        """A clarify/question wait is not an asyncio cancel point.

        The lease must stop renewing, and the session task must be done(),
        without putting a follow-up message on the queue.
        """
        import asyncio

        real = "agent:main:google_chat:dm:spaces/AAQAZbLJO78"

        async def scenario(store, job_id):
            parked = threading.Event()
            queue: asyncio.Queue = asyncio.Queue()
            renewals = {"n": 0}

            class Lease:
                def __init__(self):
                    self._stop = threading.Event()
                    self.released = False

                def run(self):
                    while not self._stop.is_set():
                        renewals["n"] += 1
                        if parked.wait(0.05):
                            break

                def release(self):
                    self.released = True
                    self._stop.set()

            lease = Lease()
            lease_thread = threading.Thread(target=lease.run, daemon=True)
            lease_thread.start()

            async def parked_turn():
                await asyncio.to_thread(parked.wait, 30)

            task = asyncio.create_task(parked_turn())
            await asyncio.sleep(0.08)
            self.assertFalse(task.done())

            class Runner:
                def __init__(self):
                    self._parked_waits = {real: parked}
                    self._active_session_leases = {real: lease}
                    self._session_history = {
                        real: [
                            {"role": "user", "content": "what does COI stand for?"},
                            {"role": "assistant", "content": "ACORD 25"},
                        ]
                    }
                    self._sessions = {}

                async def _interrupt_and_clear_session(self, key, source, **kwargs):
                    return None

            class Adapter:
                def __init__(self):
                    self._active_sessions = {real: asyncio.Event()}
                    self._session_tasks = {real: task}
                    self._pending_messages = {}
                    self.gateway_runner = Runner()

                def _event_session_key(self, event):
                    return "chat:spaces/AAQAZbLJO78"

            class Source:
                chat_id = "spaces/AAQAZbLJO78"
                thread_id = ""

            class Event:
                source = Source()

            adapter = Adapter()
            await terminate_gateway_agent(
                adapter, Event(), job_id, reason="/stop", store=store
            )
            after = renewals["n"]
            await asyncio.sleep(0.35)
            return adapter, task, queue, lease, lease_thread, after, renewals["n"]

        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job("hermes.google_chat_task", {"text": "long job"})
            store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
            try:
                adapter, task, queue, lease, lease_thread, after, later = asyncio.run(
                    scenario(store, job["id"])
                )
            finally:
                _clear_agent_stop(job["id"])
            lease_thread.join(timeout=1)
            self.assertTrue(task.done())
            self.assertTrue(lease.released)
            self.assertFalse(lease_thread.is_alive())
            self.assertEqual(later, after)
            self.assertTrue(queue.empty())
            self.assertEqual(adapter.gateway_runner._session_history[real], [])

    def test_stop_closes_history_so_the_next_message_stands_alone(self):
        import asyncio

        real = "agent:main:google_chat:dm:spaces/AAQAZbLJO78"

        async def scenario():
            class Entry:
                def __init__(self, session_id):
                    self.session_id = session_id

            class SessionStore:
                def __init__(self):
                    self.session_id = "old-session"
                    self._entries = {real: Entry("old-session")}

                def reset_session(self, key):
                    self.session_id = "fresh-session"
                    entry = Entry(self.session_id)
                    self._entries[key] = entry
                    return entry

            class Runner:
                def __init__(self):
                    self.session_store = SessionStore()
                    self._session_history = {
                        real: [
                            {"role": "assistant", "content": "ACORD 25"},
                            {"role": "assistant", "content": "Operation interrupted."},
                        ]
                    }
                    self._sessions = {}

            class Adapter:
                def __init__(self):
                    self._active_sessions = {real: object()}
                    self._session_tasks = {}
                    self._pending_messages = {}
                    self.gateway_runner = Runner()

                def _event_session_key(self, event):
                    return "chat:spaces/AAQAZbLJO78"

            class Source:
                chat_id = "spaces/AAQAZbLJO78"
                thread_id = ""

            class Event:
                source = Source()

            adapter = Adapter()
            event = Event()
            await terminate_gateway_agent(
                adapter, event, None, reason="gateway_max_turn_seconds", store=None
            )
            self.assertEqual(adapter.gateway_runner._session_history[real], [])
            self.assertEqual(adapter.gateway_runner.session_store.session_id, "fresh-session")
            adapter.gateway_runner._session_history[real] = [
                {"role": "assistant", "content": "Operation interrupted."},
                {"role": "assistant", "content": "ACORD 25"},
            ]
            adapter.gateway_runner.session_store.session_id = "old-session"
            fresh_turn_history(adapter, event)
            prior = adapter.gateway_runner._session_history[real]
            turn = messages_for_chat_turn(prior, "what does COI stand for?")
            return turn, adapter.gateway_runner.session_store.session_id, prior

        turn, session_id, prior = asyncio.run(scenario())
        self.assertEqual(prior, [])
        self.assertEqual(session_id, "fresh-session")
        self.assertEqual(turn, [{"role": "user", "content": "what does COI stand for?"}])
        blob = " ".join(item["content"] for item in turn)
        self.assertNotIn("ACORD 25", blob)
        self.assertNotIn("Operation interrupted.", blob)
        adapter = (ROOT / "integrations/google_chat/adapter.py").read_text(encoding="utf-8")
        ceiling = adapter.split("async def _run_gateway_turn_with_ceiling", 1)[1]
        ceiling = ceiling.split("async def _begin_fresh_chat_turn", 1)[0]
        self.assertLess(
            ceiling.index("_begin_fresh_chat_turn"),
            ceiling.index("self.handle_message"),
        )

    def test_idle_stop_does_not_touch_a_finished_job(self):
        with durable_temporary_directory() as tmp:
            store = JobStore(str(Path(tmp) / "jobs.db"))
            job = store.create_job(
                "hermes.google_chat_task",
                {"text": "what does COI stand for?"},
            )
            store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
            store.transition(
                job["id"],
                JobStatus.FAILED,
                expected={JobStatus.RUNNING},
                error="done",
                release_lease=True,
            )
            store.checkpoint(
                job["id"],
                "worker_response",
                {"response_text": "ACORD 25\n\nRef: job " + job["id"]},
            )
            self.assertEqual(resolve_stop_target(None, session_busy=False)[1], NOTHING_RUNNING_REPLY)
            self.assertIsNone(resolve_stop_target(None, session_busy=False)[0])
            reply = fail_cancelled_chat_job(store, job["id"])
            self.assertEqual(reply, NOTHING_RUNNING_REPLY)
            self.assertEqual(store.get_job(job["id"])["status"], JobStatus.FAILED.value)
            self.assertIsNone(store.get_checkpoint(job["id"], "cancelled"))
            self.assertIsNone(store.get_checkpoint(job["id"], "agent_abort"))
            self.assertIn(
                "ACORD 25",
                store.get_checkpoint(job["id"], "worker_response")["response_text"],
            )
        adapter = (ROOT / "integrations/google_chat/adapter.py").read_text(encoding="utf-8")
        stop = adapter.split("async def _apply_chat_stop", 1)[1].split(
            "async def _stop_chat_queue_heartbeat", 1
        )[0]
        self.assertNotIn("active_conversation_job", stop)
        self.assertIn("resolve_stop_target", stop)
        self.assertIn("NOTHING_RUNNING_REPLY", stop)
        self.assertIn("idle_stop", stop)
        self.assertIn(
            NOTHING_RUNNING_REPLY,
            (ROOT / "robie_job_engine/chat_turn_control.py").read_text(encoding="utf-8"),
        )
        send = adapter.split("async def send(", 1)[1].split("async def send_card(", 1)[0]
        idle = send.split('delivery_kind == "idle_stop"', 1)[1].split("elif", 1)[0]
        self.assertNotIn("guard_chat_response", idle)
        self.assertNotIn("conversation_job_for_event", idle)

    def test_stop_kills_the_recording_process_group(self):
        proc = subprocess.Popen(
            ["bash", "-c", "sleep 60 & sleep 60 & wait"],
            start_new_session=True,
        )
        deadline = time.time() + 2
        children: list[int] = []
        while time.time() < deadline and len(children) < 2:
            children = _child_pids(proc.pid)
            time.sleep(0.05)
        try:
            self.assertGreaterEqual(len(children), 2, children)
            with durable_temporary_directory() as tmp:
                db = str(Path(tmp) / "jobs.db")
                stop_file = Path(tmp) / "capture.stop"

                class PathStore:
                    def __init__(self, path: str) -> None:
                        self.path = path

                from robie_job_engine.recording import RecordingStore

                recordings = RecordingStore(db)
                row = recordings.create("job-rec", Path(tmp) / "capture.webm", stop_file)
                recordings.update(row["id"], status="RECORDING", capture_pid=proc.pid)
                killed = kill_job_recordings("job-rec", PathStore(db))
                self.assertIn(proc.pid, killed)
                proc.wait(timeout=3)
                saved = recordings.get(row["id"])
                self.assertTrue(stop_file.is_file())
                self.assertEqual(saved["status"], "FAILED")
            self.assertIsNotNone(proc.poll())
            self.assertNotEqual(proc.returncode, 0)
            for child in children:
                self.assertFalse(Path(f"/proc/{child}").exists(), child)
            self.assertEqual(saved["failure"], "stopped")
        finally:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    proc.kill()
                proc.wait(timeout=3)

    def test_a_new_message_is_queued_and_does_not_cancel_the_running_job(self):
        self.assertEqual(
            incoming_message_action(session_busy=True, is_stop=False), "defer"
        )
        self.assertEqual(
            incoming_message_action(session_busy=True, is_stop=True), "stop"
        )
        self.assertEqual(
            incoming_message_action(session_busy=False, is_stop=False), "run"
        )
        self.assertEqual(BUSY_SESSION_REPLY, "I'm finishing another job, one moment.")

        class Agent:
            def __init__(self):
                self.interrupted = False

            def interrupt(self, text=None):
                self.interrupted = True

        agent = Agent()
        running = {"status": "RUNNING"}
        if incoming_message_action(session_busy=True, is_stop=False) != "defer":
            agent.interrupt("Which carriers do we quote?")
            running["status"] = "FAILED"
        self.assertFalse(agent.interrupted)
        self.assertEqual(running["status"], "RUNNING")

        class Adapter:
            def __init__(self):
                self._active_sessions = {}
                self._gateway_turns = {}

            def _event_session_key(self, event):
                thread = getattr(event.source, "thread_id", "") or ""
                return f"google_chat:group:spaces/1:{thread}"

        class Source:
            def __init__(self, thread_id):
                self.chat_id = "spaces/1"
                self.thread_id = thread_id

        class Event:
            def __init__(self, thread_id):
                self.source = Source(thread_id)

        adapter = Adapter()
        live = type("Task", (), {"done": lambda self: False})()
        adapter._active_sessions["google_chat:group:spaces/1:thread-a"] = object()
        adapter._gateway_turns[("spaces/1", "thread-a")] = {
            "job_id": "job-long",
            "task": live,
            "watchdog": live,
        }
        self.assertTrue(session_is_busy(adapter, Event("thread-a")))
        self.assertFalse(session_is_busy(adapter, Event("thread-b")))
        adapter._active_sessions.clear()
        finished = type("Task", (), {"done": lambda self: True})()
        adapter._gateway_turns[("spaces/1", "thread-a")] = {
            "job_id": "job-long",
            "task": finished,
            "watchdog": finished,
        }
        self.assertFalse(session_is_busy(adapter, Event("thread-a")))

    def test_answer_only_omits_file_mutation_verifier_noise(self):
        noisy = (
            "We quote Travelers and Hanover for NJ homeowners.\n"
            "File-mutation verifier: unexpected write under /tmp\n"
        )
        self.assertEqual(
            strip_answer_verifier_noise(noisy),
            "We quote Travelers and Hanover for NJ homeowners.",
        )
        with durable_temporary_directory() as tmp:
            store = JobStore(str(Path(tmp) / "jobs.db"))
            job = store.create_job("hermes.plain_english", {"text": QUESTION})
            with mock.patch.dict(os.environ, {"ROBIE_END_STATE_REPORT": ""}, clear=False):
                reply = render_answer_only(store, store.get_job(job["id"]), noisy)
        self.assertIn("Travelers and Hanover", reply)
        self.assertNotIn("File-mutation", reply)
        self.assertNotIn("verifier", reply.casefold())

    def test_address_note_claims_only_what_readback_proved(self):
        from robie_job_engine.answer_only import address_readback_proved

        unproved = rewrite_unproved_address_note("Mailing address updated")
        self.assertIn("requested", unproved.casefold())
        self.assertNotIn("updated", unproved.casefold())
        self.assertEqual(
            rewrite_unproved_address_note("Mailing address updated", proved=True),
            "Mailing address updated",
        )
        self.assertIn("requested", POLICY_CHANGE_ROUTE.casefold())
        self.assertIn("readback", POLICY_CHANGE_ROUTE.casefold())
        with mock.patch.dict(os.environ, {"ROBIE_PLAYGROUND": ""}, clear=False):
            self.assertEqual(
                classify_request(
                    "Please change the liability limit on this policy"
                ).action_type,
                "hermes.plain_english",
            )
            self.assertEqual(
                classify_request(
                    "Change the mailing address for Buster Brown to 100 Test Mailing Rd"
                ).action_type,
                "ezlynx.policy_change",
            )
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "ezlynx.policy_change",
                {"text": "Change the mailing address"},
            )
            self.assertFalse(
                address_readback_proved({"job_id": job["id"], "db_path": db})
            )
            store.checkpoint(job["id"], "address_readback", {"passed": True})
            self.assertTrue(
                address_readback_proved({"job_id": job["id"], "db_path": db})
            )
            self.assertEqual(
                rewrite_unproved_address_note(
                    "Mailing address updated",
                    proved=address_readback_proved({"job_id": job["id"], "db_path": db}),
                ),
                "Mailing address updated",
            )
        note, previous, created = _load_hermes_tool(
            "ezlynx_note_tool_address_claim", "ezlynx_note_tool.py"
        )
        try:
            with mock.patch(
                "robie_job_engine.ezlynx_api_only_writes.add_note_to_discussion",
                return_value={"status": "filed", "note_id": "note-1"},
            ) as filed:
                note._file_note(
                    {
                        "applicant_id": "26356199",
                        "note_text": "Mailing address updated",
                    }
                )
            sent = filed.call_args[0][1]
        finally:
            _restore_modules(previous, created)
        self.assertIn("requested", sent.casefold())
        self.assertNotIn("updated", sent.casefold())
        self.assertIn("Robie was here", sent)

    def test_stop_and_ceiling_replies_record_delivery(self):
        with durable_temporary_directory() as tmp:
            store = JobStore(str(Path(tmp) / "jobs.db"))
            job = store.create_job("hermes.google_chat_task", {"text": "working"})
            self.assertFalse(
                record_chat_delivery(store, job["id"], "", "Cancelled.", "stop")
            )
            self.assertIsNone(store.get_checkpoint(job["id"], "chat_delivery"))
            self.assertTrue(
                record_chat_delivery(
                    store,
                    job["id"],
                    "spaces/1/messages/9",
                    "I stopped after 10 minutes. Send it again if you still want it done.",
                    "ceiling",
                )
            )
            saved = store.get_checkpoint(job["id"], "chat_delivery")
            worker = store.get_checkpoint(job["id"], "worker_response")
            self.assertEqual(saved["message_id"], "spaces/1/messages/9")
            self.assertTrue(saved["posted"])
            self.assertEqual(saved["kind"], "ceiling")
            self.assertIn("stopped after 10 minutes", str(worker).casefold())
            store.checkpoint(
                job["id"], "worker_response", {"response_text": "keep me"}
            )
            record_chat_delivery(
                store,
                job["id"],
                "spaces/1/messages/10",
                "Stopped. That job is cancelled.",
                "stop",
            )
            self.assertEqual(
                store.get_checkpoint(job["id"], "worker_response"),
                {"response_text": "keep me"},
            )
            self.assertEqual(
                store.get_checkpoint(job["id"], "chat_delivery")["kind"], "stop"
            )
        send = (ROOT / "integrations/google_chat/adapter.py").read_text(encoding="utf-8")
        send = send.split("async def send(", 1)[1].split("async def send_card(", 1)[0]
        self.assertIn("record_chat_delivery", send)
        self.assertIn("robie_stop_notice", send)
        self.assertIn("sent_message_id", send)
        self.assertLess(
            send.index('get("robie_job_id")'),
            send.index("conversation_job_for_event"),
        )
        stop = (ROOT / "integrations/google_chat/adapter.py").read_text(encoding="utf-8")
        stop = stop.split("async def _apply_chat_stop", 1)[1].split(
            "async def _stop_chat_queue_heartbeat", 1
        )[0]
        self.assertIn('"robie_job_id": job_id', stop)
        self.assertIn('"robie_delivery_kind": "stop"', stop)

    def test_ceiling_reply_is_the_one_line_only(self):
        from robie_job_engine.chat_guard import guard_chat_notice

        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job("hermes.google_chat_task", {"text": "long job"})
            store.transition(
                job["id"],
                JobStatus.FAILED,
                expected={JobStatus.PENDING},
                error="gateway_max_turn_seconds",
            )

            def boom(db_path, job_id):
                raise RuntimeError("audit stays in the ledger")

            with mock.patch(
                "robie_job_engine.post_job_audit.maybe_audit_terminal_job",
                side_effect=boom,
            ):
                text = guard_chat_notice(
                    db, job["id"], "  " + STOPPED_AFTER_TEN_MINUTES + "  "
                )
        self.assertEqual(text, STOPPED_AFTER_TEN_MINUTES)
        self.assertNotIn("Recording", text)
        self.assertNotIn("audit", text.casefold())
        send = (ROOT / "integrations/google_chat/adapter.py").read_text(encoding="utf-8")
        send = send.split("async def send(", 1)[1].split("async def send_card(", 1)[0]
        self.assertIn("guard_chat_notice", send)
        self.assertIn('delivery_kind == "ceiling"', send)
        ceiling = send.split('delivery_kind == "ceiling"', 1)[0]
        self.assertIn('delivery_kind == "busy"', ceiling)

    def test_busy_reply_is_recorded_without_replacing_the_job_reply(self):
        with durable_temporary_directory() as tmp:
            store = JobStore(str(Path(tmp) / "jobs.db"))
            job = store.create_job("hermes.google_chat_task", {"text": "working"})
            store.checkpoint(job["id"], "worker_response", {"response_text": "keep me"})
            self.assertFalse(
                record_busy_reply(store, job["id"], "", BUSY_SESSION_REPLY)
            )
            self.assertIsNone(store.get_checkpoint(job["id"], "busy_reply"))
            self.assertTrue(
                record_busy_reply(
                    store,
                    job["id"],
                    "spaces/1/messages/busy",
                    BUSY_SESSION_REPLY,
                )
            )
            saved = store.get_checkpoint(job["id"], "busy_reply")
            self.assertEqual(saved["message_id"], "spaces/1/messages/busy")
            self.assertTrue(saved["posted"])
            self.assertEqual(saved["kind"], "busy")
            self.assertEqual(saved["text"], BUSY_SESSION_REPLY)
            self.assertEqual(
                store.get_checkpoint(job["id"], "worker_response"),
                {"response_text": "keep me"},
            )
        adapter = (ROOT / "integrations/google_chat/adapter.py").read_text(encoding="utf-8")
        self.assertIn("[GoogleChat] busy-session reply", adapter)
        self.assertIn('"robie_delivery_kind": "busy"', adapter)
        self.assertIn('"robie_job_id": busy_job_id', adapter)
        send = adapter.split("async def send(", 1)[1].split("async def send_card(", 1)[0]
        self.assertIn("record_busy_reply", send)
        busy = send.split('delivery_kind == "busy"', 1)[1].split("elif", 1)[0]
        self.assertNotIn("guard_chat_response", busy)

    def test_playwright_silent_skips_proved_and_api_chat_jobs(self):
        from datetime import datetime, timezone

        from robie_job_engine.chat_guard import (
            open_chat_job,
            stop_generic_chat_job_heartbeat,
        )
        from robie_job_engine.models import VerificationEvidence
        from robie_job_engine.playwright_observability import (
            deterministic_path_succeeded,
            fail_closed_zero_playwright_rows,
            job_expected_to_drive_ui,
            job_requires_playwright,
        )

        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            opened = []
            try:
                proved = open_chat_job(db, "spaces/p/messages/1", "finish the commercial auto")
                opened.append(proved)
                store.add_evidence(
                    proved,
                    True,
                    VerificationEvidence(
                        method="API",
                        source="destination",
                        expected={"note_id": "42"},
                        observed={"note_id": "42"},
                        authoritative=True,
                        captured_at=datetime.now(timezone.utc).isoformat(),
                    ),
                )
                current = store.get_job(proved)
                self.assertTrue(job_expected_to_drive_ui(current))
                self.assertTrue(deterministic_path_succeeded(store, current))
                kept = fail_closed_zero_playwright_rows(store, current)
                self.assertNotEqual(kept["status"], JobStatus.FAILED.value)
                self.assertNotIn("PLAYWRIGHT_SILENT", str(kept.get("last_error") or ""))
                self.assertIsNone(store.get_checkpoint(proved, "playwright_silent"))

                readback = open_chat_job(
                    db,
                    "spaces/p/messages/2",
                    "finish the commercial auto in ezlynx",
                )
                opened.append(readback)
                store.checkpoint(readback, "destination_readback", {"passed": True})
                kept_readback = fail_closed_zero_playwright_rows(
                    store, store.get_job(readback)
                )
                self.assertNotEqual(kept_readback["status"], JobStatus.FAILED.value)
                self.assertNotIn(
                    "PLAYWRIGHT_SILENT", str(kept_readback.get("last_error") or "")
                )

                note = store.create_job(
                    "hermes.google_chat_task",
                    {"text": "File a discussion note on the EZLynx applicant"},
                )
                store.transition(
                    note["id"], JobStatus.RUNNING, expected={JobStatus.PENDING}
                )
                note_job = store.get_job(note["id"])
                self.assertFalse(job_requires_playwright(note_job))
                self.assertFalse(job_expected_to_drive_ui(note_job))
                kept_note = fail_closed_zero_playwright_rows(store, note_job)
                self.assertNotEqual(kept_note["status"], JobStatus.FAILED.value)
                self.assertNotIn("PLAYWRIGHT_SILENT", str(kept_note.get("last_error") or ""))

                answer = store.create_job(
                    "hermes.google_chat_task",
                    {"text": "open ezlynx and answer the question", "answer_only": True},
                )
                store.transition(
                    answer["id"], JobStatus.RUNNING, expected={JobStatus.PENDING}
                )
                self.assertFalse(job_expected_to_drive_ui(store.get_job(answer["id"])))
                kept_answer = fail_closed_zero_playwright_rows(
                    store, store.get_job(answer["id"])
                )
                self.assertNotEqual(kept_answer["status"], JobStatus.FAILED.value)

                silent = open_chat_job(
                    db, "spaces/p/messages/4", "finish the commercial auto"
                )
                opened.append(silent)
                failed = fail_closed_zero_playwright_rows(store, store.get_job(silent))
                self.assertEqual(failed["status"], JobStatus.FAILED.value)
                self.assertIn("PLAYWRIGHT_SILENT", failed["last_error"])

                declined = open_chat_job(
                    db,
                    "spaces/p/messages/5",
                    "Summarize the meeting notes. no EZLynx, no browser.",
                )
                opened.append(declined)
                declined_job = store.get_job(declined)
                self.assertFalse(declined_job["payload"]["expected_ui"])
                self.assertFalse(job_requires_playwright(declined_job))
                self.assertFalse(job_expected_to_drive_ui(declined_job))
                kept_declined = fail_closed_zero_playwright_rows(store, declined_job)
                self.assertNotEqual(kept_declined["status"], JobStatus.FAILED.value)
                self.assertNotIn(
                    "PLAYWRIGHT_SILENT", str(kept_declined.get("last_error") or "")
                )

                worded = store.create_job(
                    "hermes.google_chat_task",
                    {
                        "text": "finish the commercial auto in EZLynx, no browser",
                        "expected_ui": False,
                    },
                )
                store.transition(
                    worded["id"], JobStatus.RUNNING, expected={JobStatus.PENDING}
                )
                self.assertIn("ezlynx", store.get_job(worded["id"])["payload"]["text"].casefold())
                self.assertFalse(job_requires_playwright(store.get_job(worded["id"])))
                kept_worded = fail_closed_zero_playwright_rows(
                    store, store.get_job(worded["id"])
                )
                self.assertNotEqual(kept_worded["status"], JobStatus.FAILED.value)
                self.assertNotIn(
                    "PLAYWRIGHT_SILENT", str(kept_worded.get("last_error") or "")
                )

                flagged = store.create_job(
                    "hermes.google_chat_task",
                    {"text": "hello", "expected_ui": True},
                )
                store.transition(
                    flagged["id"], JobStatus.RUNNING, expected={JobStatus.PENDING}
                )
                self.assertTrue(job_requires_playwright(store.get_job(flagged["id"])))
                failed_flag = fail_closed_zero_playwright_rows(
                    store, store.get_job(flagged["id"])
                )
                self.assertEqual(failed_flag["status"], JobStatus.FAILED.value)
                self.assertIn("PLAYWRIGHT_SILENT", failed_flag["last_error"])
            finally:
                for job_id in opened:
                    if job_id:
                        stop_generic_chat_job_heartbeat(db, job_id)


def _child_pids(pid: int) -> list[int]:
    found = []
    proc = Path("/proc")
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        close = stat.rfind(")")
        if close < 0:
            continue
        parts = stat[close + 1 :].split()
        if len(parts) < 2:
            continue
        try:
            ppid = int(parts[1])
        except ValueError:
            continue
        if ppid == pid:
            found.append(int(entry.name))
    return found


if __name__ == "__main__":
    unittest.main()
