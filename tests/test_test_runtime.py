from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from robie_job_engine.models import JobStatus
from robie_job_engine.store import JobStore
from robie_job_engine.test_runtime import (
    ProductionGuardError,
    build_test_engine,
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
        with tempfile.TemporaryDirectory() as tmp:
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

    def test_test_env_runs_bounded_job_without_false_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
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
            self.assertEqual(final["status"], JobStatus.COMPLETE)
            evidence = store.list_evidence(job["id"])
            self.assertEqual(len(evidence), 1)
            self.assertEqual(evidence[0]["observed"]["agency_fee_occurrences"], 1)
            self.assertTrue(evidence[0]["authoritative"])


if __name__ == "__main__":
    unittest.main()
