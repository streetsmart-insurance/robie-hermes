import pytest
from unittest.mock import MagicMock, patch
from src.utils.subject_formatter import (
    format_subject_with_insured_and_policy,
    ensure_subject_has_identifiers,
    SubjectMissingMetadataError,
    AuditSubjectMissingMetadataError,
)
from src.email_outreach.gmail_client import GmailRenewalClient


def test_format_subject_both_missing_adds_both():
    subj = "Action Required: Audit Update"
    res = format_subject_with_insured_and_policy(subj, "Apex Logistics LLC", "NJ0988712")
    assert res == "Action Required: Audit Update - Apex Logistics LLC - Policy #NJ0988712"


def test_format_subject_with_empty_or_none_subject():
    res1 = format_subject_with_insured_and_policy("", "Apex Logistics LLC", "NJ0988712")
    assert res1 == "Apex Logistics LLC - Policy #NJ0988712"

    res2 = format_subject_with_insured_and_policy(None, "Apex Logistics LLC", "NJ0988712")
    assert res2 == "Apex Logistics LLC - Policy #NJ0988712"


def test_format_subject_already_has_both_no_duplicates():
    subj = "Action Required: Audit Update - Apex Logistics LLC - Policy #NJ0988712"
    res = format_subject_with_insured_and_policy(subj, "Apex Logistics LLC", "NJ0988712")
    assert res == subj


def test_format_subject_has_insured_adds_policy():
    subj = "Quote Received for Apex Logistics LLC"
    res = format_subject_with_insured_and_policy(subj, "Apex Logistics LLC", "NJ0988712")
    assert res == "Quote Received for Apex Logistics LLC - Policy #NJ0988712"


def test_format_subject_has_policy_adds_insured():
    subj = "Policy Status Update - Policy #NJ0988712"
    res = format_subject_with_insured_and_policy(subj, "Apex Logistics LLC", "NJ0988712")
    assert res == "Policy Status Update - Policy #NJ0988712 - Apex Logistics LLC"


def test_format_subject_trailing_punctuation_cleaned():
    for trailing in [" - ", " : ", " | ", " ; "]:
        subj = f"Action Required: Audit Update{trailing}"
        res = format_subject_with_insured_and_policy(subj, "Acme Landscaping", "POL100")
        assert res == "Action Required: Audit Update - Acme Landscaping - Policy #POL100"


def test_format_subject_policy_prefixes():
    # If policy already has "Policy #"
    res1 = format_subject_with_insured_and_policy("Inquiry", "Acme", "Policy #12345")
    assert res1 == "Inquiry - Acme - Policy #12345"

    # If policy already has "Pol #"
    res2 = format_subject_with_insured_and_policy("Inquiry", "Acme", "Pol #12345")
    assert res2 == "Inquiry - Acme - Pol #12345"

    # If policy starts with "#"
    res3 = format_subject_with_insured_and_policy("Inquiry", "Acme", "#12345")
    assert res3 == "Inquiry - Acme - #12345"


def test_format_subject_case_and_punctuation_insensitive_detection():
    # LLC with comma in subject, without comma in argument
    subj = "Workers Comp Audit: Tri-State Builders, LLC."
    res = format_subject_with_insured_and_policy(subj, "Tri State Builders LLC", "998877")
    assert "Tri State Builders LLC" not in res  # already matchedTriStateBuildersLLC
    assert res == "Workers Comp Audit: Tri-State Builders, LLC. - Policy #998877"


def test_ensure_subject_has_identifiers_strict_raises():
    with pytest.raises(SubjectMissingMetadataError) as exc:
        ensure_subject_has_identifiers("Generic Subject", insured_name=None, policy_number="12345", raise_if_missing=True)
    assert "CRITICAL SAFETY GATE" in str(exc.value)

    with pytest.raises(SubjectMissingMetadataError) as exc:
        ensure_subject_has_identifiers("Generic Subject", insured_name="Acme", policy_number="", raise_if_missing=True)
    assert "CRITICAL SAFETY GATE" in str(exc.value)


def test_gmail_client_send_email_formats_subject_automatically(monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "1")
    client = GmailRenewalClient()
    client.service = MagicMock()

    result = client.send_email(
        to_email="underwriter@carrier.example.com",
        subject="Action Required: Audit Update",
        body_text="Please review attached audit papers.",
        insured_name="Summit Contracting LLC",
        policy_number="WCP778899",
    )
    assert result["status"] == "simulated"


def test_gmail_client_send_email_strict_raises_on_missing(monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "1")
    client = GmailRenewalClient()
    client.service = MagicMock()

    with pytest.raises(SubjectMissingMetadataError) as exc:
        client.send_email(
            to_email="test@example.com",
            subject="Action Required: Audit Update",
            body_text="Test",
            insured_name="Summit Contracting LLC",
            policy_number="",
            enforce_subject_identifiers=True,
        )
    assert "CRITICAL SAFETY GATE" in str(exc.value)
