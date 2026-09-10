"""Unit tests for EZLynxApiClient."""

import json
from unittest.mock import patch, MagicMock
import pytest
from src.ezlynx.api_client import (
    EZLynxApiClient,
    ROBIE_SIGNATURE,
    document_library_total,
    extract_document_records,
    format_document_line,
)


@pytest.fixture
def api_client():
    return EZLynxApiClient(
        base_url="https://app.ezlynx.com",
        client_id="test_client",
        client_secret="test_secret",
        username="test_user",
        password="test_password",
        app_secret="test_app_secret",
        integration_group_id="159",
    )


def test_initialization(api_client):
    assert api_client.client_id == "test_client"
    assert api_client.client_secret == "test_secret"
    assert api_client.username == "test_user"
    assert api_client.password == "test_password"
    assert api_client.app_secret == "test_app_secret"
    assert api_client.integration_group_id == "159"


@patch("requests.get")
def test_authenticate_classic_success(mock_get, api_client):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"EZToken": "mock_eztoken_abc123"}
    mock_get.return_value = mock_resp

    success = api_client.authenticate_classic(force_refresh=True)
    assert success is True
    assert api_client._classic_token == "mock_eztoken_abc123"

    headers = api_client._get_classic_headers()
    assert headers["EZToken"] == "mock_eztoken_abc123"
    assert headers["EZAppSecret"] == "test_app_secret"
    assert headers["AccountUsername"] == "test_user"


@patch("requests.get")
def test_authenticate_classic_failure(mock_get, api_client):
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.text = "Unauthorized"
    mock_get.return_value = mock_resp

    success = api_client.authenticate_classic(force_refresh=True)
    assert success is False
    assert api_client._classic_token is None


@patch("requests.post")
def test_authenticate_oauth_success(mock_post, api_client):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "access_token": "mock_oauth_jwt_token",
        "expires_in": 3600,
        "scope": "DiscussionApi PolicyApi",
    }
    mock_post.return_value = mock_resp

    success = api_client.authenticate_oauth(force_refresh=True)
    assert success is True
    assert api_client._oauth_token == "mock_oauth_jwt_token"
    assert "DiscussionApi" in api_client._oauth_scopes
    assert "PolicyApi" in api_client._oauth_scopes

    headers = api_client._get_oauth_headers()
    assert headers["Authorization"] == "Bearer mock_oauth_jwt_token"


@patch("requests.get")
def test_get_applicant(mock_get, api_client):
    api_client._classic_token = "valid_token"
    api_client._classic_token_time = 9999999999.0

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "Id": 21587333,
        "BusinessName": "Acme Landscaping LLC",
        "ApplicantType": "ActiveClient",
        "AssignedTo": "agent1",
    }
    mock_get.return_value = mock_resp

    res = api_client.get_applicant("21587333")
    assert res["status"] == "success"
    assert res["applicant"]["BusinessName"] == "Acme Landscaping LLC"
    assert res["applicant"]["AssignedTo"] == "agent1"


@patch("requests.get")
def test_get_applicant_policies(mock_get, api_client):
    api_client._classic_token = "valid_token"
    api_client._classic_token_time = 9999999999.0

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [
        {"PolicyNumber": "POL-100", "LOB": "Genl Liability", "ExpirationDate": "2026-10-15"},
        {"PolicyNumber": "POL-200", "LOB": "Commercial Auto", "ExpirationDate": "2026-11-20"},
    ]
    mock_get.return_value = mock_resp

    res = api_client.get_applicant_policies("21587333")
    assert res["status"] == "success"
    assert res["count"] == 2
    assert res["policies"][0]["PolicyNumber"] == "POL-100"


@patch("requests.get")
def test_get_completed_quote(mock_get, api_client):
    api_client._classic_token = "valid_token"
    api_client._classic_token_time = 9999999999.0

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "QuoteId": "12345",
        "ApplicantId": "21587333",
        "RatingState": "NJ",
        "QuoteResults": [
            {"CarrierName": "Travelers", "Premium": 1250.0, "Status": "Succeeded"},
            {"CarrierName": "Hartford", "Premium": 1400.0, "Status": "Succeeded"},
        ],
    }
    mock_get.return_value = mock_resp

    res = api_client.get_completed_quote("12345")
    assert res["status"] == "success"
    assert res["quote_data"]["RatingState"] == "NJ"
    assert len(res["quote_data"]["QuoteResults"]) == 2


def test_note_builder_mandates_robie_signature(api_client):
    # Test that Robie signature is strictly enforced
    raw_note = "Renewal quote requested from underwriter."
    res = api_client.add_note_to_discussion(
        applicant_id="21587333",
        discussion_title="General Liability Renewal",
        note_text=raw_note,
        use_playwright_fallback=False
    )
    assert res["status"] in ("success", "simulated")
    assert "Robie was here" in res["text"]


def test_session_overview_structure(api_client):
    with patch.object(api_client, "authenticate_classic", return_value=True), \
         patch.object(api_client, "authenticate_oauth", return_value=True):
        api_client._classic_token = "test_tok"
        api_client._oauth_token = "test_bearer"
        api_client._oauth_scopes = ["DiscussionApi", "PolicyApi"]

        overview = api_client.get_session_overview()
        assert overview["classic_api"]["authenticated"] is True
        assert overview["oauth_gateway"]["authenticated"] is True
        assert "browser_session" in overview
        assert "cdp_endpoint" in overview["browser_session"]


PRODUCTION_DOCUMENT_LIBRARY_PAYLOAD = {
    "TotalRecords": 154,
    "Documents": [
        {
            "Id": 881122,
            "Description": "2026-27 Renewal Offer - Coterie CBB-00113127-02.pdf",
            "PolicyId": 99012,
            "CreatedDate": "2026-08-01T14:22:00",
        },
        {
            "Id": 881123,
            "Description": "Loss Runs - Hartford.pdf",
            "PolicyId": 0,
            "CreatedDate": "/Date(1722513600000)/",
        },
    ],
}

# Alternate / older row shape still accepted as a fallback.
LEGACY_DOCUMENT_NAME_PAYLOAD = {
    "TotalRecords": 1,
    "Documents": [
        {
            "DocumentID": 44,
            "DocumentName": "via-documentname.pdf",
            "PolicyNumber": "R2WC681352",
            "CreatedDate": "2026-09-01",
        }
    ],
}


def test_extract_document_records_uses_documents_key_not_records():
    """Production hermes payload has TotalRecords + Documents; Records/DocumentList are absent."""
    records = extract_document_records(PRODUCTION_DOCUMENT_LIBRARY_PAYLOAD)
    assert len(records) == 2
    assert records[0]["Description"].startswith("2026-27 Renewal Offer")
    assert records[0]["Id"] == 881122
    assert records[0]["PolicyId"] == 99012
    assert document_library_total(PRODUCTION_DOCUMENT_LIBRARY_PAYLOAD, records) == 154
    assert "Records" not in PRODUCTION_DOCUMENT_LIBRARY_PAYLOAD
    assert "DocumentList" not in PRODUCTION_DOCUMENT_LIBRARY_PAYLOAD
    assert "DocumentName" not in records[0]
    assert "PolicyNumber" not in records[0]


def test_extract_document_records_supports_legacy_and_wrapped_envelopes():
    legacy = {
        "TotalRecords": 1,
        "DocumentList": [{"Id": 9, "Name": "legacy.pdf", "PolicyNumber": "POL-9"}],
    }
    assert extract_document_records(legacy)[0]["Name"] == "legacy.pdf"

    records_key = {"Records": [{"DocumentID": 3, "FileName": "via-records.pdf"}]}
    assert extract_document_records(records_key)[0]["FileName"] == "via-records.pdf"

    wrapped = {"d": {"TotalRecords": 2, "Documents": [{"documentName": "nested.pdf"}]}}
    assert extract_document_records(wrapped)[0]["documentName"] == "nested.pdf"

    bare_list = [{"title": "bare.pdf"}]
    assert extract_document_records(bare_list)[0]["title"] == "bare.pdf"


def test_format_document_line_uses_description_id_and_policy_id():
    """Live API filename is Description; association is PolicyId, not PolicyNumber."""
    line = format_document_line(PRODUCTION_DOCUMENT_LIBRARY_PAYLOAD["Documents"][0])
    assert "Name: 2026-27 Renewal Offer - Coterie CBB-00113127-02.pdf" in line
    assert "ID: 881122" in line
    assert "PolicyId: 99012" in line
    assert "Uploaded: 2026-08-01" in line

    unassociated = format_document_line(PRODUCTION_DOCUMENT_LIBRARY_PAYLOAD["Documents"][1])
    assert "Name: Loss Runs - Hartford.pdf" in unassociated
    assert "Policy: —" in unassociated
    assert "PolicyId:" not in unassociated
    assert "Uploaded: 2024-08-01" in unassociated


def test_format_document_line_falls_back_to_document_name_and_policy_number():
    line = format_document_line(LEGACY_DOCUMENT_NAME_PAYLOAD["Documents"][0])
    assert "Name: via-documentname.pdf" in line
    assert "Policy: R2WC681352" in line


def test_format_document_line_prefers_description_over_document_name():
    line = format_document_line(
        {
            "Id": 7,
            "Description": "live-filename.pdf",
            "DocumentName": "should-not-win.pdf",
            "PolicyId": 55,
            "PolicyNumber": "POL-55",
        }
    )
    assert "Name: live-filename.pdf" in line
    assert "should-not-win.pdf" not in line
    assert "Policy: POL-55" in line
    assert "PolicyId: 55" in line


@patch("requests.get")
def test_list_applicant_documents_wraps_classic_payload(mock_get, api_client):
    api_client._classic_token = "valid_token"
    api_client._classic_token_time = 9999999999.0

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = PRODUCTION_DOCUMENT_LIBRARY_PAYLOAD
    mock_get.return_value = mock_resp

    res = api_client.list_applicant_documents("151445306", page_index=1, page_size=20)
    assert res["status"] == "success"
    assert res["data"]["TotalRecords"] == 154
    docs = extract_document_records(res["data"])
    assert docs[0]["Description"].endswith(".pdf")
    mock_get.assert_called_once()
    assert "/documentlibrary/list/151445306/1/20/0" in mock_get.call_args[0][0]


def test_find_matching_discussion_matches_renamed_card(api_client):
    """Verifies that an existing card renamed by CSR (e.g. adding cancellation prefix) is still matched by policy#."""
    mock_discussions = [
        {
            "discussionId": 771,
            "title": "INSURED CANCELLATION REQUEST: Renewal Workers comp | 106793-4-25",
            "lastModifiedByName": "Eimy Ramos",
            "noteCount": 3,
            "discussionNote": {"noteDescription": "Insured sent signed LCR form.", "policyNumber": "106793-4-25"}
        },
        {
            "discussionId": 772,
            "title": "Loss Runs - Travelers",
            "lastModifiedByName": "Agent",
            "noteCount": 1
        }
    ]
    with patch.object(api_client, "get_applicant_discussions", return_value=mock_discussions):
        matched = api_client.find_matching_discussion(
            applicant_id="144897143",
            policy_number="106793-4-25",
            line_of_business="Workers comp",
            carrier_name="NJCRIB"
        )
        assert matched is not None
        assert matched["discussionId"] == 771
        assert "INSURED CANCELLATION REQUEST" in matched["title"]


def test_create_user_task_fallback(api_client):
    """Verifies create_user_task falls back to adding an audit note when playwright is disabled or fails."""
    with patch.object(api_client, "resolve_discussion_title", return_value="Renewal Manual Workers comp | 106793-4-25 NJCRIB"):
        with patch.object(api_client, "add_note_to_discussion", return_value={"status": "success"}) as mock_add_note:
            res = api_client.create_user_task(
                applicant_id="144897143",
                title="URGENT: Review Cancellation Signal",
                description="Signed cancellation request uploaded.",
                assigned_user="Eimy Ramos",
                policy_number="106793-4-25",
                use_playwright=False
            )
            assert res["status"] == "success"
            assert res["method"] == "note_fallback"
            assert res["assigned_user"] == "Eimy Ramos"
            mock_add_note.assert_called_once()
            call_kwargs = mock_add_note.call_args[1]
            assert "URGENT: Review Cancellation Signal" in call_kwargs["note_text"]
            assert "Robie was here" in call_kwargs["note_text"]
