"""Unit tests for the voice CLI dispatcher."""

import json
from unittest.mock import MagicMock

import pytest

from src.voice.context_hydrator import CallingDossier
from src.voice.dispatcher import (
    VoiceCallDispatcher,
    main,
    parse_args,
    summarize_dispatch_result,
)


def _parse_printed_json(stdout: str) -> dict:
    start = stdout.find("{")
    assert start != -1, f"No JSON object in CLI output:\n{stdout}"
    return json.loads(stdout[start:])


def _sample_dossier(**overrides) -> CallingDossier:
    data = {
        "policy_number": "PWC1239278",
        "insured_name": "Yes We Do LLC",
        "carrier_name": "Associated Specialty Insurance Agency MGA",
        "line_of_business": "Workers comp",
        "carrier_phone": "866-513-5650",
        "custom_instructions": None,
    }
    data.update(overrides)
    return CallingDossier(**data)


def test_parse_args_requires_policy_number():
    with pytest.raises(SystemExit):
        parse_args([])


def test_parse_args_dry_run_phone_and_instructions():
    args = parse_args(
        [
            "--policy-number",
            "UB-6N448514-25-42-V",
            "--dry-run",
            "--phone",
            "+17329953409",
            "--instructions",
            "Ask for Buster Brown.",
        ]
    )
    assert args.policy_number == "UB-6N448514-25-42-V"
    assert args.dry_run is True
    assert args.phone == "+17329953409"
    assert args.instructions == "Ask for Buster Brown."


def test_dispatch_hydrates_and_calls_voice_client_dry_run():
    hydrator = MagicMock()
    dossier = _sample_dossier(custom_instructions="Ask if payroll audit was accepted")
    hydrator.hydrate.return_value = dossier

    voice = MagicMock()
    voice.dispatch_call.return_value = {
        "success": True,
        "mode": "SIMULATION",
        "call_id": "sim_call_PWC1239278_001",
        "status": "DISPATCHED_SIMULATED",
        "phone_number": "866-513-5650",
        "carrier": "Associated Specialty Insurance Agency MGA",
        "policy_number": "PWC1239278",
        "insured_name": "Yes We Do LLC",
    }

    dispatcher = VoiceCallDispatcher(hydrator=hydrator, voice_client=voice)
    result = dispatcher.dispatch(
        policy_number="PWC1239278",
        phone="800-555-0199",
        instructions="Ask if payroll audit was accepted",
        dry_run=True,
    )

    hydrator.hydrate.assert_called_once_with(
        policy_number="PWC1239278",
        phone_override="800-555-0199",
        instructions="Ask if payroll audit was accepted",
        call_type="carrier",
    )
    voice.dispatch_call.assert_called_once_with(dossier=dossier, dry_run=True)
    assert result["success"] is True
    assert result["mode"] == "SIMULATION"
    assert result["call_id"] == "sim_call_PWC1239278_001"
    assert result["phone"] == "866-513-5650"
    assert result["carrier"] == "Associated Specialty Insurance Agency MGA"
    assert result["policy"] == "PWC1239278"


def test_dispatch_hydrate_failure_does_not_call_voice():
    hydrator = MagicMock()
    hydrator.hydrate.return_value = None
    voice = MagicMock()

    dispatcher = VoiceCallDispatcher(hydrator=hydrator, voice_client=voice)
    result = dispatcher.dispatch(policy_number="MISSING-POL")

    voice.dispatch_call.assert_not_called()
    assert result["success"] is False
    assert result["error"] == "HYDRATE_FAILED"
    assert result["policy"] == "MISSING-POL"


def test_dispatch_hydrate_exception_is_failure():
    hydrator = MagicMock()
    hydrator.hydrate.side_effect = RuntimeError("db unavailable")
    voice = MagicMock()

    dispatcher = VoiceCallDispatcher(hydrator=hydrator, voice_client=voice)
    result = dispatcher.dispatch(policy_number="PWC1239278")

    voice.dispatch_call.assert_not_called()
    assert result["success"] is False
    assert result["error"] == "HYDRATE_FAILED"
    assert "db unavailable" in result["details"]


def test_dispatch_voice_failure_exits_nonzero_via_main(capsys):
    hydrator = MagicMock()
    dossier = _sample_dossier()
    hydrator.hydrate.return_value = dossier
    voice = MagicMock()
    voice.dispatch_call.return_value = {
        "success": False,
        "error": "NO_CARRIER_PHONE",
        "policy_number": "PWC1239278",
    }

    dispatcher = VoiceCallDispatcher(hydrator=hydrator, voice_client=voice)
    rc = main(["--policy-number", "PWC1239278"], dispatcher=dispatcher)
    captured = capsys.readouterr()

    assert rc == 1
    assert "FAILED" in captured.out
    assert "NO_CARRIER_PHONE" in captured.out
    payload = _parse_printed_json(captured.out)
    assert payload["success"] is False
    assert payload["policy"] == "PWC1239278"


def test_main_dry_run_success_prints_json_summary(capsys):
    hydrator = MagicMock()
    dossier = _sample_dossier()
    hydrator.hydrate.return_value = dossier
    voice = MagicMock()
    voice.dispatch_call.return_value = {
        "success": True,
        "mode": "SIMULATION",
        "call_id": "sim_call_PWC1239278_001",
        "status": "DISPATCHED_SIMULATED",
        "phone_number": "+17329953409",
        "carrier": "Associated Specialty Insurance Agency MGA",
        "policy_number": "PWC1239278",
        "insured_name": "Yes We Do LLC",
    }

    dispatcher = VoiceCallDispatcher(hydrator=hydrator, voice_client=voice)
    rc = main(
        [
            "--policy-number",
            "PWC1239278",
            "--dry-run",
            "--phone",
            "+17329953409",
            "--instructions",
            "Testing voice agent response. Ask for Buster Brown.",
        ],
        dispatcher=dispatcher,
    )
    captured = capsys.readouterr()

    assert rc == 0
    hydrator.hydrate.assert_called_once_with(
        policy_number="PWC1239278",
        phone_override="+17329953409",
        instructions="Testing voice agent response. Ask for Buster Brown.",
        call_type="carrier",
    )
    voice.dispatch_call.assert_called_once_with(dossier=dossier, dry_run=True)
    assert "SUCCESS" in captured.out
    payload = _parse_printed_json(captured.out)
    assert payload["success"] is True
    assert payload["mode"] == "SIMULATION"
    assert payload["call_id"] == "sim_call_PWC1239278_001"
    assert payload["phone"] == "+17329953409"
    assert payload["carrier"] == "Associated Specialty Insurance Agency MGA"
    assert payload["policy"] == "PWC1239278"


def test_main_hydrate_failure_exits_nonzero(capsys):
    hydrator = MagicMock()
    hydrator.hydrate.return_value = None
    voice = MagicMock()
    dispatcher = VoiceCallDispatcher(hydrator=hydrator, voice_client=voice)

    rc = main(["--policy-number", "UNKNOWN"], dispatcher=dispatcher)
    captured = capsys.readouterr()

    assert rc == 1
    voice.dispatch_call.assert_not_called()
    assert "HYDRATE_FAILED" in captured.out


def test_summarize_dispatch_result_fills_from_dossier():
    dossier = _sample_dossier()
    summary = summarize_dispatch_result(
        {"success": True, "mode": "LIVE_BLAND_AI", "call_id": "abc123"},
        dossier,
    )
    assert summary["phone"] == "866-513-5650"
    assert summary["carrier"] == "Associated Specialty Insurance Agency MGA"
    assert summary["policy"] == "PWC1239278"
    assert summary["call_id"] == "abc123"
