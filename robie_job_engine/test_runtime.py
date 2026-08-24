from __future__ import annotations

import os
from typing import Any

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
from .models import JobStatus
from .request_routing import BOUNDED_ENGINE_ACTIONS
from .store import JobStore


TEST_ENV_NAME = "TEST"
PRODUCTION_ENV_NAMES = frozenset({"PRODUCTION", "PROD", "LIVE"})


class ProductionGuardError(RuntimeError):
    """Raised when Test-only wiring is asked to run outside TEST."""


def current_robie_env() -> str:
    return str(os.environ.get("ROBIE_ENV") or "").strip().upper()


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
    return JobEngine(store, workers, verifiers)


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
        return False
    build_runtime_engine(store).run(job_id)
    return True


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
