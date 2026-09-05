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
def test_bland_payload_includes_warm_transfer_fields(mock_post, monkeypatch):
    mock_post.return_value = _ok_response()
    client = CarrierVoiceClient(api_key="test-key", provider="bland_ai")
    dossier = _sample_dossier(
        client_first_name="Maria",
        producer_name="Jake Ferrara",
        producer_phone="+17326688161",
        transfer_mode="warm",
        call_type="client_followup",
    )

    result = client.dispatch_call(dossier)

    assert result["success"] is True
    payload = mock_post.call_args.kwargs["json"]
    assert payload["from"] == "+17322986745"
    assert "max_duration" not in payload
    assert payload["transfer_phone_number"] == "+17326688161"
    assert payload["transfer_list"]["default"] == "+17326688161"
    assert payload["transfer_list"]["producer"] == "+17326688161"
    assert payload["webhook_events"] == ["post_transfer_transcript"]
    assert payload["first_sentence"].startswith("Hi Maria, this is Robie from StreetSmart")
    assert "Jake Ferrara" in payload["first_sentence"]
    assert payload["metadata"]["producer_name"] == "Jake Ferrara"
    assert payload["metadata"]["call_type"] == "client_followup"


@patch("src.voice.voice_client.requests.post")
def test_bland_payload_omits_transfer_without_producer_phone(mock_post):
    mock_post.return_value = _ok_response()
    client = CarrierVoiceClient(api_key="test-key", provider="bland_ai")

    client.dispatch_call(_sample_dossier())

    payload = mock_post.call_args.kwargs["json"]
    assert "transfer_phone_number" not in payload
    assert "transfer_list" not in payload
    assert payload["from"] == "+17322986745"


def test_client_followup_prompt_uses_first_name_and_consent_gated_transfer():
    client = CarrierVoiceClient(api_key="test-key")
    dossier = _sample_dossier(
        client_first_name="Maria",
        producer_name="Jake Ferrara",
        producer_phone="+17326688161",
        call_type="client_followup",
        transfer_mode="warm",
    )
    prompt = client.build_call_prompt(dossier)
    first = client.build_client_first_sentence(dossier)
    assert "Hi Maria, this is Robie from StreetSmart" in first
    assert "Jake Ferrara" in first
    assert "Are you free to discuss it?" in first
    assert "Hi Maria" in prompt
    assert "Jake Ferrara" in prompt
    assert "only transfer if they clearly agree" in prompt.lower()
    assert "transfer" in prompt.lower()


def test_carrier_prompt_offers_transfer_after_live_human():
    client = CarrierVoiceClient(api_key="test-key")
    dossier = _sample_dossier(
        producer_name="Carlo Ferrara",
        producer_phone="+17329953409",
        call_type="carrier",
        transfer_mode="warm",
    )
    prompt = client.build_call_prompt(dossier)
    briefing = client.build_transfer_briefing(dossier)
    assert "Carlo Ferrara" in prompt
    assert "live human" in prompt.lower()
    assert "transfer" in prompt.lower()
    assert "Carlo Ferrara" in briefing
    assert dossier.insured_name in briefing
    assert dossier.carrier_name in briefing


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


@patch("src.voice.voice_client.requests.get")
def test_get_call_fetches_bland_details(mock_get):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b"{}"
    mock_resp.json.return_value = {
        "call_id": "call_abc",
        "recording_url": "https://api.bland.ai/recordings/call_abc.mp3",
        "concatenated_transcript": "hello",
        "summary": "done",
    }
    mock_get.return_value = mock_resp
    client = CarrierVoiceClient(api_key="test-key", provider="bland_ai")
    data = client.get_call("call_abc")
    assert data["recording_url"].endswith(".mp3")
    assert data["concatenated_transcript"] == "hello"
    assert mock_get.call_args[0][0] == "https://api.bland.ai/v1/calls/call_abc"
    assert "max_duration" not in (mock_get.call_args.kwargs.get("json") or {})


def test_get_call_skips_without_api_key():
    client = CarrierVoiceClient(api_key=None, provider="bland_ai")
    assert client.get_call("call_abc") == {}
