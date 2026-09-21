"""Synthetic P0/P1 regressions for the 2026-08-24 engineering report.

No live EZLynx. No real credentials. Production release stays FAIL.
"""

from __future__ import annotations

import dataclasses
import os
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_guard import guard_chat_response, open_chat_job
from robie_job_engine.complete_guard import complete_is_prohibited
from robie_job_engine.request_routing import WORKER_FOR_ACTION
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
from robie_job_engine import report_registry
from robie_job_engine.report_registry import ReportRegistryError, ReportRunRegistry, get_report_spec
from robie_job_engine.runs import IsolatedRunStore, RunIsolationError
from robie_job_engine.runtime_env import ProductionGuardError
from robie_job_engine.secrets import (
    FAKE_SECRET_SENTINEL,
    REDACTED,
    RedactingLogger,
    contains_secret,
    redact_exception,
    redact_narration,
    redact_text,
    redact_tool_args,
    screenshot_may_be_logged,
)
from robie_job_engine.store import JobStore
from robie_job_engine.test_runtime import dispatch_operational_chat, maybe_run_bounded_job
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
        self.tmp = durable_temporary_directory()
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
            self.assertEqual(args["authorization"], REDACTED)
            self.assertNotIn(FAKE_SECRET_SENTINEL, narration)
            self.assertNotIn(FAKE_SECRET_SENTINEL, error)
        self.assertFalse(screenshot_may_be_logged({"page": "login", "contains_secrets": True}))
        self.assertTrue(screenshot_may_be_logged({"page": "account summary"}))

    def test_phase02_complete_requires_verifying_and_evidence(self):
        locator = "https://example.test/rec-1"
        job = self.store.create_job(
            "browser.read",
            {"worker": "x", "locator": {"url": locator}},
            idempotency_key="p2",
        )
        with self.assertRaises(PermissionError):
            self.store.transition(job["id"], JobStatus.COMPLETE)
        self.store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
        self.store.transition(job["id"], JobStatus.VERIFYING, expected={JobStatus.RUNNING})
        with self.assertRaises(PermissionError):
            self.store.transition(
                job["id"], JobStatus.COMPLETE, expected={JobStatus.VERIFYING},
                authority=VERIFIER_AUTHORITY,
            )
        self.store.checkpoint(
            job["id"],
            "action",
            {"action": "browser.read", "destination": {"url": locator}},
        )
        self.store.add_evidence(
            job["id"],
            True,
            _evidence(
                {"url": locator, "title": "Account"},
                {"url": locator, "title": "Account"},
                locator=locator,
            ),
        )
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

    def test_phase03_same_owner_can_renew_after_scheduler_pause_until_reconciled(self):
        runs = IsolatedRunStore(self.db)
        first = runs.start(owner="worker-a", job_id="job-a", lease_seconds=1)
        with runs._connect() as conn:
            conn.execute(
                "UPDATE isolated_runs SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                (first["id"],),
            )
        renewed = runs.renew_lease(first["id"], owner="worker-a", lease_seconds=1)
        self.assertEqual(renewed["status"], "ACTIVE")
        with self.assertRaises(RunIsolationError):
            runs.renew_lease(first["id"], owner="worker-b", lease_seconds=1)
        runs.terminate(first["id"], "ABANDONED")
        with self.assertRaises(RunIsolationError):
            runs.renew_lease(first["id"], owner="worker-a", lease_seconds=1)

    def test_phase04_durable_idempotency_at_most_one_action(self):
        self._assert_canonical_ephemeral_paths_rejected()
        with self.assertRaises(TypeError):
            DurableWorkLedger(self.db, require_durable=False)
        with self.assertRaises(IdempotencyError):
            DurableWorkLedger("/tmp/jobs.db")
        with self.assertRaises(IdempotencyError):
            DurableWorkLedger("/private/tmp/jobs.db")
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

    def test_canonical_ephemeral_paths_rejected_including_macos_private_tmp(self):
        """Linux CI must reject the same canonical forms that fail on macOS."""
        self._assert_canonical_ephemeral_paths_rejected()

    def _assert_canonical_ephemeral_paths_rejected(self):
        from robie_job_engine.idempotency import path_variants

        ephemeral_paths = (
            "/tmp",
            "/tmp/robie-persist.db",
            "/tmp/../tmp/robie-persist.db",
            "/private/tmp",
            "/private/tmp/robie-persist.db",
            "/var/tmp",
            "/var/tmp/robie-persist.db",
            "/private/var/tmp",
            "/private/var/tmp/ledger.db",
        )
        for ephemeral in ephemeral_paths:
            with self.assertRaises(IdempotencyError, msg=ephemeral):
                assert_durable_path(ephemeral)
            with self.assertRaises(IdempotencyError, msg=f"ledger:{ephemeral}"):
                DurableWorkLedger(ephemeral)
        link_dir = self.root / "link-to-tmp"
        if not link_dir.exists():
            try:
                os.symlink("/tmp", link_dir)
            except OSError:
                link_dir = None
        if link_dir is not None:
            with self.assertRaises(IdempotencyError):
                assert_durable_path(link_dir / "ledger.db")
        with patch.object(Path, "resolve", return_value=Path("/private/tmp/hidden.db")):
            with patch("os.path.realpath", return_value="/private/tmp/hidden.db"):
                with self.assertRaises(IdempotencyError):
                    assert_durable_path("/workspace/looks-durable.db")
                variants = path_variants("/workspace/looks-durable.db")
        self.assertIn("/private/tmp/hidden.db", variants)

    def test_intake_and_engine_call_isolated_run_and_durable_ledger(self):
        job_id = open_chat_job(
            self.db,
            "spaces/s/messages/wire-1",
            "generate a proposal and add a $350 fee",
        )
        job = self.store.get_job(job_id)
        ledger = DurableWorkLedger(self.db)
        reserved = ledger.get(job["action_type"], job["idempotency_key"])
        self.assertEqual(reserved["work_item_key"], job["idempotency_key"])
        intake_runs = IsolatedRunStore(self.db).list_runs(job_id)
        self.assertTrue(intake_runs)
        self.assertTrue(any(run["terminal_event"] == "INTAKE" for run in intake_runs))
        self.assertFalse(any(run["status"] == "ACTIVE" for run in intake_runs))
        self.assertIsNotNone(self.store.get_checkpoint(job_id, "durable_work"))

        class Worker:
            def perform(self, current, *, idempotency_key):
                return WorkerResult(True, current["action_type"], {"record_id": "wired"})

        class Verifier:
            def verify(self, current, action):
                return VerificationResult(
                    True,
                    _evidence(
                        {"record_id": "wired", "status": "done"},
                        {"record_id": "wired", "status": "done"},
                        locator="wired",
                    ),
                )

        owners = []
        original_claim = JobStore.claim

        def tracking_claim(store, claimed_id, owner, lease_seconds=120):
            owners.append(owner)
            return original_claim(store, claimed_id, owner, lease_seconds)

        with patch.object(JobStore, "claim", tracking_claim):
            final = JobEngine(
                self.store, {job["payload"]["worker"]: Worker()}, {job["action_type"]: Verifier()}
            ).run(job_id)
        self.assertEqual(final["status"], JobStatus.COMPLETE)
        self.assertEqual(len(owners), 1)
        self.assertNotEqual(owners[0], "robie-job-engine")
        self.assertTrue(owners[0].startswith("robie-job-engine:"))
        item = DurableWorkLedger(self.db).get(job["action_type"], job["idempotency_key"])
        self.assertEqual(item["external_actions"], 1)
        self.assertEqual(item["verified"], 1)
        execution_runs = IsolatedRunStore(self.db).list_runs(job_id)
        self.assertTrue(any(run["terminal_event"] == "COMPLETE" for run in execution_runs))
        self.assertTrue(
            any(
                IsolatedRunStore(self.db).bindings(run["id"], "durable_work")
                for run in execution_runs
                if run["terminal_event"] == "COMPLETE"
            )
        )

    def test_phase05_report_registry_blocks_unverified_schema(self):
        self.assertEqual(get_report_spec("4247").name, "Manual Renewals")
        self.assertEqual(get_report_spec("4372").identity_fields, ("policy_number",))
        self.assertEqual(
            get_report_spec("4372").filter_name,
            "Mortgagee Verification Queue - ROBIE",
        )
        self.assertEqual(get_report_spec("4372").look_id, "4601")
        self.assertNotEqual(get_report_spec("4372").filter_name, "ROBIE Intake")
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
        # 4359's schema is now verified: the registry flag is in sync with the
        # ingestion gate (see test_4359_registry_in_sync_with_ingestion_gate), so
        # start_run no longer blocks it. The unverified-schema block mechanism
        # is exercised with a temporarily-unverified copy of the 4359 spec.
        spec_4359 = get_report_spec("4359")
        unverified_4359 = dataclasses.replace(spec_4359, schema_verified=False)
        with patch.dict(report_registry.VERIFIED_REPORTS, {"4359": unverified_4359}):
            with self.assertRaises(ReportRegistryError):
                registry.start_run(run_id="run-4359-blocked", report_id="4359")
        started_4359 = registry.start_run(run_id="run-4359", report_id="4359")
        self.assertEqual(started_4359["report_id"], "4359")
        with self.assertRaises(ReportRegistryError):
            registry.start_run(run_id="run-missing", report_id="4247", fields=["unrelated"])
        started_4372 = registry.start_run(
            run_id="run-4372",
            report_id="4372",
            fields=["policy_number"],
        )
        self.assertEqual(started_4372["fields"], ["policy_number"])
        self.assertEqual(
            started_4372["filter_name"],
            "Mortgagee Verification Queue - ROBIE",
        )
        self.assertEqual(started_4372["look_id"], "4601")
        with self.assertRaises(ReportRegistryError):
            registry.start_run(
                run_id="run-4372-loan",
                report_id="4372",
                fields=["loan_number"],
            )

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

            final = JobEngine(self.store, {WORKER_FOR_ACTION["browser.read"]: Worker()}, {"browser.read": Verifier()}).run(job["id"])
            self.assertNotEqual(final["status"], JobStatus.COMPLETE)
            self.assertIn(final["status"], {JobStatus.UNVERIFIED, JobStatus.WAITING, JobStatus.FAILED})

    def test_unexpired_lease_is_rejected_even_for_same_owner(self):
        job = self.store.create_job("browser.read", {"worker": "x"}, idempotency_key="lease-1")
        first = self.store.claim(job["id"], "robie-job-engine:same")
        self.assertIsNotNone(first)
        second = self.store.claim(job["id"], "robie-job-engine:same")
        self.assertIsNone(second)
        other = self.store.claim(job["id"], "robie-job-engine:other")
        self.assertIsNone(other)

    def test_worker_exception_is_unknown_and_not_retried(self):
        calls = {"n": 0}

        class Boom:
            def perform(self, job, *, idempotency_key):
                calls["n"] += 1
                raise RuntimeError("injected worker crash")

        class Verifier:
            def verify(self, job, action):
                return VerificationResult(
                    False,
                    _evidence({"ok": True}, {"outcome": ACTION_OUTCOME_UNKNOWN}),
                    retryable=True,
                    error="destination unknown after worker exception",
                )

        job = self.store.create_job(
            "browser.read",
            {"worker": "boom"},
            idempotency_key="boom-1",
            max_attempts=3,
        )
        engine = JobEngine(self.store, {WORKER_FOR_ACTION["browser.read"]: Boom()}, {"browser.read": Verifier()})
        first = engine.run(job["id"])
        self.assertNotEqual(first["status"], JobStatus.COMPLETE)
        action = self.store.get_checkpoint(job["id"], "action")
        self.assertIsNotNone(action)
        self.assertEqual(action["detail"]["outcome"], ACTION_OUTCOME_UNKNOWN)
        item = DurableWorkLedger(self.db).get("browser.read", "boom-1")
        self.assertEqual(item["outcome"], ACTION_OUTCOME_UNKNOWN)
        self.assertEqual(item["external_actions"], 1)
        self.assertFalse(item["verified"])
        self.store.wake_due("9999-12-31T23:59:59+00:00")
        second = engine.run(job["id"])
        self.assertEqual(calls["n"], 1)
        self.assertNotEqual(second["status"], JobStatus.COMPLETE)

    def test_ledger_path_failure_never_invokes_hermes(self):
        hermes_calls = []

        def _fail_ledger(*_args, **_kwargs):
            raise IdempotencyError("persistent store cannot be /tmp")

        with patch.object(DurableWorkLedger, "__init__", side_effect=_fail_ledger):
            job_id = open_chat_job(
                self.db,
                "spaces/s/messages/hermes-block",
                "generate a proposal and add a $350 fee",
            )
            consumed = dispatch_operational_chat(
                self.db, job_id, hermes=lambda: hermes_calls.append("hermes")
            )
        self.assertTrue(consumed)
        self.assertEqual(hermes_calls, [])
        job = self.store.get_job(job_id)
        self.assertNotEqual(job["status"], JobStatus.COMPLETE)
        self.assertIn("durable", str(job.get("last_error") or "").casefold())

    def test_chat_persist_and_outbound_redact_fake_sentinel(self):
        text = f"generate a proposal password={FAKE_SECRET_SENTINEL}"
        job_id = open_chat_job(self.db, "spaces/s/messages/redact", text)
        job = self.store.get_job(job_id)
        self.assertFalse(contains_secret(job["payload"], FAKE_SECRET_SENTINEL))
        self.assertNotIn(FAKE_SECRET_SENTINEL, str(job["payload"].get("text") or ""))
        self.store.checkpoint(job_id, "note", {"detail": f"cookie={FAKE_SECRET_SENTINEL}"})
        self.assertFalse(contains_secret(self.store.get_checkpoint(job_id, "note"), FAKE_SECRET_SENTINEL))
        outbound = guard_chat_response(
            self.db, None, f"done token={FAKE_SECRET_SENTINEL}"
        )
        self.assertNotIn(FAKE_SECRET_SENTINEL, outbound)
        self.assertIn(REDACTED, outbound)

    def test_missing_bounded_schema_holds_and_never_acts(self):
        calls = {"n": 0}

        class Worker:
            def perform(self, current, *, idempotency_key):
                calls["n"] += 1
                return WorkerResult(True, current["action_type"], {"record_id": "x"})

        with patch.dict("robie_job_engine.job_schema.BOUNDED_JOB_SCHEMAS", {}, clear=True):
            job_id = open_chat_job(
                self.db,
                "spaces/s/messages/no-schema",
                "generate a proposal and add a $350 fee",
            )
            job = self.store.get_job(job_id)
            self.assertEqual(job["status"], JobStatus.NEEDS_CLARIFICATION)
            self.assertIn("schema", str(job.get("last_error") or "").casefold())
            JobEngine(
                self.store,
                {job["payload"]["worker"]: Worker()},
                {},
            ).run(job_id)
        self.assertEqual(calls["n"], 0)
        self.assertIsNone(self.store.get_checkpoint(job_id, "action"))

    def test_memory_destinations_forbidden_in_production(self):
        from robie_job_engine.carrier_proposal import MemoryProposalDestination
        from robie_job_engine.ezlynx import MemoryEzlynxDestination

        with patch.dict(os.environ, {"ROBIE_ENV": "PRODUCTION"}, clear=False):
            with self.assertRaises(ProductionGuardError):
                MemoryProposalDestination()
            with self.assertRaises(ProductionGuardError):
                MemoryEzlynxDestination()
        with patch.dict(os.environ, {"ROBIE_ENV": "TEST"}, clear=False):
            MemoryProposalDestination()
            MemoryEzlynxDestination()

    def test_worker_hard_timeout_is_unknown_and_not_retried(self):
        calls = {"n": 0}

        class Slow:
            def perform(self, job, *, idempotency_key):
                calls["n"] += 1
                time.sleep(0.4)
                return WorkerResult(True, "browser.read", {"record_id": "late"})

        class Verifier:
            def verify(self, job, action):
                return VerificationResult(
                    False,
                    _evidence({"ok": True}, {"outcome": ACTION_OUTCOME_UNKNOWN}),
                    retryable=True,
                    error="verification after timeout",
                )

        job = self.store.create_job(
            "browser.read",
            {"worker": "slow", "perform_timeout_seconds": 0.05},
            idempotency_key="timeout-1",
            max_attempts=3,
        )
        engine = JobEngine(
            self.store,
            {WORKER_FOR_ACTION["browser.read"]: Slow()},
            {"browser.read": Verifier()},
            perform_timeout_seconds=0.05,
        )
        first = engine.run(job["id"])
        self.assertNotEqual(first["status"], JobStatus.COMPLETE)
        action = self.store.get_checkpoint(job["id"], "action")
        self.assertEqual(action["detail"]["outcome"], ACTION_OUTCOME_UNKNOWN)
        item = DurableWorkLedger(self.db).get("browser.read", "timeout-1")
        self.assertEqual(item["outcome"], ACTION_OUTCOME_UNKNOWN)
        self.assertEqual(item["external_actions"], 1)
        runs = IsolatedRunStore(self.db).list_runs(job["id"])
        self.assertTrue(any(run["terminal_event"] == "CANCELLED" for run in runs))
        self.store.wake_due("9999-12-31T23:59:59+00:00")
        second = engine.run(job["id"])
        self.assertEqual(calls["n"], 1)
        self.assertNotEqual(second["status"], JobStatus.COMPLETE)

    def test_production_and_test_entrypoints_call_maybe_run_bounded_job(self):
        root = Path(__file__).resolve().parents[1]
        adapter = (root / "integrations/google_chat/adapter.py").read_text()
        chat_guard = (root / "robie_job_engine/chat_guard.py").read_text()
        engine = (root / "robie_job_engine/engine.py").read_text()
        self.assertIn("_enqueue_bounded_chat_job", adapter)
        self.assertIn("queue.claim_next", adapter)
        self.assertIn("maybe_run_bounded_job, ROBIE_JOB_DB, job_id", adapter)
        self.assertIn("dispatch_operational_chat(", adapter)
        self.assertIn("DurableWorkLedger(db_path)", chat_guard)
        self.assertIn("IsolatedRunStore(db_path)", chat_guard)
        self.assertIn("IsolatedRunStore(self.store.path)", engine)
        self.assertIn("DurableWorkLedger(self.store.path)", engine)
        self.assertIn('lease_owner = f"{self.owner}:{uuid.uuid4()}"', engine)
        for env in ("TEST", "PRODUCTION"):
            with durable_temporary_directory() as tmp:
                db = str(Path(tmp) / "jobs.db")
                store = JobStore(db)
                job = store.create_job(
                    "carrier.proposal",
                    {"worker": "carrier-proposal", "text": "generate a proposal"},
                    idempotency_key=f"entry-{env}",
                )
                hermes = []
                with patch.dict(os.environ, {"ROBIE_ENV": env}, clear=False):
                    handled = maybe_run_bounded_job(db, job["id"])
                    consumed = dispatch_operational_chat(
                        db, job["id"], hermes=lambda: hermes.append(env)
                    )
                self.assertTrue(handled)
                self.assertTrue(consumed)
                self.assertEqual(hermes, [])
                IsolatedRunStore(db)
                DurableWorkLedger(db)

    def test_complete_rejects_mismatched_and_stale_evidence(self):
        job = self.store.create_job("browser.read", {"worker": "x"}, idempotency_key="stale-ev")
        self.store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
        self.store.transition(job["id"], JobStatus.VERIFYING, expected={JobStatus.RUNNING})
        self.store.add_evidence(
            job["id"],
            True,
            VerificationEvidence(
                "TEST",
                "destination",
                {"status": "done"},
                {"status": "wrong"},
                True,
                "2000-01-01T00:00:00+00:00",
                "rec-stale",
            ),
        )
        with self.assertRaises(PermissionError):
            self.store.transition(
                job["id"],
                JobStatus.COMPLETE,
                expected={JobStatus.VERIFYING},
                authority=VERIFIER_AUTHORITY,
            )
        self.assertNotEqual(self.store.get_job(job["id"])["status"], JobStatus.COMPLETE)

        class Worker:
            def perform(self, current, *, idempotency_key):
                return WorkerResult(True, "browser.read", {"record_id": "r-stale"})

        class Verifier:
            def verify(self, current, action):
                return VerificationResult(
                    True,
                    VerificationEvidence(
                        "TEST",
                        "destination",
                        {"status": "done"},
                        {"status": "wrong"},
                        True,
                        "2000-01-01T00:00:00+00:00",
                        "rec-engine-stale",
                    ),
                )

        live = self.store.create_job(
            "browser.read", {"worker": "probe"}, idempotency_key="stale-engine"
        )
        final = JobEngine(self.store, {WORKER_FOR_ACTION["browser.read"]: Worker()}, {"browser.read": Verifier()}).run(
            live["id"]
        )
        self.assertNotEqual(final["status"], JobStatus.COMPLETE)
        self.assertIn(final["status"], {JobStatus.UNVERIFIED, JobStatus.FAILED})

    def test_complete_rejects_future_dated_evidence(self):
        job = self.store.create_job("browser.read", {"worker": "x"}, idempotency_key="future-ev")
        self.store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
        self.store.transition(job["id"], JobStatus.VERIFYING, expected={JobStatus.RUNNING})
        self.store.add_evidence(
            job["id"],
            True,
            VerificationEvidence(
                "TEST",
                "destination",
                {"status": "done"},
                {"status": "done"},
                True,
                "2100-01-01T00:00:00+00:00",
                "rec-future",
            ),
        )
        with self.assertRaises(PermissionError):
            self.store.transition(
                job["id"],
                JobStatus.COMPLETE,
                expected={JobStatus.VERIFYING},
                authority=VERIFIER_AUTHORITY,
            )
        self.assertNotEqual(self.store.get_job(job["id"])["status"], JobStatus.COMPLETE)

        class Worker:
            def perform(self, current, *, idempotency_key):
                return WorkerResult(True, "browser.read", {"record_id": "r-future"})

        class Verifier:
            def verify(self, current, action):
                return VerificationResult(
                    True,
                    VerificationEvidence(
                        "TEST",
                        "destination",
                        {"status": "done"},
                        {"status": "done"},
                        True,
                        "2100-01-01T00:00:00+00:00",
                        "rec-engine-future",
                    ),
                )

        live = self.store.create_job(
            "browser.read", {"worker": "probe"}, idempotency_key="future-engine"
        )
        final = JobEngine(self.store, {WORKER_FOR_ACTION["browser.read"]: Worker()}, {"browser.read": Verifier()}).run(
            live["id"]
        )
        self.assertNotEqual(final["status"], JobStatus.COMPLETE)
        self.assertIn(final["status"], {JobStatus.UNVERIFIED, JobStatus.FAILED})

    def test_complete_rejects_empty_expected_postcondition(self):
        job = self.store.create_job("browser.read", {"worker": "x"}, idempotency_key="empty-ev")
        self.store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
        self.store.transition(job["id"], JobStatus.VERIFYING, expected={JobStatus.RUNNING})
        self.store.add_evidence(
            job["id"],
            True,
            VerificationEvidence(
                "TEST",
                "destination",
                {},
                {"status": "wrong"},
                True,
                datetime.now(timezone.utc).isoformat(),
                "rec-empty",
            ),
        )
        with self.assertRaises(PermissionError):
            self.store.transition(
                job["id"],
                JobStatus.COMPLETE,
                expected={JobStatus.VERIFYING},
                authority=VERIFIER_AUTHORITY,
            )
        self.assertNotEqual(self.store.get_job(job["id"])["status"], JobStatus.COMPLETE)

        class Worker:
            def perform(self, current, *, idempotency_key):
                return WorkerResult(True, "browser.read", {"record_id": "r-empty"})

        class Verifier:
            def verify(self, current, action):
                return VerificationResult(
                    True,
                    VerificationEvidence(
                        "TEST",
                        "destination",
                        {},
                        {"status": "wrong"},
                        True,
                        datetime.now(timezone.utc).isoformat(),
                        "rec-engine-empty",
                    ),
                )

        live = self.store.create_job(
            "browser.read", {"worker": "probe"}, idempotency_key="empty-engine"
        )
        final = JobEngine(self.store, {WORKER_FOR_ACTION["browser.read"]: Worker()}, {"browser.read": Verifier()}).run(
            live["id"]
        )
        self.assertNotEqual(final["status"], JobStatus.COMPLETE)
        self.assertIn(final["status"], {JobStatus.UNVERIFIED, JobStatus.FAILED})

    def test_complete_rejects_missing_destination_identity(self):
        job = self.store.create_job("browser.read", {"worker": "x"}, idempotency_key="no-ident")
        self.store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
        self.store.transition(job["id"], JobStatus.VERIFYING, expected={JobStatus.RUNNING})
        self.store.add_evidence(
            job["id"],
            True,
            VerificationEvidence(
                "TEST",
                "destination",
                {"status": "done"},
                {"status": "done"},
                True,
                datetime.now(timezone.utc).isoformat(),
                None,
            ),
        )
        with self.assertRaises(PermissionError):
            self.store.transition(
                job["id"],
                JobStatus.COMPLETE,
                expected={JobStatus.VERIFYING},
                authority=VERIFIER_AUTHORITY,
            )
        self.assertNotEqual(self.store.get_job(job["id"])["status"], JobStatus.COMPLETE)

        class Worker:
            def perform(self, current, *, idempotency_key):
                return WorkerResult(True, "browser.read", {"record_id": "r-ident"})

        class Verifier:
            def verify(self, current, action):
                return VerificationResult(
                    True,
                    VerificationEvidence(
                        "TEST",
                        "destination",
                        {"status": "done"},
                        {"status": "done"},
                        True,
                        datetime.now(timezone.utc).isoformat(),
                        None,
                    ),
                )

        live = self.store.create_job(
            "browser.read", {"worker": "probe"}, idempotency_key="no-ident-engine"
        )
        final = JobEngine(self.store, {WORKER_FOR_ACTION["browser.read"]: Worker()}, {"browser.read": Verifier()}).run(
            live["id"]
        )
        self.assertNotEqual(final["status"], JobStatus.COMPLETE)
        self.assertIn(final["status"], {JobStatus.UNVERIFIED, JobStatus.FAILED})
        self.assertIn("identity", str(final.get("last_error") or "").casefold())

    def test_authorization_header_redacts_entire_bearer_value(self):
        header = "Authorization: Bearer TESTVALUE123"
        cleaned = redact_text(header)
        self.assertNotIn("TESTVALUE123", cleaned)
        self.assertIn(REDACTED, cleaned)
        self.assertNotIn(
            "TESTVALUE123",
            redact_narration(f"upstream failed {header}"),
        )
        try:
            raise RuntimeError(header)
        except RuntimeError as exc:
            self.assertNotIn("TESTVALUE123", redact_exception(exc))
        args = redact_tool_args({"authorization": "Bearer TESTVALUE123"})
        self.assertEqual(args["authorization"], REDACTED)
        self.assertNotIn("TESTVALUE123", str(args))

    def test_fein_is_masked_from_logs_and_structured_traces(self):
        fake_fein = "12-3456789"
        self.assertNotIn(fake_fein, redact_text(f"FEIN={fake_fein}"))
        args = redact_tool_args({"FEIN": fake_fein})
        self.assertEqual(args["FEIN"], REDACTED)
        self.assertNotIn(fake_fein, str(args))

    def test_concurrent_isolated_run_start_is_database_enforced(self):
        runs = IsolatedRunStore(self.db)
        barrier = threading.Barrier(2)
        started: list[dict] = []
        errors: list[BaseException] = []

        def attempt(owner: str) -> None:
            try:
                barrier.wait(timeout=2)
                started.append(runs.start(owner=owner, job_id="concurrent-job"))
            except BaseException as exc:
                errors.append(exc)

        workers = [
            threading.Thread(target=attempt, args=("owner-a",)),
            threading.Thread(target=attempt, args=("owner-b",)),
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=5)
        self.assertEqual(len(started), 1)
        self.assertTrue(any(isinstance(item, RunIsolationError) for item in errors))
        self.assertEqual(sum(1 for run in runs.list_runs() if run["status"] == "ACTIVE"), 1)

    def test_concurrent_external_action_is_atomic(self):
        ledger = DurableWorkLedger(self.db)
        ledger.acquire("carrier.proposal", "atomic-1", owner="w1")
        barrier = threading.Barrier(2)
        outcomes: list[object] = []

        def attempt() -> None:
            try:
                barrier.wait(timeout=2)
                outcomes.append(ledger.record_external_action("carrier.proposal", "atomic-1"))
            except BaseException as exc:
                outcomes.append(exc)

        workers = [threading.Thread(target=attempt) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=5)
        successes = [item for item in outcomes if isinstance(item, dict)]
        failures = [item for item in outcomes if isinstance(item, IdempotencyError)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(ledger.get("carrier.proposal", "atomic-1")["external_actions"], 1)

    def test_phase10_production_release_stays_fail(self):
        self.assertEqual(production_release_decision(), "FAIL")
        self.assertEqual(production_release_decision(independent_reviewer_pass=True), "FAIL")
        with patch.dict("os.environ", {"ROBIE_ENV": "PRODUCTION"}, clear=False):
            self.assertEqual(production_release_decision(), "FAIL")
        self.assertFalse(contains_secret({"password": REDACTED}, FAKE_SECRET_SENTINEL))


if __name__ == "__main__":
    unittest.main()
