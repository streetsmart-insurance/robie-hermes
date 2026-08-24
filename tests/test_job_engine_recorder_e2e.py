"""Synthetic Job Engine + recorder end-to-end coverage.

Test-only fixtures. No live Hermes host, no live EZLynx, no real credentials.
Recordings stay under .robie-durable-test and a fake Drive pointer.
production_release_decision() stays FAIL. live_test_complete stays false.
"""

from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from durable_temp import durable_temporary_directory

from robie_job_engine.browser_read import BoundedBrowserReadWorker, BrowserReadVerifier
from robie_job_engine.carrier_proposal import (
    BoundedCarrierProposalWorker,
    CarrierProposalVerifier,
    MemoryProposalDestination,
)
from robie_job_engine.complete_guard import (
    EVIDENCE_CLOCK_SKEW,
    evidence_is_stale,
    expected_postcondition_missing,
    postcondition_mismatch,
)
from robie_job_engine.engine import JobEngine
from robie_job_engine.ezlynx import (
    BoundedEzlynxWorker,
    EzlynxDestinationVerifier,
    MemoryEzlynxDestination,
)
from robie_job_engine.idempotency import DurableWorkLedger
from robie_job_engine.models import (
    ACTION_OUTCOME_UNKNOWN,
    JobStatus,
    VerificationEvidence,
    VerificationResult,
    WorkerResult,
)
from robie_job_engine.recording import RecordingManager
from robie_job_engine.release_gate import production_release_decision
from robie_job_engine.secrets import FAKE_SECRET_SENTINEL, contains_secret
from robie_job_engine.store import JobStore


TEST_DRIVE_PREFIX = "test-drive:"


class SegmentedFakeCapture:
    """Write a distinct synthetic webm per segment. Never talks to Chrome."""

    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0

    def start(self, output_path: Path, stop_file: Path) -> int:
        self.starts += 1
        output_path.parent.mkdir(parents=True, exist_ok=True)
        return 5000 + self.starts

    def stop(self, pid: int, stop_file: Path, output_path: Path) -> None:
        self.stops += 1
        stop_file.touch(mode=0o600, exist_ok=True)
        output_path.write_bytes(f"test-only-webm-segment-{self.stops}\n".encode())


class TestOnlyDriveUploader:
    """Persist a Test-only Drive pointer. Never calls Google Drive."""

    def __init__(self) -> None:
        self.uploads: list[dict[str, str]] = []

    def upload(self, path: Path, file_name: str) -> tuple[str, str]:
        file_id = f"{TEST_DRIVE_PREFIX}{path.stem}-seg{len(self.uploads) + 1}"
        url = f"https://drive.google.com/file/d/{file_id}/view"
        self.uploads.append(
            {
                "local_path": str(path),
                "file_name": file_name,
                "drive_file_id": file_id,
                "drive_url": url,
            }
        )
        return file_id, url


class MemoryBrowser:
    def __init__(self, pages: dict[str, dict]) -> None:
        self.pages = pages
        self.reads = 0

    def read_fresh(self, locator: dict) -> dict:
        self.reads += 1
        key = locator.get("url") or locator.get("title")
        if key not in self.pages:
            raise LookupError(f"page not found: {key}")
        return dict(self.pages[key])


class SequenceVerifier:
    def __init__(self, results: list[VerificationResult]) -> None:
        self.results = list(results)
        self.calls = 0

    def verify(self, job, action):
        self.calls += 1
        return self.results.pop(0)


class CountingWorker:
    def __init__(self, result: WorkerResult) -> None:
        self.result = result
        self.calls = 0

    def perform(self, job, *, idempotency_key):
        self.calls += 1
        return self.result


def _fresh_evidence(expected: dict, observed: dict | None = None, *, locator="rec-e2e", captured_at=None):
    return VerificationEvidence(
        "TEST",
        "destination",
        expected,
        observed if observed is not None else dict(expected),
        True,
        captured_at or datetime.now(timezone.utc).isoformat(),
        locator,
    )


class JobEngineRecorderE2ETests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = patch.dict(os.environ, {"ROBIE_ENV": "TEST"}, clear=False)
        self.env.start()
        self.tmp = durable_temporary_directory()
        self.root = Path(self.tmp.name)
        self.db = str(self.root / "jobs.db")
        self.store = JobStore(self.db)
        self.recording_root = self.root / "recordings"
        self.capture = SegmentedFakeCapture()
        self.uploader = TestOnlyDriveUploader()
        self.recordings = RecordingManager(
            self.db,
            root=self.recording_root,
            capture=self.capture,
            uploader=self.uploader,
            enabled=True,
            keep_local=True,
        )
        self.proposal = MemoryProposalDestination()
        self.ezlynx = MemoryEzlynxDestination()
        self.browser = MemoryBrowser(
            {
                "https://ezlynx.example/account": {
                    "url": "https://ezlynx.example/account",
                    "title": "Acme",
                }
            }
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()
        self.env.stop()

    def _engine(self, workers: dict, verifiers: dict) -> JobEngine:
        return JobEngine(self.store, workers, verifiers, recordings=self.recordings)

    def _assert_test_recording(self, job_id: str, *, min_segments: int = 1) -> list[dict]:
        segments = self.recordings.list_for_job(job_id)
        self.assertGreaterEqual(len(segments), min_segments)
        for index, item in enumerate(segments, start=1):
            self.assertEqual(item["segment_number"], index)
            self.assertTrue(str(item["local_path"]).startswith(str(self.recording_root)))
            self.assertTrue(str(item["drive_file_id"] or "").startswith(TEST_DRIVE_PREFIX))
            self.assertIn(TEST_DRIVE_PREFIX, str(item["drive_url"] or ""))
            self.assertNotIn("/opt/streetsmart-hermes/", str(item["local_path"]))
        latest = segments[-1]
        self.assertEqual(latest["status"], "READY")
        self.assertTrue(Path(latest["local_path"]).is_file())
        return segments

    def _assert_complete_success_conditions(self, job_id: str) -> dict:
        job = self.store.get_job(job_id)
        self.assertEqual(job["status"], JobStatus.COMPLETE)
        evidence_rows = self.store.list_evidence(job_id)
        self.assertTrue(evidence_rows)
        latest = evidence_rows[-1]
        expected = latest["expected"]
        observed = latest["observed"]
        self.assertTrue(latest["verified"])
        self.assertTrue(latest["authoritative"])
        self.assertTrue(latest["captured_at"])
        self.assertTrue(expected)
        self.assertIsNone(expected_postcondition_missing(expected))
        self.assertIsNone(postcondition_mismatch(expected, observed))
        self.assertIsNone(
            evidence_is_stale(
                captured_at=latest["captured_at"],
                not_before=job["created_at"],
                stored_at=latest.get("created_at"),
            )
        )
        captured = datetime.fromisoformat(latest["captured_at"])
        created = datetime.fromisoformat(job["created_at"])
        now = datetime.now(timezone.utc)
        self.assertGreaterEqual(captured, created)
        self.assertLessEqual(captured, now + EVIDENCE_CLOCK_SKEW)
        self._assert_test_recording(job_id)
        return latest

    def _run_five_successful_jobs(self) -> list[str]:
        workers = {
            "carrier-proposal": BoundedCarrierProposalWorker(self.proposal, self.store),
            "browser-read": BoundedBrowserReadWorker(self.browser),
            "hermes-cua": BoundedEzlynxWorker(self.ezlynx),
        }
        verifiers = {
            "carrier.proposal": CarrierProposalVerifier(self.proposal),
            "browser.read": BrowserReadVerifier(self.browser),
            "ezlynx.reassign": EzlynxDestinationVerifier(self.ezlynx),
            "ezlynx.move_document": EzlynxDestinationVerifier(self.ezlynx),
            "ezlynx.apply_label": EzlynxDestinationVerifier(self.ezlynx),
        }
        engine = self._engine(workers, verifiers)
        specs = [
            (
                "carrier.proposal",
                {
                    "worker": "carrier-proposal",
                    "quote_text": "Acme quote\nPremium: $1200\n",
                    "text": "generate a proposal and add a $350 fee",
                    "expected_page_count": 10,
                },
                "e2e-proposal",
            ),
            (
                "browser.read",
                {
                    "worker": "browser-read",
                    "locator": {"url": "https://ezlynx.example/account"},
                    "expected": {
                        "url": "https://ezlynx.example/account",
                        "title": "Acme",
                    },
                },
                "e2e-browser",
            ),
            (
                "ezlynx.reassign",
                {
                    "worker": "hermes-cua",
                    "resource_id": "task-e2e",
                    "assignee_id": "user-7",
                    "assignee_name": "Ann",
                },
                "e2e-reassign",
            ),
            (
                "ezlynx.move_document",
                {
                    "worker": "hermes-cua",
                    "document_id": "doc-e2e",
                    "account_id": "account-4",
                    "destination_id": "acct-9",
                    "destination_name": "Acme Test",
                },
                "e2e-move",
            ),
            (
                "ezlynx.apply_label",
                {
                    "worker": "hermes-cua",
                    "resource_id": "doc-label",
                    "account_id": "account-4",
                    "label_id": "label-2",
                    "label": "Renewal",
                },
                "e2e-label",
            ),
        ]
        job_ids = []
        for action, payload, key in specs:
            job = self.store.create_job(action, payload, idempotency_key=key)
            final = engine.run(job["id"])
            self.assertEqual(final["status"], JobStatus.COMPLETE, action)
            job_ids.append(job["id"])
        return job_ids

    def test_five_successful_jobs_complete_with_recordings(self):
        job_ids = self._run_five_successful_jobs()
        self.assertEqual(len(job_ids), 5)
        for job_id in job_ids:
            self._assert_complete_success_conditions(job_id)
        self.assertEqual(len(self.uploader.uploads), 5)

    def test_success_conditions_required_before_complete(self):
        job_ids = self._run_five_successful_jobs()
        for job_id in job_ids:
            latest = self._assert_complete_success_conditions(job_id)
            self.assertTrue(
                latest["expected"],
                "COMPLETE requires a non-empty workflow-relevant expected postcondition",
            )
            self.assertEqual(
                latest["expected"],
                {key: latest["observed"][key] for key in latest["expected"]},
                "COMPLETE requires an exact expected-versus-observed match",
            )

        now = datetime.now(timezone.utc)
        within_skew = (now + timedelta(minutes=2)).isoformat()
        worker = CountingWorker(WorkerResult(True, "browser.read", {"record_id": "skew"}))
        allowed = self.store.create_job(
            "browser.read", {"worker": "probe"}, idempotency_key="e2e-skew-ok"
        )
        final = self._engine(
            {"probe": worker},
            {
                "browser.read": SequenceVerifier(
                    [
                        VerificationResult(
                            True,
                            _fresh_evidence(
                                {"status": "done"},
                                locator="skew-ok",
                                captured_at=within_skew,
                            ),
                        )
                    ]
                )
            },
        ).run(allowed["id"])
        self.assertEqual(final["status"], JobStatus.COMPLETE)

        violations = [
            ("e2e-empty-expected", {}, {"status": "wrong"}, now.isoformat()),
            (
                "e2e-mismatch",
                {"status": "done"},
                {"status": "wrong"},
                now.isoformat(),
            ),
            (
                "e2e-future-evidence",
                {"status": "done"},
                {"status": "done"},
                "2100-01-01T00:00:00+00:00",
            ),
        ]
        for key, expected, observed, captured_at in violations:
            job = self.store.create_job(
                "browser.read", {"worker": "probe"}, idempotency_key=key
            )
            refused = self._engine(
                {"probe": CountingWorker(WorkerResult(True, "browser.read", {"record_id": key}))},
                {
                    "browser.read": SequenceVerifier(
                        [
                            VerificationResult(
                                True,
                                _fresh_evidence(
                                    expected,
                                    observed,
                                    locator=key,
                                    captured_at=captured_at,
                                ),
                            )
                        ]
                    )
                },
            ).run(job["id"])
            self.assertNotEqual(refused["status"], JobStatus.COMPLETE, key)
            self.assertIn(refused["status"], {JobStatus.UNVERIFIED, JobStatus.FAILED}, key)
            self._assert_test_recording(job["id"])

    def test_authentication_mfa_job_never_completes_or_stores_credentials(self):
        class MfaLoginWorker:
            def perform(self, job, *, idempotency_key):
                return WorkerResult(
                    False,
                    "browser.read",
                    {},
                    {
                        "page_state": "mfa_required",
                        "outcome": ACTION_OUTCOME_UNKNOWN,
                        "login": True,
                    },
                    retryable=False,
                    error="MFA challenge on login; human authentication required",
                    hold_status=JobStatus.NEEDS_CLARIFICATION,
                )

        job = self.store.create_job(
            "browser.read",
            {
                "worker": "browser-read",
                "locator": {"url": "https://ezlynx.example/login"},
                "password": FAKE_SECRET_SENTINEL,
                "mfa": FAKE_SECRET_SENTINEL,
            },
            idempotency_key="e2e-mfa-login",
        )
        self.assertNotIn(FAKE_SECRET_SENTINEL, json.dumps(job["payload"]))
        final = self._engine(
            {"browser-read": MfaLoginWorker()},
            {"browser.read": BrowserReadVerifier(self.browser)},
        ).run(job["id"])
        self.assertEqual(final["status"], JobStatus.NEEDS_CLARIFICATION)
        self.assertNotEqual(final["status"], JobStatus.COMPLETE)
        self.assertIn("mfa", str(final.get("last_error") or "").casefold())
        blob = {
            "job": self.store.get_job(job["id"]),
            "action": self.store.get_checkpoint(job["id"], "action"),
            "evidence": self.store.list_evidence(job["id"]),
            "recordings": self.recordings.list_for_job(job["id"]),
        }
        self.assertFalse(contains_secret(blob, FAKE_SECRET_SENTINEL))
        self.assertNotIn(FAKE_SECRET_SENTINEL, json.dumps(blob, default=str))
        self._assert_test_recording(job["id"])

    def test_retry_records_multiple_video_segments(self):
        worker = CountingWorker(
            WorkerResult(True, "ezlynx.apply_label", {"resource_id": "doc-retry"})
        )
        first = VerificationResult(
            False, _fresh_evidence({"resource_id": "doc-retry"}, {}), True, error="not yet"
        )
        later = VerificationResult(
            True, _fresh_evidence({"resource_id": "doc-retry"}), False
        )
        job = self.store.create_job(
            "ezlynx.apply_label",
            {"worker": "hermes-cua"},
            idempotency_key="e2e-retry-segments",
            max_attempts=3,
        )
        engine = self._engine({"hermes-cua": worker}, {"ezlynx.apply_label": SequenceVerifier([first, later])})
        waiting = engine.run(job["id"])
        self.assertEqual(waiting["status"], JobStatus.RETRY_WAIT)
        self.assertEqual(worker.calls, 1)
        first_segments = self._assert_test_recording(job["id"], min_segments=1)
        self.assertEqual(first_segments[0]["final_job_status"], JobStatus.RETRY_WAIT.value)
        self.store.wake_due("2099-01-01T00:00:00+00:00")
        final = engine.run(job["id"])
        self.assertEqual(final["status"], JobStatus.COMPLETE)
        self.assertEqual(worker.calls, 1)
        segments = self._assert_test_recording(job["id"], min_segments=2)
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]["segment_number"], 1)
        self.assertEqual(segments[1]["segment_number"], 2)
        self.assertEqual(segments[1]["final_job_status"], JobStatus.COMPLETE.value)
        self.assertEqual(
            DurableWorkLedger(self.db).get("ezlynx.apply_label", "e2e-retry-segments")[
                "external_actions"
            ],
            1,
        )

    def test_worker_restart_resumes_verification_without_second_external_action(self):
        worker = CountingWorker(
            WorkerResult(True, "ezlynx.apply_label", {"resource_id": "doc-restart"})
        )
        waiting_verify = VerificationResult(
            False,
            _fresh_evidence({"resource_id": "doc-restart"}, {}),
            True,
            error="destination not ready",
            hold_status=JobStatus.WAITING,
        )
        later = VerificationResult(
            True, _fresh_evidence({"resource_id": "doc-restart"}), False
        )
        job = self.store.create_job(
            "ezlynx.apply_label",
            {"worker": "hermes-cua"},
            idempotency_key="e2e-restart",
            max_attempts=3,
        )
        first_engine = self._engine(
            {"hermes-cua": worker},
            {"ezlynx.apply_label": SequenceVerifier([waiting_verify])},
        )
        held = first_engine.run(job["id"])
        self.assertEqual(held["status"], JobStatus.WAITING)
        self.assertEqual(worker.calls, 1)
        self.assertEqual(
            DurableWorkLedger(self.db).get("ezlynx.apply_label", "e2e-restart")[
                "external_actions"
            ],
            1,
        )
        self.store.resume(job["id"])
        restarted = self._engine(
            {"hermes-cua": worker},
            {"ezlynx.apply_label": SequenceVerifier([later])},
        )
        final = restarted.run(job["id"])
        self.assertEqual(final["status"], JobStatus.COMPLETE)
        self.assertEqual(worker.calls, 1)
        self.assertEqual(
            DurableWorkLedger(self.db).get("ezlynx.apply_label", "e2e-restart")[
                "external_actions"
            ],
            1,
        )
        self._assert_complete_success_conditions(job["id"])
        segments = self.recordings.list_for_job(job["id"])
        self.assertGreaterEqual(len(segments), 2)

    def test_production_release_stays_fail_and_not_live(self):
        self.assertEqual(production_release_decision(), "FAIL")
        self.assertEqual(production_release_decision(independent_reviewer_pass=True), "FAIL")
        self.assertTrue(str(self.recording_root).startswith(str(self.root)))
        self.assertNotIn("/opt/streetsmart-hermes/", str(self.recording_root))


if __name__ == "__main__":
    unittest.main()
