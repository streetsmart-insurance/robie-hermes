"""JE-KILL-01 deterministic process-death regressions.

No browser, network, Test VM, or Production resource is used. The fake
destination is a separate SQLite table so its consequence survives SIGKILL
independently of the Job Engine's ledger/checkpoints.
"""

from __future__ import annotations

import multiprocessing
import sqlite3
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from durable_temp import durable_temporary_directory

from robie_job_engine.engine import JobEngine
from robie_job_engine.request_routing import WORKER_FOR_ACTION
from robie_job_engine.ezlynx import (
    BoundedEzlynxWorker,
    EzlynxDestinationVerifier,
    MemoryEzlynxDestination,
)
from robie_job_engine.idempotency import DurableWorkLedger
from robie_job_engine.models import (
    JobStatus,
    ReconciliationOutcome,
    ReconciliationResult,
    VerificationEvidence,
    VerificationResult,
    WorkerResult,
)
from robie_job_engine.runs import IsolatedRunStore
from robie_job_engine.store import JobStore


ACTION = "ezlynx.apply_label"
DESTINATION = {
    "resource_id": "document-je-kill-01",
    "account_id": "account-test-only",
    "document_name": "renewal-test.pdf",
    "label_id": "manual-renewal",
    "label": "Manual Renewal",
    "label_control": "add-label",
}


def _connect_destination(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS je_kill_destination (
               idempotency_key TEXT PRIMARY KEY,
               action_count INTEGER NOT NULL,
               resource_id TEXT NOT NULL,
               updated_at TEXT NOT NULL
           )"""
    )
    return conn


def _destination_row(db_path: str, idempotency_key: str) -> dict | None:
    with _connect_destination(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM je_kill_destination WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
    return dict(row) if row else None


class PersistentConsequenceWorker:
    def __init__(self, db_path: str, marker_dir: str, kill_phase: str | None) -> None:
        self.db_path = db_path
        self.marker_dir = Path(marker_dir)
        self.kill_phase = kill_phase

    def _mark_and_block(self, name: str) -> None:
        (self.marker_dir / name).write_text("ready")
        while True:
            time.sleep(0.1)

    def perform(self, job: dict, *, idempotency_key: str) -> WorkerResult:
        if self.kill_phase == "before_action":
            self._mark_and_block("before_action")
        stamp = datetime.now(timezone.utc).isoformat()
        with _connect_destination(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT action_count FROM je_kill_destination WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if row is None:
                conn.execute(
                    """INSERT INTO je_kill_destination
                       (idempotency_key,action_count,resource_id,updated_at)
                       VALUES (?,?,?,?)""",
                    (idempotency_key, 1, DESTINATION["resource_id"], stamp),
                )
            else:
                conn.execute(
                    """UPDATE je_kill_destination
                       SET action_count=action_count+1,updated_at=?
                       WHERE idempotency_key=?""",
                    (stamp, idempotency_key),
                )
            conn.commit()
        if self.kill_phase == "after_action":
            self._mark_and_block("after_action")
        return WorkerResult(
            True,
            ACTION,
            dict(DESTINATION),
            {"idempotency_key": idempotency_key},
        )


class PersistentDestinationReader:
    def __init__(self, db_path: str, marker_dir: str, kill_during_verify: bool = False) -> None:
        self.db_path = db_path
        self.marker_dir = Path(marker_dir)
        self.kill_during_verify = kill_during_verify

    def reconcile(self, job: dict, *, idempotency_key: str) -> ReconciliationResult:
        row = _destination_row(self.db_path, idempotency_key)
        if row is None:
            return ReconciliationResult(
                ReconciliationOutcome.NOT_APPLIED,
                ACTION,
                destination=dict(DESTINATION),
                detail={"source": "fake-authoritative-destination", "observed": {}},
                authoritative=True,
            )
        if row["resource_id"] == DESTINATION["resource_id"]:
            return ReconciliationResult(
                ReconciliationOutcome.APPLIED,
                ACTION,
                destination=dict(DESTINATION),
                detail={"source": "fake-authoritative-destination", "observed": row},
                authoritative=True,
            )
        return ReconciliationResult(
            ReconciliationOutcome.UNKNOWN,
            ACTION,
            destination=dict(DESTINATION),
            detail={"source": "fake-authoritative-destination", "observed": row},
            authoritative=True,
            error="destination identity mismatch",
            hold_status=JobStatus.NEEDS_CLARIFICATION,
        )

    def verify(self, job: dict, action: dict) -> VerificationResult:
        if self.kill_during_verify:
            (self.marker_dir / "during_verification").write_text("ready")
            while True:
                time.sleep(0.1)
        expected = dict(action.get("destination") or {})
        row = _destination_row(self.db_path, job["idempotency_key"])
        observed = dict(expected) if row and row["action_count"] == 1 else {}
        evidence = VerificationEvidence(
            method="TEST_AUTHORITATIVE_DESTINATION_READBACK",
            source="je-kill-01-fake-destination",
            expected=expected,
            observed=observed,
            authoritative=True,
            captured_at=datetime.now(timezone.utc).isoformat(),
            locator=expected.get("resource_id"),
        )
        return VerificationResult(
            bool(expected and observed == expected),
            evidence,
            error=None if observed == expected else "destination did not match",
        )


def _run_until_killed(
    db_path: str,
    job_id: str,
    marker_dir: str,
    kill_phase: str,
) -> None:
    store = JobStore(db_path)
    worker = PersistentConsequenceWorker(db_path, marker_dir, kill_phase)
    reader = PersistentDestinationReader(
        db_path,
        marker_dir,
        kill_during_verify=kill_phase == "during_verification",
    )
    JobEngine(
        store,
        {WORKER_FOR_ACTION[ACTION]: worker},
        {ACTION: reader},
        reconcilers={ACTION: reader},
        lease_seconds=1,
    ).run(job_id)


class JeKill01Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = durable_temporary_directory()
        self.root = Path(self.tmp.name)
        self.db = str(self.root / "jobs.db")
        self.store = JobStore(self.db)
        _connect_destination(self.db).close()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _new_job(self, key: str) -> dict:
        return self.store.create_job(
            ACTION,
            {"worker": "kill-worker", **DESTINATION},
            idempotency_key=key,
            max_attempts=3,
        )

    def _kill_at(self, job: dict, marker: str) -> None:
        context = multiprocessing.get_context("spawn")
        process = context.Process(
            target=_run_until_killed,
            args=(self.db, job["id"], str(self.root), marker),
        )
        process.start()
        marker_path = self.root / marker
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not marker_path.exists():
            if not process.is_alive():
                self.fail(f"kill child exited before {marker}: exit={process.exitcode}")
            time.sleep(0.05)
        self.assertTrue(marker_path.exists(), f"child never reached {marker}")
        process.kill()
        process.join(timeout=5)
        self.assertFalse(process.is_alive())
        self.assertNotEqual(process.exitcode, 0)
        # The dead process cannot heartbeat. Let all three one-second leases
        # expire naturally; no database row is edited by the test.
        time.sleep(1.4)

    def _restart(self, job: dict) -> dict:
        worker = PersistentConsequenceWorker(self.db, str(self.root), None)
        reader = PersistentDestinationReader(self.db, str(self.root))
        return JobEngine(
            self.store,
            {WORKER_FOR_ACTION[ACTION]: worker},
            {ACTION: reader},
            reconcilers={ACTION: reader},
            lease_seconds=1,
        ).run(job["id"])

    def _assert_recovered_once(self, job: dict, final: dict) -> None:
        self.assertEqual(final["status"], JobStatus.COMPLETE)
        destination = _destination_row(self.db, job["idempotency_key"])
        self.assertIsNotNone(destination)
        self.assertEqual(destination["action_count"], 1)
        ledger = DurableWorkLedger(self.db).get(ACTION, job["idempotency_key"])
        self.assertEqual(ledger["external_actions"], 1)
        self.assertEqual(ledger["verified"], 1)
        runs = IsolatedRunStore(self.db).list_runs(job["id"])
        self.assertTrue(any(run["terminal_event"] == "ABANDONED" for run in runs))
        self.assertTrue(any(run["terminal_event"] == "COMPLETE" for run in runs))

    def test_missing_reconciler_fails_closed_without_repeating_worker(self) -> None:
        job = self._new_job("je-kill-no-reconciler")
        self.store.checkpoint(
            job["id"],
            "action_intent",
            {"action": ACTION, "state": "PREPARED", "run_id": "dead-run"},
        )

        class MustNotRun:
            calls = 0

            def perform(self, current: dict, *, idempotency_key: str) -> WorkerResult:
                self.calls += 1
                return WorkerResult(True, ACTION, dict(DESTINATION))

        worker = MustNotRun()
        final = JobEngine(
            self.store,
            {WORKER_FOR_ACTION[ACTION]: worker},
            {},
            lease_seconds=1,
        ).run(job["id"])
        self.assertEqual(final["status"], JobStatus.NEEDS_CLARIFICATION)
        self.assertEqual(worker.calls, 0)
        self.assertIn("no reconciler", final["last_error"])
        reconciliation = self.store.get_checkpoint(job["id"], "action_reconciliation")
        self.assertEqual(reconciliation["outcome"], ReconciliationOutcome.UNKNOWN.value)

    def test_nonauthoritative_not_applied_never_allows_repeat(self) -> None:
        job = self._new_job("je-kill-nonauthoritative")
        self.store.checkpoint(
            job["id"],
            "action_intent",
            {"action": ACTION, "state": "PREPARED", "run_id": "dead-run"},
        )

        class MustNotRun:
            calls = 0

            def perform(self, current: dict, *, idempotency_key: str) -> WorkerResult:
                self.calls += 1
                return WorkerResult(True, ACTION, dict(DESTINATION))

        class WeakReader:
            def reconcile(self, current: dict, *, idempotency_key: str) -> ReconciliationResult:
                return ReconciliationResult(
                    ReconciliationOutcome.NOT_APPLIED,
                    ACTION,
                    destination=dict(DESTINATION),
                    authoritative=False,
                    error="cached DOM is not authoritative",
                )

        worker = MustNotRun()
        final = JobEngine(
            self.store,
            {WORKER_FOR_ACTION[ACTION]: worker},
            {},
            reconcilers={ACTION: WeakReader()},
            lease_seconds=1,
        ).run(job["id"])
        self.assertEqual(final["status"], JobStatus.WAITING)
        self.assertEqual(worker.calls, 0)
        reconciliation = self.store.get_checkpoint(job["id"], "action_reconciliation")
        self.assertEqual(reconciliation["outcome"], ReconciliationOutcome.UNKNOWN.value)
        self.assertFalse(reconciliation["authoritative"])

    def test_ledger_recorded_without_action_checkpoint_still_reconciles(self) -> None:
        job = self._new_job("je-kill-ledger-before-checkpoint")
        worker = PersistentConsequenceWorker(self.db, str(self.root), None)
        worker.perform(job, idempotency_key=job["idempotency_key"])
        ledger = DurableWorkLedger(self.db)
        ledger.reserve(ACTION, job["idempotency_key"])
        ledger.acquire(
            ACTION,
            job["idempotency_key"],
            owner="worker-that-died",
            timeout_seconds=0,
        )
        ledger.record_external_action(ACTION, job["idempotency_key"])
        self.store.checkpoint(
            job["id"],
            "action_intent",
            {"action": ACTION, "state": "PREPARED", "run_id": "dead-run"},
        )
        reader = PersistentDestinationReader(self.db, str(self.root))
        final = JobEngine(
            self.store,
            {WORKER_FOR_ACTION[ACTION]: worker},
            {ACTION: reader},
            reconcilers={ACTION: reader},
            lease_seconds=1,
        ).run(job["id"])
        self.assertEqual(final["status"], JobStatus.COMPLETE)
        self.assertEqual(
            _destination_row(self.db, job["idempotency_key"])["action_count"],
            1,
        )
        self.assertEqual(
            self.store.get_checkpoint(job["id"], "action_reconciliation")["outcome"],
            ReconciliationOutcome.APPLIED.value,
        )

    def test_real_ezlynx_reconciler_distinguishes_absent_applied_and_unknown(self) -> None:
        job = self._new_job("je-kill-real-ezlynx-reconciler")
        destination = MemoryEzlynxDestination()
        reconciler = EzlynxDestinationVerifier(destination)
        absent = reconciler.reconcile(job, idempotency_key=job["idempotency_key"])
        self.assertEqual(absent.outcome, ReconciliationOutcome.NOT_APPLIED)
        self.assertTrue(absent.authoritative)
        BoundedEzlynxWorker(destination).perform(
            job,
            idempotency_key=job["idempotency_key"],
        )
        applied = reconciler.reconcile(job, idempotency_key=job["idempotency_key"])
        self.assertEqual(applied.outcome, ReconciliationOutcome.APPLIED)
        self.assertTrue(applied.authoritative)
        destination.unavailable = True
        unknown = reconciler.reconcile(job, idempotency_key=job["idempotency_key"])
        self.assertEqual(unknown.outcome, ReconciliationOutcome.UNKNOWN)
        self.assertFalse(unknown.authoritative)

    def test_kill_before_action_reconciles_not_applied_then_writes_once(self) -> None:
        job = self._new_job("je-kill-before")
        self._kill_at(job, "before_action")
        self.assertIsNone(_destination_row(self.db, job["idempotency_key"]))
        final = self._restart(job)
        self._assert_recovered_once(job, final)
        reconciliation = self.store.get_checkpoint(job["id"], "action_reconciliation")
        self.assertEqual(reconciliation["outcome"], ReconciliationOutcome.NOT_APPLIED.value)
        self.assertTrue(reconciliation["authoritative"])

    def test_kill_after_action_reconciles_applied_without_repeating(self) -> None:
        job = self._new_job("je-kill-after")
        self._kill_at(job, "after_action")
        self.assertEqual(
            _destination_row(self.db, job["idempotency_key"])["action_count"],
            1,
        )
        final = self._restart(job)
        self._assert_recovered_once(job, final)
        reconciliation = self.store.get_checkpoint(job["id"], "action_reconciliation")
        self.assertEqual(reconciliation["outcome"], ReconciliationOutcome.APPLIED.value)
        self.assertTrue(self.store.get_checkpoint(job["id"], "action")["detail"]["reconciled_after_interruption"])

    def test_kill_during_verification_repeats_readback_not_action(self) -> None:
        job = self._new_job("je-kill-verify")
        self._kill_at(job, "during_verification")
        self.assertIsNotNone(self.store.get_checkpoint(job["id"], "action"))
        self.assertEqual(
            _destination_row(self.db, job["idempotency_key"])["action_count"],
            1,
        )
        final = self._restart(job)
        self._assert_recovered_once(job, final)
        self.assertIsNone(self.store.get_checkpoint(job["id"], "action_reconciliation"))
        self.assertGreaterEqual(final["verification_count"], 2)


if __name__ == "__main__":
    unittest.main()
