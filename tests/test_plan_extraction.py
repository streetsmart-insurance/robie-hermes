"""Tests for request -> plan-draft extraction (evidence-loop pilot).

The model is always a fake here: these tests pin the deterministic harness
(contract, quote verification, validation, fail-closed locking), not any
particular model's judgment.
"""
from __future__ import annotations

import json

import pytest

from robie_job_engine.plan_extraction import (
    PlanDraft,
    PlanExtractionError,
    PlanNeedsHumanReview,
    build_extraction_prompt,
    draft_summary_text,
    draft_to_locked_plan,
    extract_plan_draft,
    is_known_field,
    normalize_field_name,
    parse_llm_plan_json,
)


REQUEST_OK = (
    "Hi, please update the written premium to $1,250.00 on policy "
    "TEST-HO-20260911-E01 for applicant 220250093. "
    "Also move the expiration date to 2027-10-02. Thanks!"
)

MODEL_OK = {
    "applicant_id": "220250093",
    "policy_number": "TEST-HO-20260911-E01",
    "changes": [
        {
            "field": "writtenPremium",
            "value": 1250.00,
            "quote": "update the written premium to $1,250.00",
        },
        {
            "field": "expirationDate",
            "value": "2027-10-02",
            "quote": "move the expiration date to 2027-10-02",
        },
    ],
    "uncertainties": [],
}


def _fake_llm(payload) -> object:
    return lambda prompt: json.dumps(payload) if not isinstance(payload, str) else payload


def test_happy_path_extracts_and_locks():
    draft = extract_plan_draft(
        REQUEST_OK,
        llm_json_fn=_fake_llm(MODEL_OK),
        policy_exists_fn=lambda number: True,
    )
    assert isinstance(draft, PlanDraft)
    assert draft.needs_human_review is False
    assert draft.review_reasons == []
    assert draft.applicant_id == "220250093"
    assert draft.policy_number == "TEST-HO-20260911-E01"
    assert draft.changes == {"writtenPremium": "1250.0", "expirationDate": "2027-10-02"}

    locked = draft_to_locked_plan(draft, "job-001")
    assert locked.job_id == "job-001"
    assert locked.fields["writtenPremium"] == "1250.0"
    assert locked.fields["policyNumber"] == "TEST-HO-20260911-E01"
    assert locked.field_tiers["writtenPremium"] == "A"


def test_uncertainties_force_human_review_but_named_override_locks():
    payload = dict(MODEL_OK)
    payload["uncertainties"] = ["request mentions two different dates elsewhere"]
    draft = extract_plan_draft(REQUEST_OK, llm_json_fn=_fake_llm(payload))
    assert draft.needs_human_review is True
    assert any("uncertaint" in r for r in draft.review_reasons)

    with pytest.raises(PlanNeedsHumanReview):
        draft_to_locked_plan(draft, "job-002")

    locked = draft_to_locked_plan(draft, "job-002", human_confirmed_by="Carlo")
    assert locked.locked_by == "human:Carlo"


def test_hallucinated_quote_goes_to_review():
    payload = dict(MODEL_OK)
    payload["changes"] = [
        {
            "field": "writtenPremium",
            "value": 9999.00,
            # Not a substring of REQUEST_OK: the model invented its source.
            "quote": "set the premium to nine thousand",
        }
    ]
    draft = extract_plan_draft(REQUEST_OK, llm_json_fn=_fake_llm(payload))
    assert draft.needs_human_review is True
    assert "writtenPremium" not in draft.changes
    assert any("not found in the request" in r for r in draft.review_reasons)


def test_unknown_field_goes_to_review():
    payload = {
        "applicant_id": "220250093",
        "policy_number": "TEST-HO-20260911-E01",
        "changes": [
            {
                "field": "telepathyLevel",
                "value": "high",
                "quote": "Thanks!",
            }
        ],
        "uncertainties": [],
    }
    draft = extract_plan_draft(REQUEST_OK, llm_json_fn=_fake_llm(payload))
    assert draft.needs_human_review is True
    assert any("unknown field" in r for r in draft.review_reasons)


def test_missing_policy_identity_goes_to_review():
    payload = {
        "applicant_id": None,
        "policy_number": None,
        "changes": [
            {
                "field": "writtenPremium",
                "value": 1250.00,
                "quote": "update the written premium to $1,250.00",
            }
        ],
        "uncertainties": [],
    }
    draft = extract_plan_draft(REQUEST_OK, llm_json_fn=_fake_llm(payload))
    assert draft.needs_human_review is True
    assert any("policy number" in r for r in draft.review_reasons)
    with pytest.raises(PlanNeedsHumanReview):
        draft_to_locked_plan(draft, "job-003")


def test_context_ids_fill_gaps():
    payload = {
        "applicant_id": None,
        "policy_number": None,
        "changes": [
            {
                "field": "writtenPremium",
                "value": 1250.00,
                "quote": "update the written premium to $1,250.00",
            }
        ],
        "uncertainties": [],
    }
    draft = extract_plan_draft(
        REQUEST_OK,
        llm_json_fn=_fake_llm(payload),
        applicant_id="220250093",
        policy_number="TEST-HO-20260911-E01",
    )
    assert draft.needs_human_review is False
    assert draft.applicant_id == "220250093"
    assert draft.policy_number == "TEST-HO-20260911-E01"
    prompt = build_extraction_prompt(
        REQUEST_OK, applicant_id="220250093", policy_number="TEST-HO-20260911-E01"
    )
    assert "220250093" in prompt and "TEST-HO-20260911-E01" in prompt


def test_malformed_model_json_goes_to_review_not_crash():
    draft = extract_plan_draft(
        REQUEST_OK, llm_json_fn=lambda prompt: "sure thing, boss { not json"
    )
    assert draft.needs_human_review is True
    assert any("not valid JSON" in r for r in draft.review_reasons)


def test_model_call_failure_goes_to_review():
    def boom(prompt):
        raise RuntimeError("model is down")

    draft = extract_plan_draft(REQUEST_OK, llm_json_fn=boom)
    assert draft.needs_human_review is True
    assert any("model call failed" in r for r in draft.review_reasons)


def test_policy_not_found_at_destination_goes_to_review():
    draft = extract_plan_draft(
        REQUEST_OK,
        llm_json_fn=_fake_llm(MODEL_OK),
        policy_exists_fn=lambda number: False,
    )
    assert draft.needs_human_review is True
    assert any("did not resolve" in r for r in draft.review_reasons)


def test_insane_premium_value_goes_to_review():
    payload = dict(MODEL_OK)
    payload["changes"] = [
        {
            "field": "writtenPremium",
            "value": -50,
            "quote": "update the written premium to $1,250.00",
        }
    ]
    draft = extract_plan_draft(REQUEST_OK, llm_json_fn=_fake_llm(payload))
    assert draft.needs_human_review is True
    assert "writtenPremium" not in draft.changes


def test_bad_date_format_goes_to_review():
    payload = dict(MODEL_OK)
    payload["changes"] = [
        {
            "field": "expirationDate",
            "value": "next October",
            "quote": "move the expiration date to 2027-10-02",
        }
    ]
    draft = extract_plan_draft(REQUEST_OK, llm_json_fn=_fake_llm(payload))
    assert draft.needs_human_review is True


def test_tier_b_coverage_field_is_known():
    assert is_known_field("coverageA_limit") is True
    assert normalize_field_name("premium") == "writtenPremium"
    assert normalize_field_name("writtenPremium") == "writtenPremium"
    assert normalize_field_name("telepathy") is None


def test_empty_request_is_caller_error():
    with pytest.raises(ValueError):
        extract_plan_draft("   ", llm_json_fn=_fake_llm(MODEL_OK))


def test_parse_llm_plan_json_rejects_non_object():
    with pytest.raises(PlanExtractionError):
        parse_llm_plan_json("[1, 2, 3]")
    # ...but tolerates markdown fences around the object.
    parsed = parse_llm_plan_json('```json\n{"a": 1}\n```')
    assert parsed == {"a": 1}


def test_draft_summary_is_human_readable():
    draft = extract_plan_draft(REQUEST_OK, llm_json_fn=_fake_llm(MODEL_OK))
    text = draft_summary_text(draft)
    assert "TEST-HO-20260911-E01" in text
    assert "writtenPremium -> 1250.0" in text
    assert "ready to lock" in text


def test_quote_check_tolerates_whitespace_differences():
    # The request has "effective\n10/01/2026" (line break); a model quoting
    # "effective 10/01/2026" (space) is quoting the actual request.
    request = (
        "Please increase the written premium to 2450.00 on our policy effective\n"
        "10/01/2026 to reflect the added exposure. Applicant 220250093, "
        "policy TEST-HO-20260911-E01."
    )
    payload = {
        "applicant_id": "220250093",
        "policy_number": "TEST-HO-20260911-E01",
        "changes": [
            {
                "field": "effectiveDate",
                "value": "2026-10-01",
                "quote": "policy effective 10/01/2026",
            },
        ],
        "uncertainties": [],
    }
    draft = extract_plan_draft(
        request,
        llm_json_fn=_fake_llm(payload),
        policy_exists_fn=lambda number: True,
    )
    assert draft.changes.get("effectiveDate") == "2026-10-01"
    assert not draft.needs_human_review


def test_quote_check_still_rejects_invented_words():
    request = "Please update the written premium to 100. Applicant 220250093."
    payload = {
        "applicant_id": "220250093",
        "policy_number": "TEST-HO-20260911-E01",
        "changes": [
            {
                "field": "writtenPremium",
                "value": 100,
                "quote": "update the written premium to 999 (totally different)",
            },
        ],
        "uncertainties": [],
    }
    draft = extract_plan_draft(
        request,
        llm_json_fn=_fake_llm(payload),
        policy_exists_fn=lambda number: True,
    )
    assert draft.needs_human_review
    assert any("not found in the request" in r for r in draft.review_reasons)
