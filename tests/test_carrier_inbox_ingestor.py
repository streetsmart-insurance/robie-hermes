import base64
import pytest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

from src.email_outreach.carrier_inbox_ingestor import (
    CarrierInboxIngestor,
    _clean_str,
    compute_sha256,
)


def test_clean_str():
    assert _clean_str("JCF Masonry, LLC / Inc.") == "JCF_Masonry__LLC___Inc."
    assert _clean_str("WC-PI-2629746-000") == "WC-PI-2629746-000"


def test_compute_sha256():
    data = b"Hello StreetSmart"
    assert len(compute_sha256(data)) == 64


def test_compile_correspondence_pdf(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.email_outreach.carrier_inbox_ingestor.CARRIER_EMAILS_DIR",
        tmp_path
    )
    ingestor = CarrierInboxIngestor(
        gmail_client=MagicMock(),
        ezlynx_client=MagicMock(),
        cdp_url="http://mock:9222"
    )

    pdf_path = ingestor.compile_correspondence_pdf(
        subject="RE: Audit Documents Required",
        sender="underwriting@asiaworkerscomp.com",
        recipients="robie@streetsmart.insurance",
        date_str="2026-09-08 07:13:00 EDT",
        body_text="Debbie handles this account and will assist you.",
        applicant_name="JCF Masonry LLC",
        policy_number="WCPI2629746-000",
        carrier_name="Associated Specialty",
        message_id="test_msg_123"
    )

    assert pdf_path.exists()
    assert pdf_path.stat().st_size > 500
    with open(pdf_path, "rb") as f:
        header = f.read(5)
        assert header == b"%PDF-"


def test_save_raw_eml(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.email_outreach.carrier_inbox_ingestor.CARRIER_EMAILS_DIR",
        tmp_path
    )
    mock_gmail = MagicMock()
    mock_service = MagicMock()
    mock_gmail.service = mock_service

    raw_payload = b"From: carrier@example.com\r\nSubject: Test\r\n\r\nHello World"
    b64_raw = base64.urlsafe_b64encode(raw_payload).decode("utf-8")
    mock_service.users().messages().get().execute.return_value = {"raw": b64_raw}

    ingestor = CarrierInboxIngestor(gmail_client=mock_gmail, ezlynx_client=MagicMock())
    eml_path = ingestor.save_raw_eml(
        message_id="msg_456",
        applicant_name="Test Applicant",
        policy_number="POL123"
    )

    assert eml_path.exists()
    assert eml_path.read_bytes() == raw_payload


def test_verify_document_in_ezlynx():
    mock_ezlynx = MagicMock()
    mock_ezlynx.list_applicant_documents.return_value = {
        "status": "success",
        "data": {
            "TotalRecords": 1,
            "Documents": [
                {
                    "Id": "ez_doc_999",
                    "Description": "JCF Masonry LLC - Associated Specialty Audit Correspondence.pdf",
                    "PolicyId": "pol_id_111"
                }
            ]
        }
    }

    ingestor = CarrierInboxIngestor(gmail_client=MagicMock(), ezlynx_client=mock_ezlynx)
    result = ingestor.verify_document_in_ezlynx(
        applicant_id="187626597",
        target_doc_title="JCF Masonry LLC - Associated Specialty Audit Correspondence"
    )

    assert result is not None
    assert result["document_id"] == "ez_doc_999"
    assert result["policy_id"] == "pol_id_111"


@pytest.mark.asyncio
async def test_process_and_archive_carrier_message(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.email_outreach.carrier_inbox_ingestor.CARRIER_EMAILS_DIR",
        tmp_path
    )
    mock_gmail = MagicMock()
    mock_service = MagicMock()
    mock_gmail.service = mock_service

    raw_payload = b"From: carrier@example.com\r\nSubject: Test\r\n\r\nBody text"
    b64_raw = base64.urlsafe_b64encode(raw_payload).decode("utf-8")
    mock_service.users().messages().get().execute.return_value = {"raw": b64_raw}

    mock_ezlynx = MagicMock()
    mock_ezlynx.list_applicant_documents.return_value = {
        "status": "success",
        "data": {
            "TotalRecords": 1,
            "Documents": [
                {
                    "Id": "verified_doc_42",
                    "Description": "Acme Inc - Carrier Audit Correspondence.pdf",
                    "PolicyId": "pol_42"
                }
            ]
        }
    }
    mock_ezlynx.add_note_to_discussion.return_value = {"status": "success", "note_id": "1123499"}

    ingestor = CarrierInboxIngestor(gmail_client=mock_gmail, ezlynx_client=mock_ezlynx)

    with patch.object(ingestor, "upload_document_to_ezlynx", new_callable=AsyncMock) as mock_upload:
        mock_upload.return_value = {
            "success": True,
            "document_name": "Acme Inc - Carrier Audit Correspondence.pdf",
            "policy_number": "POL-999"
        }

        res = await ingestor.process_and_archive_carrier_message(
            message_id="msg_test_888",
            applicant_id="12345",
            applicant_name="Acme Inc",
            policy_number="POL-999",
            carrier_name="Carrier",
            subject="Audit Request",
            sender="carrier@example.com",
            body_text="Please find audit instructions attached.",
            doc_type="audit correspondence"
        )

        assert res["status"] == "success"
        assert res["verified"]["document_id"] == "verified_doc_42"
        assert res["note"]["note_id"] == "1123499"
        # Verify discussion note included ROBIE was here and doc ID
        call_args = mock_ezlynx.add_note_to_discussion.call_args[1]
        assert "ROBIE was here" in call_args["note_text"]
        assert "verified_doc_42" in call_args["note_text"]
