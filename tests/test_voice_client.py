"""Unit tests for CarrierVoiceClient Bland/Retell dispatch payloads."""

from unittest.mock import MagicMock, patch

from src.voice.context_hydrator import CallingDossier
from src.voice.outreach_pathways import CANCELLATION_GENERIC_BODY, PATHWAY_CANCELLATION
from src.voice.voice_client import (
    AGENCY_MAIN_CALLBACK_DISPLAY,
    AGENCY_MAIN_CALLBACK_E164,
    AGENCY_MAIN_CALLBACK_SPOKEN,
    WARM_TRANSFER_CLIENT_HANDOFF_LINE,
    CarrierVoiceClient,
    warm_transfer_timing_rules,
)

# Jake's personal/DID — must never be invented as the client_followup callback.
JAKE_PERSONAL_DID = "+17324812520"
JAKE_PERSONAL_DISPLAY = "732-481-2520"


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


def _assert_agency_callback_not_jake(text: str) -> None:
    """Prompt/voicemail must cite agency main 732-462-8343, never Jake's DID."""
    has_display = AGENCY_MAIN_CALLBACK_DISPLAY in text
    has_e164 = AGENCY_MAIN_CALLBACK_E164 in text
    has_spoken = AGENCY_MAIN_CALLBACK_SPOKEN in text.lower()
    assert has_display or has_e164 or has_spoken, (
        f"expected agency callback {AGENCY_MAIN_CALLBACK_DISPLAY} "
        f"(or {AGENCY_MAIN_CALLBACK_E164} / spoken form) in:\n{text}"
    )
    assert JAKE_PERSONAL_DID not in text
    assert JAKE_PERSONAL_DISPLAY not in text
    assert "481-2520" not in text
    assert "4812520" not in text


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
        requestor_name="Mike Sosa",
        requestor_phone="+17326540947",
        transfer_mode="warm",
        call_type="client_followup",
    )

    result = client.dispatch_call(dossier)

    assert result["success"] is True
    payload = mock_post.call_args.kwargs["json"]
    assert payload["from"] == "+17322986745"
    assert "max_duration" not in payload
    assert payload["transfer_phone_number"] == "+17326540947"
    assert payload["transfer_list"]["default"] == "+17326540947"
    assert payload["transfer_list"]["requestor"] == "+17326540947"
    assert payload["webhook_events"] == ["post_transfer_transcript"]
    assert payload["first_sentence"].startswith("Hi Maria, this is Robie from StreetSmart")
    assert "Jake Ferrara" in payload["first_sentence"]
    assert payload["metadata"]["producer_name"] == "Jake Ferrara"
    assert payload["metadata"]["requestor_name"] == "Mike Sosa"
    assert payload["metadata"]["requestor_phone"] == "+17326540947"
    assert payload["metadata"]["call_type"] == "client_followup"
    _assert_agency_callback_not_jake(payload["voicemail_message"])
    _assert_agency_callback_not_jake(payload["task"])
    assert JAKE_PERSONAL_DID not in payload["voicemail_message"]


@patch("src.voice.voice_client.requests.post")
def test_bland_payload_omits_transfer_without_requestor_phone(mock_post):
    mock_post.return_value = _ok_response()
    client = CarrierVoiceClient(api_key="test-key", provider="bland_ai")

    client.dispatch_call(
        _sample_dossier(
            producer_name="Jake Ferrara",
            producer_phone="+17324812520",
        )
    )

    payload = mock_post.call_args.kwargs["json"]
    assert "transfer_phone_number" not in payload
    assert "transfer_list" not in payload
    assert payload["from"] == "+17322986745"


def test_client_followup_prompt_uses_first_name_and_consent_gated_transfer():
    client = CarrierVoiceClient(api_key="test-key")
    dossier = _sample_dossier(
        client_first_name="Maria",
        producer_name="Jake Ferrara",
        requestor_name="Mike Sosa",
        requestor_phone="+17326540947",
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
    assert "Mike Sosa" in prompt
    assert "+17326540947" in prompt
    assert "only transfer if they clearly agree" in prompt.lower()
    assert "transfer" in prompt.lower()
    _assert_agency_callback_not_jake(prompt)


def test_client_followup_voicemail_uses_agency_main_callback_not_jake_did():
    client = CarrierVoiceClient(api_key="test-key")
    dossier = _sample_dossier(
        client_first_name="Maria",
        producer_name="Jake Ferrara",
        requestor_name="Mike Sosa",
        requestor_phone="+17326540947",
        call_type="client_followup",
        transfer_mode="warm",
    )
    prompt = client.build_call_prompt(dossier)
    voicemail = client._voicemail_message(dossier)
    transfer_block = client._transfer_objective_block(dossier)

    _assert_agency_callback_not_jake(prompt)
    _assert_agency_callback_not_jake(voicemail)
    _assert_agency_callback_not_jake(transfer_block)
    assert "do not transfer" in prompt.lower()
    assert "voicemail" in prompt.lower()
    assert AGENCY_MAIN_CALLBACK_DISPLAY in voicemail
    assert AGENCY_MAIN_CALLBACK_SPOKEN in voicemail
    assert "reply to your email" not in voicemail.lower()
    assert "call StreetSmart or reply" not in prompt


def test_client_followup_voicemail_callback_without_requestor_phone():
    client = CarrierVoiceClient(api_key="test-key")
    dossier = _sample_dossier(
        client_first_name="Maria",
        producer_name="Jake Ferrara",
        call_type="client_followup",
    )
    prompt = client.build_call_prompt(dossier)
    voicemail = client._voicemail_message(dossier)
    _assert_agency_callback_not_jake(prompt)
    _assert_agency_callback_not_jake(voicemail)
    assert "do not transfer this call" in prompt.lower()


def test_client_outreach_prompt_greets_first_name_without_sales_producer():
    client = CarrierVoiceClient(api_key="test-key")
    dossier = _sample_dossier(
        client_first_name="Buster",
        producer_name="Jake Ferrara",
        requestor_name="Mike Sosa",
        requestor_phone="+17326540947",
        assigned_producer_name="Carlo Ferrara",
        assigned_producer_phone="+17324622360",
        call_type="client_outreach",
        transfer_mode="warm",
        custom_instructions="Policy is pending cancellation — please confirm they want to keep coverage.",
    )
    prompt = client.build_call_prompt(dossier)
    first = client.build_client_first_sentence(dossier)
    briefing = client.build_transfer_briefing(dossier)
    assert first.startswith("Hi Buster, this is Robie from StreetSmart")
    assert "quote" not in first.lower()
    assert "Carlo Ferrara" not in first
    assert "Jake Ferrara" not in first
    assert "put together" not in prompt.lower()
    assert "client outreach" in prompt.lower()
    assert "pending cancellation" in prompt or "cancelled" in first.lower()
    assert "connect you to Carlo now" in first
    assert "+17324622360" in prompt
    assert "only transfer if they clearly agree" in prompt.lower()
    assert "Jake Ferrara" not in briefing
    assert "put together" not in briefing.lower()
    _assert_agency_callback_not_jake(prompt)


def test_client_outreach_voicemail_uses_agency_main_not_jake_did():
    client = CarrierVoiceClient(api_key="test-key")
    dossier = _sample_dossier(
        client_first_name="Buster",
        producer_name="Jake Ferrara",
        requestor_name="Mike Sosa",
        requestor_phone="+17326540947",
        call_type="client_outreach",
        transfer_mode="warm",
    )
    prompt = client.build_call_prompt(dossier)
    voicemail = client._voicemail_message(dossier)
    transfer_block = client._transfer_objective_block(dossier)
    _assert_agency_callback_not_jake(prompt)
    _assert_agency_callback_not_jake(voicemail)
    _assert_agency_callback_not_jake(transfer_block)
    assert AGENCY_MAIN_CALLBACK_DISPLAY in voicemail
    assert AGENCY_MAIN_CALLBACK_SPOKEN in voicemail
    assert "put together" not in voicemail.lower()
    assert JAKE_PERSONAL_DID not in voicemail
    assert "do not transfer" in prompt.lower()


@patch("src.voice.voice_client.requests.post")
def test_bland_client_outreach_payload_transfers_to_assigned_producer(mock_post):
    mock_post.return_value = _ok_response()
    client = CarrierVoiceClient(api_key="test-key", provider="bland_ai")
    dossier = _sample_dossier(
        client_first_name="Buster",
        producer_name="Jake Ferrara",
        requestor_name="Mike Sosa",
        requestor_phone="+17326540947",
        assigned_producer_name="Carlo Ferrara",
        assigned_producer_phone="+17324622360",
        transfer_mode="warm",
        call_type="client_outreach",
    )
    result = client.dispatch_call(dossier)
    assert result["success"] is True
    payload = mock_post.call_args.kwargs["json"]
    assert payload["from"] == "+17322986745"
    assert "max_duration" not in payload
    assert payload["transfer_phone_number"] == "+17324622360"
    assert payload["transfer_list"]["assigned_producer"] == "+17324622360"
    assert "requestor" not in payload["transfer_list"]
    assert payload["metadata"]["call_type"] == "client_outreach"
    assert payload["first_sentence"].startswith("Hi Buster")
    assert "put together" not in payload["first_sentence"].lower()
    _assert_agency_callback_not_jake(payload["voicemail_message"])
    _assert_agency_callback_not_jake(payload["task"])


def test_client_followup_custom_instructions_may_cite_explicit_number():
    """Jake's DID is allowed only when a CSR writes it in custom instructions."""
    client = CarrierVoiceClient(api_key="test-key")
    dossier = _sample_dossier(
        client_first_name="Maria",
        producer_name="Jake Ferrara",
        requestor_name="Carlo Ferrara",
        requestor_phone="+17324622360",
        call_type="client_followup",
        custom_instructions=f"If they ask, they can also reach Jake at {JAKE_PERSONAL_DID}.",
    )
    prompt = client.build_call_prompt(dossier)
    voicemail = client._voicemail_message(dossier)
    assert AGENCY_MAIN_CALLBACK_DISPLAY in prompt
    assert JAKE_PERSONAL_DID in prompt
    _assert_agency_callback_not_jake(voicemail)


def _assert_client_copy_first_name_only(text: str, first: str, full_name: str) -> None:
    """Spoken client copy may use first name, never the full personal name."""
    assert f"Hi {first}" in text or f"Hello {first}" in text or first in text
    assert full_name not in text
    assert f"Hi {full_name}" not in text
    assert f"({full_name})" not in text


def test_personal_client_followup_never_uses_full_name_in_spoken_copy():
    client = CarrierVoiceClient(api_key="test-key")
    dossier = _sample_dossier(
        insured_name="Buster Brown",
        client_first_name="Buster",
        producer_name="Carlo Ferrara",
        requestor_name="Mike Sosa",
        requestor_phone="+17326540947",
        call_type="client_followup",
        transfer_mode="warm",
    )
    first = client.build_client_first_sentence(dossier)
    voicemail = client._voicemail_message(dossier)
    prompt = client.build_call_prompt(dossier)
    briefing = client.build_transfer_briefing(dossier)
    _assert_client_copy_first_name_only(first, "Buster", "Buster Brown")
    _assert_client_copy_first_name_only(voicemail, "Buster", "Buster Brown")
    assert "Hi Buster" in prompt
    assert "Hi Buster Brown" not in prompt
    assert "Buster (Buster Brown)" not in briefing
    assert "(Buster Brown)" not in briefing
    assert "I have Buster on the line" in briefing
    assert "Carlo Ferrara" in first


def test_personal_client_outreach_never_uses_full_name_in_spoken_copy():
    client = CarrierVoiceClient(api_key="test-key")
    dossier = _sample_dossier(
        insured_name="Buster Brown",
        client_first_name="Buster",
        producer_name="Carlo Ferrara",
        requestor_name="Mike Sosa",
        requestor_phone="+17326540947",
        call_type="client_outreach",
        transfer_mode="warm",
    )
    first = client.build_client_first_sentence(dossier)
    voicemail = client._voicemail_message(dossier)
    prompt = client.build_call_prompt(dossier)
    briefing = client.build_transfer_briefing(dossier)
    _assert_client_copy_first_name_only(first, "Buster", "Buster Brown")
    _assert_client_copy_first_name_only(voicemail, "Buster", "Buster Brown")
    assert "Hi Buster" in prompt
    assert "Hi Buster Brown" not in prompt
    assert "Buster (Buster Brown)" not in briefing
    assert "(Buster Brown)" not in briefing
    assert "Carlo Ferrara" not in first
    assert "put together" not in first.lower()


def test_spoken_copy_strips_full_name_if_hydrator_leaks_first_last():
    """Defense in depth: even a 'Buster Brown' first-name field greets 'Hi Buster'."""
    client = CarrierVoiceClient(api_key="test-key")
    for call_type in ("client_followup", "client_outreach"):
        dossier = _sample_dossier(
            insured_name="Buster Brown",
            client_first_name="Buster Brown",
            producer_name="Carlo Ferrara",
            call_type=call_type,
        )
        first = client.build_client_first_sentence(dossier)
        voicemail = client._voicemail_message(dossier)
        prompt = client.build_call_prompt(dossier)
        assert first.startswith("Hi Buster,")
        assert "Hi Buster Brown" not in first
        assert "Hi Buster Brown" not in voicemail
        assert "Hi Buster Brown" not in prompt


def test_commercial_llc_uses_contact_first_name_on_followup_and_outreach():
    client = CarrierVoiceClient(api_key="test-key")
    for call_type in ("client_followup", "client_outreach"):
        dossier = _sample_dossier(
            insured_name="Green Lion Lawn Care LLC",
            client_first_name="Luis",
            producer_name="Carlo Ferrara",
            requestor_name="Mike Sosa",
            requestor_phone="+17326540947",
            call_type=call_type,
            transfer_mode="warm",
        )
        first = client.build_client_first_sentence(dossier)
        voicemail = client._voicemail_message(dossier)
        prompt = client.build_call_prompt(dossier)
        briefing = client.build_transfer_briefing(dossier)
        assert first.startswith("Hi Luis")
        assert "Hi Green" not in first
        assert "Green Lion" not in first
        assert "Hi Green" not in voicemail
        assert "Hi Green" not in prompt
        assert "I have Luis on the line about Green Lion Lawn Care LLC" in briefing
        assert "Luis (Green Lion" not in briefing


def test_commercial_llc_without_contact_uses_generic_hi():
    client = CarrierVoiceClient(api_key="test-key")
    for call_type in ("client_followup", "client_outreach"):
        dossier = _sample_dossier(
            insured_name="Green Lion Lawn Care LLC",
            client_first_name=None,
            producer_name="Carlo Ferrara",
            requestor_name="Mike Sosa",
            requestor_phone="+17326540947",
            call_type=call_type,
        )
        first = client.build_client_first_sentence(dossier)
        voicemail = client._voicemail_message(dossier)
        prompt = client.build_call_prompt(dossier)
        briefing = client.build_transfer_briefing(dossier)
        assert first.startswith("Hi,")
        assert "Hi Green" not in first
        assert "Green Lion Lawn Care LLC" not in first
        assert voicemail.startswith("Hello,")
        assert "Hi Green" not in voicemail
        assert "Hi Green" not in prompt
        assert "I have the client on the line about Green Lion Lawn Care LLC" in briefing


def test_carrier_prompt_offers_transfer_after_live_human():
    client = CarrierVoiceClient(api_key="test-key")
    dossier = _sample_dossier(
        producer_name="Jake Ferrara",
        requestor_name="Carlo Ferrara",
        requestor_phone="+17324622360",
        call_type="carrier",
        transfer_mode="warm",
    )
    prompt = client.build_call_prompt(dossier)
    briefing = client.build_transfer_briefing(dossier)
    assert "Carlo Ferrara" in prompt
    assert "Jake Ferrara" in prompt
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


def _assert_warm_transfer_pause_wording(text: str) -> None:
    """Every warm-transfer prompt path: finish the line, wait 10-15s, then transfer."""
    lower = text.lower()
    assert WARM_TRANSFER_CLIENT_HANDOFF_LINE in text
    assert "10" in text and "15" in text
    assert "after that pause" in lower or "after the pause" in lower
    assert "use the transfer action" in lower or "using the transfer action" in lower
    assert "never fire the transfer action while still speaking" in lower
    assert "wait silently" in lower or "wait about 10-15 seconds" in lower


def test_warm_transfer_timing_rules_finish_then_pause_then_action():
    rules = warm_transfer_timing_rules()
    assert WARM_TRANSFER_CLIENT_HANDOFF_LINE in rules
    assert "10" in rules and "15" in rules
    assert "transfer action" in rules.lower()
    assert "connecting you now" in rules.lower()
    assert "never fire the transfer action while still speaking" in rules.lower()


def test_client_followup_prompt_instructs_handoff_pause_before_transfer():
    client = CarrierVoiceClient(api_key="test-key")
    dossier = _sample_dossier(
        client_first_name="Maria",
        producer_name="Jake Ferrara",
        requestor_name="Mike Sosa",
        requestor_phone="+17326540947",
        call_type="client_followup",
        transfer_mode="warm",
    )
    prompt = client.build_call_prompt(dossier)
    transfer_block = client._transfer_objective_block(dossier)
    briefing = client.build_transfer_briefing(dossier)
    _assert_warm_transfer_pause_wording(prompt)
    _assert_warm_transfer_pause_wording(transfer_block)
    assert "Mike Sosa" in prompt
    assert "Connecting you now." in briefing
    assert briefing not in prompt.split("WARM TRANSFER TIMING")[0]
    assert "spoken only to Mike Sosa after they answer" in prompt
    assert "max_duration" not in prompt


def test_client_outreach_prompt_instructs_handoff_pause_before_transfer():
    client = CarrierVoiceClient(api_key="test-key")
    dossier = _sample_dossier(
        client_first_name="Buster",
        assigned_producer_name="Carlo Ferrara",
        assigned_producer_phone="+17324622360",
        requestor_name="Mike Sosa",
        requestor_phone="+17326540947",
        call_type="client_outreach",
        transfer_mode="warm",
    )
    prompt = client.build_call_prompt(dossier)
    transfer_block = client._transfer_objective_block(dossier)
    briefing = client.build_transfer_briefing(dossier)
    _assert_warm_transfer_pause_wording(prompt)
    _assert_warm_transfer_pause_wording(transfer_block)
    assert "Assigned Producer" in prompt
    assert "Carlo Ferrara" in prompt
    assert "Connecting you now." in briefing
    assert "spoken only to Carlo Ferrara after they answer" in prompt
    assert "+17324622360" in prompt
    assert "Mike Sosa" not in transfer_block or "NOT the transfer target" in prompt


def test_carrier_prompt_instructs_handoff_pause_before_transfer():
    client = CarrierVoiceClient(api_key="test-key")
    dossier = _sample_dossier(
        requestor_name="Carlo Ferrara",
        requestor_phone="+17324622360",
        call_type="carrier",
        transfer_mode="warm",
    )
    prompt = client.build_call_prompt(dossier)
    transfer_block = client._transfer_objective_block(dossier)
    briefing = client.build_transfer_briefing(dossier)
    _assert_warm_transfer_pause_wording(prompt)
    _assert_warm_transfer_pause_wording(transfer_block)
    assert "Carlo Ferrara" in prompt
    assert "Connecting you now." in briefing
    assert "spoken only to Carlo Ferrara after they answer" in prompt
    assert "live human" in prompt.lower()


def test_voice_client_cancellation_is_generic_when_hydrate_is_stubbed():
    client = CarrierVoiceClient(api_key="test-key")
    dossier = _sample_dossier(
        policy_number="TEST-STUB",
        insured_name="Buster Brown",
        carrier_name="Unknown Carrier",
        line_of_business="Commercial",
        client_first_name="Buster",
        assigned_producer_name="Carlo Ferrara",
        assigned_producer_phone="+17324622360",
        call_type="client_outreach",
        outreach_pathway=PATHWAY_CANCELLATION,
        transfer_mode="warm",
        carrier_phone="+17329953409",
    )
    first = client.build_client_first_sentence(dossier)
    voicemail = client._voicemail_message(dossier)
    prompt = client.build_call_prompt(dossier)
    assert CANCELLATION_GENERIC_BODY in first
    assert CANCELLATION_GENERIC_BODY in voicemail
    assert "Unknown Carrier" not in first
    assert "Unknown Carrier" not in voicemail
    assert "Commercial policy" not in first
    assert first.startswith("Hi Buster, this is Robie from StreetSmart Insurance.")
    assert "connect you to Carlo now" in first
    _assert_warm_transfer_pause_wording(prompt)
