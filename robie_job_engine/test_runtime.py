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
from .ezlynx import EzlynxDestinationVerifier, HermesCuaEzlynxWorker
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
    destination = proposal_destination or MemoryProposalDestination()
    workers: dict[str, Any] = {
        "carrier-proposal": BoundedCarrierProposalWorker(destination, store),
        "hermes-cua": HermesCuaEzlynxWorker(ezlynx_browser) if ezlynx_browser else _UnavailableWorker(),
    }
    verifiers: dict[str, Any] = {
        "carrier.proposal": CarrierProposalVerifier(destination),
    }
    if browser_port is not None:
        workers["browser-read"] = BoundedBrowserReadWorker(browser_port)
        verifiers["browser.read"] = BrowserReadVerifier(browser_port)
    if ezlynx_readback is not None:
        ezlynx_verifier = EzlynxDestinationVerifier(ezlynx_readback)
        verifiers["ezlynx.reassign"] = ezlynx_verifier
        verifiers["ezlynx.move_document"] = ezlynx_verifier
        verifiers["ezlynx.apply_label"] = ezlynx_verifier
    return JobEngine(store, workers, verifiers)


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
    store = JobStore(db_path)
    job = store.get_job(job_id)
    if job["action_type"] not in BOUNDED_ENGINE_ACTIONS:
        return False
    if JobStatus(job["status"]) != JobStatus.PENDING:
        return False
    engine = build_test_engine(store)
    engine.run(job_id)
    return True
