"""Unit tests for EZLynx Audit Verification module."""

from datetime import date
from unittest.mock import MagicMock
import pytest

from src.ezlynx.audit_verification import (
    AuditDocumentClassifier,
    CarrierChannelMatcher,
    AuditNoteBuilder,
    AuditEmailBuilder,
    AuditVerifier,
    ROBIE_SIGNATURE,
)


def test_audit_document_classifier():
    mock_payload = {
        "status": "success",
        "data": {
            "Documents": [
                {
                    "Id": "doc1",
                    "Description": "Audits",
                    "PolicyId": 0,
                    "DateTimeCreated": "2026-08-01T10:00:00"
                },
                {
                    "Id": "doc2",
                    "Description": "PRMAU: Premium Audit 13WBCAT1G7C WORK Effective 2025-07-21",
                    "PolicyId": "pol123",
                    "DateTimeCreated": "2026-08-12T09:31:56"
                },
                {
                    "Id": "doc3",
                    "Description": "PRMAU: Premium Audit 13WBCAT1G7C WORK Effective 2024-07-21",
                    "PolicyId": "pol123",
                    "DateTimeCreated": "2025-07-25T16:45:44"
                },
                {
                    "Id": "doc4",
                    "Description": "Voice Mail from the Auditor.mp3",
                    "PolicyId": 0,
                    "DateTimeCreated": "2026-08-15T11:00:00"
                },
                {
                    "Id": "doc5",
                    "Description": "WC Manual Policy Cancellation - Reason: Audit NonCompliance",
                    "PolicyId": 0,
                    "DateTimeCreated": "2026-08-16T12:00:00"
                },
                {
                    "Id": "doc6",
                    "Description": "Workers Comp Audit Request Form.pdf",
                    "PolicyId": 0,
                    "DateTimeCreated": "2026-08-05T09:00:00"
                }
            ]
        }
    }

    res = AuditDocumentClassifier.classify(mock_payload, eff_date_str="2026-07-21", target_policy_number="13WBCAT1G7C")

    assert len(res["current_statements"]) == 1
    assert res["current_statements"][0]["filename"] == "PRMAU: Premium Audit 13WBCAT1G7C WORK Effective 2025-07-21"

    assert len(res["historical_statements"]) == 1
    assert res["historical_statements"][0]["filename"] == "PRMAU: Premium Audit 13WBCAT1G7C WORK Effective 2024-07-21"

    assert len(res["voicemails"]) == 1
    assert res["voicemails"][0]["filename"] == "Voice Mail from the Auditor.mp3"

    assert len(res["non_compliance"]) == 1
    assert "Audit NonCompliance" in res["non_compliance"][0]["filename"]

    assert len(res["requests"]) == 1
    assert "Audit Request Form" in res["requests"][0]["filename"]


def test_carrier_channel_matcher():
    # Assigned Risk tests
    res_pma = CarrierChannelMatcher.match("NJCRIB - Pennsylvania Lumbermans Assigned Risk")
    assert res_pma["channel"] == "EMAIL_AND_PHONE"
    assert "policyservices@ormarks.com" in res_pma["underwriter_emails"]
    assert res_pma.get("phone") == "800-752-1895"

    res_njm = CarrierChannelMatcher.match("NJCRIB - New Jersey Manufacturers Assigned Risk")
    assert res_njm["channel"] == "EMAIL_AND_PHONE"
    assert "wcumail@njm.com" in res_njm["underwriter_emails"]
    assert res_njm.get("phone") == "800-232-6600"

    res_arwc = CarrierChannelMatcher.match("NJCRIB - Hartford Assigned Risk")
    assert res_arwc["channel"] == "EMAIL"
    assert "assignedrisk@hartford.com" in res_arwc["underwriter_emails"]

    # Portal tests
    res_hartford = CarrierChannelMatcher.match("The Hartford")
    assert res_hartford["channel"] == "PORTAL"
    assert "ebc.thehartford.com" in res_hartford["portal_url"]

    res_pie = CarrierChannelMatcher.match("Pie Insurance")
    assert res_pie["channel"] == "PORTAL"
    assert "portal.pieinsurance.com" in res_pie["portal_url"]

    res_travelers = CarrierChannelMatcher.match("Travelers")
    assert res_travelers["channel"] == "PORTAL"

    res_nysif = CarrierChannelMatcher.match("NYSIF")
    assert res_nysif["channel"] == "EMAIL_AND_PHONE"
    assert "CustomerService@nysif.com" in res_nysif["underwriter_emails"]
    assert res_nysif.get("phone") == "888-875-5790"


def test_audit_note_builder_signature():
    note = AuditNoteBuilder.build(
        policy_number="POL-999",
        carrier_name="The Hartford",
        lob="Workers comp",
        term_dates="2026-07-21 to 2027-07-21",
        csr_name="Eimy Ramos",
        status_summary="Statement retrieved",
        actions_taken=["Checked library", "Drafted delivery"],
        next_steps="Send email",
        follow_up_date="2026-09-06"
    )
    assert note.strip().endswith(ROBIE_SIGNATURE)
    assert "ROBIE was here" in note
    assert "Policy: #POL-999" in note


def test_audit_email_builder():
    delivery = AuditEmailBuilder.build_client_delivery(
        client_name="Acme Corp",
        contact_name="John Doe",
        policy_number="POL-123",
        carrier_name="The Hartford",
        audit_file_name="Final Audit.pdf",
        csr_name="Eimy Ramos",
        term_dates="2026-07-21 to 2027-07-21"
    )
    assert delivery["recipient"] == "John Doe"
    assert "Hello John," in delivery["body"]
    assert "Final Audit.pdf" in delivery["body"]
    assert delivery["template_name"] == "Audit (StreetSmart SOP Built-in)"

    outreach = AuditEmailBuilder.build_carrier_outreach(
        carrier_name="PMA / Ormarks",
        client_name="Acme Corp",
        policy_number="POL-123",
        term_dates="2025-07-21 to 2026-07-21",
        recipient_email="policyservices@ormarks.com",
        csr_name="Eimy Ramos"
    )
    assert outreach["recipient"] == "policyservices@ormarks.com"
    assert "Final Payroll Audit Request" in outreach["subject"]


def test_audit_verifier_process_account():
    mock_client = MagicMock()
    mock_client.get_applicant.return_value = {
        "status": "success",
        "applicant": {
            "ContactName": "Marisa Ullrich",
            "BusinessEmail": "stella@example.com"
        }
    }
    mock_client.list_applicant_documents.return_value = {
        "status": "success",
        "data": {
            "Documents": [
                {
                    "Id": "doc10",
                    "Description": "PRMAU: Premium Audit 13WBCAT1G7C WORK Effective 2025-07-21",
                    "PolicyId": "pol123",
                    "DateTimeCreated": "2026-08-12T09:31:56"
                }
            ]
        }
    }

    verifier = AuditVerifier(mock_client)
    row = {
        "Applicant ID": "112237823",
        "Account Name": "Advanced Hair Designs Llc",
        "Policy Number": "13WBCAT1G7C",
        "Master Company": "The Hartford",
        "Effective Date": "2026-07-21",
        "Expiration Date": "2027-07-21",
        "Line of Business": "Workers comp",
        "CSR": "Eimy Ramos",
        "Assigned Producer": "Sandy Santana"
    }

    res = verifier.process_account(row)
    assert res["state"] == "completed"
    assert res["discussion_title"] == "Audit"
    assert res["client_email_draft"] is not None
    assert "Hello Marisa," in res["client_email_draft"]["body"]
    assert res["draft_note"].strip().endswith(ROBIE_SIGNATURE)


def test_audit_voice_dispatcher():
    from src.ezlynx.audit_verification import AuditVoiceDispatcher

    client_prompt = AuditVoiceDispatcher.build_client_autodial_prompt(
        client_name="Acme Corp",
        contact_name="John Doe",
        policy_number="POL-123",
        carrier_name="The Hartford",
        client_email="john@acme.com"
    )
    assert "Hello John" in client_prompt
    assert "Workers' Compensation policy #POL-123" in client_prompt
    assert "john@acme.com" in client_prompt
    assert "732-298-6745" in client_prompt

    carrier_prompt = AuditVoiceDispatcher.build_carrier_call_prompt(
        carrier_name="PMA",
        client_name="Acme Corp",
        policy_number="POL-123",
        term_dates="2025-07-21 to 2026-07-21",
        agency_code="SS1234"
    )
    assert "StreetSmart Insurance" in carrier_prompt
    assert "Acme Corp" in carrier_prompt
    assert "POL-123" in carrier_prompt
    assert "SS1234" in carrier_prompt


def test_audit_verifier_rejects_non_auditable_lob():
    mock_client = MagicMock()
    verifier = AuditVerifier(mock_client)

    # Test Commercial Auto rejection (e.g. Kuzy Trucking case)
    kuzy_row = {
        "Applicant ID": "194416437",
        "Account Name": "KUZY TRUCKING LLC",
        "Policy Number": "9300186932",
        "Master Company": "GEICO",
        "Effective Date": "2025-09-02",
        "Expiration Date": "2026-09-02",
        "Line of Business": "Auto (Commercial)",
        "CSR": "Ricardo Aguilar",
        "Assigned Producer": "Ricardo Aguilar"
    }
    result = verifier.process_account(kuzy_row)
    assert result["state"] == "rejected_non_auditable_lob"
    assert result["status"] == "REJECTED_NON_AUDITABLE_LOB"
    assert "not subject to annual payroll audit" in result["error"]
    # Ensure client API was not called
    mock_client.get_applicant.assert_not_called()


def test_audit_eligibility_gate():
    from src.intake.safety_gate import AuditEligibilityGate

    # Eligible
    ok, err = AuditEligibilityGate.is_eligible_for_audit("Workers comp")
    assert ok is True
    assert err is None

    ok, err = AuditEligibilityGate.is_eligible_for_audit("WORK")
    assert ok is True

    # Ineligible
    ok, err = AuditEligibilityGate.is_eligible_for_audit("Commercial Auto")
    assert ok is False
    assert "not subject to payroll audit" in err

    ok, err = AuditEligibilityGate.is_eligible_for_audit("Homeowners")
    assert ok is False

    ok, err = AuditEligibilityGate.is_eligible_for_audit("BOP")
    assert ok is False

