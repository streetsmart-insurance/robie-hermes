"""Auditable model fallback. Mid-run changes must be checkpointed."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Callable, Iterable

from .model_fallback import AllModelsFailed, ModelTarget, execute_with_fallback
from .store import utc_now


class FallbackPolicyError(RuntimeError):
    pass


_SUITE_PASSING: set[tuple[str, str]] = set()
DEFAULT_SAFETY_POLICY_VERSION = "robie-safety-2026-08-24"


def allow_suite_passing(provider: str, model: str) -> None:
    _SUITE_PASSING.add((provider, model))


def clear_suite_passing() -> None:
    _SUITE_PASSING.clear()


def is_suite_passing(target: ModelTarget) -> bool:
    return (target.provider, target.model) in _SUITE_PASSING


class FallbackAuditStore:
    def __init__(self, db_path: str | Path) -> None:
        self.path = str(db_path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS model_fallback_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    run_id TEXT,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    version TEXT,
                    prompt_hash TEXT,
                    config_json TEXT NOT NULL,
                    fallback_reason TEXT,
                    safety_policy_version TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def record(
        self,
        *,
        job_id: str,
        target: ModelTarget,
        outcome: str,
        run_id: str | None = None,
        version: str | None = None,
        prompt: str | None = None,
        config: dict[str, Any] | None = None,
        fallback_reason: str | None = None,
        safety_policy_version: str = DEFAULT_SAFETY_POLICY_VERSION,
    ) -> None:
        prompt_hash = (
            hashlib.sha256(prompt.encode()).hexdigest() if prompt is not None else None
        )
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO model_fallback_audit
                   (job_id,run_id,provider,model,version,prompt_hash,config_json,
                    fallback_reason,safety_policy_version,outcome,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    job_id,
                    run_id,
                    target.provider,
                    target.model,
                    version,
                    prompt_hash,
                    json.dumps(config or {}, sort_keys=True),
                    fallback_reason,
                    safety_policy_version,
                    outcome,
                    utc_now(),
                ),
            )

    def events(self, job_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM model_fallback_audit WHERE job_id=? ORDER BY id",
                (job_id,),
            ).fetchall()
        return [dict(row) for row in rows]


def execute_with_audited_fallback(
    targets: Iterable[ModelTarget],
    call: Callable[[ModelTarget], Any],
    audit: FallbackAuditStore,
    *,
    job_id: str,
    run_id: str | None = None,
    prompt: str | None = None,
    config: dict[str, Any] | None = None,
    version: str | None = None,
    checkpointed_target: ModelTarget | None = None,
    allow_mid_run_change: bool = False,
    fallback_reason: str | None = None,
    safety_policy_version: str = DEFAULT_SAFETY_POLICY_VERSION,
) -> Any:
    approved = []
    for target in targets:
        if not is_suite_passing(target):
            raise FallbackPolicyError(
                f"{target.provider}/{target.model} is not a suite-passing fallback"
            )
        if (
            checkpointed_target is not None
            and (target.provider, target.model)
            != (checkpointed_target.provider, checkpointed_target.model)
            and not allow_mid_run_change
        ):
            raise FallbackPolicyError(
                "mid-run model change requires a checkpointed auditable fallback"
            )
        approved.append(target)
    if not approved:
        raise AllModelsFailed("no suite-passing model targets configured")

    def record(target: ModelTarget, ordinal: int, outcome: str, exc: Exception | None) -> None:
        reason = fallback_reason
        if ordinal > 1:
            reason = reason or "retryable_provider_failure"
        if exc is not None:
            reason = f"{reason or 'provider_failure'}:{type(exc).__name__}"
        audit.record(
            job_id=job_id,
            target=target,
            outcome=outcome,
            run_id=run_id,
            version=version,
            prompt=prompt,
            config=config,
            fallback_reason=reason,
            safety_policy_version=safety_policy_version,
        )

    result = execute_with_fallback(approved, call, record)
    return result
