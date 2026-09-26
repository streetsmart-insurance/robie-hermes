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

Integrity hardening (H4):

- ``locked_plans`` carries ``idempotency_key`` (the plan's declared
  idempotency, round-tripped on read) and ``plan_hash`` (SHA-256 over the
  canonical ``plan_json`` bytes). ``get_locked_plan`` verifies the hash on
  every read and raises ``LockedPlanTampered`` instead of returning a plan
  whose stored content does not match its hash.
- The write-once property is enforced in the database itself: SQLite
  triggers refuse UPDATE and DELETE on ``locked_plans`` (and on the
  transitions table), so the guarantee holds for raw
  ``store.connect()`` holders too, not just callers of this module.
- ``locked_plan_transitions`` is an append-only audit log: every lock,
  approval decision, grade, and retry claim appends a row. The table has
  no update or delete path (also trigger-guarded).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
from typing import Any

from .evidence import EvidenceSpan, Idempotency, LockedPlan, utc_now_iso


class PlanAlreadyLocked(RuntimeError):
    """A locked plan already exists for this job. The lock is write-once:
    re-locking is refused rather than overwriting."""


class LockedPlanTampered(RuntimeError):
    """A locked plan failed its integrity check on read.

    The stored ``plan_hash`` does not match the stored ``plan_json`` (or no
    hash is stored at all). Raised fail-closed: a plan that cannot prove it
    is the exact plan that was locked is never returned.
    """


def canonical_plan_json(payload: dict[str, Any]) -> str:
    """Canonical serialization of a locked-plan payload.

    Same canonicalization the plan fingerprint uses (sorted keys, compact
    separators): identical payloads always produce identical bytes, so the
    stored ``plan_hash`` is stable and comparable.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def plan_hash_of(canonical_json: str) -> str:
    """SHA-256 hex digest over canonical plan_json bytes."""
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS locked_plans (
            job_id TEXT PRIMARY KEY,
            plan_json TEXT NOT NULL,
            locked_at TEXT NOT NULL,
            locked_by TEXT NOT NULL,
            idempotency_key TEXT,
            plan_hash TEXT
        )
        """
    )
    cols = {row[1] for row in conn.execute("PRAGMA table_info(locked_plans)")}
    if "idempotency_key" not in cols or "plan_hash" not in cols:
        # The migration legitimately UPDATEs pre-H4 rows, so it must run
        # BEFORE the immutability triggers below are created.
        _migrate_locked_plans(conn, cols)
    _ensure_transitions_schema(conn)
    _ensure_immutability_triggers(conn)


def _migrate_locked_plans(conn: sqlite3.Connection, cols: set[str]) -> None:
    """Add the H4 columns to a pre-H4 locked_plans table and backfill them.

    Backfill: ``plan_hash`` is the SHA-256 of the stored ``plan_json``
    bytes exactly as persisted (that is what the read path verifies);
    ``idempotency_key`` comes from the plan's stored idempotency
    declaration, defaulting to NON_IDEMPOTENT (fail closed) when the old
    row carries none. Rows whose plan_json is not parseable get no hash:
    reading them later raises LockedPlanTampered.
    """
    if "idempotency_key" not in cols:
        conn.execute("ALTER TABLE locked_plans ADD COLUMN idempotency_key TEXT")
    if "plan_hash" not in cols:
        conn.execute("ALTER TABLE locked_plans ADD COLUMN plan_hash TEXT")
    for job_id, plan_json in conn.execute(
        "SELECT job_id, plan_json FROM locked_plans"
        " WHERE idempotency_key IS NULL OR plan_hash IS NULL"
    ):
        try:
            payload = json.loads(plan_json)
        except (TypeError, ValueError):
            payload = None
        idempotency_key = Idempotency.NON_IDEMPOTENT.value
        plan_hash: str | None = None
        if isinstance(payload, dict):
            try:
                idempotency_key = Idempotency(str(payload.get("idempotency"))).value
            except ValueError:
                idempotency_key = Idempotency.NON_IDEMPOTENT.value
            # Hash the stored bytes exactly as persisted: the read path
            # verifies sha256(plan_json) over those same bytes.
            plan_hash = hashlib.sha256(plan_json.encode("utf-8")).hexdigest()
        conn.execute(
            "UPDATE locked_plans SET idempotency_key = ?, plan_hash = ?"
            " WHERE job_id = ?",
            (idempotency_key, plan_hash, job_id),
        )


def _ensure_transitions_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS locked_plan_transitions (
            job_id TEXT NOT NULL,
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            transition TEXT NOT NULL,
            at TEXT NOT NULL,
            actor TEXT,
            detail_json TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_locked_plan_transitions_job
        ON locked_plan_transitions(job_id, seq)
        """
    )


def _ensure_immutability_triggers(conn: sqlite3.Connection) -> None:
    """Fail-closed write-once enforcement that binds every connection.

    These triggers live in the database itself, so they refuse UPDATE and
    DELETE for ANY holder of store.connect() -- not just callers of this
    module. Python surfaces the refusal as sqlite3.IntegrityError.
    """
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_locked_plans_no_update
        BEFORE UPDATE ON locked_plans
        BEGIN
            SELECT RAISE(ABORT, 'locked_plans is immutable: UPDATE refused');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_locked_plans_no_delete
        BEFORE DELETE ON locked_plans
        BEGIN
            SELECT RAISE(ABORT, 'locked_plans is immutable: DELETE refused');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_locked_plan_transitions_no_update
        BEFORE UPDATE ON locked_plan_transitions
        BEGIN
            SELECT RAISE(ABORT,
                'locked_plan_transitions is append-only: UPDATE refused');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_locked_plan_transitions_no_delete
        BEFORE DELETE ON locked_plan_transitions
        BEGIN
            SELECT RAISE(ABORT,
                'locked_plan_transitions is append-only: DELETE refused');
        END
        """
    )


def append_transition(
    conn: sqlite3.Connection,
    job_id: str,
    transition: str,
    *,
    actor: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Append one row to the locked-plan transition audit log.

    The transitions table is INSERT-only (triggers refuse UPDATE/DELETE),
    so the log is append-only for every holder of the connection. Does NOT
    commit: the caller owns the transaction, so the audit row commits or
    rolls back atomically with the state change it records.
    """
    _ensure_schema(conn)
    conn.execute(
        "INSERT INTO locked_plan_transitions"
        "(job_id, transition, at, actor, detail_json)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            str(job_id),
            str(transition),
            utc_now_iso(),
            None if actor is None else str(actor),
            None
            if detail is None
            else json.dumps(detail, sort_keys=True, default=str),
        ),
    )


def get_transitions(store: Any, job_id: str) -> list[dict[str, Any]]:
    """Return the audit log for a job, oldest first. Read-only."""
    conn = store.connect()
    try:
        _ensure_schema(conn)
        rows = conn.execute(
            "SELECT seq, transition, at, actor, detail_json"
            " FROM locked_plan_transitions WHERE job_id = ? ORDER BY seq",
            (str(job_id),),
        ).fetchall()
    finally:
        conn.close()
    out: list[dict[str, Any]] = []
    for seq, transition, at, actor, detail_json in rows:
        out.append(
            {
                "seq": seq,
                "transition": transition,
                "at": at,
                "actor": actor,
                "detail": json.loads(detail_json) if detail_json else None,
            }
        )
    return out


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
        # The idempotency declaration lives inside plan_json so plan_hash
        # covers it: the declaration cannot be swapped without breaking
        # the hash. (The idempotency_key column mirrors it for querying.)
        "idempotency": plan.idempotency.value,
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
    raw = row.get("idempotency")
    try:
        idempotency = (
            Idempotency(str(raw)) if raw is not None else Idempotency.NON_IDEMPOTENT
        )
    except ValueError:
        # An unknown stored value fails closed to NON_IDEMPOTENT: no blind
        # retry on a declaration we do not understand.
        idempotency = Idempotency.NON_IDEMPOTENT
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
        idempotency=idempotency,
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

    Raises PlanAlreadyLocked when a plan is already locked for this job.
    """
    payload = _plan_to_row(plan)
    canonical = canonical_plan_json(payload)
    digest = plan_hash_of(canonical)
    _ensure_schema(conn)
    try:
        conn.execute(
            "INSERT INTO locked_plans"
            "(job_id, plan_json, locked_at, locked_by,"
            " idempotency_key, plan_hash)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                plan.job_id,
                canonical,
                plan.locked_at,
                plan.locked_by,
                plan.idempotency.value,
                digest,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise PlanAlreadyLocked(
            f"job {plan.job_id} already has a locked plan; refusing to overwrite"
        ) from exc
    append_transition(
        conn,
        plan.job_id,
        "locked",
        actor=plan.locked_by,
        detail={
            "job_type": plan.job_type,
            "field_count": len(plan.fields),
            "idempotency": plan.idempotency.value,
            "plan_hash": digest,
        },
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

    plan = _normalize_plan(plan, locked_by)
    conn = store.connect()
    try:
        _insert_locked_plan(conn, plan)
        conn.commit()
    finally:
        conn.close()
    return plan


def _verify_plan_hash(job_id: str, plan_json: str, stored_hash: Any) -> None:
    """Fail closed when the stored content does not match its hash."""
    if not stored_hash:
        raise LockedPlanTampered(
            f"locked plan for job {job_id!r} has no stored plan_hash;"
            " refusing to read a plan that cannot prove its integrity"
        )
    if plan_hash_of(plan_json) != str(stored_hash):
        raise LockedPlanTampered(
            f"locked plan for job {job_id!r} failed its integrity check:"
            " stored plan_json does not match plan_hash (the locked content"
            " was modified after locking); refusing to read"
        )


def get_locked_plan(store: Any, job_id: str) -> LockedPlan | None:
    """Return the locked plan for a job, or None if no plan was locked.

    Verifies ``plan_hash`` on every read: a tampered row raises
    ``LockedPlanTampered`` instead of returning a plan.
    """

    conn = store.connect()
    try:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT plan_json, idempotency_key, plan_hash FROM locked_plans"
            " WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    plan_json, idempotency_key, stored_hash = row[0], row[1], row[2]
    _verify_plan_hash(job_id, plan_json, stored_hash)
    payload = json.loads(plan_json)
    if idempotency_key and not payload.get("idempotency"):
        payload["idempotency"] = idempotency_key
    return _plan_from_row(payload)


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
