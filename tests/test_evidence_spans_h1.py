"""H1: evidence spans bound to plan fields.

Every plan field must carry an evidence span
{source_id, source_hash, offset_start, offset_end, quote} pointing into the
real fetched source text. Spans survive draft -> LockedPlan -> plan_json,
are covered by the draft fingerprint, are attached to FieldChecks by
compare_plan_to_evidence, are shown by the approval renderers, and are
validated fail-closed (a source whose hash no longer matches refuses to
grade).

Top-level imports are limited to names that exist on the pre-fix base so
these tests demonstrate the behavior gap by FAILING there (not by import
errors). New-API imports are deferred inside the test functions.
"""
from __future__ import annotations

import hashlib
import json
import re

import pytest

from robie_job_engine.confirmations import (
    confirm_and_lock,
    confirmation_summary,
    get,
    request_confirmation,
)
from robie_job_engine.confirmation_board import _details_text
from robie_job_engine.evidence import (
    LockedPlan,
    compare_plan_to_evidence,
)
from robie_job_engine.plan_extraction import (
    draft_fingerprint,
    draft_to_locked_plan,
    extract_plan_draft,
)
from robie_job_engine.plan_lock import get_locked_plan, lock_plan
from robie_job_engine.store import JobStore


REQUEST = (
    "Hi, please set the written premium to 2450.00 on policy "
    "TEST-HO-20260911-E01 for applicant 220250093. Thanks!"
)

MODEL = {
    "applicant_id": "220250093",
    "policy_number": "TEST-HO-20260911-E01",
    "changes": [
        {
            "field": "writtenPremium",
            "value": 2450.00,
            "quote": "set the written premium to 2450.00",
        },
    ],
    "uncertainties": [],
}


def _fake_llm(payload):
    return lambda prompt: json.dumps(payload)


def _draft():
    return extract_plan_draft(
        REQUEST,
        llm_json_fn=_fake_llm(MODEL),
        policy_exists_fn=lambda number: True,
    )


def _canonical(text):
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# 1. extraction binds offsets into the real source text
# ---------------------------------------------------------------------------

def test_extracted_span_has_offsets_into_the_request_text():
    draft = _draft()
    assert not draft.needs_human_review
    span = draft.evidence_spans["writtenPremium"]
    # AttributeError on the pre-fix base: evidence_spans held bare strings.
    assert span.offset_start < span.offset_end
    assert span.source_id == "change_request_text"
    canonical = _canonical(REQUEST)
    assert canonical[span.offset_start:span.offset_end] == span.quote
    assert "written premium to 2450.00" in span.quote
    # The hash is over the exact bytes the offsets index into.
    assert span.source_hash == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 2. spans survive draft -> LockedPlan
# ---------------------------------------------------------------------------

def test_spans_survive_draft_to_locked_plan():
    draft = _draft()
    locked = draft_to_locked_plan(draft, "job-h1-2")
    # AttributeError on the pre-fix base: LockedPlan has no field_spans.
    draft_hash = draft.evidence_spans["writtenPremium"].source_hash
    assert locked.field_spans["writtenPremium"].source_hash == draft_hash
    assert locked.field_spans["writtenPremium"].offset_start >= 0
    # The auto-added identity field carries a span from agency context.
    assert locked.field_spans["policyNumber"].source_id == "agency_context"


# ---------------------------------------------------------------------------
# 3. spans round-trip through plan_json (lock -> read back)
# ---------------------------------------------------------------------------

def test_spans_round_trip_through_plan_lock(tmp_path):
    store = JobStore(str(tmp_path / "jobs.db"))
    draft = _draft()
    locked = draft_to_locked_plan(draft, "job-h1-3")
    lock_plan(store, locked)
    fetched = get_locked_plan(store, "job-h1-3")
    assert fetched is not None
    assert fetched.field_spans["writtenPremium"].source_hash == (
        draft.evidence_spans["writtenPremium"].source_hash
    )
    assert fetched.field_spans["writtenPremium"].quote == (
        draft.evidence_spans["writtenPremium"].quote
    )
    # Round-trip through the approval path too: confirm -> lock -> read.
    draft2 = _draft()
    cid = request_confirmation(
        store,
        loop_job_id="loop-h1-3",
        job_type="policy_change",
        draft_summary="writtenPremium -> 2450.0",
        requested_by="Carlo Ferrara",
        draft=draft2,
    )
    confirm_and_lock(store, cid, draft2, "loop-h1-3", "Carlo Ferrara")
    fetched2 = get_locked_plan(store, "loop-h1-3")
    assert fetched2 is not None
    assert fetched2.field_spans["writtenPremium"].source_hash == (
        draft2.evidence_spans["writtenPremium"].source_hash
    )


# ---------------------------------------------------------------------------
# 4. span validation fails closed on a tampered source
# ---------------------------------------------------------------------------

def test_span_validation_refuses_tampered_source():
    from robie_job_engine.evidence import SpanValidationError, validate_plan_spans

    draft = _draft()
    locked = draft_to_locked_plan(draft, "job-h1-4")
    # Same offsets, but the source text was modified: the hash no longer
    # matches, so grading must be refused, not graded on the wrong bytes.
    tampered = REQUEST.replace("2450.00", "9999.00")
    with pytest.raises(SpanValidationError):
        validate_plan_spans(locked, {"change_request_text": tampered})


def test_span_validation_accepts_the_true_source():
    from robie_job_engine.evidence import validate_plan_spans

    draft = _draft()
    locked = draft_to_locked_plan(draft, "job-h1-4b")
    # The genuine source (plus the agency-context identity bytes) grades.
    validate_plan_spans(
        locked,
        {
            "change_request_text": REQUEST,
            "agency_context": "TEST-HO-20260911-E01",
        },
    )


def test_compare_attaches_span_to_each_field_check_and_refuses_tampered():
    from robie_job_engine.evidence import SpanValidationError

    draft = _draft()
    locked = draft_to_locked_plan(draft, "job-h1-5")
    result = compare_plan_to_evidence(
        locked,
        {"writtenPremium": "2450.0", "policyNumber": "TEST-HO-20260911-E01"},
        now="2026-09-22T10:00:00+00:00",
        sources={
            "change_request_text": REQUEST,
            "agency_context": "TEST-HO-20260911-E01",
        },
    )
    by_field = {c.field: c for c in result.checks}
    # AttributeError on the pre-fix base: FieldCheck has no span attribute.
    assert by_field["writtenPremium"].span is not None
    assert by_field["writtenPremium"].span.source_id == "change_request_text"
    # A tampered source refuses the comparison instead of grading it.
    with pytest.raises(SpanValidationError):
        compare_plan_to_evidence(
            locked,
            {"writtenPremium": "2450.0"},
            sources={"change_request_text": REQUEST.replace("2450.00", "1.00")},
        )


# ---------------------------------------------------------------------------
# 5. the draft fingerprint covers the span set
# ---------------------------------------------------------------------------

def test_fingerprint_changes_when_only_the_quote_changes():
    draft_a = _draft()
    # Same change set, different supporting quote -> different fingerprint.
    model_b = json.loads(json.dumps(MODEL))
    model_b["changes"][0]["quote"] = "set the written premium to 2450.00 on policy"
    draft_b = extract_plan_draft(
        REQUEST,
        llm_json_fn=_fake_llm(model_b),
        policy_exists_fn=lambda number: True,
    )
    assert draft_a.changes == draft_b.changes
    # Equal on the pre-fix base (spans were not fingerprinted).
    assert draft_fingerprint(draft_a) != draft_fingerprint(draft_b)


# ---------------------------------------------------------------------------
# 6. approval renderers show quote + source, not just the value
# ---------------------------------------------------------------------------

def test_approval_renderers_show_quote_and_source(tmp_path):
    store = JobStore(str(tmp_path / "jobs.db"))
    draft = _draft()
    cid = request_confirmation(
        store,
        loop_job_id="loop-h1-6",
        job_type="policy_change",
        draft_summary="writtenPremium -> 2450.0",
        requested_by="Carlo Ferrara",
        draft=draft,
    )
    record = get(cid, store=store)
    line = confirmation_summary(record)
    # On the pre-fix base the line names the change but no quote or source.
    assert "change_request_text" in line
    assert "written premium to 2450.00" in line
    details = _details_text(record)
    assert "change_request_text" in details
    assert "written premium to 2450.00" in details
