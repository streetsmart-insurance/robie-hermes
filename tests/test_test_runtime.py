from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from durable_temp import durable_temporary_directory

from robie_job_engine.models import JobStatus
from robie_job_engine.store import JobStore
from robie_job_engine.test_runtime import (
    ProductionGuardError,
    build_test_engine,
    dispatch_operational_chat,
    maybe_run_bounded_job,
    maybe_run_test_bounded_job,
    require_test_environment,
)


class TestRuntimeGuardTests(unittest.TestCase):
    def test_production_and_unset_env_are_refused(self):
        with patch.dict(os.environ, {"ROBIE_ENV": "PRODUCTION"}, clear=False):
            with self.assertRaises(ProductionGuardError):
                require_test_environment()
        with patch.dict(os.environ, {"ROBIE_ENV": ""}, clear=False):
            os.environ.pop("ROBIE_ENV", None)
            with self.assertRaises(ProductionGuardError):
                require_test_environment()
        with patch.dict(os.environ, {"ROBIE_ENV": "TEST"}, clear=False):
            self.assertEqual(require_test_environment(), "TEST")

    def test_maybe_run_is_a_no_op_outside_test(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "carrier.proposal",
                {"worker": "carrier-proposal", "text": "generate a proposal"},
                idempotency_key="prod-guard",
            )
            with patch.dict(os.environ, {"ROBIE_ENV": "PRODUCTION"}, clear=False):
                self.assertFalse(maybe_run_test_bounded_job(db, job["id"]))
            self.assertEqual(store.get_job(job["id"])["status"], JobStatus.PENDING)

    def test_production_bounded_chat_uses_job_engine_not_hermes(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "carrier.proposal",
                {
                    "worker": "carrier-proposal",
                    "text": "generate a proposal and add a $350 fee",
                    "quote_text": "quote",
                    "expected_page_count": 10,
                },
                idempotency_key="prod-engine",
            )
            with patch.dict(os.environ, {"ROBIE_ENV": "PRODUCTION"}, clear=False):
                self.assertFalse(maybe_run_test_bounded_job(db, job["id"]))
                handled = maybe_run_bounded_job(db, job["id"])
            self.assertTrue(handled)
            final = store.get_job(job["id"])
            # Production has no in-memory destination, so the engine owns the
            # Job and Hermes/cua-driver does not continue. COMPLETE is refused.
            self.assertNotEqual(final["status"], JobStatus.COMPLETE)
            self.assertIn(final["status"], {JobStatus.FAILED, JobStatus.UNVERIFIED})

    def test_test_env_without_required_recorder_fails_before_work(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "carrier.proposal",
                {
                    "worker": "carrier-proposal",
                    "text": "generate a proposal and add a $350 fee",
                    "quote_text": "quote",
                    "expected_page_count": 10,
                },
                idempotency_key="test-run",
            )
            with patch.dict(os.environ, {"ROBIE_ENV": "TEST"}, clear=False):
                handled = maybe_run_test_bounded_job(db, job["id"])
            self.assertTrue(handled)
            final = store.get_job(job["id"])
            self.assertEqual(final["status"], JobStatus.FAILED)
            self.assertIn("recording is required", final["last_error"])
            evidence = store.list_evidence(job["id"])
            self.assertEqual(evidence, [])

    def test_production_ledger_failure_does_not_invoke_hermes(self):
        hermes = []
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "carrier.proposal",
                {"worker": "carrier-proposal", "text": "generate a proposal"},
                idempotency_key="prod-ledger-fail",
            )
            with patch.dict(os.environ, {"ROBIE_ENV": "PRODUCTION"}, clear=False):
                with patch(
                    "robie_job_engine.engine.DurableWorkLedger",
                    side_effect=ProductionGuardError("ledger cannot start"),
                ):
                    consumed = dispatch_operational_chat(
                        db, job["id"], hermes=lambda: hermes.append("hermes")
                    )
            self.assertTrue(consumed)
            self.assertEqual(hermes, [])
            self.assertNotEqual(store.get_job(job["id"])["status"], JobStatus.COMPLETE)

    def test_worker_aliases_audit_and_mortgagee(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            from robie_job_engine.test_runtime import build_runtime_engine
            engine = build_runtime_engine(store)
            self.assertIn("audit", engine.workers)
            self.assertIn("mortgagee", engine.workers)
            self.assertIs(engine.workers["audit"], engine.workers["audit-verification"])
            self.assertIs(engine.workers["mortgagee"], engine.workers["mortgagee-verification"])


if __name__ == "__main__":
    unittest.main()
