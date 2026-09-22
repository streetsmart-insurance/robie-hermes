"""Evidence-loop jobs on the job engine board.

The board is the Operations Control Center sheet: its Jobs tab is fed by
``operations.dashboard_rows()`` and its Evidence tab by the
``verification_evidence`` table. This module puts evidence-loop work there:

- ``record_draft``   -- a structured draft becomes a board job row
  (action_type = the job type, so the board groups by kind of work);
- ``record_evidence`` -- evidence items become ``verification_evidence``
  rows, which the Evidence tab already syncs;
- ``record_grade``    -- the deterministic grade becomes a ``job_grades``
  row, which the Jobs tab surfaces as the job's verification status.

This module never drives job STATUS transitions: the existing worker and
independent-verifier flow owns those (including the COMPLETE postcondition
gate). The grade is a verdict layer on top, not a bypass around it.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

from . import grade_registry
from .models import VerificationEvidence
from .store import JobStore, utc_now


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS job_grades (
            job_id TEXT PRIMARY KEY,
            loop_job_id TEXT NOT NULL,
            job_type TEXT NOT NULL,
            grade TEXT NOT NULL,
            passed INTEGER NOT NULL,
            reason TEXT NOT NULL,
            evidence_summary_json TEXT NOT NULL,
            graded_at TEXT NOT NULL
        )
        """
    )


def _conn_for(store: JobStore) -> sqlite3.Connection:
    conn = store.connect()
    _ensure_schema(conn)
    return conn


def record_draft(
    store: JobStore,
    *,
    loop_job_id: str,
    job_type: str,
    draft_summary: str,
    payload: Mapping[str, Any] | None = None,
) -> str:
    """Put a structured draft on the board. Idempotent per loop job.

    Raises ValueError for a job type the grade registry does not know --
    ungradable work does not get a board row.
    """
    loop_job_id = str(loop_job_id or "").strip()
    if not loop_job_id:
        raise ValueError("record_draft requires a loop_job_id")
    spec = grade_registry.spec_for(job_type)
    if spec is None:
        raise ValueError(
            f"unknown job type {job_type!r}: not in the grade registry; "
            "refusing to board ungradable work"
        )
    body = {
        "loop_job_id": loop_job_id,
        "job_type": job_type,
        "draft_summary": str(draft_summary or ""),
    }
    if payload:
        body.update(dict(payload))
    job = store.create_job(
        action_type=job_type,
        payload=body,
        idempotency_key=f"evidence-loop:{loop_job_id}",
    )
    return str(job["id"])


# Evidence kind -> (method, source) for the verification_evidence table.
_KIND_METHOD_SOURCE = {
    "field_match": ("policy_api_readback", "ezlynx_policy_api"),
    "quote_document": ("document_retrieval", "carrier_website"),
    "quoted_value_match": ("page_readback", "carrier_website"),
    "call_record": ("call_record", "carrier_call"),
}


def _item_verified(item: Mapping[str, Any]) -> bool:
    kind = str(item.get("kind") or "")
    if kind == "field_match":
        return bool(item.get("matched"))
    if kind == "quote_document":
        return bool(item.get("present"))
    if kind == "quoted_value_match":
        return bool(item.get("matched"))
    if kind == "call_record":
        return bool(item.get("completed")) and bool(
            str(item.get("disposition") or "").strip()
        )
    return False


def record_evidence(
    store: JobStore,
    job_id: str,
    items: list[Mapping[str, Any]],
) -> int:
    """Write evidence items as verification_evidence rows (Evidence tab)."""
    count = 0
    for raw in items or []:
        item = dict(raw)
        kind = str(item.get("kind") or "unknown")
        method, source = _KIND_METHOD_SOURCE.get(kind, ("manual_check", "robie"))
        expected = {
            k: v for k, v in item.items() if k not in ("observed", "kind")
        }
        raw_observed = item.get("observed")
        if isinstance(raw_observed, Mapping):
            observed: dict[str, Any] = dict(raw_observed)
        elif raw_observed is None:
            observed = {}
        else:
            observed = {"value": raw_observed}
        store.add_evidence(
            job_id,
            _item_verified(item),
            VerificationEvidence(
                method=method,
                source=source,
                expected=expected,
                observed=observed,
                authoritative=True,
                captured_at=utc_now(),
                locator=str(
                    item.get("policy_number")
                    or item.get("document_ref")
                    or item.get("notes_ref")
                    or job_id
                ),
            ),
        )
        count += 1
    return count


def record_grade(
    store: JobStore,
    job_id: str,
    *,
    loop_job_id: str,
    result: grade_registry.GradeResult,
) -> grade_registry.GradeResult:
    """Persist the deterministic grade. Upsert: the board shows the latest."""
    conn = _conn_for(store)
    try:
        conn.execute(
            """INSERT INTO job_grades
               (job_id, loop_job_id, job_type, grade, passed, reason,
                evidence_summary_json, graded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(job_id) DO UPDATE SET
                 loop_job_id=excluded.loop_job_id,
                 job_type=excluded.job_type,
                 grade=excluded.grade,
                 passed=excluded.passed,
                 reason=excluded.reason,
                 evidence_summary_json=excluded.evidence_summary_json,
                 graded_at=excluded.graded_at""",
            (
                job_id,
                str(loop_job_id or ""),
                result.job_type,
                result.grade,
                int(result.passed),
                "; ".join(result.reasons)[:2000],
                json.dumps(result.evidence_summary, sort_keys=True, default=str),
                utc_now(),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return result


def grade_label_for_conn(conn: sqlite3.Connection, job_id: str) -> str | None:
    """Board label for a graded job using an open connection.

    Returns None when the job has no grade yet.
    """
    _ensure_schema(conn)
    row = conn.execute(
        "SELECT job_type, grade, passed, reason FROM job_grades WHERE job_id=?",
        (job_id,),
    ).fetchone()
    if not row:
        return None
    job_type, _grade, passed, reason = row
    spec = grade_registry.spec_for(str(job_type))
    label = str((spec or {}).get("label") or job_type)
    verdict = "PASS" if passed else "NEEDS REVIEW"
    detail = str(reason or "")[:160]
    return f"{verdict} - {label}: {detail}" if detail else f"{verdict} - {label}"


def grade_label_for(db_path: str, job_id: str) -> str | None:
    """Board label for a graded job, e.g. 'PASS - Policy change: all 3 fields matched'.

    Returns None when the job has no grade yet.
    """
    conn = sqlite3.connect(db_path)
    try:
        return grade_label_for_conn(conn, job_id)
    finally:
        conn.close()
