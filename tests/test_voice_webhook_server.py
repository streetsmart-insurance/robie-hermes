"""Unit tests for Voice Webhook Receiver and EZLynx note handling."""

from unittest.mock import patch, MagicMock

from src.voice.webhook_server import handle_completed_call


def test_handle_completed_call_formatting_and_sync():
    payload = {
        "call_id": "call_test_12345",
        "recording_url": "https://api.bland.ai/recordings/call_12345.mp3",
        "summary": "Representative Sarah confirmed renewal quote was issued yesterday at $2,450.00 and uploaded to the agent portal.",
        "concatenated_transcript": "Agent: Hello from StreetSmart... Rep: The quote is issued.",
        "metadata": {
            "policy_number": "PWC1239278",
            "insured_name": "Yes We Do LLC",
            "carrier_name": "Associated Specialty Insurance Agency MGA",
            "applicant_id": 21588091,
            "assigned_csr_email": "eimy@streetsmart.insurance",
            "line_of_business": "Workers comp"
        }
    }

    with patch("src.voice.call_completion.EZLynxApiClient") as mock_ezlynx_cls, \
         patch("src.voice.call_completion.GmailRenewalClient") as mock_gmail_cls, \
         patch("src.voice.call_completion.fetch_bland_call_details", return_value={}), \
         patch(
             "src.voice.call_completion.upload_call_artifacts",
             return_value={"recording_uploaded": True, "transcript_uploaded": False},
         ), \
         patch("src.voice.call_completion.SessionLocal") as mock_session:

        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        mock_session.return_value = db

        mock_client = MagicMock()
        mock_client.add_note_to_discussion.return_value = {
            "success": True,
            "note_id": 998877,
            "discussion_title": "Renewal Manual Workers comp | PWC1239278 Associated Specialty"
        }
        mock_ezlynx_cls.return_value = mock_client

        mock_gmail = MagicMock()
        mock_gmail_cls.return_value = mock_gmail

        res = handle_completed_call(payload)

        assert res["call_id"] == "call_test_12345"
        assert res["ezlynx_posted"] is True
        assert res["note_id"] == 998877
        assert res["email_sent"] is True

        mock_client.add_note_to_discussion.assert_called_once()
        call_kwargs = mock_client.add_note_to_discussion.call_args[1]
        assert call_kwargs["applicant_id"] == 21588091
        assert call_kwargs["policy_number"] == "PWC1239278"
        assert "Policy: #PWC1239278 (Workers comp - Associated Specialty Insurance Agency MGA)" in call_kwargs["note_text"]
        assert "Robie was here" in call_kwargs["note_text"]
        assert "https://api.bland.ai/recordings/call_12345.mp3" in call_kwargs["note_text"]
        assert "Agent: Hello from StreetSmart" in call_kwargs["note_text"]

        mock_gmail.send_email.assert_called_once()
        email_kwargs = mock_gmail.send_email.call_args[1]
        assert email_kwargs["to_email"] == "eimy@streetsmart.insurance"
        assert "PWC1239278" in email_kwargs["subject"]
