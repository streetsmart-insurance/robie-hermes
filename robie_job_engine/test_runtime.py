from __future__ import annotations

from typing import Any, Callable

from .browser_read import BoundedBrowserReadWorker, BrowserReadVerifier
from .carrier_proposal import (
    BoundedCarrierProposalWorker,
    CarrierProposalVerifier,
    MemoryProposalDestination,
)
from .engine import JobEngine
from .ezlynx import (
    BoundedEzlynxWorker,
    EzlynxDestinationVerifier,
    HermesCuaEzlynxWorker,
    MemoryEzlynxDestination,
)
from .idempotency import IdempotencyError
from .models import TERMINAL_STATUSES, WAITING_STATUSES, JobStatus
from .request_routing import BOUNDED_ENGINE_ACTIONS
from .submission_audit import EzlynxSubmissionAuditWorker, SubprocessSubmissionReadback
from .runtime_env import (
    PRODUCTION_ENV_NAMES,
    TEST_ENV_NAME,
    ProductionGuardError,
    chat_path_is_sandbox,
    current_robie_env,
)
from .store import JobStore


def require_test_environment() -> str:
    """Refuse to wire or run bounded workers anywhere except TEST."""
    env = current_robie_env()
    if env in PRODUCTION_ENV_NAMES:
        raise ProductionGuardError(
            "refusing to wire bounded workers: ROBIE_ENV is Production"
        )
    if env != TEST_ENV_NAME:
        raise ProductionGuardError(
            "bounded Test gateway wiring requires ROBIE_ENV=TEST; "
            f"current value is {env or '<unset>'}"
        )
    return env


def build_test_engine(
    store: JobStore,
    *,
    proposal_destination: Any | None = None,
    browser_port: Any | None = None,
    ezlynx_browser: Any | None = None,
    ezlynx_readback: Any | None = None,
) -> JobEngine:
    require_test_environment()
    return build_runtime_engine(
        store,
        proposal_destination=proposal_destination,
        browser_port=browser_port,
        ezlynx_browser=ezlynx_browser,
        ezlynx_readback=ezlynx_readback,
        enforce_recording_policy=False,
    )


class _UnavailableWorker:
    def perform(self, job: dict[str, Any], *, idempotency_key: str):
        from .models import WorkerResult

        return WorkerResult(
            False,
            job.get("action_type") or "unknown",
            {},
            retryable=False,
            error="Test gateway worker is not registered for this action",
        )


def build_runtime_engine(
    store: JobStore,
    *,
    proposal_destination: Any | None = None,
    browser_port: Any | None = None,
    ezlynx_browser: Any | None = None,
    ezlynx_readback: Any | None = None,
    skill_roots: tuple[str, ...] | None = None,
    submission_readback: Any | None = None,
    enforce_recording_policy: bool = True,
) -> JobEngine:
    """Bounded Job Engine for Test and Production Chat intake.

    Does not open live EZLynx unless an explicit port is supplied.
    Hermes/cua-driver remains the path for non-bounded chat.
    """
    destination = proposal_destination
    if destination is None and current_robie_env() == TEST_ENV_NAME:
        destination = MemoryProposalDestination()
    if ezlynx_browser is not None:
        ezlynx_worker: Any = HermesCuaEzlynxWorker(ezlynx_browser)
    elif isinstance(ezlynx_readback, MemoryEzlynxDestination):
        ezlynx_worker = BoundedEzlynxWorker(ezlynx_readback)
    else:
        ezlynx_worker = _UnavailableWorker()
    workers: dict[str, Any] = {
        "carrier-proposal": (
            BoundedCarrierProposalWorker(destination, store)
            if destination is not None
            else _UnavailableWorker()
        ),
        "hermes-cua": ezlynx_worker,
        "submission-audit": EzlynxSubmissionAuditWorker(),
    }
    verifiers: dict[str, Any] = {}
    if destination is not None:
        verifiers["carrier.proposal"] = CarrierProposalVerifier(destination)
    if browser_port is not None:
        workers["browser-read"] = BoundedBrowserReadWorker(browser_port)
        verifiers["browser.read"] = BrowserReadVerifier(browser_port)
    if ezlynx_readback is not None:
        ezlynx_verifier = EzlynxDestinationVerifier(ezlynx_readback)
        verifiers["ezlynx.reassign"] = ezlynx_verifier
        verifiers["ezlynx.move_document"] = ezlynx_verifier
        verifiers["ezlynx.apply_label"] = ezlynx_verifier
    if skill_roots:
        from .chat_verifiers import FilesystemSkillUpdateVerifier

        verifiers["filesystem.skill_update"] = FilesystemSkillUpdateVerifier(skill_roots)
    from .chat_verifiers import EzlynxSubmissionAuditVerifier

    verifiers["ezlynx.submission_audit"] = EzlynxSubmissionAuditVerifier(
        submission_readback or SubprocessSubmissionReadback()
    )
    return JobEngine(
        store,
        workers,
        verifiers,
        enforce_recording_policy=enforce_recording_policy,
    )


def _fail_closed_engine_start(store: JobStore, job_id: str, exc: BaseException) -> None:
    current = store.get_job(job_id)
    status = JobStatus(current["status"])
    if status in TERMINAL_STATUSES or status in WAITING_STATUSES:
        return
    try:
        store.transition(
            job_id,
            JobStatus.FAILED,
            expected={
                JobStatus.PENDING,
                JobStatus.RUNNING,
                JobStatus.VERIFYING,
            },
            error=f"durable engine failed: {exc}",
            release_lease=True,
        )
    except Exception:
        return


def maybe_run_bounded_job(db_path: str, job_id: str | None) -> bool:
    """Run a bounded operational Job through the durable Job Engine.

    Used for Test and Production Chat. Non-bounded conversation still
    returns False so Hermes/cua-driver can continue.
    """
    if not job_id:
        return False
    store = JobStore(db_path)
    job = store.get_job(job_id)
    if job["action_type"] not in BOUNDED_ENGINE_ACTIONS:
        return False
    if JobStatus(job["status"]) != JobStatus.PENDING:
        return True
    try:
        build_runtime_engine(store).run(job_id)
    except (IdempotencyError, ProductionGuardError, OSError) as exc:
        _fail_closed_engine_start(store, job_id, exc)
    return True


def dispatch_operational_chat(
    db_path: str,
    job_id: str | None,
    *,
    sandbox: bool = False,
    hermes: Callable[[], None] | None = None,
) -> bool:
    """Route operational Chat. Return True when Hermes must not continue.

    Hermes/cua-driver may run only for non-operational messages or an
    explicitly labeled sandbox path. A ledger/path/engine start failure
    is fail-closed and never invokes Hermes.
    """
    if sandbox or chat_path_is_sandbox():
        if hermes is not None:
            hermes()
        return False
    if not job_id:
        if hermes is not None:
            hermes()
        return False
    try:
        store = JobStore(db_path)
        job = store.get_job(job_id)
    except Exception:
        return True
    error = str(job.get("last_error") or "")
    durable_failed = (
        "durable intake failed" in error
        or "durable engine failed" in error
        or "persistent store cannot" in error
    )
    if durable_failed or job["action_type"] in BOUNDED_ENGINE_ACTIONS:
        if JobStatus(job["status"]) == JobStatus.PENDING:
            try:
                maybe_run_bounded_job(db_path, job_id)
            except (IdempotencyError, ProductionGuardError, OSError) as exc:
                _fail_closed_engine_start(store, job_id, exc)
        return True
    if hermes is not None:
        hermes()
    return False


def maybe_run_test_bounded_job(db_path: str, job_id: str | None) -> bool:
    """Run a bounded Job on the Test gateway only.

    Returns True when the Test engine handled the Job so Hermes/cua-driver
    must not continue as an unrestricted Chat worker. Returns False when
    this is not TEST or the Job is not a bounded action, leaving the
    existing Hermes path unchanged.
    """
    if not job_id or current_robie_env() != TEST_ENV_NAME:
        return False
    require_test_environment()
    return maybe_run_bounded_job(db_path, job_id)
