"""H3: the grade spec in force at plan-lock time is pinned; grading later
against a weakened spec (env override or runtime re-registration) fails
closed instead of grading against the weakened spec.

Each test below FAILS on the pre-fix tree (no ``expected_spec_hash``
parameter, no pin stored at lock) and PASSES with the fix.
"""

from __future__ import annotations

import json
import os

import pytest

from robie_job_engine import grade_registry, plan_lock
from robie_job_engine.evidence import EvidenceSpan, LockedPlan, span_source_hash
from robie_job_engine.store import JobStore


_WEAKENED_YAML = """\
version: 1
job_types:
  policy_change:
    label: "Policy change (WEAKENED)"
    description: "Weakened spec injected after plan lock."
    evidence_kinds: ["field_match"]
    passing_rule: "all_fields_matched"
    extraction_model: "gemini-3.8-flash"
  carrier_quote:
    label: "Carrier website quote"
    description: "Quote an account on a carrier website."
    evidence_kinds: ["quote_document", "quoted_value_match"]
    passing_rule: "document_present_and_values_match"
    extraction_model: "gemini-3.8-flash"
  carrier_call:
    label: "Carrier call"
    description: "Call a carrier."
    evidence_kinds: ["call_record"]
    passing_rule: "completed_with_disposition"
    extraction_model: null
"""

_WEAKENED_SPEC = {
    "label": "Policy change (WEAKENED)",
    "description": "Weakened spec re-registered after plan lock.",
    "evidence_kinds": ["field_match"],
    "passing_rule": "all_fields_matched",
    "extraction_model": "gemini-3.8-flash",
}


def _field(name, matched):
    return {"kind": "field_match", "field": name, "matched": matched}


def _store(tmp_path):
    return JobStore(str(tmp_path / "jobs.db"))


def _locked_plan(job_id="job-h3", job_type="policy_change"):
    return LockedPlan(
        job_id=job_id,
        job_type=job_type,
        fields={"writtenPremium": 1284.00, "effectiveDate": "2026-10-01"},
        field_tiers={"writtenPremium": "A", "effectiveDate": "A"},
        locked_by="test",
    )


def _restore_registry_from_disk():
    """Return the singleton registry to the on-disk grades.yaml state."""
    grade_registry.registry._runtime_names.discard("policy_change")
    grade_registry.registry._specs.pop("policy_change", None)
    os.environ.pop("ROBIE_GRADES_YAML", None)
    grade_registry.registry.reload()


# ---------------------------------------------------------------------------
# Attack 1: ROBIE_GRADES_YAML env override after lock
# ---------------------------------------------------------------------------


def test_env_override_after_lock_refuses_grade(tmp_path, monkeypatch):
    store = _store(tmp_path)
    plan_lock.lock_plan(store, _locked_plan(job_id="job-h3-env"))
    pinned = plan_lock.locked_plan_grade_spec_hash(store, "job-h3-env")
    assert pinned and len(pinned) == 64

    weak_path = tmp_path / "weak_grades.yaml"
    weak_path.write_text(_WEAKENED_YAML)
    monkeypatch.setenv("ROBIE_GRADES_YAML", str(weak_path))
    grade_registry.registry.reload()
    try:
        result = grade_registry.grade(
            "policy_change",
            [_field("writtenPremium", True), _field("effectiveDate", True)],
            expected_spec_hash=pinned,
        )
    finally:
        monkeypatch.delenv("ROBIE_GRADES_YAML", raising=False)
        grade_registry.registry.reload()

    # The evidence is all-matched, so without the pin this would PASS under
    # the weakened spec. With the pin it must refuse.
    assert result.passed is False
    assert result.grade == "NEEDS_REVIEW"
    assert "grade spec changed since plan lock" in " ".join(result.reasons)


# ---------------------------------------------------------------------------
# Attack 2: runtime re-registration of the same job type after lock
# ---------------------------------------------------------------------------


def test_runtime_reregistration_after_lock_refuses_grade(tmp_path):
    store = _store(tmp_path)
    plan_lock.lock_plan(store, _locked_plan(job_id="job-h3-runtime"))
    pinned = plan_lock.locked_plan_grade_spec_hash(store, "job-h3-runtime")
    assert pinned and len(pinned) == 64

    grade_registry.register_job_type("policy_change", _WEAKENED_SPEC)
    try:
        result = grade_registry.grade(
            "policy_change",
            [_field("writtenPremium", True), _field("effectiveDate", True)],
            expected_spec_hash=pinned,
        )
    finally:
        _restore_registry_from_disk()

    assert result.passed is False
    assert result.grade == "NEEDS_REVIEW"
    assert "grade spec changed since plan lock" in " ".join(result.reasons)


# ---------------------------------------------------------------------------
# Positive control: no spec change -> grades normally
# ---------------------------------------------------------------------------


def test_unchanged_spec_with_pin_grades_normally(tmp_path):
    store = _store(tmp_path)
    plan_lock.lock_plan(store, _locked_plan(job_id="job-h3-ok"))
    pinned = plan_lock.locked_plan_grade_spec_hash(store, "job-h3-ok")
    assert pinned == grade_registry.validated_spec_hash("policy_change")

    result = grade_registry.grade(
        "policy_change",
        [_field("writtenPremium", True), _field("effectiveDate", True)],
        expected_spec_hash=pinned,
    )
    assert result.passed is True
    assert result.grade == "PASS"

    # Legacy callers (no pin) keep working exactly as before.
    legacy = grade_registry.grade(
        "policy_change",
        [_field("writtenPremium", True), _field("effectiveDate", True)],
    )
    assert legacy.passed is True


# ---------------------------------------------------------------------------
# Pin storage: reserved keys inside plan_json, never inside fields
# ---------------------------------------------------------------------------


def test_lock_pins_spec_hash_and_model_in_plan_json(tmp_path):
    store = _store(tmp_path)
    plan_lock.lock_plan(store, _locked_plan(job_id="job-h3-pin"))

    conn = store.connect()
    try:
        row = conn.execute(
            "SELECT plan_json FROM locked_plans WHERE job_id = ?",
            ("job-h3-pin",),
        ).fetchone()
    finally:
        conn.close()
    payload = json.loads(row[0])

    assert payload["grade_spec_hash"] == grade_registry.validated_spec_hash(
        "policy_change"
    )
    # The effective extraction model is pinned too (documents the
    # ROBIE_GEMINI_MODEL choice: pin it, since the spec hash does not cover
    # the env override).
    assert payload["grade_extraction_model"] == (
        grade_registry.default_extraction_model("policy_change")
    )
    # Reserved keys are siblings of the plan data, not smuggled into fields.
    assert "grade_spec_hash" not in payload["fields"]
    assert "grade_extraction_model" not in payload["fields"]


def test_reading_pin_for_unknown_job_returns_none(tmp_path):
    store = _store(tmp_path)
    assert plan_lock.locked_plan_grade_spec_hash(store, "no-such-job") is None


# ---------------------------------------------------------------------------
# Both lock paths pin: confirm_and_lock funnels through _insert_locked_plan
# ---------------------------------------------------------------------------


def test_confirm_and_lock_path_also_pins_spec_hash(tmp_path):
    from robie_job_engine import confirmations
    from robie_job_engine.plan_extraction import PlanDraft

    store = _store(tmp_path)
    quote = "written premium to 2450.00"
    request = f"please increase the {quote} on the policy"
    start = request.index(quote)
    draft = PlanDraft(
        applicant_id="220250093",
        policy_number="TEST-HO-1",
        changes={"writtenPremium": "2450.0"},
        evidence_spans={
            "writtenPremium": EvidenceSpan(
                source_id="change_request_text",
                source_hash=span_source_hash(request),
                offset_start=start,
                offset_end=start + len(quote),
                quote=quote,
            )
        },
        uncertainties=[],
        needs_human_review=True,
        review_reasons=["test"],
        request_excerpt="increase premium",
        extracted_at="2026-09-21T00:00:00+00:00",
    )
    cid = confirmations.request_confirmation(
        store,
        loop_job_id="loop-h3",
        job_type="policy_change",
        draft_summary="writtenPremium -> 2450.0",
        requested_by="Carlo Ferrara",
        draft=draft,
    )
    confirmations.confirm_and_lock(store, cid, draft, "loop-h3", "Carlo Ferrara")

    pinned = plan_lock.locked_plan_grade_spec_hash(store, "loop-h3")
    assert pinned == grade_registry.validated_spec_hash("policy_change")


# ---------------------------------------------------------------------------
# Fail closed at lock: no spec, no pin, no lock
# ---------------------------------------------------------------------------


def test_lock_unknown_job_type_fails_closed(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="no spec for it"):
        plan_lock.lock_plan(store, _locked_plan(job_type="mystery_job_xyz"))
    assert not plan_lock.plan_is_locked(store, "job-h3")


# ---------------------------------------------------------------------------
# Hash stability: key order must not change the pin
# ---------------------------------------------------------------------------


def test_grade_spec_hash_is_key_order_stable():
    spec_a = {
        "label": "Policy change",
        "description": "d",
        "evidence_kinds": ["field_match"],
        "passing_rule": "all_fields_matched",
        "extraction_model": "gemini-3.8-flash",
    }
    spec_b = {
        "extraction_model": "gemini-3.8-flash",
        "passing_rule": "all_fields_matched",
        "evidence_kinds": ["field_match"],
        "description": "d",
        "label": "Policy change",
    }
    hash_a = grade_registry.grade_spec_hash(spec_a)
    hash_b = grade_registry.grade_spec_hash(spec_b)
    assert hash_a == hash_b
    assert len(hash_a) == 64
    # ...but a real content change moves the pin.
    spec_c = dict(spec_a, label="Policy change (tweaked)")
    assert grade_registry.grade_spec_hash(spec_c) != hash_a
