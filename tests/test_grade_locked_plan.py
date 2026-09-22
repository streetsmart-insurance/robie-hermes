"""Tests for H2: grade completeness and provenance from the immutable plan.

The enforcement point is ``board.grade_locked_plan``: it loads the LOCKED
plan via ``plan_lock.get_locked_plan``, derives evidence items from
``compare_plan_to_evidence``/``run_evidence_check`` FieldChecks (never from
caller-supplied ``matched`` flags), enforces the job type's exact required
field set, and requires provenance (span object OR fetch citation) on every
item. Anything missing or provenance-less is UNGRADABLE (passed=False) --
it must not grade clean.

Every test in this file FAILS on the pre-fix tree (the functions do not
exist) and PASSES with the fix.
"""

from __future__ import annotations

import sqlite3

import pytest

from robie_job_engine import board, grade_registry, plan_lock
from robie_job_engine.evidence import (
    EvidenceUnavailable,
    LockedPlan,
    compare_plan_to_evidence,
)
from robie_job_engine.store import JobStore


def _plan(**overrides):
    base = {
        "job_id": "loop-100",
        "job_type": "policy_change",
        "fields": {"writtenPremium": 1284.00, "effectiveDate": "2026-10-01"},
        "field_tiers": {"writtenPremium": "A", "effectiveDate": "A"},
        "settle_delay_seconds": 0,
        "locked_at": "2026-09-21T20:00:00+00:00",
        "locked_by": "planner",
    }
    base.update(overrides)
    return LockedPlan(**base)


def _store(tmp_path):
    return JobStore(str(tmp_path / "jobs.db"))


def _lock(store, plan):
    return plan_lock.lock_plan(store, plan)


def _derived_items(plan, observed):
    result = compare_plan_to_evidence(plan, observed)
    return board.evidence_items_from_checks(result, plan)


# ---------------------------------------------------------------------------
# 1. A subset of the locked fields must not grade clean
# ---------------------------------------------------------------------------

def test_subset_of_locked_fields_does_not_grade_clean(tmp_path):
    store = _store(tmp_path)
    plan = _plan()
    _lock(store, plan)
    # Fetch output covers only one of the two locked fields.
    result = board.grade_locked_plan(
        store, "loop-100", plan, {"writtenPremium": 1284.00}
    )
    assert result.passed is False
    assert result.grade in ("NEEDS_REVIEW", "UNGRADABLE")


def test_legacy_grade_trusts_caller_but_enforcement_does_not(tmp_path):
    """The old hole: hand-rolled all-matched subset grades PASS via
    ``registry.grade``. The enforcement path must refuse the same input."""
    store = _store(tmp_path)
    plan = _plan()
    _lock(store, plan)
    hand_rolled = [
        {
            "kind": "field_match",
            "field": "writtenPremium",
            "matched": True,
            "expected": 1284.00,
            "observed": 1284.00,
        }
    ]
    legacy = grade_registry.grade("policy_change", hand_rolled)
    assert legacy.passed is True  # the hole, documented
    enforced = board.grade_derived_evidence("policy_change", plan, hand_rolled)
    assert enforced.passed is False
    assert enforced.grade == "UNGRADABLE"


# ---------------------------------------------------------------------------
# 2. Evidence for a field outside the locked plan is rejected
# ---------------------------------------------------------------------------

def test_evidence_for_unknown_field_rejected(tmp_path):
    store = _store(tmp_path)
    plan = _plan()
    _lock(store, plan)
    items = _derived_items(plan, {"writtenPremium": 1284.00,
                                 "effectiveDate": "2026-10-01"})
    items.append(
        {
            "kind": "field_match",
            "field": "intruderField",
            "matched": True,
            "expected": "x",
            "observed": "x",
            "provenance": {"fetch": "compare_plan_to_evidence",
                           "source": "ezlynx_policy_api"},
        }
    )
    result = board.grade_derived_evidence("policy_change", plan, items)
    assert result.passed is False
    assert result.grade == "UNGRADABLE"
    assert any("intruderField" in r for r in result.reasons)


# ---------------------------------------------------------------------------
# 3. Provenance is required on every evidence item
# ---------------------------------------------------------------------------

def test_item_without_provenance_is_ungradable(tmp_path):
    store = _store(tmp_path)
    plan = _plan()
    _lock(store, plan)
    items = _derived_items(plan, {"writtenPremium": 1284.00,
                                 "effectiveDate": "2026-10-01"})
    assert all(board._has_provenance(i) for i in items)
    del items[0]["provenance"]  # strip the fetch citation
    result = board.grade_derived_evidence("policy_change", plan, items)
    assert result.passed is False
    assert result.grade == "UNGRADABLE"
    assert any("provenance" in r for r in result.reasons)


def test_span_counts_as_provenance(tmp_path):
    """Merge-order independence: an H1 span object also satisfies the
    provenance gate."""
    store = _store(tmp_path)
    plan = _plan()
    _lock(store, plan)
    items = _derived_items(plan, {"writtenPremium": 1284.00,
                                 "effectiveDate": "2026-10-01"})
    for item in items:
        del item["provenance"]
        item["span"] = {"source_id": "change_request_text",
                        "offset_start": 0, "offset_end": 4}
    result = board.grade_derived_evidence("policy_change", plan, items)
    assert result.passed is True
    assert result.grade == "PASS"


# ---------------------------------------------------------------------------
# 4. Fetch failure (NO_EVIDENCE) never grades clean
# ---------------------------------------------------------------------------

def test_fetch_failure_never_grades_clean(tmp_path):
    store = _store(tmp_path)
    plan = _plan()
    _lock(store, plan)

    def _down():
        raise EvidenceUnavailable("policy api unreachable")

    result = board.grade_locked_plan(store, "loop-100", plan, _down)
    assert result.passed is False
    assert result.grade == "UNGRADABLE"
    assert result.grade != "PASS"


def test_missing_fetch_output_never_grades_clean(tmp_path):
    store = _store(tmp_path)
    plan = _plan()
    _lock(store, plan)
    result = board.grade_locked_plan(store, "loop-100", plan, None)
    assert result.passed is False
    assert result.grade == "UNGRADABLE"


# ---------------------------------------------------------------------------
# The locked plan is authoritative, not the caller's copy
# ---------------------------------------------------------------------------

def test_no_locked_plan_is_ungradable(tmp_path):
    store = _store(tmp_path)
    result = board.grade_locked_plan(
        store, "loop-404", _plan(job_id="loop-404"),
        {"writtenPremium": 1284.00},
    )
    assert result.passed is False
    assert result.grade == "UNGRADABLE"


def test_caller_plan_mismatch_against_lock_is_ungradable(tmp_path):
    store = _store(tmp_path)
    locked = _plan()
    _lock(store, locked)
    tampered = _plan(fields={"writtenPremium": 999.00,
                             "effectiveDate": "2026-10-01"})
    result = board.grade_locked_plan(
        store, "loop-100", tampered,
        {"writtenPremium": 1284.00, "effectiveDate": "2026-10-01"},
    )
    assert result.passed is False
    assert result.grade == "UNGRADABLE"
    assert any("locked plan" in r for r in result.reasons)


# ---------------------------------------------------------------------------
# Positive controls
# ---------------------------------------------------------------------------

def test_full_match_grades_pass_and_persists(tmp_path):
    store = _store(tmp_path)
    plan = _plan()
    _lock(store, plan)
    result = board.grade_locked_plan(
        store, "loop-100", plan,
        {"writtenPremium": 1284.00, "effectiveDate": "2026-10-01"},
    )
    assert result.passed is True
    assert result.grade == "PASS"
    conn = sqlite3.connect(str(tmp_path / "jobs.db"))
    try:
        row = conn.execute(
            "SELECT grade, passed FROM job_grades"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == "PASS" and row[1] == 1


def test_mismatch_grades_needs_review_not_ungradable(tmp_path):
    """An assessable negative is NEEDS_REVIEW; UNGRADABLE is reserved for
    'could not assess'."""
    store = _store(tmp_path)
    plan = _plan()
    _lock(store, plan)
    result = board.grade_locked_plan(
        store, "loop-100", plan,
        {"writtenPremium": 999.00, "effectiveDate": "2026-10-01"},
    )
    assert result.passed is False
    assert result.grade == "NEEDS_REVIEW"


# ---------------------------------------------------------------------------
# Per-job-type required field sets
# ---------------------------------------------------------------------------

def _quote_plan(**overrides):
    base = {
        "job_id": "loop-200",
        "job_type": "carrier_quote",
        "fields": {"premium": 1240.00, "coverage_a": 250000},
        "field_tiers": {"premium": "B", "coverage_a": "B"},
        "settle_delay_seconds": 0,
        "locked_at": "2026-09-21T20:00:00+00:00",
        "locked_by": "planner",
    }
    base.update(overrides)
    return LockedPlan(**base)


def _prov():
    return {"fetch": "carrier_website", "source": "carrier_website"}


def test_carrier_quote_requires_document_with_provenance():
    plan = _quote_plan()
    values_only = [
        {"kind": "quoted_value_match", "field": "premium", "matched": True,
         "provenance": _prov()},
        {"kind": "quoted_value_match", "field": "coverage_a", "matched": True,
         "provenance": _prov()},
    ]
    result = board.grade_derived_evidence("carrier_quote", plan, values_only)
    assert result.passed is False
    assert result.grade == "UNGRADABLE"

    complete = values_only + [
        {"kind": "quote_document", "present": True,
         "document_ref": "doc-1", "provenance": _prov()},
    ]
    result = board.grade_derived_evidence("carrier_quote", plan, complete)
    assert result.passed is True
    assert result.grade == "PASS"


def test_carrier_quote_requires_every_requested_value():
    plan = _quote_plan()
    items = [
        {"kind": "quote_document", "present": True,
         "document_ref": "doc-1", "provenance": _prov()},
        {"kind": "quoted_value_match", "field": "premium", "matched": True,
         "provenance": _prov()},
        # coverage_a missing from the evidence
    ]
    result = board.grade_derived_evidence("carrier_quote", plan, items)
    assert result.passed is False
    assert result.grade == "UNGRADABLE"


def _call_plan(**overrides):
    base = {
        "job_id": "loop-300",
        "job_type": "carrier_call",
        "fields": {"account": "ACME-1"},
        "field_tiers": {},
        "settle_delay_seconds": 0,
        "locked_at": "2026-09-21T20:00:00+00:00",
        "locked_by": "planner",
    }
    base.update(overrides)
    return LockedPlan(**base)


def test_carrier_call_requires_completed_record_with_disposition_and_provenance():
    plan = _call_plan()
    no_provenance = [
        {"kind": "call_record", "completed": True,
         "disposition": "quoted 1240", "recording_ref": "rec-1"},
    ]
    result = board.grade_derived_evidence("carrier_call", plan, no_provenance)
    assert result.passed is False
    assert result.grade == "UNGRADABLE"

    no_disposition = [
        {"kind": "call_record", "completed": True, "disposition": "",
         "provenance": _prov()},
    ]
    result = board.grade_derived_evidence("carrier_call", plan, no_disposition)
    assert result.passed is False
    assert result.grade == "UNGRADABLE"

    complete = [
        {"kind": "call_record", "completed": True,
         "disposition": "quoted 1240", "recording_ref": "rec-1",
         "provenance": _prov()},
    ]
    result = board.grade_derived_evidence("carrier_call", plan, complete)
    assert result.passed is True
    assert result.grade == "PASS"


# ---------------------------------------------------------------------------
# Spec format: required_fields declaration
# ---------------------------------------------------------------------------

def test_required_fields_declared_per_seed_job_type():
    assert grade_registry.required_fields_for("policy_change") == "plan"
    assert grade_registry.required_fields_for("carrier_quote") == "document_and_plan"
    assert grade_registry.required_fields_for("carrier_call") == "call_record"
    assert grade_registry.required_fields_for("no_such_type") is None


def test_required_fields_rejects_unknown_mode():
    with pytest.raises(ValueError):
        grade_registry.register_job_type(
            "bad_mode_type",
            {
                "label": "Bad",
                "description": "x",
                "evidence_kinds": ["field_match"],
                "passing_rule": "all_fields_matched",
                "extraction_model": None,
                "required_fields": "whatever",
            },
        )


def test_required_fields_defaults_to_plan_when_undeclared():
    grade_registry.register_job_type(
        "undeclared_mode_type",
        {
            "label": "Undeclared",
            "description": "x",
            "evidence_kinds": ["field_match"],
            "passing_rule": "all_fields_matched",
            "extraction_model": None,
        },
    )
    assert grade_registry.required_fields_for("undeclared_mode_type") == "plan"
