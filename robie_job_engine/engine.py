from __future__ import annotations

import concurrent.futures
import logging
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
from .perform_deadline import (
    bind_perform_job_context,
    resolve_perform_idle_seconds,
    resolve_perform_max_seconds,
    wait_for_perform_result,
)
from .models import (
    ACTION_OUTCOME_UNKNOWN,
    VERIFIER_AUTHORITY,
    WAITING_STATUSES,
    JobStatus,
    ReconciliationOutcome,
    ReconciliationResult,
    VerificationResult,
    WorkerResult,
)
from .recording import RecordingManager, RecordingRequiredError
from .request_routing import BOUNDED_ENGINE_ACTIONS, WORKER_FOR_ACTION
from .runs import IsolatedRunStore, RunIsolationError
from .secrets import redact_exception, redact_mapping
from .store import JobStore


logger = logging.getLogger(__name__)


LEFTOVER_RETRY_REFUSED = "LEFTOVER_RETRY_REFUSED"
_RETRY_TEXTS = frozenset({"retry", "/retry"})
RECONCILIATION_REQUIRED_ACTIONS = frozenset(
    {"ezlynx.reassign", "ezlynx.move_document", "ezlynx.apply_label"}
)


def is_retry_text(text: str) -> bool:
    return " ".join(str(text or "").casefold().split()) in _RETRY_TEXTS


def leftover_retry_hold_reason(
    job: dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> str | None:
    """Refuse leftover RETRY. Job Engine rule, not a handoff policy.

    RETRY is allowed only when the job is currently AWAITING_HUMAN_INPUT
    and younger than ``LIVE_TAB_CLAIM_MAX_AGE``. Terminal FAILED /
    UNVERIFIED / leftover ids must not resume via RETRY. New @robie is
    the path. No auto-retry.
    """
    from .tab_cleanup import LIVE_TAB_CLAIM_MAX_AGE, job_holds_live_tab_claim

    job = dict(job or {})
    job_id = str(job.get("id") or "").strip() or "unknown"
    try:
        status = JobStatus(job.get("status"))
    except (TypeError, ValueError):
        return (
            f"{LEFTOVER_RETRY_REFUSED}: leftover job {job_id} has no usable "
            "status. RETRY is refused. Start a new @robie. No auto-retry."
        )
    if status != JobStatus.AWAITING_HUMAN_INPUT:
        return (
            f"{LEFTOVER_RETRY_REFUSED}: leftover RETRY is refused for "
            f"{status.value} job {job_id}. RETRY is allowed only for a fresh "
            "AWAITING_HUMAN_INPUT HITL younger than one hour. Start a new "
            "@robie. No auto-retry."
        )
    if not job_holds_live_tab_claim(job, now=now):
        return (
            f"{LEFTOVER_RETRY_REFUSED}: HITL job {job_id} is older than "
            f"{LIVE_TAB_CLAIM_MAX_AGE}. Leftover RETRY is refused. Start a "
            "new @robie. No auto-retry."
        )
    return None


def resolve_worker_name(action_type, payload):
    """Authoritative worker for an action.

    For a bounded action the registry decides, not the payload. Legacy or
    conflicting payload names are advisory and cannot redirect execution.
    A bounded action with no registry entry fails closed.
    Unbounded actions keep the legacy payload-then-default behaviour.

    Returns None when the job must be refused.
    """
    registry_name = WORKER_FOR_ACTION.get(action_type)
    claimed = payload.get("worker")
    if action_type in BOUNDED_ENGINE_ACTIONS:
        if registry_name is None:
            return None
        return registry_name
    return claimed or registry_name or "hermes-cua"


class ComputerWorker(Protocol):
    def perform(self, job: dict[str, Any], *, idempotency_key: str) -> WorkerResult: ...


class Verifier(Protocol):
    def verify(self, job: dict[str, Any], action: dict[str, Any]) -> VerificationResult: ...


class DestinationReconciler(Protocol):
    def reconcile(
        self,
        job: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> ReconciliationResult: ...


class JobEngine:
    """The only component authorized to set COMPLETE."""

    def __init__(
        self,
        store: JobStore,
        workers: dict[str, ComputerWorker],
        verifiers: dict[str, Verifier],
        *,
        reconcilers: dict[str, DestinationReconciler] | None = None,
        owner: str = "robie-job-engine",
        recordings: RecordingManager | None = None,
        perform_timeout_seconds: float = 120,
        enforce_recording_policy: bool = False,
        lease_seconds: int = 120,
        call_worker_on_calling_thread: bool = False,
    ):
        self.store = store
        self.workers = workers
        self.verifiers = verifiers
        self.reconcilers = dict(reconcilers or {})
        for action_type, verifier in verifiers.items():
            if action_type not in self.reconcilers and callable(
                getattr(verifier, "reconcile", None)
            ):
                self.reconcilers[action_type] = verifier  # type: ignore[assignment]
        self.owner = owner
        self.recordings = recordings or RecordingManager(store.path)
        self.perform_timeout_seconds = perform_timeout_seconds
        self.enforce_recording_policy = enforce_recording_policy
        self.lease_seconds = max(1, int(lease_seconds))
        # Playwright sync is greenlet-bound. JE-KILL live sets this so
        # perform stays on the thread that already opened CDP in
        # reconcile / verify. Production default keeps the timeout pool.
        self.call_worker_on_calling_thread = bool(call_worker_on_calling_thread)

    def request_retry(
        self,
        job_id: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Resume via RETRY only for a fresh HITL. Leftover ids stay put."""
        from .hitl_ladder import unanswered_hitl_kill_reason

        job = self.store.get_job(job_id)
        kill = unanswered_hitl_kill_reason(job, now=now)
        if kill:
            return self.store.transition(
                job_id,
                JobStatus.FAILED,
                expected={JobStatus.AWAITING_HUMAN_INPUT},
                error=kill,
                release_lease=True,
            )
        reason = leftover_retry_hold_reason(job, now=now)
        self.store.checkpoint(
            job_id,
            "leftover_retry",
            {"refused": bool(reason), "reason": reason, "auto_retry": False},
        )
        if reason:
            current = self.store.get_job(job_id)
            current["leftover_retry_refused"] = True
            current["leftover_retry_reason"] = reason
            return current
        return self.store.resume(job_id)

    def expire_unanswered_hitl(self, *, now: datetime | None = None) -> list[str]:
        """Kill HITL jobs Carlo did not answer within 30 minutes."""
        from .hitl_ladder import expire_unanswered_hitl_jobs

        return expire_unanswered_hitl_jobs(self.store, now=now)

    def run(self, job_id: str) -> dict[str, Any]:
        self.expire_unanswered_hitl()
        lease_owner = f"{self.owner}:{uuid.uuid4()}"
        runs = IsolatedRunStore(self.store.path)
        ledger = DurableWorkLedger(self.store.path)
        try:
            run = runs.start(
                owner=lease_owner,
                job_id=job_id,
                lease_seconds=self.lease_seconds,
            )
        except RunIsolationError:
            return self.store.get_job(job_id)
        job = self.store.claim(
            job_id, lease_owner, lease_seconds=self.lease_seconds
        )
        if not job:
            runs.terminate(run["id"], "BLOCKED")
            return self.store.get_job(job_id)
        runs.bind(run["id"], "lease", {"owner": lease_owner, "job_id": job_id})
        hitl_resume = bool(dict(job.get("payload") or {}).get("hitl_resume"))
        verify_only = False
        existing_action = self.store.get_checkpoint(job_id, "action")
        existing_intent = self.store.get_checkpoint(job_id, "action_intent")
        try:
            ledger.acquire(
                job["action_type"],
                job["idempotency_key"],
                owner=lease_owner,
                timeout_seconds=self.lease_seconds,
                allow_unverified_existing=bool(existing_intent and existing_action is None),
            )
        except IdempotencyError:
            if existing_action is None:
                self.store.release_lease(job_id)
                runs.terminate(run["id"], "BLOCKED")
                return self.store.get_job(job_id)
            # A HITL reply is new work on the same job (coverage amounts).
            # Do not skip the worker just because the first attempt left an
            # action checkpoint (job 28bff7c8 / a8068d3d).
            verify_only = not hitl_resume
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
            elif contract:
                reason = (
                    "recording not enforced: registered Skill "
                    f"{job['action_type']} has recording_policy="
                    f"{contract.recording_policy} but this engine run has "
                    "enforce_recording_policy=False"
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
            interval = max(0.1, min(float(self.lease_seconds) / 3.0, 30.0))
            while not heartbeat_stop.wait(interval):
                try:
                    runs.renew_lease(
                        run["id"],
                        owner=lease_owner,
                        lease_seconds=self.lease_seconds,
                    )
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
            from .playwright_observability import maybe_snapshot_and_bind

            maybe_snapshot_and_bind(self.store.path, job_id, phase="start")
            if self._job_requires_browser(job):
                from .session_preflight import check as check_session_preflight
                from .session_recovery import attempt_session_recovery, recovery_summary

                session_check = check_session_preflight()
                if session_check.get("blocking"):
                    self.store.checkpoint(job_id, "session_preflight", session_check)
                    # The session is provably logged out. Before failing the
                    # job, attempt automatic re-authentication through the
                    # Secret Manager credential path — no human in the loop,
                    # no guard lifted. The attempt and its outcome are
                    # checkpointed so the job's trail shows what was tried.
                    recovery = attempt_session_recovery()
                    self.store.checkpoint(job_id, "session_recovery", recovery)
                    if recovery.get("recovered"):
                        session_check = check_session_preflight()
                        self.store.checkpoint(
                            job_id, "session_preflight_recheck", session_check
                        )
                if session_check.get("blocking"):
                    blocker_reason = session_check.get("reason") or "SESSION_LOGGED_OUT"
                    recovery = self.store.get_checkpoint(job_id, "session_recovery")
                    if recovery:
                        blocker_reason = (
                            f"{blocker_reason} ({recovery_summary(recovery)})"
                        )
                    self.store.checkpoint(
                        job_id,
                        "email_response",
                        {
                            "response_text": blocker_reason,
                            "body": blocker_reason,
                            "subject": "ROBIE Blocker: EZLynx Session Logged Out",
                        },
                    )
                    return self.store.transition(
                        job_id,
                        JobStatus.FAILED,
                        expected={JobStatus.PENDING, JobStatus.RUNNING},
                        error=blocker_reason,
                        release_lease=True,
                    )
            action = self.store.get_checkpoint(job_id, "action")
            if (action is None or hitl_resume) and not verify_only:
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
                    from .playwright_observability import (
                        fail_closed_zero_playwright_rows,
                        maybe_snapshot_and_bind,
                    )
                    from .post_job_audit import maybe_audit_terminal_job

                    if status_name == "UNVERIFIED":
                        fail_closed_zero_playwright_rows(
                            self.store,
                            self.store.get_job(job_id),
                            expected={JobStatus.UNVERIFIED},
                        )
                    maybe_snapshot_and_bind(self.store.path, job_id, phase="end")
                    maybe_audit_terminal_job(self.store.path, job_id)
                    if self.recordings is not None:
                        self.recordings.release_local_after_audit(job_id)
                except Exception as exc:
                    # Best-effort: a failure here (snapshot, terminal audit,
                    # tab_cleanup, or recording release) must never flip a
                    # completed job's outcome. It used to fail completely
                    # silently, which meant a broken tab_cleanup run left no
                    # trace anywhere — see Carlo's 2026-08-31 report of
                    # leftover tabs accumulating with no visible cause.
                    logger.error(
                        "post-terminal cleanup failed for job %s (status=%s): %s",
                        job_id,
                        status_name,
                        redact_exception(exc),
                    )

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
        worker_name = resolve_worker_name(job["action_type"], job["payload"])
        if worker_name is None:
            return self.store.transition(
                job["id"],
                JobStatus.FAILED,
                error=(
                    f"worker resolution refused for {job['action_type']}: "
                    f"payload names {job['payload'].get('worker')!r}, "
                    f"registry expects {WORKER_FOR_ACTION.get(job['action_type'])!r}"
                ),
                release_lease=True,
            )
        worker = self.workers.get(worker_name)
        if not worker:
            return self.store.transition(job["id"], JobStatus.FAILED, error=f"unknown worker: {worker_name}", release_lease=True)
        reconciliation_required = (
            job["action_type"] in RECONCILIATION_REQUIRED_ACTIONS
            or bool(job["payload"].get("require_destination_reconciliation"))
        )
        prior_intent = self.store.get_checkpoint(job["id"], "action_intent")
        if reconciliation_required and prior_intent is not None:
            reconciled = self._reconcile_before_repeat(
                job,
                ledger=ledger,
                run_id=run_id,
                intent=prior_intent,
            )
            if reconciled is not None:
                return reconciled
        number = self.store.increment(job["id"], "attempt_count")
        if reconciliation_required:
            intent = {
                "action": job["action_type"],
                "idempotency_key": job["idempotency_key"],
                "attempt_number": number,
                "run_id": run_id,
                "state": "PREPARED",
                "prepared_at": datetime.now(timezone.utc).isoformat(),
            }
            self.store.checkpoint(job["id"], "action_intent", intent)
            IsolatedRunStore(self.store.path).bind(run_id, "action_intent", intent)
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
            if result.hold_status == JobStatus.AWAITING_HUMAN_INPUT:
                from .hitl_ladder import stamp_hitl_posted_at

                payload = stamp_hitl_posted_at(dict(job.get("payload") or {}))
                self.store.update_payload(job["id"], payload)
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
        if reconciliation_required:
            self.store.checkpoint(
                job["id"],
                "action_intent",
                {
                    "action": job["action_type"],
                    "idempotency_key": job["idempotency_key"],
                    "attempt_number": number,
                    "run_id": run_id,
                    "state": "ACTION_RECORDED",
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        return self.store.transition(job["id"], JobStatus.VERIFYING, expected={JobStatus.RUNNING})

    def _reconcile_before_repeat(
        self,
        job: dict[str, Any],
        *,
        ledger: DurableWorkLedger,
        run_id: str,
        intent: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Read destination state before an interrupted intent can write again.

        ``None`` means an authoritative read proved the consequence did not
        occur, so a new perform attempt may proceed. Every other return value
        is a Job state and the worker is not called.
        """
        reconciler = self.reconcilers.get(job["action_type"])
        if reconciler is None:
            result = ReconciliationResult(
                ReconciliationOutcome.UNKNOWN,
                job["action_type"],
                detail={"intent": intent},
                error="destination reconciliation is required before retry but no reconciler is registered",
                hold_status=JobStatus.NEEDS_CLARIFICATION,
            )
        else:
            try:
                result = reconciler.reconcile(
                    job,
                    idempotency_key=job["idempotency_key"],
                )
            except Exception as exc:
                result = ReconciliationResult(
                    ReconciliationOutcome.UNKNOWN,
                    job["action_type"],
                    detail={"intent": intent},
                    error=f"destination reconciliation failed: {redact_exception(exc)}",
                )
        outcome = result.outcome
        if not isinstance(outcome, ReconciliationOutcome):
            try:
                outcome = ReconciliationOutcome(str(outcome))
            except ValueError:
                outcome = ReconciliationOutcome.UNKNOWN
        if not result.authoritative and outcome != ReconciliationOutcome.UNKNOWN:
            outcome = ReconciliationOutcome.UNKNOWN
        if outcome == ReconciliationOutcome.APPLIED and not result.destination:
            outcome = ReconciliationOutcome.UNKNOWN
        evidence = {
            "outcome": outcome.value,
            "authoritative": bool(result.authoritative),
            "action": result.action,
            "destination": redact_mapping(result.destination),
            "detail": redact_mapping(result.detail),
            "error": result.error,
            "intent": intent,
            "reconciled_at": datetime.now(timezone.utc).isoformat(),
        }
        self.store.checkpoint(job["id"], "action_reconciliation", evidence)
        IsolatedRunStore(self.store.path).bind(run_id, "action_reconciliation", evidence)
        if outcome == ReconciliationOutcome.NOT_APPLIED:
            self.store.checkpoint(
                job["id"],
                "action_intent",
                {
                    **intent,
                    "state": "RECONCILED_NOT_APPLIED",
                    "reconciled_at": evidence["reconciled_at"],
                },
            )
            return None
        if outcome == ReconciliationOutcome.APPLIED:
            try:
                ledger.record_external_action(job["action_type"], job["idempotency_key"])
            except IdempotencyError:
                pass
            action = {
                "action": result.action or job["action_type"],
                "destination": result.destination,
                "detail": {
                    **result.detail,
                    "reconciled_after_interruption": True,
                    "idempotency_key": job["idempotency_key"],
                },
            }
            self.store.checkpoint(job["id"], "action", action)
            self.store.checkpoint(
                job["id"],
                "action_intent",
                {
                    **intent,
                    "state": "RECONCILED_APPLIED",
                    "reconciled_at": evidence["reconciled_at"],
                },
            )
            return self.store.transition(
                job["id"],
                JobStatus.VERIFYING,
                expected={JobStatus.RUNNING},
            )
        hold = result.hold_status
        if hold not in WAITING_STATUSES:
            hold = JobStatus.WAITING
        return self.store.transition(
            job["id"],
            hold,
            expected={JobStatus.RUNNING},
            error=result.error or "destination reconciliation was inconclusive; action not repeated",
            resume_status=JobStatus.PENDING,
            release_lease=True,
        )

    def _call_worker(self, worker: ComputerWorker, job: dict[str, Any]) -> WorkerResult:
        idle_seconds = resolve_perform_idle_seconds(
            job, engine_default=self.perform_timeout_seconds
        )
        max_seconds = resolve_perform_max_seconds(job, idle_seconds=idle_seconds)
        if self.call_worker_on_calling_thread:
            return worker.perform(job, idempotency_key=job["idempotency_key"])
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            with bind_perform_job_context(
                self.store, job, idle_seconds=idle_seconds, max_seconds=max_seconds
            ):
                future = pool.submit(
                    worker.perform, job, idempotency_key=job["idempotency_key"]
                )
                return wait_for_perform_result(
                    future,
                    self.store,
                    job,
                    idle_seconds=idle_seconds,
                    max_seconds=max_seconds,
                )
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
            if result.hold_status == JobStatus.AWAITING_HUMAN_INPUT:
                from .hitl_ladder import stamp_hitl_posted_at

                payload = stamp_hitl_posted_at(dict(job.get("payload") or {}))
                self.store.update_payload(job["id"], payload)
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

    @staticmethod
    def _job_requires_browser(job: dict[str, Any] | None) -> bool:
        if not job:
            return False
        from .playwright_observability import (
            job_requires_playwright,
            job_text,
            PLAYWRIGHT_REQUEST_MARKERS,
        )

        if job_requires_playwright(job):
            return True
        action = str(job.get("action_type") or "")
        if action in {
            "browser.read",
            "ezlynx.reassign",
            "ezlynx.move_document",
            "ezlynx.apply_label",
            "ezlynx.submission_audit",
            "ezlynx.overdue_submission_reports",
            "ezlynx.session_refresh",
        }:
            return True
        if action == "hermes.email_task":
            text = " ".join(job_text(job).casefold().split())
            return any(marker in text for marker in PLAYWRIGHT_REQUEST_MARKERS)
        return False
