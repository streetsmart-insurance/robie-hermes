"""Unit tests for EZLynxApiClient."""

import json
from unittest.mock import patch, MagicMock
import pytest
from src.ezlynx.api_client import EZLynxApiClient, ROBIE_SIGNATURE


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
