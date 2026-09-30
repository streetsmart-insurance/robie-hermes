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
)
from robie_job_engine.chat_guard import (
    build_chat_execution_text,
    chat_hermes_should_run,
    guard_chat_response,
    open_chat_job,
)
from robie_job_engine.chat_turn_control import (
    DEFAULT_GATEWAY_MAX_TURN_SECONDS,
    HAND_DRIVEN_EZLYNX_STOP,
    STOPPED_AFTER_TEN_MINUTES,
    _abandon_timed_out_gateway_turn,
    fail_cancelled_chat_job,
    gateway_max_turn_seconds,
    is_stop_command,
    kill_agent_processes,
    record_note_tool_failure,
    refuse_hand_driven_ezlynx,
    register_agent_process,
    terminate_gateway_agent,
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
        ceiling = adapter.split("async def _run_gateway_turn_with_ceiling", 1)[1]
        ceiling = ceiling.split("async def _apply_chat_stop", 1)[0]
        self.assertIn("running_agent_task", ceiling)
        self.assertIn("await asyncio.wait_for(agent, timeout=limit)", ceiling)
        self.assertNotIn("wait_for(self.handle_message", ceiling)
        self.assertIn("terminate_gateway_agent", adapter)
        self.assertIn("fail_gateway_restart_orphans", adapter)
        control = (ROOT / "robie_job_engine/chat_turn_control.py").read_text(encoding="utf-8")
        self.assertIn("cancel_session_processing", control)
        self.assertIn("kill_agent_processes", control)


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


if __name__ == "__main__":
    unittest.main()
