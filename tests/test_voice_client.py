"""Unit tests for CarrierVoiceClient Bland/Retell dispatch payloads."""

from unittest.mock import MagicMock, patch

from src.voice.context_hydrator import CallingDossier
from src.voice.voice_client import (
    DEFAULT_VOICE_MAX_DURATION_SECONDS,
    CarrierVoiceClient,
)


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
def test_bland_payload_includes_default_max_duration(mock_post, monkeypatch):
    monkeypatch.delenv("VOICE_MAX_DURATION_SECONDS", raising=False)
    monkeypatch.setattr(
        "src.voice.voice_client.settings",
        type("S", (), {"voice_max_duration_seconds": 180})(),
    )
    mock_post.return_value = _ok_response()
    client = CarrierVoiceClient(api_key="test-key", provider="bland_ai")

    result = client.dispatch_call(_sample_dossier())

    assert result["success"] is True
    mock_post.assert_called_once()
    payload = mock_post.call_args.kwargs["json"]
    assert "max_duration" in payload
    # Bland documents max_duration in minutes; 180 seconds → 3 minutes.
    assert payload["max_duration"] == 3
    assert DEFAULT_VOICE_MAX_DURATION_SECONDS == 180
    assert client.max_duration_seconds == 180


@patch("src.voice.voice_client.requests.post")
def test_bland_payload_respects_env_max_duration(mock_post, monkeypatch):
    monkeypatch.setenv("VOICE_MAX_DURATION_SECONDS", "300")
    mock_post.return_value = _ok_response()
    client = CarrierVoiceClient(api_key="test-key", provider="bland_ai")

    client.dispatch_call(_sample_dossier())

    payload = mock_post.call_args.kwargs["json"]
    assert payload["max_duration"] == 5  # 300 seconds → 5 minutes
    assert client.max_duration_seconds == 300


@patch("src.voice.voice_client.requests.post")
def test_retell_payload_includes_max_call_duration(mock_post, monkeypatch):
    monkeypatch.delenv("VOICE_MAX_DURATION_SECONDS", raising=False)
    monkeypatch.setattr(
        "src.voice.voice_client.settings",
        type("S", (), {"voice_max_duration_seconds": 180})(),
    )
    mock_post.return_value = _ok_response()
    client = CarrierVoiceClient(api_key="test-key", provider="retell")

    result = client.dispatch_call(_sample_dossier())

    assert result["success"] is True
    payload = mock_post.call_args.kwargs["json"]
    assert payload["agent_override"]["agent"]["max_call_duration_ms"] == 180_000
