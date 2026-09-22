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
- ``execute_idempotent`` enforces idempotency keys: a key that already
  executed refuses to run again (fail closed against double-apply).

Crash-window honesty: the key is recorded after the wrapped function
succeeds. If the process dies between the destination write and the key
record, a later retry could double-apply -- VERIFY_BEFORE_RETRY is the
mitigation (the re-read sees the landed write and skips the rewrite).
"""

from __future__ import annotations

import json
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


def execute_idempotent(
    store: JobStore,
    *,
    idempotency_key: str,
    loop_job_id: str,
    fn: Callable[[], Any],
) -> Any:
    """Run ``fn`` once per idempotency key; refuse a second run.

    Raises IdempotencyViolation if the key already executed. The key is
    recorded after ``fn`` succeeds; if ``fn`` raises, nothing is recorded
    and the evidence-loop disposition logic decides what happens next.
    """
    key = str(idempotency_key or "").strip()
    if not key:
        raise ValueError("execute_idempotent requires an idempotency key")
    conn = store.connect()
    try:
        _ensure_schema(conn)
        hit = conn.execute(
            "SELECT loop_job_id FROM executed_idempotency_keys WHERE "
            "idempotency_key=?",
            (key,),
        ).fetchone()
    finally:
        conn.close()
    if hit:
        raise IdempotencyViolation(
            f"idempotency key {key!r} already executed "
            f"(job {hit['loop_job_id']}); refusing to run again"
        )
    result = fn()
    with store.transaction() as conn:
        _ensure_schema(conn)
        conn.execute(
            """INSERT OR IGNORE INTO executed_idempotency_keys
               (idempotency_key, loop_job_id, executed_at)
               VALUES (?, ?, ?)""",
            (key, str(loop_job_id), _iso(_utc_now())),
        )
    return result


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
