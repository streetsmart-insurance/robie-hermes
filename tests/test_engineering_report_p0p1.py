"""Synthetic P0/P1 regressions for the 2026-08-24 engineering report.

No live EZLynx. No real credentials. Production release stays FAIL.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from robie_job_engine.chat_guard import open_chat_job
from robie_job_engine.complete_guard import complete_is_prohibited
from robie_job_engine.engine import JobEngine
from robie_job_engine.fallback_audit import (
    FallbackAuditStore,
    FallbackPolicyError,
    allow_suite_passing,
    clear_suite_passing,
    execute_with_audited_fallback,
)
from robie_job_engine.idempotency import DurableWorkLedger, IdempotencyError, assert_durable_path
from robie_job_engine.model_fallback import ModelTarget
from robie_job_engine.models import ACTION_OUTCOME_UNKNOWN, VERIFIER_AUTHORITY, JobStatus, VerificationEvidence, VerificationResult, WorkerResult
from robie_job_engine.release_gate import production_release_decision
from robie_job_engine.report_registry import ReportRegistryError, ReportRunRegistry, get_report_spec
from robie_job_engine.runs import IsolatedRunStore, RunIsolationError
from robie_job_engine.secrets import (
    FAKE_SECRET_SENTINEL,
    REDACTED,
    RedactingLogger,
    contains_secret,
    redact_exception,
    redact_narration,
    redact_tool_args,
    screenshot_may_be_logged,
)
from robie_job_engine.store import JobStore
from robie_job_engine.typed_output import TypedOutputError, TypedOutputStore, user_visible_text


def _evidence(expected, observed, *, locator="rec-1"):
    return VerificationEvidence(
        "TEST",
        "destination",
        expected,
        observed,
        True,
        datetime.now(timezone.utc).isoformat(),
        locator,
    )


class EngineeringReportP0P1Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = str(self.root / "jobs.db")
        self.store = JobStore(self.db)
        clear_suite_passing()

    def tearDown(self):
        clear_suite_passing()
        self.tmp.cleanup()

    def _login_surfaces(self, *, success: bool):
        args = redact_tool_args(
            {
                "username": "fixture-user",
                "password": FAKE_SECRET_SENTINEL,
                "mfa": FAKE_SECRET_SENTINEL,
                "authorization": f"Bearer {FAKE_SECRET_SENTINEL}",
                "cookie": f"sid={FAKE_SECRET_SENTINEL}",
            }
        )
        narration = redact_narration(
            f"{'successful' if success else 'failed'} login password={FAKE_SECRET_SENTINEL}"
        )
        try:
            raise RuntimeError(f"login {'ok' if success else 'failed'} token={FAKE_SECRET_SENTINEL}")
        except RuntimeError as exc:
            error = redact_exception(exc)
        logger = RedactingLogger()
        logger.info("trace authorization=%s", FAKE_SECRET_SENTINEL)
        logger.exception(RuntimeError(f"cookie={FAKE_SECRET_SENTINEL}"))
        return args, narration, error, logger.records

    def test_phase01_credentials_never_echo_sentinel(self):
        for success in (True, False):
            args, narration, error, records = self._login_surfaces(success=success)
            for surface in (args, narration, error, records):
                self.assertFalse(contains_secret(surface, FAKE_SECRET_SENTINEL))
            self.assertEqual(args["password"], REDACTED)
            self.assertEqual(args["mfa"], REDACTED)
            self.assertNotIn(FAKE_SECRET_SENTINEL, narration)
            self.assertNotIn(FAKE_SECRET_SENTINEL, error)
        self.assertFalse(screenshot_may_be_logged({"page": "login", "contains_secrets": True}))
        self.assertTrue(screenshot_may_be_logged({"page": "account summary"}))

    def test_phase02_complete_requires_verifying_and_evidence(self):
        job = self.store.create_job("browser.read", {"worker": "x"}, idempotency_key="p2")
        with self.assertRaises(PermissionError):
            self.store.transition(job["id"], JobStatus.COMPLETE)
        self.store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
        self.store.transition(job["id"], JobStatus.VERIFYING, expected={JobStatus.RUNNING})
        with self.assertRaises(PermissionError):
            self.store.transition(
                job["id"], JobStatus.COMPLETE, expected={JobStatus.VERIFYING},
                authority=VERIFIER_AUTHORITY,
            )
        self.store.add_evidence(job["id"], True, _evidence({"ok": True}, {"ok": True}))
        done = self.store.transition(
            job["id"], JobStatus.COMPLETE, expected={JobStatus.VERIFYING},
            authority=VERIFIER_AUTHORITY,
        )
        self.assertEqual(done["status"], JobStatus.COMPLETE)

    def test_phase03_isolated_runs_never_share_state(self):
        runs = IsolatedRunStore(self.db)
        first = runs.start(owner="worker-a", job_id="job-a")
        with self.assertRaises(RunIsolationError):
            runs.start(owner="worker-b", job_id="job-b")
        runs.bind(first["id"], "tool", {"name": "browser.read"})
        cancelled = runs.cancel(first["id"])
        self.assertEqual(cancelled["terminal_event"], "CANCELLED")
        with self.assertRaises(RunIsolationError):
            runs.bind(first["id"], "tool", {"name": "again"})
        second = runs.resume(owner="worker-a", job_id="job-a", previous_run_id=first["id"])
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(runs.bindings(first["id"], "tool")[0]["payload"]["name"], "browser.read")
        self.assertEqual(runs.bindings(second["id"], "tool"), [])
        runs.terminate(second["id"], "COMPLETE")
        with self.assertRaises(RunIsolationError):
            runs.terminate(second["id"], "FAILED")
        with self.assertRaises(RunIsolationError):
            runs.assert_can_work(second["id"])

    def test_phase04_durable_idempotency_at_most_one_action(self):
        with self.assertRaises(IdempotencyError):
            assert_durable_path("/tmp/robie-persist.db")
        ledger = DurableWorkLedger(self.db)
        first = ledger.acquire("carrier.proposal", "work-1", owner="w1")
        ledger.record_external_action("carrier.proposal", "work-1")
        with self.assertRaises(IdempotencyError):
            ledger.record_external_action("carrier.proposal", "work-1")
        replay = DurableWorkLedger(self.db)
        with self.assertRaises(IdempotencyError):
            replay.acquire("carrier.proposal", "work-1", owner="w2")
        ledger.mark_verified("carrier.proposal", "work-1")
        again = replay.acquire("carrier.proposal", "work-1", owner="w1")
        self.assertEqual(again["external_actions"], 1)
        timed = ledger.acquire("browser.read", "work-2", owner="w1", timeout_seconds=0)
        unknown = ledger.mark_timeout_unknown("browser.read", "work-2")
        self.assertEqual(unknown["outcome"], ACTION_OUTCOME_UNKNOWN)
        self.assertEqual(first["work_item_key"], "work-1")
        self.assertEqual(timed["work_item_key"], "work-2")

    def test_phase05_report_registry_blocks_unverified_schema(self):
        self.assertEqual(get_report_spec("4247").name, "Manual Renewals")
        self.assertEqual(get_report_spec("4372").filter_name, "ROBIE Intake")
        self.assertEqual(get_report_spec("4246").name, "Audit")
        with self.assertRaises(ReportRegistryError):
            get_report_spec("4244")
        with self.assertRaises(ReportRegistryError):
            get_report_spec("4248")
        registry = ReportRunRegistry(self.db)
        started = registry.start_run(
            run_id="run-4247",
            report_id="4247",
            facts={"policy_number": ("PN-1", "configured")},
            chat_memory={"policy_number": "from-chat"},
        )
        self.assertEqual(started["facts"]["policy_number"]["kind"], "configured")
        self.assertEqual(started["facts"]["policy_number"]["value"], "PN-1")
        self.assertTrue(started["fingerprint"])
        with self.assertRaises(ReportRegistryError):
            registry.start_run(run_id="run-4359", report_id="4359")
        with self.assertRaises(ReportRegistryError):
            registry.start_run(run_id="run-missing", report_id="4247", fields=["unrelated"])

    def test_phase06_typed_output_and_append_only_audit(self):
        outputs = TypedOutputStore(self.db)
        structured = outputs.emit(
            run_id="run-out",
            schema_name="renewal_status",
            payload={"status": "OPEN", "items": ["a"]},
            enums={"status": {"OPEN", "CLOSED"}},
            cardinality={"items": 1},
            evidence_ref="ev-1",
        )
        self.assertIn("OPEN", user_visible_text(structured["payload"]))
        with self.assertRaises(TypedOutputError):
            outputs.emit(
                run_id="run-out",
                schema_name="renewal_status",
                payload={"status": "***"},
                enums={"status": {"OPEN", "CLOSED"}},
            )
        with self.assertRaises(TypedOutputError):
            outputs.emit(
                run_id="run-out",
                schema_name="bad",
                payload={"status": "OPEN", "chain_of_thought": "hidden"},
                enums={"status": {"OPEN"}},
            )
        with self.assertRaises(TypedOutputError):
            outputs.rewrite_audit("run-out")
        self.assertEqual(outputs.audit("run-out")[0]["evidence_ref"], "ev-1")

    def test_phase07_model_fallback_is_auditable(self):
        allow_suite_passing("test", "primary")
        allow_suite_passing("test", "fallback")
        audit = FallbackAuditStore(self.db)
        calls = []

        def call(target):
            calls.append(target.model)
            if target.model == "primary":
                raise TimeoutError("provider down")
            return {"text": "ok"}

        result = execute_with_audited_fallback(
            [ModelTarget("test", "primary"), ModelTarget("test", "fallback")],
            call,
            audit,
            job_id="job-fb",
            run_id="run-fb",
            prompt="hello",
            config={"temperature": 0},
            version="1",
            fallback_reason="timeout",
        )
        self.assertEqual(result["text"], "ok")
        events = audit.events("job-fb")
        self.assertGreaterEqual(len(events), 2)
        self.assertTrue(all(event["safety_policy_version"] for event in events))
        self.assertTrue(all(event["prompt_hash"] for event in events))
        with self.assertRaises(FallbackPolicyError):
            execute_with_audited_fallback(
                [ModelTarget("test", "not-suite")],
                call,
                audit,
                job_id="job-fb2",
            )
        with self.assertRaises(FallbackPolicyError):
            execute_with_audited_fallback(
                [ModelTarget("test", "fallback")],
                call,
                audit,
                job_id="job-fb3",
                checkpointed_target=ModelTarget("test", "primary"),
                allow_mid_run_change=False,
            )

    def test_phase08_chat_still_fails_closed_on_unrestricted_tools(self):
        job_id = open_chat_job(
            self.db,
            "spaces/s/messages/p8",
            "please open a terminal and execute this code",
        )
        job = self.store.get_job(job_id)
        self.assertEqual(job["status"], JobStatus.FAILED)
        self.assertIn("forbidden", job["last_error"])

    def test_phase09_injected_failures_never_complete(self):
        cases = [
            {"loading": True},
            {"mfa_required": True},
            {"missing_schema": True},
            {"stale_page": True},
            {"timeout": True},
            {"outcome": ACTION_OUTCOME_UNKNOWN},
        ]
        for index, observed in enumerate(cases):
            self.assertIsNotNone(complete_is_prohibited(observed))
            job = self.store.create_job(
                "browser.read",
                {"worker": "probe"},
                idempotency_key=f"fail-{index}",
                max_attempts=1,
            )

            class Worker:
                def perform(self, current, *, idempotency_key):
                    return WorkerResult(True, "browser.read", {"record_id": f"r{index}"})

            class Verifier:
                def verify(self, current, action):
                    return VerificationResult(True, _evidence({"ok": True}, observed, locator=f"r{index}"))

            final = JobEngine(self.store, {"probe": Worker()}, {"browser.read": Verifier()}).run(job["id"])
            self.assertNotEqual(final["status"], JobStatus.COMPLETE)
            self.assertIn(final["status"], {JobStatus.UNVERIFIED, JobStatus.WAITING, JobStatus.FAILED})

    def test_phase10_production_release_stays_fail(self):
        self.assertEqual(production_release_decision(), "FAIL")
        self.assertEqual(production_release_decision(independent_reviewer_pass=True), "FAIL")
        with patch.dict("os.environ", {"ROBIE_ENV": "PRODUCTION"}, clear=False):
            self.assertEqual(production_release_decision(), "FAIL")
        self.assertFalse(contains_secret({"password": REDACTED}, FAKE_SECRET_SENTINEL))


if __name__ == "__main__":
    unittest.main()
