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
- ``grade_locked_plan`` -- the H2 enforcement point: loads the LOCKED plan
  via ``plan_lock.get_locked_plan``, derives evidence items from
  ``run_evidence_check``/``compare_plan_to_evidence`` FieldChecks (never
  from caller-supplied ``matched`` flags), enforces the job type's exact
  required field set with provenance on every item, and records the grade.
  A plan missing fields or provenance is UNGRADABLE (passed=False) -- it
  must not grade clean.

This module never drives job STATUS transitions: the existing worker and
independent-verifier flow owns those (including the COMPLETE postcondition
gate). The grade is a verdict layer on top, not a bypass around it.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable, Mapping

from . import grade_registry, plan_lock
from .evidence import (
    EvidenceOutcome,
    EvidenceResult,
    LockedPlan,
    compare_plan_to_evidence,
    run_evidence_check,
    utc_now_iso,
)
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


# ---------------------------------------------------------------------------
# H2 enforcement: grade completeness and provenance from the immutable
# locked plan + authoritative fetch output.
#
# Provenance contract (merge-order independent: does NOT assume H1's span
# schema): an evidence item has provenance iff it carries a non-empty
# ``provenance`` mapping (a fetch citation) OR a ``span`` object. Items
# without either are UNGRADABLE.
# ---------------------------------------------------------------------------


def _has_provenance(item: Mapping[str, Any]) -> bool:
    """True when the item cites where its value came from.

    Accepts a ``provenance`` mapping (the fetch citation attached by
    ``evidence_items_from_checks`` or by a job-type fetcher) or a ``span``
    object (the H1 evidence-span shape, when it lands) -- whichever is
    present. Never assumes one schema.
    """
    provenance = item.get("provenance")
    if isinstance(provenance, Mapping) and len(provenance) > 0:
        return True
    return bool(item.get("span"))


def _provenance_for_field(plan: LockedPlan, field_name: str, captured_at: str) -> dict[str, Any]:
    """Build the fetch citation for one derived field-match item.

    The citation names the authoritative fetch the FieldCheck came from:
    tier "A" fields are read back through the EZLynx policy API, tier "B"
    fields through a fresh destination page read.
    """
    tier = str((plan.field_tiers or {}).get(field_name, "B")).upper()
    if tier == "A":
        method, source = "policy_api_readback", "ezlynx_policy_api"
    else:
        method, source = "page_readback", "destination_page"
    return {
        "fetch": "compare_plan_to_evidence",
        "method": method,
        "source": source,
        "captured_at": captured_at,
    }


def evidence_items_from_checks(
    result: EvidenceResult, plan: LockedPlan
) -> list[dict[str, Any]]:
    """Derive grade evidence items from an evidence run's FieldChecks.

    The ``matched`` flags come from ``compare_plan_to_evidence`` /
    ``run_evidence_check`` against the locked plan -- never from
    caller-supplied booleans. Every item carries a ``provenance`` fetch
    citation so the enforcement gate can verify where each value came
    from.
    """
    items: list[dict[str, Any]] = []
    for check in result.checks or ():
        items.append(
            {
                "kind": "field_match",
                "field": check.field,
                "matched": bool(check.matched),
                "expected": check.expected,
                "observed": check.actual,
                "check_note": check.note,
                "pending_settle": bool(check.pending_settle),
                "provenance": _provenance_for_field(
                    plan, check.field, result.captured_at
                ),
            }
        )
    return items


def _summarize_items(items: list[dict[str, Any]]) -> dict[str, Any]:
    by_kind: dict[str, int] = {}
    for item in items:
        kind = str(item.get("kind") or "unknown")
        by_kind[kind] = by_kind.get(kind, 0) + 1
    return {"total_items": len(items), "by_kind": by_kind}


def _ungradable(
    job_type: str, reasons: list[str], items: list[dict[str, Any]]
) -> grade_registry.GradeResult:
    """Build the UNGRADABLE verdict: could not assess, must not grade clean."""
    name = str(job_type or "").strip() or "unknown"
    spec = grade_registry.spec_for(name)
    label = str((spec or {}).get("label") or name)
    return grade_registry.GradeResult(
        job_type=name,
        label=label,
        grade="UNGRADABLE",
        passed=False,
        reasons=reasons,
        evidence_summary=_summarize_items(items),
    )


def _group_field_items(
    items: list[dict[str, Any]], kind: str
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        if item.get("kind") != kind:
            continue
        grouped.setdefault(str(item.get("field") or ""), []).append(item)
    return grouped


def _enforce_plan_fields(
    plan: LockedPlan, items: list[dict[str, Any]], problems: list[str]
) -> None:
    """mode "plan": every locked field evidenced with provenance; nothing extra."""
    required = list((plan.fields or {}).keys())
    by_field = _group_field_items(items, "field_match")
    for field_name in required:
        got = by_field.get(field_name, [])
        if not got:
            problems.append(
                f"no evidence for locked field {field_name!r}; "
                "refusing to grade an incomplete check"
            )
        elif len(got) > 1:
            problems.append(
                f"evidence for locked field {field_name!r} appears "
                f"{len(got)} times; refusing to grade ambiguous evidence"
            )
        elif not _has_provenance(got[0]):
            problems.append(
                f"evidence for locked field {field_name!r} has no "
                "provenance (span or fetch citation); refusing to grade"
            )
    for field_name in sorted(set(by_field) - set(required)):
        problems.append(
            f"evidence for {field_name!r} is not in the locked plan; "
            "refusing to grade"
        )


def _enforce_document_and_plan(
    plan: LockedPlan, items: list[dict[str, Any]], problems: list[str]
) -> None:
    """mode "document_and_plan": quote document present + every requested value."""
    docs = [i for i in items if i.get("kind") == "quote_document"]
    if not any(i.get("present") and _has_provenance(i) for i in docs):
        problems.append(
            "no quote document retrieved from the carrier site with "
            "provenance; refusing to grade"
        )
    required = list((plan.fields or {}).keys())
    by_field = _group_field_items(items, "quoted_value_match")
    for field_name in required:
        got = by_field.get(field_name, [])
        if not got:
            problems.append(
                f"no quoted value for requested field {field_name!r}; "
                "refusing to grade an incomplete check"
            )
        elif len(got) > 1:
            problems.append(
                f"quoted value for {field_name!r} appears {len(got)} times; "
                "refusing to grade ambiguous evidence"
            )
        elif not _has_provenance(got[0]):
            problems.append(
                f"quoted value for {field_name!r} has no provenance "
                "(span or fetch citation); refusing to grade"
            )
    for field_name in sorted(set(by_field) - set(required)):
        problems.append(
            f"quoted value for {field_name!r} was not requested in the "
            "locked plan; refusing to grade"
        )


def _enforce_call_record(
    items: list[dict[str, Any]], problems: list[str]
) -> None:
    """mode "call_record": a completed record with disposition and provenance."""
    calls = [i for i in items if i.get("kind") == "call_record"]
    good = [
        c
        for c in calls
        if c.get("completed")
        and str(c.get("disposition") or "").strip()
        and _has_provenance(c)
    ]
    if not good:
        problems.append(
            "no completed carrier call with a captured disposition and "
            "provenance; refusing to grade"
        )


def grade_derived_evidence(
    job_type: str,
    plan: LockedPlan,
    items: list[Mapping[str, Any]] | None,
) -> grade_registry.GradeResult:
    """Enforce the required field set, then grade.

    ``items`` must have been derived from FieldChecks (see
    ``evidence_items_from_checks``) -- the ``matched`` flags are re-checked
    here against the locked plan's exact field set, and every item must
    carry provenance. Returns UNGRADABLE (passed=False) when a required
    field has no evidence, evidence lacks provenance, or evidence covers
    fields outside the locked plan. Otherwise delegates to
    ``grade_registry.grade`` for the PASS / NEEDS_REVIEW verdict, keeping
    that function's existing behavior.
    """
    name = str(job_type or "").strip()
    spec = grade_registry.spec_for(name)
    normalized = [dict(i) for i in (items or [])]
    if spec is None:
        # Unknown job types fail closed in the registry itself.
        return grade_registry.grade(name, normalized)
    mode = grade_registry.required_fields_for(name) or "plan"
    problems: list[str] = []
    if mode == "plan":
        _enforce_plan_fields(plan, normalized, problems)
    elif mode == "document_and_plan":
        _enforce_document_and_plan(plan, normalized, problems)
    elif mode == "call_record":
        _enforce_call_record(normalized, problems)
    else:  # defensive: _validate_spec rejects unknown modes
        problems.append(
            f"unknown required_fields mode {mode!r}; refusing to grade"
        )
    if problems:
        return _ungradable(name, problems, normalized)
    return grade_registry.grade(name, normalized)


def _plan_matches_locked(plan: LockedPlan, locked: LockedPlan) -> bool:
    """The caller's plan must be the locked plan: same job, type, and fields."""
    if plan is None:
        return False
    if str(plan.job_id or "") != str(locked.job_id or ""):
        return False
    if str(plan.job_type or "") != str(locked.job_type or ""):
        return False
    left = json.dumps(plan.fields or {}, sort_keys=True, default=str)
    right = json.dumps(locked.fields or {}, sort_keys=True, default=str)
    return left == right


def grade_locked_plan(
    store: JobStore,
    loop_job_id: str,
    plan: LockedPlan | None,
    observed: Mapping[str, Any] | Callable[[], Mapping[str, Any]] | None,
) -> grade_registry.GradeResult:
    """Grade a job from its immutable locked plan + authoritative fetch output.

    The missing enforcement point for evidence-loop grading:

    1. Loads the LOCKED plan via ``plan_lock.get_locked_plan`` -- the
       grader never trusts the caller's plan object. No locked plan, or a
       caller plan that diverges from the lock, is UNGRADABLE.
    2. Runs the authoritative fetch -- ``observed`` may be the fetch
       output mapping, a zero-argument fetch callable (run through
       ``run_evidence_check`` so fetch failures become NO_EVIDENCE), or
       None (no usable fetch output). NO_EVIDENCE is UNGRADABLE: a fetch
       failure must not grade clean.
    3. Derives evidence items from the run's FieldChecks via
       ``evidence_items_from_checks`` -- never from caller-supplied
       ``matched`` flags.
    4. Enforces the job type's exact required field set with provenance on
       every item via ``grade_derived_evidence``.
    5. Persists the verdict with ``record_grade`` on the loop job's board
       row and returns it.

    Verdicts: PASS (all required checks evidenced and matched),
    NEEDS_REVIEW (assessed; something did not match), UNGRADABLE
    (could not assess; always passed=False).
    """
    loop_job_id = str(loop_job_id or "").strip()
    if not loop_job_id:
        raise ValueError("grade_locked_plan requires a loop_job_id")

    locked = plan_lock.get_locked_plan(store, loop_job_id)
    if locked is None:
        return _ungradable(
            getattr(plan, "job_type", "") or "unknown",
            [f"no locked plan for loop job {loop_job_id!r}; refusing to grade"],
            [],
        )
    if plan is not None and not _plan_matches_locked(plan, locked):
        result = _ungradable(
            locked.job_type,
            [
                "the plan handed to the grader does not match the locked "
                "plan for this job; refusing to grade"
            ],
            [],
        )
        _persist_grade(store, loop_job_id, locked, result)
        return result

    if callable(observed):
        evidence_result = run_evidence_check(locked, observed)
    elif isinstance(observed, Mapping):
        evidence_result = compare_plan_to_evidence(locked, observed)
    else:
        evidence_result = EvidenceResult(
            outcome=EvidenceOutcome.NO_EVIDENCE,
            checks=(),
            detail="no usable fetch output was provided; refusing to grade",
            captured_at=utc_now_iso(),
            plan_job_id=locked.job_id,
        )
    if evidence_result.outcome is EvidenceOutcome.NO_EVIDENCE:
        result = _ungradable(
            locked.job_type,
            [
                "evidence fetch produced no usable output "
                f"({evidence_result.detail}); refusing to grade clean"
            ],
            [],
        )
        _persist_grade(store, loop_job_id, locked, result)
        return result

    items = evidence_items_from_checks(evidence_result, locked)
    result = grade_derived_evidence(locked.job_type, locked, items)
    _persist_grade(store, loop_job_id, locked, result)
    return result


def _persist_grade(
    store: JobStore,
    loop_job_id: str,
    locked: LockedPlan,
    result: grade_registry.GradeResult,
) -> None:
    """Record the verdict on the loop job's board row (idempotent per loop job)."""
    board_job = store.create_job(
        action_type=locked.job_type,
        payload={
            "loop_job_id": loop_job_id,
            "job_type": locked.job_type,
            "draft_summary": f"graded from locked plan ({result.grade})",
        },
        idempotency_key=f"evidence-loop:{loop_job_id}",
    )
    record_grade(store, str(board_job["id"]), loop_job_id=loop_job_id, result=result)


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
