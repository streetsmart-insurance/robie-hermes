from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from robie_job_engine.models import JobStatus
from robie_job_engine.quote_replay import (
    QuoteReplayError,
    complete_is_allowed,
    main,
    refuse_complete_without_evidence,
    refuse_production_targets,
    resolve_quote_pdf,
    run_quote_replay,
)
from robie_job_engine.store import JobStore
from robie_job_engine.test_runtime import ProductionGuardError


class QuoteReplayHarnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = str(self.root / "jobs.db")
        self.artifacts = str(self.root / "artifacts")

    def tearDown(self):
        self.tmp.cleanup()

    def _write_provided_quote(self, name="provided-quote.pdf"):
        path = self.root / name
        path.write_text("Hartford workers compensation quote\nPremium: $1200\n")
        return path

    def test_missing_pdf_is_blocked_and_creates_no_job(self):
        with self.assertRaises(QuoteReplayError) as raised:
            resolve_quote_pdf(None)
        self.assertIn("Do not invent a PDF", str(raised.exception))
        report = run_quote_replay(
            quote_pdf=None, db_path=self.db, artifact_root=self.artifacts
        )
        self.assertEqual(report["outcome"], "BLOCKED")
        self.assertFalse(report["live_test_complete"])
        self.assertIsNone(report["job_id"])
        self.assertFalse(Path(self.db).exists())

    def test_unreadable_or_empty_pdf_is_blocked(self):
        missing = self.root / "missing.pdf"
        with self.assertRaises(QuoteReplayError):
            resolve_quote_pdf(str(missing))
        empty = self.root / "empty.pdf"
        empty.write_bytes(b"")
        with self.assertRaises(QuoteReplayError):
            resolve_quote_pdf(str(empty))

    def test_production_env_and_live_hermes_paths_are_refused(self):
        with patch.dict(os.environ, {"ROBIE_ENV": "PRODUCTION"}, clear=False):
            with self.assertRaises(ProductionGuardError):
                refuse_production_targets(self.db, self.artifacts)
        with patch.dict(os.environ, {"ROBIE_ENV": "TEST"}, clear=False):
            with self.assertRaises(ProductionGuardError):
                refuse_production_targets(
                    "/opt/streetsmart-hermes/robie-job-engine/data/jobs.db",
                    self.artifacts,
                )

    def test_test_host_paths_require_test_env(self):
        test_db = "/opt/streetsmart-hermes-test/robie-job-engine/data/jobs.db"
        test_art = "/opt/streetsmart-hermes-test/robie-job-engine/data/artifacts"
        with patch.dict(os.environ, {"ROBIE_ENV": "PRODUCTION"}, clear=False):
            with self.assertRaises(ProductionGuardError):
                refuse_production_targets(test_db, test_art)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ROBIE_ENV", None)
            with self.assertRaises(ProductionGuardError):
                refuse_production_targets(test_db, test_art)

    def test_provided_pdf_creates_job_and_completes_only_with_evidence(self):
        quote = self._write_provided_quote()
        report = run_quote_replay(
            quote_pdf=str(quote),
            db_path=self.db,
            artifact_root=self.artifacts,
            message_id="quote-replay:provided",
        )
        self.assertEqual(report["outcome"], "COMPLETE")
        self.assertTrue(report["complete_allowed"])
        self.assertFalse(report["complete_refused"])
        self.assertFalse(report["live_test_complete"])
        self.assertIn("not live Test COMPLETE", report["claim"])
        self.assertEqual(report["evidence_count"], 1)
        evidence = report["evidence"][0]
        self.assertTrue(evidence["authoritative"])
        self.assertTrue(evidence["verified"])
        self.assertEqual(evidence["method"], "FRESH_PROPOSAL_READBACK")
        self.assertEqual(evidence["observed"]["agency_fee_occurrences"], 1)
        store = JobStore(self.db)
        job = store.get_job(report["job_id"])
        self.assertEqual(job["status"], JobStatus.COMPLETE)
        self.assertTrue(store.get_checkpoint(job["id"], "ingestion"))

    def test_complete_refused_without_stored_evidence(self):
        store = JobStore(self.db)
        job = store.create_job(
            "carrier.proposal",
            {"worker": "carrier-proposal", "text": "generate a proposal"},
            idempotency_key="no-evidence",
        )
        self.assertFalse(complete_is_allowed(job, []))
        refuse_complete_without_evidence(store, job["id"])

        class SilentComplete:
            def run(self, job_id):
                store.checkpoint(
                    job_id,
                    "action",
                    {"action": "carrier.proposal", "destination": {}, "detail": {}},
                )
                try:
                    store.transition(job_id, JobStatus.COMPLETE)
                except PermissionError:
                    store.transition(
                        job_id,
                        JobStatus.UNVERIFIED,
                        error="COMPLETE requires independently stored authoritative evidence",
                        release_lease=True,
                    )
                return store.get_job(job_id)

        quote = self._write_provided_quote()
        report = run_quote_replay(
            quote_pdf=str(quote),
            db_path=self.db,
            artifact_root=self.artifacts,
            message_id="quote-replay:no-evidence",
            engine=SilentComplete(),
        )
        self.assertNotEqual(report["outcome"], "COMPLETE")
        self.assertFalse(report["complete_allowed"])
        self.assertFalse(report["live_test_complete"])
        self.assertFalse(report["complete_refused"] and report["outcome"] == "COMPLETE")
        store = JobStore(self.db)
        final = store.get_job(report["job_id"])
        self.assertNotEqual(final["status"], JobStatus.COMPLETE)
        self.assertEqual(store.list_evidence(report["job_id"]), [])

    def test_forced_complete_without_evidence_is_refused(self):
        store = JobStore(self.db)
        job = store.create_job(
            "carrier.proposal", {"worker": "x"}, idempotency_key="forced"
        )
        with self.assertRaises(PermissionError):
            store.transition(job["id"], JobStatus.COMPLETE)
        job = store.get_job(job["id"])
        job["status"] = JobStatus.COMPLETE
        with self.assertRaises(QuoteReplayError) as raised:
            refuse_complete_without_evidence(
                _ForcedCompleteStore(store, job), job["id"]
            )
        self.assertIn("COMPLETE refused", str(raised.exception))

    def test_cli_without_pdf_exits_blocked(self):
        work = self.root / "cli-work"
        with redirect_stdout(io.StringIO()) as captured:
            code = main(
                [
                    "--work-dir",
                    str(work),
                    "--db",
                    str(work / "jobs.db"),
                    "--artifact-root",
                    str(work / "artifacts"),
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("Do not invent a PDF", captured.getvalue())

    def test_cli_with_provided_pdf_stays_local(self):
        quote = self._write_provided_quote("cli-quote.pdf")
        work = self.root / "cli-complete"
        with redirect_stdout(io.StringIO()) as captured:
            code = main(
                [
                    "--quote-pdf",
                    str(quote),
                    "--work-dir",
                    str(work),
                    "--message-id",
                    "quote-replay:cli",
                ]
            )
        self.assertEqual(code, 0)
        self.assertIn("not live Test COMPLETE", captured.getvalue())
        report_db = JobStore(str(work / "jobs.db"))
        jobs = _list_jobs(report_db)
        self.assertEqual(len(jobs), 1)
        evidence = report_db.list_evidence(jobs[0]["id"])
        self.assertTrue(evidence)
        self.assertTrue(complete_is_allowed(jobs[0], evidence))


class _ForcedCompleteStore:
    def __init__(self, store, job):
        self._store = store
        self._job = job

    def get_job(self, job_id):
        return self._job

    def list_evidence(self, job_id):
        return []


def _list_jobs(store: JobStore):
    import sqlite3

    with sqlite3.connect(store.path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT id, status FROM jobs").fetchall()
    return [{"id": row["id"], "status": JobStatus(row["status"])} for row in rows]


if __name__ == "__main__":
    unittest.main()
