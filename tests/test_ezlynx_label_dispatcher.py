"""Unit tests for EZLynx Label and Note Call Dispatcher."""

from unittest.mock import patch, MagicMock
import pytest

from src.voice.ezlynx_label_dispatcher import parse_call_note_instructions, EZLynxLabelCallDispatcher


def test_parse_call_note_with_explicit_fields():
    sample_note = """
    robie call
    Who to call: The Hartford (800-555-1234)
    Policy: PWC1239278
    What to say: Follow up on the upcoming workers comp renewal quote and request premium details.
    """
    parsed = parse_call_note_instructions(sample_note)
    assert parsed["is_robie_call"] is True
    assert parsed["phone_number"] == "+18005551234"
    assert "The Hartford" in parsed["target_name"]
    assert parsed["policy_number"] == "PWC1239278"
    assert "Follow up on the upcoming workers comp renewal" in parsed["instructions"]


def test_parse_call_note_with_alternate_formatting():
    sample_note = """
    [ROBIE CALL]
    Carrier: Coterie Insurance
    Phone: (855) 567-3421
    Policy#: COT-992144
    Instructions: Check if renewal terms are issued. If not, ask when underwriter will release them.
    Robie was here
    """
    parsed = parse_call_note_instructions(sample_note)
    assert parsed["is_robie_call"] is True
    assert parsed["phone_number"] == "+18555673421"
    assert parsed["target_name"] == "Coterie Insurance"
    assert parsed["policy_number"] == "COT-992144"
    assert "Check if renewal terms are issued" in parsed["instructions"]
    assert "Robie was here" not in parsed["instructions"]


def test_parse_non_robie_note():
    sample_note = """
    Spoke with insured regarding general liability endorsement. Added new location.
    """
    parsed = parse_call_note_instructions(sample_note)
    assert parsed["is_robie_call"] is False


def test_dispatcher_processes_robie_call():
    mock_ezlynx = MagicMock()
    mock_voice = MagicMock()

    mock_ezlynx.get_applicant_discussions.return_value = [
        {
            "discussionId": 999111,
            "title": "Robie Call - Follow up with Travelers",
            "discussionNote": {
                "noteText": "Who to call: Travelers (800-238-6225)\nPolicy: TRV445566\nWhat to say: Ask for renewal quote."
            }
        }
    ]
    mock_ezlynx.get_applicant.return_value = {
        "status": "success",
        "applicant": {"BusinessName": "Acme Widgets LLC"}
    }
    mock_voice.from_phone = "+17322986745"
    mock_voice.build_call_prompt.return_value = "Test prompt"
    mock_voice.dispatch_call.return_value = {"call_id": "call_12345", "status": "DISPATCHED"}

    dispatcher = EZLynxLabelCallDispatcher(ezlynx_client=mock_ezlynx, voice_client=mock_voice)
    results = dispatcher.process_applicant_notes_for_calls("123456", dry_run=True)

    assert len(results) == 1
    assert results[0]["call_id"] == "call_12345"
    assert results[0]["phone"] == "+18002386225"
    mock_voice.dispatch_call.assert_called_once()
    mock_ezlynx.add_note_to_discussion.assert_called_once()
