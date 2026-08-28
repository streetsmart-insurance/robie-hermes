from __future__ import annotations

import concurrent.futures
import random
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from .action_gate import hold_reason_for_job
from .complete_guard import (
    complete_is_prohibited,
    destination_identity_missing,
    evidence_is_stale,
    intended_destination_identity,
    postcondition_mismatch,
)
from .idempotency import DurableWorkLedger, IdempotencyError
from .job_schema import bounded_schema_hold_reason, get_executable_skill_contract
from .models import (
    ACTION_OUTCOME_UNKNOWN,
    VERIFIER_AUTHORITY,
    WAITING_STATUSES,
    JobStatus,
    VerificationResult,
    WorkerResult,
)
from .recording import RecordingManager, RecordingRequiredError
from .runs import IsolatedRunStore, RunIsolationError
from .secrets import redact_exception, redact_mapping
from .store import JobStore


class ComputerWorker(Protocol):
    def perform(self, job: dict[str, Any], *, idempotency_key: str) -> WorkerResult: ...


class Verifier(Protocol):
    def verify(self, job: dict[str, Any], action: dict[str, Any]) -> VerificationResult: ...


class JobEngine:
    """The only component authorized to set COMPLETE."""

    def __init__(
        self,
        store: JobStore,
        workers: dict[str, ComputerWorker],
        verifiers: dict[str, Verifier],
        *,
        owner: str = "robie-job-engine",
        recordings: RecordingManager | None = None,
        perform_timeout_seconds: float = 120,
        enforce_recording_policy: bool = False,
        lease_seconds: int = 120,
    ):
        self.store = store
        self.workers = workers
        self.verifiers = verifiers
        self.owner = owner
        self.recordings = recordings or RecordingManager(store.path)
        self.perform_timeout_seconds = perform_timeout_seconds
        self.enforce_recording_policy = enforce_recording_policy
        self.lease_seconds = max(15, int(lease_seconds))

    def run(self, job_id: str) -> dict[str, Any]:
        lease_owner = f"{self.owner}:{uuid.uuid4()}"
        runs = IsolatedRunStore(self.store.path)
        ledger = DurableWorkLedger(self.store.path)
        try:
            run = runs.start(owner=lease_owner, job_id=job_id)
        except RunIsolationError:
            return self.store.get_job(job_id)
        job = self.store.claim(
            job_id, lease_owner, lease_seconds=self.lease_seconds
        )
        if not job:
            runs.terminate(run["id"], "BLOCKED")
            return self.store.get_job(job_id)
        runs.bind(run["id"], "lease", {"owner": lease_owner, "job_id": job_id})
        verify_only = False
        try:
            ledger.acquire(
                job["action_type"],
                job["idempotency_key"],
                owner=lease_owner,
                timeout_seconds=self.lease_seconds,
            )
        except IdempotencyError:
            if self.store.get_checkpoint(job_id, "action") is None:
                self.store.release_lease(job_id)
                runs.terminate(run["id"], "BLOCKED")
                return self.store.get_job(job_id)
            verify_only = True
        runs.bind(
            run["id"],
            "durable_work",
            {
                "namespace": job["action_type"],
                "work_item_key": job["idempotency_key"],
                "verify_only": verify_only,
                "lease_owner": lease_owner,
            },
        )
        contract = get_executable_skill_contract(job["action_type"])
        recording_required = bool(
            contract
            and contract.recording_policy == "REQUIRED"
            and (self.enforce_recording_policy or self.recordings.enabled)
        )
        recording_started = False
        recording_finalized = False
        if recording_required:
            try:
                self.recordings.start_required(job_id)
                recording_started = True
            except RecordingRequiredError as exc:
                failed = self.store.transition(
                    job_id,
                    JobStatus.FAILED,
                    expected={JobStatus.PENDING, JobStatus.RUNNING, JobStatus.VERIFYING},
                    error=str(exc),
                    release_lease=True,
                )
                runs.terminate(run["id"], "FAILED")
                return failed
        else:
            if job["action_type"] == "drive.skill_sync":
                reason = (
                    "recording exemption: read-only Google Drive ingestion has no browser UI; "
                    "the immutable snapshot is independently verified by exact SHA-256 reread"
                )
            elif contract and contract.recording_policy == "EXEMPT":
                reason = (
                    "registered Skill recording policy is EXEMPT because authentication "
                    "may display credentials or MFA data"
                )
            else:
                reason = "action is not registered as an executable Skill"
            self.store.checkpoint(
                job_id,
                "recording_exemption",
                {"reason": reason},
            )
        heartbeat_stop = threading.Event()
        heartbeat_errors: list[Exception] = []

        def maintain_leases() -> None:
            interval = max(5.0, min(float(self.lease_seconds) / 3.0, 30.0))
            while not heartbeat_stop.wait(interval):
                try:
                    self.store.renew_lease(
                        job_id,
                        lease_owner,
                        lease_seconds=self.lease_seconds,
                    )
                    ledger.renew_lease(
                        job["action_type"],
                        job["idempotency_key"],
                        owner=lease_owner,
                        timeout_seconds=self.lease_seconds,
                    )
                except Exception as exc:
                    heartbeat_errors.append(exc)
                    heartbeat_stop.set()

        heartbeat = threading.Thread(
            target=maintain_leases,
            name=f"robie-job-lease:{job_id}",
            daemon=True,
        )
        heartbeat.start()
        try:
            action = self.store.get_checkpoint(job_id, "action")
            if action is None and not verify_only:
                job = self._perform(job, ledger=ledger, run_id=run["id"])
                if job["status"] != JobStatus.VERIFYING:
                    if recording_started:
                        recording = self.recordings.stop_and_upload(
                            job_id, JobStatus(job["status"]).value
                        )
                        recording_finalized = True
                        recording_error = self.recordings.completion_error(job_id)
                        if recording is None or recording_error:
                            return self.store.transition(
                                job_id,
                                JobStatus.FAILED,
                                expected={JobStatus(job["status"])},
                                error=recording_error or "Recording upload failed",
                                release_lease=True,
                            )
                    return job
                action = self.store.get_checkpoint(job_id, "action")
            elif action is not None and JobStatus(self.store.get_job(job_id)["status"]) in {
                JobStatus.PENDING,
                JobStatus.RUNNING,
            }:
                self.store.transition(
                    job_id,
                    JobStatus.VERIFYING,
                    expected={JobStatus.PENDING, JobStatus.RUNNING},
                )
            if heartbeat_errors:
                raise RuntimeError("durable execution lease heartbeat failed")
            final = self._verify(self.store.get_job(job_id), action or {}, defer_complete=True)
            if heartbeat_errors:
                raise RuntimeError("durable execution lease heartbeat failed")
            if (
                JobStatus(final["status"]) == JobStatus.VERIFYING
                and self.store.get_checkpoint(job_id, "verification_passed")
            ):
                if recording_required:
                    recording = self.recordings.stop_and_upload(job_id, JobStatus.COMPLETE.value)
                    recording_finalized = True
                    recording_error = self.recordings.completion_error(job_id)
                    if recording is None or recording_error:
                        return self.store.transition(
                            job_id,
                            JobStatus.FAILED,
                            expected={JobStatus.VERIFYING},
                            error=recording_error or "Recording upload failed",
                            release_lease=True,
                        )
                final = self.store.transition(
                    job_id,
                    JobStatus.COMPLETE,
                    expected={JobStatus.VERIFYING},
                    release_lease=True,
                    authority=VERIFIER_AUTHORITY,
                )
            elif recording_started:
                recording = self.recordings.stop_and_upload(
                    job_id, JobStatus(final["status"]).value
                )
                recording_finalized = True
                recording_error = self.recordings.completion_error(job_id)
                if recording is None or recording_error:
                    final = self.store.transition(
                        job_id,
                        JobStatus.FAILED,
                        expected={JobStatus(final["status"])},
                        error=recording_error or "Recording upload failed",
                        release_lease=True,
                    )
            if JobStatus(final["status"]) == JobStatus.COMPLETE:
                try:
                    ledger.mark_verified(job["action_type"], job["idempotency_key"])
                except KeyError:
                    pass
            return final
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=5)
            final = self.store.get_job(job_id)
            final_status = final["status"]
            status_name = (
                final_status.value if isinstance(final_status, JobStatus) else str(final_status)
            )
            if recording_started and not recording_finalized:
                recording = self.recordings.stop_and_upload(job_id, status_name)
                recording_finalized = True
                recording_error = self.recordings.completion_error(job_id)
                if recording is None or recording_error:
                    current = self.store.get_job(job_id)
                    current_status = JobStatus(current["status"])
                    if current_status != JobStatus.COMPLETE:
                        try:
                            final = self.store.transition(
                                job_id,
                                JobStatus.FAILED,
                                expected={current_status},
                                error=recording_error or "Recording upload failed",
                                release_lease=True,
                            )
                            status_name = JobStatus.FAILED.value
                        except RuntimeError:
                            pass
            if not runs.get(run["id"]).get("terminal_event"):
                event = status_name if status_name in {"COMPLETE", "FAILED", "UNVERIFIED", "CANCELLED"} else "BLOCKED"
                try:
                    runs.terminate(run["id"], event)
                except RunIsolationError:
                    pass
            if status_name in {"COMPLETE", "FAILED", "UNVERIFIED"}:
                try:
                    from .post_job_audit import maybe_audit_terminal_job

                    maybe_audit_terminal_job(self.store.path, job_id)
                    if self.recordings is not None:
                        self.recordings.release_local_after_audit(job_id)
                except Exception:
                    pass

    def _perform(
        self,
        job: dict[str, Any],
        *,
        ledger: DurableWorkLedger,
        run_id: str,
    ) -> dict[str, Any]:
        action_hold = hold_reason_for_job(job)
        if action_hold:
            return self.store.transition(
                job["id"],
                JobStatus.FAILED,
                expected={JobStatus.PENDING, JobStatus.RUNNING},
                error=action_hold,
                release_lease=True,
            )
        schema_hold = bounded_schema_hold_reason(job["action_type"], job["payload"])
        if schema_hold:
            return self.store.transition(
                job["id"],
                JobStatus.NEEDS_CLARIFICATION,
                expected={JobStatus.PENDING, JobStatus.RUNNING},
                error=schema_hold,
                resume_status=JobStatus.PENDING,
                release_lease=True,
            )
        self.store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING, JobStatus.RUNNING})
        number = self.store.increment(job["id"], "attempt_count")
        worker_name = job["payload"].get("worker", "hermes-cua")
        worker = self.workers.get(worker_name)
        if not worker:
            return self.store.transition(job["id"], JobStatus.FAILED, error=f"unknown worker: {worker_name}", release_lease=True)
        try:
            result = self._call_worker(worker, job)
        except Exception as exc:
            error = redact_exception(exc)
            self.store.add_attempt(
                job["id"],
                "perform",
                number,
                ACTION_OUTCOME_UNKNOWN,
                {"error": error, "outcome": ACTION_OUTCOME_UNKNOWN},
            )
            self.store.checkpoint(
                job["id"],
                "action",
                {
                    "action": job["action_type"],
                    "destination": {},
                    "detail": {"outcome": ACTION_OUTCOME_UNKNOWN, "error": error, "run_id": run_id},
                },
            )
            try:
                ledger.mark_timeout_unknown(job["action_type"], job["idempotency_key"])
            except KeyError:
                pass
            try:
                ledger.record_external_action(job["action_type"], job["idempotency_key"])
            except (IdempotencyError, KeyError):
                pass
            if isinstance(exc, TimeoutError):
                try:
                    IsolatedRunStore(self.store.path).cancel(run_id)
                except (RunIsolationError, KeyError):
                    pass
            return self.store.transition(
                job["id"],
                JobStatus.VERIFYING,
                expected={JobStatus.RUNNING},
                error=f"{ACTION_OUTCOME_UNKNOWN}: {error}",
                release_lease=True,
            )
        self.store.add_attempt(
            job["id"],
            "perform",
            number,
            "success" if result.succeeded else "failure",
            {"error": result.error, "detail": redact_mapping(result.detail)},
        )
        if result.succeeded:
            try:
                ledger.record_external_action(job["action_type"], job["idempotency_key"])
            except IdempotencyError:
                return self.store.transition(
                    job["id"],
                    JobStatus.VERIFYING,
                    expected={JobStatus.RUNNING},
                    error="external action already recorded; resume at verification only",
                    release_lease=True,
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
            from .retry_policy import FailureClass, classify_failure

            classification = classify_failure(result.error)
            hold = classification.suggested_hold_status or result.hold_status
            if hold is not None and hold in WAITING_STATUSES:
                return self.store.transition(
                    job["id"],
                    hold,
                    expected={JobStatus.RUNNING},
                    error=result.error,
                    resume_status=JobStatus.PENDING,
                    release_lease=True,
                )
            if classification.failure_class in (
                FailureClass.LOCATOR_AMBIGUITY,
                FailureClass.EMPTY_OR_CORRUPT_ARTIFACT,
                FailureClass.AUTH_CHALLENGE,
            ):
                retryable = False
            elif classification.failure_class == FailureClass.TRANSIENT_NETWORK:
                retryable = True
            else:
                retryable = result.retryable
            return self._retry_or_fail(
                job,
                number,
                result.error or "worker failed",
                retryable,
                JobStatus.PENDING,
            )
        action = {"action": result.action, "destination": result.destination, "detail": result.detail}
        self.store.checkpoint(job["id"], "action", action)
        return self.store.transition(job["id"], JobStatus.VERIFYING, expected={JobStatus.RUNNING})

    def _call_worker(self, worker: ComputerWorker, job: dict[str, Any]) -> WorkerResult:
        timeout = float(job["payload"].get("perform_timeout_seconds", self.perform_timeout_seconds))
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(worker.perform, job, idempotency_key=job["idempotency_key"])
            try:
                return future.result(timeout=timeout)
            except concurrent.futures.TimeoutError as exc:
                raise TimeoutError(f"worker perform exceeded {timeout}s") from exc
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def _verify(
        self,
        job: dict[str, Any],
        action: dict[str, Any],
        *,
        defer_complete: bool = False,
    ) -> dict[str, Any]:
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
            mismatch = postcondition_mismatch(
                result.evidence.expected, result.evidence.observed
            )
            stale = evidence_is_stale(
                captured_at=result.evidence.captured_at,
                not_before=job.get("created_at"),
            )
            identity = destination_identity_missing(
                locator=result.evidence.locator,
                expected=result.evidence.expected,
                observed=result.evidence.observed,
                intended=intended_destination_identity(
                    action=action, payload=job.get("payload")
                ),
                job_id=job.get("id"),
            )
            reason = prohibited or mismatch or stale or identity
            if reason:
                return self.store.transition(
                    job["id"],
                    JobStatus.UNVERIFIED,
                    expected={JobStatus.VERIFYING},
                    error=reason,
                    release_lease=True,
                )
            if defer_complete:
                self.store.checkpoint(
                    job["id"],
                    "verification_passed",
                    {"evidence": "authoritative", "recording_pending": True},
                )
                return self.store.get_job(job["id"])
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
        if retryable and number < self._maximum_attempts(job):
            return self._backoff(job["id"], number, error, resume)
        return self.store.transition(job["id"], JobStatus.FAILED, error=error, release_lease=True)

    def _verification_retry_or_unverified(self, job: dict[str, Any], number: int, error: str, retryable: bool) -> dict[str, Any]:
        if retryable and number < self._maximum_attempts(job):
            return self._backoff(job["id"], number, error, JobStatus.VERIFYING)
        return self.store.transition(job["id"], JobStatus.UNVERIFIED, error=error, release_lease=True)

    def _backoff(self, job_id: str, number: int, error: str, resume: JobStatus) -> dict[str, Any]:
        delay = min(300, 2 ** max(0, number - 1)) + random.uniform(0, 0.25)
        wake = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
        return self.store.transition(job_id, JobStatus.RETRY_WAIT, error=error, next_wakeup_at=wake, resume_status=resume, release_lease=True)

    @staticmethod
    def _maximum_attempts(job: dict[str, Any]) -> int:
        contract = get_executable_skill_contract(job["action_type"])
        configured = int(job["max_attempts"])
        return min(configured, contract.maximum_attempts) if contract else configured
