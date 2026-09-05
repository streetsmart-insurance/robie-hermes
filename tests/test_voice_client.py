"""Unit tests for CarrierVoiceClient Bland/Retell dispatch payloads."""

from unittest.mock import MagicMock, patch

from src.voice.context_hydrator import CallingDossier
from src.voice.voice_client import CarrierVoiceClient


def _sample_dossier(**overrides) -> CallingDossier:
    data = {
        "policy_number": "PWC1239278",
        "insured_name": "Yes We Do LLC",
        "carrier_name": "Associated Specialty Insurance Agency MGA",
        "line_of_business": "Workers comp",
        "carrier_phone": "866-513-5650",
    }
    data.update(overrides)
    return CallingDossier(**data)


def _ok_response(call_id: str = "call_test_123") -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"call_id": call_id}
    return mock_resp


@patch("src.voice.voice_client.requests.post")
def test_bland_payload_omits_max_duration(mock_post, monkeypatch):
    """Carrier holds must not be cut off by a Bland max_duration cap."""
    monkeypatch.delenv("VOICE_MAX_DURATION_SECONDS", raising=False)
    mock_post.return_value = _ok_response()
    client = CarrierVoiceClient(api_key="test-key", provider="bland_ai")

    result = client.dispatch_call(_sample_dossier())

    assert result["success"] is True
    mock_post.assert_called_once()
    payload = mock_post.call_args.kwargs["json"]
    assert "max_duration" not in payload
    assert not hasattr(client, "max_duration_seconds")
    # Voice features unrelated to duration stay in the send-call payload.
    assert payload["voicemail_action"] == "leave_message"
    assert payload["ivr_navigation"] is True
    assert payload["answered_by_enabled"] is True
    assert payload["wait_for_greeting"] is True


@patch("src.voice.voice_client.requests.post")
def test_bland_payload_ignores_legacy_max_duration_env(mock_post, monkeypatch):
    monkeypatch.setenv("VOICE_MAX_DURATION_SECONDS", "300")
    mock_post.return_value = _ok_response()
    client = CarrierVoiceClient(api_key="test-key", provider="bland_ai")

    client.dispatch_call(_sample_dossier())

    payload = mock_post.call_args.kwargs["json"]
    assert "max_duration" not in payload
    assert not hasattr(client, "max_duration_seconds")


@patch("src.voice.voice_client.requests.post")
def test_retell_payload_omits_call_duration_override(mock_post, monkeypatch):
    monkeypatch.delenv("VOICE_MAX_DURATION_SECONDS", raising=False)
    mock_post.return_value = _ok_response()
    client = CarrierVoiceClient(api_key="test-key", provider="retell")

    result = client.dispatch_call(_sample_dossier())

    assert result["success"] is True
    payload = mock_post.call_args.kwargs["json"]
    assert "agent_override" not in payload
    assert "max_call_duration_ms" not in payload
