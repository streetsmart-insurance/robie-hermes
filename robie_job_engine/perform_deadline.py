"""Progress-aware Job Engine perform deadline.

The default starting budget stays 120 seconds so short jobs that never
report progress still fail closed on the same schedule. While a worker
writes durable progress — ``perform_progress``, ``gateway_progress``,
``worker_progress``, attempt detail, or ``playwright_exec`` — the idle
deadline refreshes. A silent hung browser still dies after the idle
window. Lease heartbeats do not count: they only keep ownership.

Optional ``perform_max_seconds`` (payload, then skill contract) is a hard
ceiling so a worker that keeps emitting heartbeats cannot run forever.
"""

from __future__ import annotations

import concurrent.futures
import os
from contextlib import contextmanager
from typing import Any, Iterator

from .job_schema import get_executable_skill_contract
from .store import JobStore


DEFAULT_PERFORM_TIMEOUT_SECONDS = 120.0
PROGRESS_CHECKPOINT_KINDS = (
    "gateway_progress",
    "perform_progress",
    "worker_progress",
)
_ENV_KEYS = (
    "ROBIE_JOB_ID",
    "ROBIE_CURRENT_JOB_ID",
    "ROBIE_JOB_DB",
    "ROBIE_JOB_ACTION",
    "ROBIE_PERFORM_MAX_SECONDS",
    "ROBIE_PERFORM_IDLE_SECONDS",
)


def _positive_seconds(raw: Any, fallback: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(fallback)
    if value <= 0:
        return float(fallback)
    return value


def resolve_perform_idle_seconds(
    job: dict[str, Any] | None,
    *,
    engine_default: float = DEFAULT_PERFORM_TIMEOUT_SECONDS,
) -> float:
    """Starting / idle budget. Payload, then skill contract, then engine default."""
    job = dict(job or {})
    payload = dict(job.get("payload") or {})
    if payload.get("perform_timeout_seconds") not in (None, ""):
        return _positive_seconds(payload.get("perform_timeout_seconds"), engine_default)
    contract = get_executable_skill_contract(str(job.get("action_type") or ""))
    if contract is not None and contract.perform_timeout_seconds is not None:
        return float(contract.perform_timeout_seconds)
    return _positive_seconds(engine_default, DEFAULT_PERFORM_TIMEOUT_SECONDS)


def resolve_perform_max_seconds(
    job: dict[str, Any] | None,
    *,
    idle_seconds: float,
) -> float | None:
    """Optional hard ceiling. Payload, then skill contract. None means no extra cap."""
    job = dict(job or {})
    payload = dict(job.get("payload") or {})
    raw = payload.get("perform_max_seconds")
    if raw not in (None, ""):
        return max(_positive_seconds(raw, idle_seconds), float(idle_seconds))
    contract = get_executable_skill_contract(str(job.get("action_type") or ""))
    if contract is not None and contract.perform_max_seconds is not None:
        return max(float(contract.perform_max_seconds), float(idle_seconds))
    return None


def progress_fingerprint(store: JobStore, job_id: str | None) -> tuple[Any, ...] | None:
    """Durable progress token. Lease renewals and jobs.updated_at are ignored."""
    resolved = str(job_id or "").strip()
    if not resolved:
        return None
    return store.latest_perform_progress_fingerprint(resolved)


def wait_for_perform_result(
    future: concurrent.futures.Future[Any],
    store: JobStore,
    job: dict[str, Any],
    *,
    idle_seconds: float,
    max_seconds: float | None = None,
    monotonic: Any = None,
) -> Any:
    """Wait for ``future`` while refreshing the idle deadline on new progress."""
    import time

    clock = time.monotonic if monotonic is None else monotonic
    job_id = str(job.get("id") or "").strip() or None
    started = clock()
    idle = max(0.001, float(idle_seconds))
    deadline = started + idle
    ceiling_at = None if max_seconds is None else started + max(float(max_seconds), idle)
    last_fingerprint = progress_fingerprint(store, job_id)
    poll = min(1.0, max(0.02, idle / 5.0))

    while True:
        now = clock()
        if ceiling_at is not None and now >= ceiling_at:
            raise TimeoutError(
                f"worker perform exceeded {max_seconds}s ceiling"
            )
        remaining_idle = deadline - now
        if remaining_idle <= 0:
            raise TimeoutError(
                f"worker perform exceeded {idle_seconds}s without progress"
            )
        remaining = remaining_idle
        if ceiling_at is not None:
            remaining = min(remaining, ceiling_at - now)
        try:
            return future.result(timeout=max(0.001, min(remaining, poll)))
        except concurrent.futures.TimeoutError:
            fingerprint = progress_fingerprint(store, job_id)
            if fingerprint is not None and fingerprint != last_fingerprint:
                last_fingerprint = fingerprint
                deadline = clock() + idle


@contextmanager
def bind_perform_job_context(
    store: JobStore,
    job: dict[str, Any],
    *,
    idle_seconds: float,
    max_seconds: float | None,
) -> Iterator[None]:
    """Expose the live job so workers and helper subprocesses can heartbeat."""
    job_id = str(job.get("id") or "").strip()
    previous = {key: os.environ.get(key) for key in _ENV_KEYS}
    try:
        if job_id:
            os.environ["ROBIE_JOB_ID"] = job_id
            os.environ["ROBIE_CURRENT_JOB_ID"] = job_id
            os.environ["ROBIE_JOB_DB"] = str(store.path)
            os.environ["ROBIE_JOB_ACTION"] = str(job.get("action_type") or "")
            os.environ["ROBIE_PERFORM_IDLE_SECONDS"] = str(idle_seconds)
            if max_seconds is not None:
                os.environ["ROBIE_PERFORM_MAX_SECONDS"] = str(int(max_seconds))
            elif "ROBIE_PERFORM_MAX_SECONDS" in os.environ:
                del os.environ["ROBIE_PERFORM_MAX_SECONDS"]
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
