"""Job Engine perform deadline: progress refreshes; idle still fails closed."""

from __future__ import annotations

import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from durable_temp import durable_temporary_directory

from robie_job_engine.engine import JobEngine
from robie_job_engine.job_schema import get_executable_skill_contract
from robie_job_engine.models import (
    ACTION_OUTCOME_UNKNOWN,
    JobStatus,
    VerificationEvidence,
    VerificationResult,
    WorkerResult,
)
from robie_job_engine.perform_deadline import (
    DEFAULT_PERFORM_TIMEOUT_SECONDS,
    resolve_perform_idle_seconds,
    resolve_perform_max_seconds,
)
from robie_job_engine.request_routing import WORKER_FOR_ACTION
from robie_job_engine.store import JobStore, report_current_job_perform_progress
from robie_job_engine.submission_audit import (
    JOB_BOUND_RUNNER_CEILING_SECONDS,
    RUNNER_TIMEOUT_SECONDS,
    job_bound_runner_timeout,
)


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


class _OkVerifier:
    def verify(self, job, action):
        record_id = str((action.get("destination") or {}).get("record_id") or "rec-1")
        return VerificationResult(
            True,
            _evidence(
                {"record_id": record_id},
                {"record_id": record_id},
                locator=record_id,
            ),
            retryable=False,
        )


class PerformDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = durable_temporary_directory()
        self.db = str(Path(self.tmp.name) / "jobs.db")
        self.store = JobStore(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_overdue_and_submission_audit_keep_120s_idle_and_3600s_ceiling(self):
        for action_type in (
            "ezlynx.overdue_submission_reports",
            "ezlynx.submission_audit",
        ):
            contract = get_executable_skill_contract(action_type)
            self.assertIsNotNone(contract)
            self.assertIsNone(contract.perform_timeout_seconds)
            self.assertEqual(contract.perform_max_seconds, 3600)
            job = {"action_type": action_type, "payload": {}}
            self.assertEqual(
                resolve_perform_idle_seconds(
                    job, engine_default=DEFAULT_PERFORM_TIMEOUT_SECONDS
                ),
                120.0,
            )
            self.assertEqual(
                resolve_perform_max_seconds(job, idle_seconds=120.0),
                3600.0,
            )

    def test_payload_timeout_overrides_contract_and_engine_default(self):
        job = {
            "action_type": "ezlynx.overdue_submission_reports",
            "payload": {
                "perform_timeout_seconds": 15,
                "perform_max_seconds": 90,
            },
        }
        self.assertEqual(resolve_perform_idle_seconds(job, engine_default=120), 15.0)
        self.assertEqual(resolve_perform_max_seconds(job, idle_seconds=15.0), 90.0)

    def test_short_job_without_progress_hooks_still_times_out_at_budget(self):
        started = {"n": 0}

        class Slow:
            def perform(self, job, *, idempotency_key):
                started["n"] += 1
                time.sleep(0.35)
                return WorkerResult(True, "browser.read", {"record_id": "late"})

        job = self.store.create_job(
            "browser.read",
            {"worker": "slow", "perform_timeout_seconds": 0.08},
            idempotency_key="deadline-idle-1",
        )
        engine = JobEngine(
            self.store,
            {WORKER_FOR_ACTION["browser.read"]: Slow()},
            {"browser.read": _OkVerifier()},
            perform_timeout_seconds=0.08,
        )
        finished = engine.run(job["id"])
        self.assertNotEqual(finished["status"], JobStatus.COMPLETE)
        action = self.store.get_checkpoint(job["id"], "action")
        self.assertEqual(action["detail"]["outcome"], ACTION_OUTCOME_UNKNOWN)
        self.assertIn("without progress", str(action["detail"].get("error") or ""))
        self.assertEqual(started["n"], 1)

    def test_progress_heartbeat_extends_deadline_and_completes(self):
        class Progressing:
            def perform(self, job, *, idempotency_key):
                store = JobStore(self_store_path)
                for page in range(1, 4):
                    time.sleep(0.12)
                    store.report_perform_progress(
                        job["id"],
                        {"pages_reviewed": page, "rows_inspected": page * 10},
                    )
                return WorkerResult(True, "browser.read", {"record_id": "done"})

        self_store_path = self.db
        job = self.store.create_job(
            "browser.read",
            {"worker": "progress", "perform_timeout_seconds": 0.2},
            idempotency_key="deadline-progress-1",
        )
        engine = JobEngine(
            self.store,
            {WORKER_FOR_ACTION["browser.read"]: Progressing()},
            {"browser.read": _OkVerifier()},
            perform_timeout_seconds=0.2,
        )
        finished = engine.run(job["id"])
        self.assertEqual(finished["status"], JobStatus.COMPLETE)
        progress = self.store.get_checkpoint(job["id"], "perform_progress")
        self.assertEqual(progress["pages_reviewed"], 3)
        self.assertEqual(progress["rows_inspected"], 30)

    def test_idle_after_progress_still_times_out(self):
        class StallsAfterProgress:
            def perform(self, job, *, idempotency_key):
                JobStore(self_store_path).report_perform_progress(
                    job["id"],
                    {"pages_reviewed": 1, "rows_inspected": 5},
                )
                time.sleep(0.45)
                return WorkerResult(True, "browser.read", {"record_id": "late"})

        self_store_path = self.db
        job = self.store.create_job(
            "browser.read",
            {"worker": "stall", "perform_timeout_seconds": 0.12},
            idempotency_key="deadline-stall-1",
        )
        engine = JobEngine(
            self.store,
            {WORKER_FOR_ACTION["browser.read"]: StallsAfterProgress()},
            {"browser.read": _OkVerifier()},
            perform_timeout_seconds=0.12,
        )
        finished = engine.run(job["id"])
        self.assertNotEqual(finished["status"], JobStatus.COMPLETE)
        action = self.store.get_checkpoint(job["id"], "action")
        self.assertEqual(action["detail"]["outcome"], ACTION_OUTCOME_UNKNOWN)
        self.assertIn("without progress", str(action["detail"].get("error") or ""))

    def test_lease_renewal_does_not_count_as_progress(self):
        job = self.store.create_job(
            "browser.read",
            {"worker": "lease"},
            idempotency_key="deadline-lease-1",
        )
        claimed = self.store.claim(job["id"], "worker-a", lease_seconds=1)
        self.assertIsNotNone(claimed)
        before = self.store.latest_perform_progress_fingerprint(job["id"])
        self.store.renew_lease(job["id"], "worker-a", lease_seconds=120)
        after = self.store.latest_perform_progress_fingerprint(job["id"])
        self.assertIsNone(before)
        self.assertIsNone(after)

    def test_gateway_progress_and_playwright_exec_change_fingerprint(self):
        job = self.store.create_job(
            "hermes.google_chat_task",
            {"text": "walk the portal"},
            idempotency_key="deadline-signals-1",
        )
        self.store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
        self.assertIsNone(self.store.latest_perform_progress_fingerprint(job["id"]))
        self.store.heartbeat_generic_chat_job(job["id"])
        first = self.store.latest_perform_progress_fingerprint(job["id"])
        self.assertIsNotNone(first)
        time.sleep(0.01)
        self.store.heartbeat_generic_chat_job(job["id"])
        second = self.store.latest_perform_progress_fingerprint(job["id"])
        self.assertNotEqual(first, second)
        self.store.add_playwright_exec(job["id"], "playwright_exec", "ok")
        third = self.store.latest_perform_progress_fingerprint(job["id"])
        self.assertNotEqual(second, third)

    def test_job_bound_runner_uses_ceiling_standalone_stays_short(self):
        cleared = {
            "ROBIE_JOB_ID": "",
            "ROBIE_CURRENT_JOB_ID": "",
            "JOB_ID": "",
            "ROBIE_PERFORM_MAX_SECONDS": "",
        }
        with patch.dict("os.environ", cleared, clear=False):
            self.assertEqual(job_bound_runner_timeout(), RUNNER_TIMEOUT_SECONDS)
        with patch.dict(
            "os.environ",
            {**cleared, "ROBIE_JOB_ID": "job-1", "ROBIE_PERFORM_MAX_SECONDS": "3600"},
            clear=False,
        ):
            self.assertEqual(job_bound_runner_timeout(), 3600)
        with patch.dict(
            "os.environ",
            {**cleared, "ROBIE_JOB_ID": "job-1"},
            clear=False,
        ):
            self.assertEqual(
                job_bound_runner_timeout(),
                max(RUNNER_TIMEOUT_SECONDS, JOB_BOUND_RUNNER_CEILING_SECONDS),
            )

    def test_progress_cannot_outrun_declared_ceiling(self):
        class Chatter:
            def perform(self, job, *, idempotency_key):
                store = JobStore(self_store_path)
                for page in range(1, 20):
                    store.report_perform_progress(
                        job["id"],
                        {"pages_reviewed": page, "rows_inspected": page},
                    )
                    time.sleep(0.04)
                return WorkerResult(True, "browser.read", {"record_id": "late"})

        self_store_path = self.db
        job = self.store.create_job(
            "browser.read",
            {
                "worker": "chatter",
                "perform_timeout_seconds": 0.12,
                "perform_max_seconds": 0.18,
            },
            idempotency_key="deadline-ceiling-1",
        )
        engine = JobEngine(
            self.store,
            {WORKER_FOR_ACTION["browser.read"]: Chatter()},
            {"browser.read": _OkVerifier()},
            perform_timeout_seconds=0.12,
        )
        finished = engine.run(job["id"])
        self.assertNotEqual(finished["status"], JobStatus.COMPLETE)
        action = self.store.get_checkpoint(job["id"], "action")
        self.assertEqual(action["detail"]["outcome"], ACTION_OUTCOME_UNKNOWN)
        self.assertIn("ceiling", str(action["detail"].get("error") or ""))

    def test_bound_worker_env_lets_helpers_report_progress(self):
        reported = threading.Event()

        class Helper:
            def perform(self, job, *, idempotency_key):
                ok = report_current_job_perform_progress(
                    {"pages_reviewed": 2, "rows_inspected": 20},
                    source="helper",
                )
                reported.set()
                self.ok = ok
                time.sleep(0.1)
                return WorkerResult(True, "browser.read", {"record_id": "env"})

        helper = Helper()
        job = self.store.create_job(
            "browser.read",
            {"worker": "helper", "perform_timeout_seconds": 0.2},
            idempotency_key="deadline-env-1",
        )
        engine = JobEngine(
            self.store,
            {WORKER_FOR_ACTION["browser.read"]: helper},
            {"browser.read": _OkVerifier()},
            perform_timeout_seconds=0.2,
        )
        finished = engine.run(job["id"])
        self.assertTrue(reported.wait(1))
        self.assertTrue(helper.ok)
        self.assertEqual(finished["status"], JobStatus.COMPLETE)


class OverdueSkillPerformNoteTests(unittest.TestCase):
    def test_skill_documents_progress_aware_deadline(self):
        from pathlib import Path as _Path

        content = _Path("skills/ezlynx-overdue-submission-reports/SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("## Perform time", content)
        self.assertIn("120-second starting budget", content)
        self.assertIn("perform_max_seconds", content)
        self.assertIn("fails closed", content)
        deploy = _Path(
            "deploy/hermes/skills/ezlynx-overdue-submission-reports/SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertEqual(content, deploy)
