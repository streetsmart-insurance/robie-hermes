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


def test_dispatcher_resolves_phone_from_carrier_directory():
    """Tests that Robie auto-resolves phone if user only provided carrier name."""
    mock_ezlynx = MagicMock()
    mock_voice = MagicMock()

    mock_ezlynx.get_applicant_discussions.return_value = [
        {
            "discussionId": 999222,
            "title": "Robie Call - The Hartford",
            "discussionNote": {
                # Note has NO phone number, only carrier name
                "noteText": "Who to call: The Hartford\nPolicy: PWC998877\nWhat to say: Check if audit completed."
            }
        }
    ]
    mock_ezlynx.get_applicant.return_value = {
        "status": "success",
        "applicant": {"BusinessName": "Main Street Cafe"}
    }
    mock_voice.dispatch_call.return_value = {"call_id": "call_hartford_01", "status": "DISPATCHED"}

    dispatcher = EZLynxLabelCallDispatcher(ezlynx_client=mock_ezlynx, voice_client=mock_voice)
    results = dispatcher.process_applicant_notes_for_calls("123456", dry_run=True)

    assert len(results) == 1
    # Auto-resolved Hartford phone +18005551234
    assert results[0]["phone"] == "+18005551234"
    assert results[0]["status"] == "DISPATCHED"
    mock_voice.dispatch_call.assert_called_once()


def test_dispatcher_posts_clarification_note_when_phone_unknown():
    """Tests that Robie posts a clarification note via direct API (no Playwright) when phone cannot be resolved."""
    mock_ezlynx = MagicMock()
    mock_voice = MagicMock()

    mock_ezlynx.get_applicant_discussions.return_value = [
        {
            "discussionId": 999333,
            "title": "robie call",
            "discussionNote": {
                # Completely unknown carrier, no phone, no policy
                "noteText": "Who to call: Obscure Local Mutual\nWhat to say: Inquire about status."
            }
        }
    ]
    mock_ezlynx.get_applicant.return_value = {
        "status": "success",
        "applicant": {"BusinessName": "Unknown Corp"}
    }
    mock_ezlynx.get_applicant_policies.return_value = []

    dispatcher = EZLynxLabelCallDispatcher(ezlynx_client=mock_ezlynx, voice_client=mock_voice)
    results = dispatcher.process_applicant_notes_for_calls("123456", dry_run=True)

    assert len(results) == 1
    assert results[0]["status"] == "CLARIFICATION_NEEDED"
    assert results[0]["reason"] == "MISSING_PHONE_NUMBER"
    # Verify no call was placed
    mock_voice.dispatch_call.assert_not_called()
    # Verify a clarification note was posted to EZLynx directly
    mock_ezlynx.add_note_to_discussion.assert_called_once()
    posted_text = mock_ezlynx.add_note_to_discussion.call_args[1]["note_text"]
    assert "⚠️ [ROBIE CALL - PHONE NUMBER NEEDED]" in posted_text


def test_dispatcher_skips_when_latest_note_is_from_robie():
    """Tests that Robie prevents loops by not re-triggering on its own notes."""
    mock_ezlynx = MagicMock()
    mock_voice = MagicMock()

    mock_ezlynx.get_applicant_discussions.return_value = [
        {
            "discussionId": 999444,
            "title": "robie call",
            "discussionNote": {
                "createdByName": "Robie AI",
                "noteText": "🤖 [ROBIE AUTONOMOUS CALL DISPATCHED]\nRobie has placed an outbound call..."
            }
        }
    ]

    dispatcher = EZLynxLabelCallDispatcher(ezlynx_client=mock_ezlynx, voice_client=mock_voice)
    results = dispatcher.process_applicant_notes_for_calls("123456", dry_run=True)

    assert len(results) == 0
    mock_voice.dispatch_call.assert_not_called()
    mock_ezlynx.add_note_to_discussion.assert_not_called()

