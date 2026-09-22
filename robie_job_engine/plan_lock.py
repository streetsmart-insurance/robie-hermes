"""Write-once persistence for locked plans.

"Nobody redefines this list mid-job, including the agent doing the work"
only holds if the checklist lives somewhere the executing agent cannot
rewrite. That is this module: ``locked_plans`` is a write-once table --
locking the same job twice raises instead of overwriting. There is no
update path, by design.

This deliberately does NOT use the ``checkpoints`` table: checkpoints
upsert on (job_id, kind), which makes the lock decorative.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from typing import Any

from .evidence import LockedPlan, utc_now_iso


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
    return {
        "job_id": plan.job_id,
        "job_type": plan.job_type,
        "fields": plan.fields,
        "field_tiers": plan.field_tiers,
        "field_notes": plan.field_notes,
        "settle_delay_seconds": plan.settle_delay_seconds,
        "locked_at": plan.locked_at,
        "locked_by": plan.locked_by,
    }


def _plan_from_row(row: dict[str, Any]) -> LockedPlan:
    return LockedPlan(
        job_id=str(row.get("job_id") or ""),
        job_type=str(row.get("job_type") or ""),
        fields=dict(row.get("fields") or {}),
        field_tiers=dict(row.get("field_tiers") or {}),
        field_notes=dict(row.get("field_notes") or {}),
        settle_delay_seconds=int(row.get("settle_delay_seconds") or 0),
        locked_at=str(row.get("locked_at") or ""),
        locked_by=str(row.get("locked_by") or ""),
    )


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

    if not str(plan.job_id or "").strip():
        raise ValueError("cannot lock a plan without a job_id")
    if not plan.fields:
        raise ValueError("cannot lock a plan with no fields")
    if not str(plan.locked_by or "").strip():
        plan = dataclasses.replace(plan, locked_by=locked_by)
    if not str(plan.locked_at or "").strip():
        plan = dataclasses.replace(plan, locked_at=utc_now_iso())

    payload = json.dumps(_plan_to_row(plan), sort_keys=True, default=str)
    conn = store.connect()
    try:
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


def plan_is_locked(store: Any, job_id: str) -> bool:
    return get_locked_plan(store, job_id) is not None
