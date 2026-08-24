from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from .complete_guard import complete_is_prohibited
from .models import (
    VERIFIER_AUTHORITY,
    WAITING_STATUSES,
    JobStatus,
    VerificationResult,
    WorkerResult,
)
from .recording import RecordingManager
from .secrets import redact_exception, redact_mapping
from .store import JobStore


class ComputerWorker(Protocol):
    def perform(self, job: dict[str, Any], *, idempotency_key: str) -> WorkerResult: ...


class Verifier(Protocol):
    def verify(self, job: dict[str, Any], action: dict[str, Any]) -> VerificationResult: ...


class JobEngine:
    """The only component authorized to set COMPLETE."""

    def __init__(self, store: JobStore, workers: dict[str, ComputerWorker], verifiers: dict[str, Verifier], *, owner: str = "robie-job-engine", recordings: RecordingManager | None = None):
        self.store = store
        self.workers = workers
        self.verifiers = verifiers
        self.owner = owner
        self.recordings = recordings or RecordingManager(store.path)

    def run(self, job_id: str) -> dict[str, Any]:
        job = self.store.claim(job_id, self.owner)
        if not job:
            return self.store.get_job(job_id)
        self.recordings.safe_start(job_id)
        try:
            action = self.store.get_checkpoint(job_id, "action")
            if action is None:
                job = self._perform(job)
                if job["status"] != JobStatus.VERIFYING:
                    return job
                action = self.store.get_checkpoint(job_id, "action")
            return self._verify(self.store.get_job(job_id), action or {})
        finally:
            final = self.store.get_job(job_id)
            final_status = final["status"]
            self.recordings.safe_stop(
                job_id,
                final_status.value if isinstance(final_status, JobStatus) else str(final_status),
            )

    def _perform(self, job: dict[str, Any]) -> dict[str, Any]:
        self.store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING, JobStatus.RUNNING})
        number = self.store.increment(job["id"], "attempt_count")
        worker_name = job["payload"].get("worker", "hermes-cua")
        worker = self.workers.get(worker_name)
        if not worker:
            return self.store.transition(job["id"], JobStatus.FAILED, error=f"unknown worker: {worker_name}", release_lease=True)
        try:
            result = worker.perform(job, idempotency_key=job["idempotency_key"])
        except Exception as exc:
            result = WorkerResult(
                False,
                job["action_type"],
                {},
                retryable=True,
                error=redact_exception(exc),
            )
        self.store.add_attempt(
            job["id"],
            "perform",
            number,
            "success" if result.succeeded else "failure",
            {"error": result.error, "detail": redact_mapping(result.detail)},
        )
        if result.hold_status in WAITING_STATUSES:
            return self.store.transition(
                job["id"],
                result.hold_status,
                expected={JobStatus.RUNNING},
                error=result.error,
                resume_status=JobStatus.PENDING,
                release_lease=True,
            )
        if not result.succeeded:
            return self._retry_or_fail(job, number, result.error or "worker failed", result.retryable, JobStatus.PENDING)
        action = {"action": result.action, "destination": result.destination, "detail": result.detail}
        self.store.checkpoint(job["id"], "action", action)
        return self.store.transition(job["id"], JobStatus.VERIFYING, expected={JobStatus.RUNNING})

    def _verify(self, job: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
        verifier = self.verifiers.get(job["action_type"])
        if not verifier:
            return self.store.transition(job["id"], JobStatus.UNVERIFIED, error="no independent verifier registered", release_lease=True)
        number = self.store.increment(job["id"], "verification_count")
        try:
            result = verifier.verify(job, action)
        except Exception as exc:
            return self._verification_retry_or_unverified(job, number, f"{type(exc).__name__}: {exc}", True)
        self.store.add_evidence(job["id"], result.verified, result.evidence)
        self.store.add_attempt(job["id"], "verify", number, "verified" if result.verified else "not_verified", {"error": result.error, "method": result.evidence.method, "authoritative": result.evidence.authoritative})
        if result.hold_status in WAITING_STATUSES:
            return self.store.transition(
                job["id"],
                result.hold_status,
                expected={JobStatus.VERIFYING},
                error=result.error,
                resume_status=JobStatus.VERIFYING,
                release_lease=True,
            )
        if result.verified and result.evidence.authoritative:
            prohibited = complete_is_prohibited(result.evidence.observed)
            if prohibited:
                return self.store.transition(
                    job["id"],
                    JobStatus.UNVERIFIED,
                    expected={JobStatus.VERIFYING},
                    error=prohibited,
                    release_lease=True,
                )
            return self.store.transition(
                job["id"],
                JobStatus.COMPLETE,
                expected={JobStatus.VERIFYING},
                release_lease=True,
                authority=VERIFIER_AUTHORITY,
            )
        if result.verified and not result.evidence.authoritative:
            return self.store.transition(job["id"], JobStatus.UNVERIFIED, error="evidence was not authoritative", release_lease=True)
        return self._verification_retry_or_unverified(job, number, result.error or "destination state not verified", result.retryable)

    def _retry_or_fail(self, job: dict[str, Any], number: int, error: str, retryable: bool, resume: JobStatus) -> dict[str, Any]:
        if retryable and number < int(job["max_attempts"]):
            return self._backoff(job["id"], number, error, resume)
        return self.store.transition(job["id"], JobStatus.FAILED, error=error, release_lease=True)

    def _verification_retry_or_unverified(self, job: dict[str, Any], number: int, error: str, retryable: bool) -> dict[str, Any]:
        if retryable and number < int(job["max_attempts"]):
            return self._backoff(job["id"], number, error, JobStatus.VERIFYING)
        return self.store.transition(job["id"], JobStatus.UNVERIFIED, error=error, release_lease=True)

    def _backoff(self, job_id: str, number: int, error: str, resume: JobStatus) -> dict[str, Any]:
        delay = min(300, 2 ** max(0, number - 1)) + random.uniform(0, 0.25)
        wake = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
        return self.store.transition(job_id, JobStatus.RETRY_WAIT, error=error, next_wakeup_at=wake, resume_status=resume, release_lease=True)
