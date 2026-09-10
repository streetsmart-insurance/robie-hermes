"""Unit tests for RenewalSafetyGate (pre-flight cancellation & non-renewal detection)."""

import pytest
from unittest.mock import MagicMock
from src.intake.safety_gate import RenewalSafetyGate, SafetyCheckResult


def test_safety_gate_flags_cancellation_in_discussion_title():
    mock_ezlynx = MagicMock()
    mock_ezlynx.get_applicant_discussions.return_value = [
        {
            "discussionId": 999,
            "title": "INSURED CANCELLATION REQUEST: Renewal Workers comp | 106793-4-25",
            "discussionNote": {"noteDescription": "Insured sent signed LCR form."}
        }
    ]
    mock_ezlynx.list_applicant_documents.return_value = []

    gate = RenewalSafetyGate(ezlynx_client=mock_ezlynx)
    result = gate.check_cancellation_risk(applicant_id="144897143", policy_number="106793-4-25")

    assert result.is_safe is False
    assert result.risk_detected is True
    assert "cancellation" in result.reason.lower()
    assert len(result.flags) >= 1
    assert any(e["source"] == "discussion_title" for e in result.evidence)


def test_safety_gate_flags_cancellation_in_documents():
    mock_ezlynx = MagicMock()
    mock_ezlynx.get_applicant_discussions.return_value = [
        {
            "discussionId": 100,
            "title": "Renewal Manual Workers comp | 106793-4-25",
            "discussionNote": {"noteDescription": "Standard renewal thread."}
        }
    ]
    mock_ezlynx.list_applicant_documents.return_value = [
        {
            "documentId": 555,
            "fileName": "Signed Cancellation Request LCR 106793.pdf",
            "description": "Insured signed LCR request"
        }
    ]

    gate = RenewalSafetyGate(ezlynx_client=mock_ezlynx)
    result = gate.check_cancellation_risk(applicant_id="144897143", policy_number="106793-4-25")

    assert result.is_safe is False
    assert result.risk_detected is True
    assert any(e["source"] == "document" for e in result.evidence)


def test_safety_gate_flags_non_renewal_discussion_note():
    mock_ezlynx = MagicMock()
    mock_ezlynx.get_applicant_discussions.return_value = [
        {
            "discussionId": 101,
            "title": "Account Review",
            "discussionNote": {"noteDescription": "Customer stated they do not renew this term due to selling business."}
        }
    ]
    mock_ezlynx.list_applicant_documents.return_value = []

    gate = RenewalSafetyGate(ezlynx_client=mock_ezlynx)
    result = gate.check_cancellation_risk(applicant_id="144897143", policy_number="106793-4-25")

    assert result.is_safe is False
    assert result.risk_detected is True
    assert any(e["source"] == "discussion_note" for e in result.evidence)


def test_safety_gate_passes_clean_account():
    mock_ezlynx = MagicMock()
    mock_ezlynx.get_applicant_discussions.return_value = [
        {
            "discussionId": 200,
            "title": "Renewal Manual Workers comp | 106793-4-25",
            "discussionNote": {"noteDescription": "Prior policy terms retrieved."}
        }
    ]
    mock_ezlynx.list_applicant_documents.return_value = [
        {
            "documentId": 300,
            "fileName": "2024-25 Policy Declarations.pdf",
            "description": "Declarations"
        }
    ]

    gate = RenewalSafetyGate(ezlynx_client=mock_ezlynx)
    result = gate.check_cancellation_risk(applicant_id="144897143", policy_number="106793-4-25")

    assert result.is_safe is True
    assert result.risk_detected is False
    assert result.reason is None
    assert len(result.flags) == 0
