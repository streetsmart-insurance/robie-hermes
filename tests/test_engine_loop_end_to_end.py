"""The engine loop itself: worker runs, an INDEPENDENT verifier reads the
destination, and only then does the job complete.

Registration tests prove a verifier is present. This proves the contract the
presence is for — that a job reaches COMPLETE only when something other than
the worker confirmed the destination, and lands UNVERIFIED in every way that
confirmation can fail. Runs entirely in-process against a temp sqlite db, so
it needs no box, no browser and no network. Stdlib unittest.
"""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from robie_job_engine.engine import JobEngine
from robie_job_engine.models import (
    JobStatus,
    VerificationEvidence,
    VerificationResult,
    WorkerResult,
)
from robie_job_engine.store import JobStore

ACTION = "browser.read"
DESTINATION = {"url": "https://app.ezlynx.com/applicantportal/220250093"}


class RecordingWorker:
    """Reports what it attempted. It may never report completion."""

    def __init__(self, succeeded=True, writes_destination=True):
        self.succeeded = succeeded
        self.writes_destination = writes_destination
        self.calls = 0

    def perform(self, job, *, idempotency_key):
        self.calls += 1
        return WorkerResult(
            self.succeeded,
            job["action_type"],
            dict(DESTINATION),
            detail={"claimed_by": "the worker itself"},
            error=None if self.succeeded else "worker failed",
        )


class DestinationVerifier:
    """Stands in for a real read-back port."""

    def __init__(self, *, verified=True, authoritative=True):
        self.verified = verified
        self.authoritative = authoritative
        self.calls = 0

    def verify(self, job, action):
        self.calls += 1
        return VerificationResult(
            self.verified,
            VerificationEvidence(
                method="TEST_READBACK",
                source="independent",
                expected=dict(DESTINATION),
                observed=dict(DESTINATION) if self.verified else {},
                authoritative=self.authoritative,
                captured_at=datetime.now(timezone.utc).isoformat(),
            ),
            error=None if self.verified else "destination did not match",
        )


def _run(worker, verifier):
    tmp = tempfile.mkdtemp()
    store = JobStore(str(Path(tmp) / "jobs.db"))
    verifiers = {ACTION: verifier} if verifier is not None else {}
    engine = JobEngine(store, {"browser-read": worker}, verifiers)
    job = store.create_job(ACTION, {"worker": "browser-read", **DESTINATION})
    engine.run(job["id"])
    return store.get_job(job["id"])


class EngineLoopCompletesOnlyOnIndependentEvidence(unittest.TestCase):
    def test_worker_plus_authoritative_verifier_completes(self):
        worker, verifier = RecordingWorker(), DestinationVerifier()
        job = _run(worker, verifier)
        self.assertEqual(job["status"], JobStatus.COMPLETE.value, job.get("last_error"))
        self.assertEqual(worker.calls, 1)
        self.assertEqual(verifier.calls, 1, "the verifier must actually have been called")

    def test_no_verifier_registered_lands_unverified_not_complete(self):
        job = _run(RecordingWorker(), None)
        self.assertEqual(job["status"], JobStatus.UNVERIFIED.value)
        self.assertIn("verifier", (job.get("last_error") or "").lower())

    def test_verifier_says_no_lands_unverified(self):
        job = _run(RecordingWorker(), DestinationVerifier(verified=False))
        self.assertNotEqual(job["status"], JobStatus.COMPLETE.value)

    def test_verified_but_not_authoritative_does_not_complete(self):
        # The invariant that stops a plausible-looking receipt from passing as
        # proof: completion needs verified AND evidence.authoritative.
        job = _run(RecordingWorker(), DestinationVerifier(verified=True, authoritative=False))
        self.assertNotEqual(
            job["status"], JobStatus.COMPLETE.value,
            "non-authoritative evidence must never complete a job",
        )

    def test_failed_worker_does_not_complete(self):
        job = _run(RecordingWorker(succeeded=False), DestinationVerifier())
        self.assertNotEqual(job["status"], JobStatus.COMPLETE.value)

    def test_verifier_that_raises_lands_unverified_with_the_reason(self):
        class Exploding:
            def verify(self, job, action):
                raise PermissionError("EPERM reading the destination")

        job = _run(RecordingWorker(), Exploding())
        self.assertNotEqual(job["status"], JobStatus.COMPLETE.value)
        self.assertIn("PermissionError", job.get("last_error") or "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
