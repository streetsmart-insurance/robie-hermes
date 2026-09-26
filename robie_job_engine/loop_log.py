"""Persistent run log for the evidence loop.

Every phase of a loop job (extract, confirm, lock, execute, evidence,
grade, retry) appends a row to the ``loop_runs`` table. This is the
durable record of what the loop did and what it saw -- the thing Carlo
reads when a job lands in the human queue and asks "what happened".

Phases are plain strings; the LOOP_PHASES frozenset documents the
expected vocabulary, but ``record_run`` does not reject unknown phases
(a new phase should not silently lose its log row).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping

from .store import JobStore

LOOP_PHASES = frozenset(
    {
        "EXTRACT",  # model turned the request into a draft
        "CONFIRM",  # human confirmation requested / decided
        "LOCK",  # draft -> locked plan (or refused)
        "EXECUTE",  # the worker applied the locked plan
        "EVIDENCE",  # destination re-read + compared
        "GRADE",  # deterministic grade recorded
        "RETRY",  # a retry was scheduled / claimed / completed
    }
)


def _ensure_schema(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS loop_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            loop_job_id TEXT NOT NULL,
            job_type TEXT NOT NULL,
            phase TEXT NOT NULL,
            outcome TEXT NOT NULL,
            detail_json TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_loop_runs_job "
        "ON loop_runs(loop_job_id, id)"
    )


def record_run(
    store: JobStore,
    *,
    loop_job_id: str,
    job_type: str,
    phase: str,
    outcome: str,
    detail: Mapping[str, Any] | None = None,
) -> int:
    """Append one run-log row. Returns the row id."""
    phase = str(phase or "").strip().upper() or "UNKNOWN"
    with store.transaction() as conn:
        _ensure_schema(conn)
        cur = conn.execute(
            """INSERT INTO loop_runs
               (loop_job_id, job_type, phase, outcome, detail_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                str(loop_job_id),
                str(job_type),
                phase,
                str(outcome),
                json.dumps(dict(detail or {}), sort_keys=True, default=str),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        return int(cur.lastrowid)


def runs_for(store: JobStore, loop_job_id: str) -> list[dict[str, Any]]:
    """All log rows for one loop job, oldest first."""
    conn = store.connect()
    try:
        _ensure_schema(conn)
        rows = conn.execute(
            """SELECT id, loop_job_id, job_type, phase, outcome,
                      detail_json, created_at
               FROM loop_runs WHERE loop_job_id=? ORDER BY id""",
            (str(loop_job_id),),
        ).fetchall()
        out = []
        for row in rows:
            record = dict(row)
            try:
                record["detail"] = json.loads(record.pop("detail_json") or "{}")
            except (TypeError, ValueError):
                record["detail"] = {}
            out.append(record)
        return out
    finally:
        conn.close()


def loop_summary(store: JobStore, loop_job_id: str) -> str:
    """Plain-English one-job timeline for the human queue."""
    rows = runs_for(store, loop_job_id)
    if not rows:
        return f"job {loop_job_id}: no loop runs recorded"
    lines = [f"job {loop_job_id} ({rows[0]['job_type']}):"]
    for row in rows:
        lines.append(
            f"  [{row['created_at']}] {row['phase']}: {row['outcome']}"
        )
    return "\n".join(lines)
