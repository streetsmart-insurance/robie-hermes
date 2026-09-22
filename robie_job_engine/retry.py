"""Retry scheduling with VERIFY_BEFORE_RETRY and idempotency enforcement.

The evidence loop decides dispositions (see evidence.next_disposition); this
module is the durable machinery behind the retrying ones:

- ``schedule_retry`` persists a retry for a retryable disposition
  (RETRY_ONCE, RETRY_DELAYED, ALERT_AND_RETRY_DELAYED). Non-retryable
  dispositions are rejected -- they go to a human, never to this table.
- ``claim_due`` hands due retries to a worker exactly once (SCHEDULED ->
  CLAIMED inside one transaction).
- ``verify_before_retry`` re-reads the destination BEFORE re-executing. If
  the first attempt actually landed late (evidence read failed, settlement
  lag), the fresh read MATCHES and the caller closes clean instead of
  rewriting. Retries re-run the write only when the destination still
  disagrees.
- ``execute_idempotent`` enforces idempotency keys: the key is claimed
  atomically before the side effect runs, a completed key refuses to run
  again, and an attempt whose outcome is unknown (crash or exception
  mid-flight) can only re-run after a destination readback proves the
  effect is absent.
- ``reconcile_idempotency_key`` resolves a CLAIMED/UNKNOWN key through a
  destination readback instead of guessing from a function return.

Crash-window honesty: the key is CLAIMED atomically BEFORE the
wrapped function runs, so two workers can never both pass the check. A
crash or exception after the claim leaves the key CLAIMED or UNKNOWN --
never silently re-runnable. UNKNOWN means the attempt may have landed at
the destination; re-execution then requires a destination readback
(``reconcile``), which is what VERIFY_BEFORE_RETRY provides.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from .evidence import (
    Disposition,
    EvidenceOutcome,
    EvidenceResult,
    LockedPlan,
    run_evidence_check,
)
from .store import JobStore


class IdempotencyViolation(RuntimeError):
    """An idempotency key already executed; refusing to run again."""


class IdempotencyClaimed(IdempotencyViolation):
    """An idempotency key is CLAIMED or UNKNOWN: an earlier attempt started
    (and may have landed at the destination) but never recorded a clean
    completion. Refusing to re-run blindly; resolve the key through
    ``reconcile_idempotency_key`` (or pass ``reconcile`` to
    ``execute_idempotent``) before any retry.
    """


# Idempotency-claim states. CLAIMED: a worker holds the key right now (or
#   crashed mid-attempt). COMPLETED: the wrapped function returned and the
#   completion was recorded. UNKNOWN: the wrapped function raised -- the
#   external effect may or may not have landed; a function return is not
#   proof of what the destination committed.
CLAIM_STATES = ("CLAIMED", "COMPLETED", "UNKNOWN")

# A CLAIMED row older than this is treated as a crashed worker, not a live
# one, by ``reconcile_idempotency_key``.
DEFAULT_CLAIM_STALE_SECONDS = 3600


RETRYABLE_DISPOSITIONS = frozenset(
    {
        Disposition.RETRY_ONCE,
        Disposition.RETRY_DELAYED,
        Disposition.ALERT_AND_RETRY_DELAYED,
    }
)

# Sensible backoffs when the caller does not name a delay (seconds).
DEFAULT_DELAYS = {
    Disposition.RETRY_ONCE: 300,  # 5 min: transient write failure
    Disposition.RETRY_DELAYED: 3600,  # 1 h: carrier settlement lag
    Disposition.ALERT_AND_RETRY_DELAYED: 1800,  # 30 min: evidence read failed
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _ensure_schema(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS retry_schedule (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            loop_job_id TEXT NOT NULL,
            job_type TEXT NOT NULL,
            disposition TEXT NOT NULL,
            attempt_number INTEGER NOT NULL,
            not_before TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            claimed_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS executed_idempotency_keys (
            idempotency_key TEXT PRIMARY KEY,
            loop_job_id TEXT NOT NULL,
            executed_at TEXT NOT NULL
        )
        """
    )
    # Claim-state columns, added after the table first shipped. Rows from
    # before the claim protocol are known-completed (the old code only
    # recorded a key after fn succeeded), so they migrate as COMPLETED.
    columns = {
        row[1] for row in conn.execute(
            "PRAGMA table_info(executed_idempotency_keys)"
        )
    }
    if "status" not in columns:
        conn.execute(
            "ALTER TABLE executed_idempotency_keys "
            "ADD COLUMN status TEXT NOT NULL DEFAULT 'COMPLETED'"
        )
    if "claimed_at" not in columns:
        conn.execute(
            "ALTER TABLE executed_idempotency_keys ADD COLUMN claimed_at TEXT"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_retry_due "
        "ON retry_schedule(status, not_before)"
    )


def schedule_retry(
    store: JobStore,
    *,
    loop_job_id: str,
    job_type: str,
    disposition: Disposition | str,
    attempt_number: int,
    delay_seconds: int | None = None,
    now: datetime | None = None,
) -> int:
    """Persist a retry. Returns the schedule row id.

    Raises ValueError for a non-retryable disposition -- those go to a
    human, never to this table.
    """
    disp = (
        disposition
        if isinstance(disposition, Disposition)
        else Disposition(str(disposition))
    )
    if disp not in RETRYABLE_DISPOSITIONS:
        raise ValueError(
            f"disposition {disp.value} is not retryable; "
            "route it to a human instead"
        )
    if delay_seconds is None:
        delay_seconds = DEFAULT_DELAYS[disp]
    if delay_seconds < 0:
        raise ValueError("delay_seconds must be >= 0")
    moment = now or _utc_now()
    with store.transaction() as conn:
        _ensure_schema(conn)
        cur = conn.execute(
            """INSERT INTO retry_schedule
               (loop_job_id, job_type, disposition, attempt_number,
                not_before, status, created_at)
               VALUES (?, ?, ?, ?, ?, 'SCHEDULED', ?)""",
            (
                str(loop_job_id),
                str(job_type),
                disp.value,
                int(attempt_number),
                _iso(moment + timedelta(seconds=delay_seconds)),
                _iso(moment),
            ),
        )
        return int(cur.lastrowid)


def claim_due(store: JobStore, now: datetime | None = None) -> list[dict[str, Any]]:
    """Claim due retries exactly once. Returns oldest-first dicts."""
    moment = _iso(now or _utc_now())
    with store.transaction() as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            """SELECT id, loop_job_id, job_type, disposition, attempt_number,
                      not_before, status, created_at
               FROM retry_schedule
               WHERE status='SCHEDULED' AND not_before <= ?
               ORDER BY not_before, id""",
            (moment,),
        ).fetchall()
        claimed = []
        for row in rows:
            conn.execute(
                "UPDATE retry_schedule SET status='CLAIMED', claimed_at=? "
                "WHERE id=? AND status='SCHEDULED'",
                (moment, row["id"]),
            )
            if conn.total_changes > 0:
                claimed.append(dict(row))
        return claimed


def complete_retry(store: JobStore, row_id: int) -> None:
    with store.transaction() as conn:
        _ensure_schema(conn)
        conn.execute(
            "UPDATE retry_schedule SET status='DONE' WHERE id=?", (row_id,)
        )


def cancel_retries(store: JobStore, loop_job_id: str) -> int:
    """Cancel pending retries for a job (human took over / job closed)."""
    with store.transaction() as conn:
        _ensure_schema(conn)
        cur = conn.execute(
            "UPDATE retry_schedule SET status='CANCELLED' "
            "WHERE loop_job_id=? AND status IN ('SCHEDULED','CLAIMED')",
            (str(loop_job_id),),
        )
        return cur.rowcount


def pending_retries(store: JobStore, loop_job_id: str) -> list[dict[str, Any]]:
    conn = store.connect()
    try:
        _ensure_schema(conn)
        rows = conn.execute(
            """SELECT id, loop_job_id, job_type, disposition, attempt_number,
                      not_before, status, created_at
               FROM retry_schedule
               WHERE loop_job_id=? AND status IN ('SCHEDULED','CLAIMED')
               ORDER BY not_before, id""",
            (str(loop_job_id),),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def verify_before_retry(
    plan: LockedPlan,
    fetch: Callable[[], Mapping[str, Any]],
) -> EvidenceResult:
    """Re-read the destination before deciding whether a retry may rewrite.

    Returns the fresh EvidenceResult. The caller MUST close clean on
    MATCHED (the earlier attempt landed; rewriting would double-apply) and
    only re-execute on MISMATCH / NO_EVIDENCE / PENDING_SETTLEMENT.
    """
    return run_evidence_check(plan, fetch)


def _claim_key(store: JobStore, key: str, loop_job_id: str) -> str:
    """Atomically claim ``key``. Returns "FRESH" when the claim was taken,
    otherwise the existing row's status ("CLAIMED" | "COMPLETED" | "UNKNOWN").

    The INSERT is the synchronization point: the PRIMARY KEY constraint plus
    one BEGIN IMMEDIATE transaction mean two concurrent workers can never
    both observe the key as free -- the loser's INSERT fails and it reads
    the winner's CLAIMED row instead of running.
    """
    moment = _iso(_utc_now())
    try:
        with store.transaction() as conn:
            _ensure_schema(conn)
            conn.execute(
                """INSERT INTO executed_idempotency_keys
                   (idempotency_key, loop_job_id, status, executed_at,
                    claimed_at)
                   VALUES (?, ?, 'CLAIMED', ?, ?)""",
                (key, str(loop_job_id), moment, moment),
            )
            return "FRESH"
    except sqlite3.IntegrityError:
        pass
    conn = store.connect()
    try:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT status FROM executed_idempotency_keys "
            "WHERE idempotency_key=?",
            (key,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        # The row vanished between the failed INSERT and the read (a
        # reconcile cleared it). Claim once more rather than guessing.
        return _claim_key(store, key, loop_job_id)
    return str(row["status"])


def _transition_key(
    store: JobStore, key: str, from_status: str, to_status: str
) -> bool:
    """Compare-and-set a claim's state. Returns True when this call moved it."""
    with store.transaction() as conn:
        _ensure_schema(conn)
        cur = conn.execute(
            "UPDATE executed_idempotency_keys SET status=?, executed_at=? "
            "WHERE idempotency_key=? AND status=?",
            (to_status, _iso(_utc_now()), key, from_status),
        )
        return cur.rowcount == 1


def _clear_key(store: JobStore, key: str, from_status: str) -> bool:
    """Remove a claim proven not to have landed. Next execute claims fresh."""
    with store.transaction() as conn:
        _ensure_schema(conn)
        cur = conn.execute(
            "DELETE FROM executed_idempotency_keys "
            "WHERE idempotency_key=? AND status=?",
            (key, from_status),
        )
        return cur.rowcount == 1


def execute_idempotent(
    store: JobStore,
    *,
    idempotency_key: str,
    loop_job_id: str,
    fn: Callable[[], Any],
    reconcile: Callable[[], bool] | None = None,
) -> Any:
    """Run ``fn`` once per idempotency key; never run it twice.

    The key is claimed atomically BEFORE ``fn`` runs:

    - a COMPLETED key raises IdempotencyViolation (as before);
    - a CLAIMED key raises IdempotencyClaimed -- another worker holds it or
      a worker crashed mid-attempt; resolve it with
      ``reconcile_idempotency_key`` instead of re-running blind;
    - an UNKNOWN key means an earlier attempt raised after (possibly)
      landing the external effect. It re-runs only when ``reconcile`` -- a
      destination readback returning True when the effect is already
      visible -- proves the effect absent; reconcile returning True marks
      the key COMPLETED and raises IdempotencyViolation.

    If ``fn`` raises, the key is recorded UNKNOWN (a raised function is not
    proof the destination did not commit) and the exception propagates.
    """
    key = str(idempotency_key or "").strip()
    if not key:
        raise ValueError("execute_idempotent requires an idempotency key")
    status = _claim_key(store, key, loop_job_id)
    if status == "COMPLETED":
        raise IdempotencyViolation(
            f"idempotency key {key!r} already executed; refusing to run again"
        )
    if status == "CLAIMED":
        raise IdempotencyClaimed(
            f"idempotency key {key!r} is already claimed; the earlier "
            "attempt may have landed. Resolve it through "
            "reconcile_idempotency_key before retrying."
        )
    if status == "UNKNOWN":
        if reconcile is None:
            raise IdempotencyClaimed(
                f"idempotency key {key!r} has an unknown outcome; pass a "
                "reconcile readback (e.g. verify_before_retry) before "
                "re-running."
            )
        if reconcile():
            _transition_key(store, key, "UNKNOWN", "COMPLETED")
            raise IdempotencyViolation(
                f"idempotency key {key!r} already landed at the destination "
                "(reconcile matched); refusing to run again"
            )
        if not _transition_key(store, key, "UNKNOWN", "CLAIMED"):
            raise IdempotencyClaimed(
                f"idempotency key {key!r} was claimed by another worker "
                "during reconciliation; refusing to run"
            )
    result: Any = None
    try:
        result = fn()
    except Exception:
        _transition_key(store, key, "CLAIMED", "UNKNOWN")
        raise
    if not _transition_key(store, key, "CLAIMED", "COMPLETED"):
        # Lost the claim mid-flight: another worker is reconciling this key.
        # The side effect already ran; do not let a retry replay it.
        raise IdempotencyClaimed(
            f"idempotency key {key!r} changed state while executing; the "
            "effect may have landed. Reconcile before retrying."
        )
    return result


def reconcile_idempotency_key(
    store: JobStore,
    *,
    idempotency_key: str,
    reconcile: Callable[[], bool],
    stale_after_seconds: int = DEFAULT_CLAIM_STALE_SECONDS,
    now: datetime | None = None,
) -> str:
    """Resolve a CLAIMED/UNKNOWN key by reading the destination.

    ``reconcile`` returns True when the attempt's effect is already visible
    at the destination (build it from ``verify_before_retry``: MATCHED ->
    True). Returns the resolution:

    - "COMPLETED" -- the effect landed; the key is sealed, re-running would
      double-apply;
    - "CLEARED" -- the effect is provably absent; the claim was removed and
      the next ``execute_idempotent`` call claims the key fresh;
    - "CLAIMED" -- a live (non-stale) worker still holds the key; nothing
      was changed;
    - "ABSENT" -- the key was never claimed.

    A CLAIMED key is only reconciled once it is stale (older than
    ``stale_after_seconds``); younger claims belong to a worker that is
    still running.
    """
    key = str(idempotency_key or "").strip()
    if not key:
        raise ValueError("reconcile_idempotency_key requires an idempotency key")
    if stale_after_seconds < 0:
        raise ValueError("stale_after_seconds must be >= 0")
    conn = store.connect()
    try:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT status, claimed_at, executed_at "
            "FROM executed_idempotency_keys WHERE idempotency_key=?",
            (key,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return "ABSENT"
    status = str(row["status"])
    if status == "COMPLETED":
        return "COMPLETED"
    if status == "CLAIMED":
        claimed_at = str(row["claimed_at"] or row["executed_at"] or "")
        try:
            claimed_dt = datetime.fromisoformat(claimed_at)
        except ValueError:
            claimed_dt = None
        moment = now or _utc_now()
        age = (
            (moment - claimed_dt).total_seconds()
            if claimed_dt is not None
            else float("inf")
        )
        if age < stale_after_seconds:
            return "CLAIMED"
    # UNKNOWN, or a stale CLAIMED (crashed worker): the destination decides.
    if reconcile():
        _transition_key(store, key, status, "COMPLETED")
        return "COMPLETED"
    if _clear_key(store, key, status):
        return "CLEARED"
    # Lost the race with another reconciler; report the row's new state.
    conn = store.connect()
    try:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT status FROM executed_idempotency_keys "
            "WHERE idempotency_key=?",
            (key,),
        ).fetchone()
    finally:
        conn.close()
    return str(row["status"]) if row is not None else "ABSENT"


def retry_summary(store: JobStore, loop_job_id: str) -> str:
    rows = pending_retries(store, loop_job_id)
    if not rows:
        return f"job {loop_job_id}: no pending retries"
    lines = [f"job {loop_job_id}: {len(rows)} pending retr(ies):"]
    for row in rows:
        lines.append(
            f"  - {row['disposition']} attempt {row['attempt_number']} "
            f"not before {row['not_before']} [{row['status']}]"
        )
    return "\n".join(lines)


def describe_scheduled(row: Mapping[str, Any]) -> str:
    detail = dict(row)
    return json.dumps(detail, sort_keys=True, default=str)
