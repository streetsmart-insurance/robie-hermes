"""Unit checks: Quote IDs must never be treated as policies (Carlo 2026-09-05)."""

from __future__ import annotations

from pathlib import Path

from src.ezlynx.policy_renewer import (
    extract_quote_from_pdf,
    is_quote_not_policy,
    prefer_renew_on_expiring_wc,
    SAFE_APPLICANTS,
    ALLOWED_PRODUCTION_APPLICANTS,
)


def test_is_quote_not_policy_biberk_quote_id_only():
    meta = {
        "quote_id": "77359736",
        "quote_markers": ["Quote ID", "Your … Quote", "biBERK Quote", "quoted premium"],
        "policy_number": None,
        "notes": ["marketing_quote_not_policy"],
    }
    assert is_quote_not_policy(meta) is True


def test_is_quote_not_policy_false_when_distinct_policy_number():
    meta = {
        "quote_id": "77359736",
        "quote_markers": ["Quote ID"],
        "policy_number": "WPH5080304 001",
        "notes": ["marketing_quote_not_policy"],
    }
    assert is_quote_not_policy(meta) is False


def test_is_quote_not_policy_rejects_policy_start_token():
    meta = {
        "text": "Your biBERK Quote\nQuote ID: 77359736\nquoted premium",
        "policy_number": "Start",
        "quote_id": "77359736",
    }
    assert is_quote_not_policy(meta) is True


def test_prefer_renew_on_expiring_wc_tracked():
    policies = [
        {
            "PolicyNumber": "R2WC571899",
            "LOB": "Workers comp",
            "Status": "Inactive",
            "ExpirationDate": "2025-07-25T00:00:00",
        },
        {
            "PolicyNumber": "WPH5080304 001",
            "LOB": "Workers comp",
            "Status": "Active",
            "ExpirationDate": "2026-10-01T00:00:00",
        },
    ]
    hit = prefer_renew_on_expiring_wc(
        policies, tracked_policy_number="WPH5080304 001", lob="Workers comp"
    )
    assert hit is not None
    assert hit["PolicyNumber"] == "WPH5080304 001"


def test_extract_biberk_pdf_sets_quote_id_not_policy(tmp_path=None):
    pdf = Path("/opt/renewal-automation-system/data/downloads/julio_26042781/BIBERK-QUOTE.pdf")
    if not pdf.is_file():
        return  # skip if fixture absent
    facts = extract_quote_from_pdf(pdf)
    assert facts.quote_id == "77359736"
    assert facts.policy_number != "77359736"
    assert facts.policy_number != "Start"
    assert is_quote_not_policy(facts) is True
    assert any("Quote ID" in m or "biBERK" in m or "quoted" in m for m in facts.quote_markers) or (
        "marketing_quote_not_policy" in facts.notes
    )


def test_safe_allow_live_constants_unchanged():
    # Safe Man / Pross hard-block + Test LLC allow-list must remain
    assert "163863318" in SAFE_APPLICANTS
    assert "151445306" in SAFE_APPLICANTS
    assert "220250093" in ALLOWED_PRODUCTION_APPLICANTS
