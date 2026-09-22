"""Write-once persistence for locked plans.

"Nobody redefines this list mid-job, including the agent doing the work"
only holds if the checklist lives somewhere the executing agent cannot
rewrite. That is this module: ``locked_plans`` is a write-once table --
locking the same job twice raises instead of overwriting. There is no
update path, by design.

This deliberately does NOT use the ``checkpoints`` table: checkpoints
upsert on (job_id, kind), which makes the lock decorative.

At lock time the plan is also bound to the grade spec in force right now:
the SHA-256 of the job type's validated grade spec is pinned into the
locked plan's ``plan_json`` under the reserved key ``"grade_spec_hash"``
(see ``grade_registry``), with the effective extraction model pinned under
``"grade_extraction_model"``. Grading later with that pin refuses to grade
when the spec has moved since lock (fail closed).
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from typing import Any

from .evidence import EvidenceSpan, LockedPlan, utc_now_iso


class PlanAlreadyLocked(RuntimeError):
    """A locked plan already exists for this job. The lock is write-once:
    re-locking is refused rather than overwriting."""


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS locked_plans (
            job_id TEXT PRIMARY KEY,
            plan_json TEXT NOT NULL,
            locked_at TEXT NOT NULL,
            locked_by TEXT NOT NULL
        )
        """
    )


def _plan_to_row(plan: LockedPlan) -> dict[str, Any]:
    row: dict[str, Any] = {
        "job_id": plan.job_id,
        "job_type": plan.job_type,
        "fields": plan.fields,
        "field_tiers": plan.field_tiers,
        "field_notes": plan.field_notes,
        "field_spans": {
            name: span.to_dict()
            for name, span in sorted((plan.field_spans or {}).items())
        },
        "settle_delay_seconds": plan.settle_delay_seconds,
        "locked_at": plan.locked_at,
        "locked_by": plan.locked_by,
    }
    # Grade-spec pin (H3): bind this locked plan to the grade spec in force
    # right now. Both lock paths (lock_plan and confirmations.confirm_and_lock)
    # serialize through this function, so the pin is always taken.
    #
    # The pin and the effective extraction model are RESERVED top-level keys
    # of plan_json -- never inside "fields" -- so they cannot collide with
    # plan data and need no schema migration. Lazy import: grade_registry
    # pulls in PyYAML, which must not become a hard import-time dependency
    # of this module.
    from .grade_registry import (
        GRADE_EXTRACTION_MODEL_KEY,
        GRADE_SPEC_HASH_KEY,
        default_extraction_model,
        validated_spec_hash,
    )

    spec_hash = validated_spec_hash(plan.job_type)
    if spec_hash is None:
        raise ValueError(
            f"cannot lock plan for job type {plan.job_type!r}: the grade "
            "registry has no spec for it; refusing to lock without a spec "
            "pin (fail closed)"
        )
    row[GRADE_SPEC_HASH_KEY] = spec_hash
    row[GRADE_EXTRACTION_MODEL_KEY] = default_extraction_model(plan.job_type)
    return row


def _plan_from_row(row: dict[str, Any]) -> LockedPlan:
    raw_spans = row.get("field_spans") or {}
    if not isinstance(raw_spans, dict):
        raise ValueError("locked plan row has a malformed field_spans payload")
    spans: dict[str, EvidenceSpan] = {}
    for name, raw in raw_spans.items():
        # Fail closed on a tampered row: a span entry that does not parse
        # refuses the read instead of silently dropping provenance.
        spans[str(name)] = EvidenceSpan.from_dict(raw)
    return LockedPlan(
        job_id=str(row.get("job_id") or ""),
        job_type=str(row.get("job_type") or ""),
        fields=dict(row.get("fields") or {}),
        field_tiers=dict(row.get("field_tiers") or {}),
        field_notes=dict(row.get("field_notes") or {}),
        field_spans=spans,
        settle_delay_seconds=int(row.get("settle_delay_seconds") or 0),
        locked_at=str(row.get("locked_at") or ""),
        locked_by=str(row.get("locked_by") or ""),
    )


def _normalize_plan(plan: LockedPlan, locked_by: str) -> LockedPlan:
    """Validate the plan and fill lock metadata defaults."""
    if not str(plan.job_id or "").strip():
        raise ValueError("cannot lock a plan without a job_id")
    if not plan.fields:
        raise ValueError("cannot lock a plan with no fields")
    if not str(plan.locked_by or "").strip():
        plan = dataclasses.replace(plan, locked_by=locked_by)
    if not str(plan.locked_at or "").strip():
        plan = dataclasses.replace(plan, locked_at=utc_now_iso())
    return plan


def _insert_locked_plan(conn: sqlite3.Connection, plan: LockedPlan) -> None:
    """Insert the locked-plan row on an existing connection.

    Does NOT commit: the caller owns the transaction, so the insert can
    commit or roll back atomically with related writes (e.g. the approval
    flip in confirmations.confirm_and_lock).

    Raises PlanAlreadyLocked when a plan is already locked for the job.
    """
    payload = json.dumps(_plan_to_row(plan), sort_keys=True, default=str)
    _ensure_schema(conn)
    try:
        conn.execute(
            "INSERT INTO locked_plans(job_id, plan_json, locked_at, locked_by)"
            " VALUES (?, ?, ?, ?)",
            (plan.job_id, payload, plan.locked_at, plan.locked_by),
        )
    except sqlite3.IntegrityError as exc:
        raise PlanAlreadyLocked(
            f"job {plan.job_id} already has a locked plan; refusing to overwrite"
        ) from exc


def lock_plan(
    store: Any,
    plan: LockedPlan,
    *,
    locked_by: str = "planner",
) -> LockedPlan:
    """Persist a plan as the job's immutable checklist. Write-once.

    Raises:
        PlanAlreadyLocked: a plan is already locked for this job.
        ValueError: the plan has no job_id or no fields.
    """

    plan = _normalize_plan(plan, locked_by)
    conn = store.connect()
    try:
        _insert_locked_plan(conn, plan)
        conn.commit()
    finally:
        conn.close()
    return plan


def get_locked_plan(store: Any, job_id: str) -> LockedPlan | None:
    """Return the locked plan for a job, or None if no plan was locked."""

    conn = store.connect()
    try:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT plan_json FROM locked_plans WHERE job_id = ?", (job_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return _plan_from_row(json.loads(row[0]))


def locked_plan_grade_spec_hash(store: Any, job_id: str) -> str | None:
    """Read the reserved grade-spec pin back from a locked plan's plan_json.

    This is the value to pass as ``expected_spec_hash`` when grading the
    job's evidence: ``grade_registry.grade(job_type, evidence,
    expected_spec_hash=locked_plan_grade_spec_hash(store, job_id))``.

    Returns the pinned hash, or None when the job has no locked plan (or a
    legacy row locked before pinning existed, in which case the caller
    cannot prove the spec is unchanged).
    """
    from .grade_registry import GRADE_SPEC_HASH_KEY

    conn = store.connect()
    try:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT plan_json FROM locked_plans WHERE job_id = ?", (job_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    try:
        payload = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get(GRADE_SPEC_HASH_KEY)
    return str(value) if value else None


def plan_is_locked(store: Any, job_id: str) -> bool:
    return get_locked_plan(store, job_id) is not None
