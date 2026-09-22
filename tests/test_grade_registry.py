"""Tests for the dynamic grade registry and the board wiring."""

from __future__ import annotations

import sqlite3

import pytest

from robie_job_engine import board, grade_registry
from robie_job_engine.grade_registry import GradeResult, register_job_type
from robie_job_engine.store import JobStore


# ---------------------------------------------------------------------------
# Registry: seed job types
# ---------------------------------------------------------------------------

def test_seed_job_types_present():
    types = grade_registry.job_types()
    assert "policy_change" in types
    assert "carrier_quote" in types
    assert "carrier_call" in types


def test_extraction_models_pinned_to_light_models():
    # The model move: extraction runs on light flash-lite, not high reasoning.
    assert (
        grade_registry.extraction_model_for("policy_change")
        == "gemini-3.1-flash-lite-preview"
    )
    assert (
        grade_registry.extraction_model_for("carrier_quote")
        == "gemini-3.1-flash-lite-preview"
    )
    assert grade_registry.extraction_model_for("carrier_call") is None


# ---------------------------------------------------------------------------
# Grader: policy_change
# ---------------------------------------------------------------------------

def _field(name, matched, expected="x", observed="x"):
    return {
        "kind": "field_match",
        "field": name,
        "matched": matched,
        "expected": expected,
        "observed": observed,
        "policy_number": "TEST-HO-1",
    }


def test_policy_change_pass_all_matched():
    result = grade_registry.grade(
        "policy_change", [_field("writtenPremium", True), _field("effectiveDate", True)]
    )
    assert isinstance(result, GradeResult)
    assert result.passed is True
    assert result.grade == "PASS"


def test_policy_change_needs_review_on_mismatch():
    result = grade_registry.grade(
        "policy_change",
        [_field("writtenPremium", True), _field("effectiveDate", False, "2026-10-01", "2026-09-01")],
    )
    assert result.passed is False
    assert result.grade == "NEEDS_REVIEW"
    assert "effectiveDate" in " ".join(result.reasons)


def test_policy_change_needs_review_on_empty_evidence():
    result = grade_registry.grade("policy_change", [])
    assert result.passed is False
    assert result.grade == "NEEDS_REVIEW"


# ---------------------------------------------------------------------------
# Grader: carrier_quote
# ---------------------------------------------------------------------------

def _quote_doc(present=True):
    return {"kind": "quote_document", "present": present, "document_ref": "doc-1"}


def _quote_value(name, matched):
    return {"kind": "quoted_value_match", "field": name, "matched": matched}


def test_carrier_quote_pass():
    result = grade_registry.grade(
        "carrier_quote",
        [_quote_doc(True), _quote_value("premium", True), _quote_value("coverage_a", True)],
    )
    assert result.passed is True
    assert result.grade == "PASS"


def test_carrier_quote_needs_review_without_document():
    result = grade_registry.grade("carrier_quote", [_quote_value("premium", True)])
    assert result.passed is False
    assert any("document" in r for r in result.reasons)


def test_carrier_quote_needs_review_on_value_mismatch():
    result = grade_registry.grade(
        "carrier_quote", [_quote_doc(True), _quote_value("premium", False)]
    )
    assert result.passed is False
    assert any("premium" in r for r in result.reasons)


# ---------------------------------------------------------------------------
# Grader: carrier_call
# ---------------------------------------------------------------------------

def _call(completed=True, disposition="quoted 1240"):
    return {
        "kind": "call_record",
        "completed": completed,
        "disposition": disposition,
        "recording_ref": "rec-1",
        "notes_ref": "note-1",
    }


def test_carrier_call_pass():
    result = grade_registry.grade("carrier_call", [_call()])
    assert result.passed is True
    assert result.grade == "PASS"


def test_carrier_call_needs_review_without_disposition():
    result = grade_registry.grade("carrier_call", [_call(disposition="")])
    assert result.passed is False


def test_carrier_call_needs_review_when_not_completed():
    result = grade_registry.grade("carrier_call", [_call(completed=False)])
    assert result.passed is False


# ---------------------------------------------------------------------------
# Grader: fail closed + dynamic registration
# ---------------------------------------------------------------------------

def test_unknown_job_type_fails_closed():
    result = grade_registry.grade("mystery_job_xyz", [_field("a", True)])
    assert result.passed is False
    assert result.grade == "UNKNOWN_JOB_TYPE"


def test_register_new_job_type_at_runtime_no_code_change():
    register_job_type(
        "test_certificate_issue",
        {
            "label": "Certificate issuance",
            "description": "Runtime-registered for the test.",
            "evidence_kinds": ["field_match"],
            "passing_rule": "all_fields_matched",
            "extraction_model": None,
        },
    )
    assert "test_certificate_issue" in grade_registry.job_types()
    ok = grade_registry.grade("test_certificate_issue", [_field("cert_holder", True)])
    assert ok.passed is True
    bad = grade_registry.grade("test_certificate_issue", [_field("cert_holder", False)])
    assert bad.passed is False


def test_register_job_type_rejects_bad_spec():
    with pytest.raises(ValueError):
        register_job_type("bad_type", {"label": "Bad"})  # missing keys
    with pytest.raises(ValueError):
        register_job_type(
            "bad_rule",
            {
                "label": "Bad",
                "description": "x",
                "evidence_kinds": ["field_match"],
                "passing_rule": "no_such_rule",
                "extraction_model": None,
            },
        )


# ---------------------------------------------------------------------------
# Board wiring
# ---------------------------------------------------------------------------

@pytest.fixture()
def store(tmp_path):
    return JobStore(str(tmp_path / "jobs.db"))


def test_record_draft_creates_job_and_is_idempotent(store):
    job_id = board.record_draft(
        store,
        loop_job_id="loop-1",
        job_type="policy_change",
        draft_summary="writtenPremium -> 2450.0",
    )
    again = board.record_draft(
        store,
        loop_job_id="loop-1",
        job_type="policy_change",
        draft_summary="writtenPremium -> 2450.0",
    )
    assert job_id == again
    job = store.get_job(job_id)
    assert job["action_type"] == "policy_change"


def test_record_draft_rejects_unknown_job_type(store):
    with pytest.raises(ValueError):
        board.record_draft(
            store,
            loop_job_id="loop-9",
            job_type="no_such_type",
            draft_summary="x",
        )


def test_full_board_flow_draft_evidence_grade_label(store, tmp_path):
    job_id = board.record_draft(
        store,
        loop_job_id="loop-2",
        job_type="policy_change",
        draft_summary="writtenPremium -> 2450.0",
    )
    items = [
        {
            "kind": "field_match",
            "field": "writtenPremium",
            "matched": True,
            "expected": "2450.0",
            "observed": "2450.0",
            "policy_number": "TEST-HO-1",
        },
        {
            "kind": "field_match",
            "field": "effectiveDate",
            "matched": True,
            "expected": "2026-10-01",
            "observed": "2026-10-01",
            "policy_number": "TEST-HO-1",
        },
    ]
    assert board.record_evidence(store, job_id, items) == 2
    evidence_rows = store.list_evidence(job_id)
    assert len(evidence_rows) == 2
    assert all(r["authoritative"] == 1 for r in evidence_rows)

    result = grade_registry.grade("policy_change", items)
    board.record_grade(store, job_id, loop_job_id="loop-2", result=result)
    label = board.grade_label_for(str(tmp_path / "jobs.db"), job_id)
    assert label is not None
    assert label.startswith("PASS - Policy change:")


def test_grade_label_needs_review(store, tmp_path):
    job_id = board.record_draft(
        store, loop_job_id="loop-3", job_type="carrier_call", draft_summary="call carrier"
    )
    result = grade_registry.grade("carrier_call", [])
    board.record_grade(store, job_id, loop_job_id="loop-3", result=result)
    label = board.grade_label_for(str(tmp_path / "jobs.db"), job_id)
    assert label is not None
    assert label.startswith("NEEDS REVIEW - Carrier call:")


def test_grade_label_none_before_grading(store, tmp_path):
    job_id = board.record_draft(
        store, loop_job_id="loop-4", job_type="policy_change", draft_summary="x"
    )
    assert board.grade_label_for(str(tmp_path / "jobs.db"), job_id) is None


def test_runtime_job_fields_prefers_grade_label():
    from robie_job_engine.sheets_sync import _runtime_job_fields

    item = {
        "status": "PENDING",
        "verification_count": 0,
        "verified_evidence_count": 0,
        "authoritative_evidence_count": 0,
        "updated_at": "2026-09-22T00:00:00+00:00",
        "grade_label": "PASS - Policy change: all 2 field(s) matched",
    }
    fields = _runtime_job_fields(item)
    assert fields["verification_status"] == "PASS - Policy change: all 2 field(s) matched"


def test_runtime_job_fields_falls_back_without_grade():
    from robie_job_engine.sheets_sync import _runtime_job_fields

    item = {
        "status": "PENDING",
        "verification_count": 0,
        "verified_evidence_count": 0,
        "authoritative_evidence_count": 0,
        "updated_at": "2026-09-22T00:00:00+00:00",
        "grade_label": None,
    }
    fields = _runtime_job_fields(item)
    assert fields["verification_status"] == "Pending"


def test_job_grades_schema_created_on_demand(tmp_path):
    db_path = str(tmp_path / "plain.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.commit()
    board._ensure_schema(conn)
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='job_grades'"
    ).fetchone()
    conn.close()
    assert row is not None


def test_default_extraction_model_uses_registry_pin():
    assert (
        grade_registry.default_extraction_model("policy_change")
        == "gemini-3.1-flash-lite-preview"
    )


def test_default_extraction_model_env_override(monkeypatch):
    monkeypatch.setenv("ROBIE_GEMINI_MODEL", "custom-model-1")
    assert grade_registry.default_extraction_model("policy_change") == "custom-model-1"


def test_default_extraction_model_unknown_type_gets_light_default():
    assert (
        grade_registry.default_extraction_model("no_such_type")
        == "gemini-3.1-flash-lite-preview"
    )
