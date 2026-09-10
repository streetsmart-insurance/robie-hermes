"""Tests for Bland post-call completion → EZLynx note + document upload."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from src.voice.call_completion import (
    DEFAULT_ASSIGNED_CSR_NAME,
    ROBIE_CALL_LABEL,
    SAFE_DOCUMENT_LABEL,
    TRANSCRIPT_NOTE_CHAR_LIMIT,
    build_completion_note,
    handle_completed_call,
    merge_call_payload,
    truncate_transcript,
    upload_call_artifacts,
)
from src.voice.processed_robie_notes import ProcessedRobieCallStore
from src.voice.webhook_server import handle_completed_call as webhook_handle


def test_truncate_and_completion_note_include_transcript_and_robie_markers():
    huge = "LINE\n" * 4000
    excerpt, truncated = truncate_transcript(huge)
    assert truncated is True
    assert len(excerpt) < len(huge)
    assert "truncated" in excerpt.lower()

    note = build_completion_note(
        policy_number="PWC1239278",
        line_of_business="Workers comp",
        carrier_name="Associated Specialty",
        call_id="call_test_12345",
        summary="Quote issued yesterday.",
        recording_url="https://api.bland.ai/recordings/call_12345.mp3",
        transcript=huge,
        recording_uploaded=True,
        transcript_attached=True,
    )
    assert note.startswith("Policy: #PWC1239278 (Workers comp - Associated Specialty)")
    assert "Autonomous Carrier Phone Outreach Completed" in note
    assert "Robie was here" in note
    assert "Transcript:" in note
    assert ROBIE_CALL_LABEL not in note or "[ROBIE CALL]" not in note


def test_merge_prefers_webhook_then_fills_from_bland():
    merged = merge_call_payload(
        {"call_id": "c1", "metadata": {"policy_number": "P1"}},
        {
            "recording_url": "https://api.bland.ai/r.mp3",
            "concatenated_transcript": "hello",
            "summary": "done",
            "metadata": {"carrier_name": "Hartford"},
        },
    )
    assert merged["recording_url"].endswith(".mp3")
    assert merged["concatenated_transcript"] == "hello"
    assert merged["metadata"]["policy_number"] == "P1"
    assert merged["metadata"]["carrier_name"] == "Hartford"


def test_upload_call_artifacts_prefers_api_and_skips_robie_call_label(tmp_path):
    recording = tmp_path / "rec.mp3"
    recording.write_bytes(b"ID3fake-audio")
    ezlynx = MagicMock()
    ezlynx.upload_document.return_value = {"status": "success", "method": "api"}
    voice = MagicMock()
    voice.download_recording.return_value = True

    huge = "x" * (TRANSCRIPT_NOTE_CHAR_LIMIT + 50)
    result = upload_call_artifacts(
        ezlynx=ezlynx,
        applicant_id="21588091",
        policy_number="PWC1239278",
        call_id="call_test_12345",
        recording_url="https://api.bland.ai/recordings/call_12345.mp3",
        transcript=huge,
        voice_client=voice,
        downloads_dir=tmp_path,
    )

    assert result["recording_uploaded"] is True
    assert result["transcript_uploaded"] is True
    assert ezlynx.upload_document.call_count == 2
    for call in ezlynx.upload_document.call_args_list:
        kwargs = call.kwargs
        assert kwargs["prefer_api"] is True
        assert kwargs["use_playwright_fallback"] is True
        assert kwargs["label_to_apply"] == SAFE_DOCUMENT_LABEL
        assert kwargs["label_to_apply"].lower() != ROBIE_CALL_LABEL.lower()
        assert kwargs["folder_name"] == "Documents"
        assert kwargs["policy_number"] == "PWC1239278"


def test_handle_completed_call_fetches_bland_posts_note_and_uploads(tmp_path):
    payload = {
        "call_id": "call_test_12345",
        "metadata": {
            "policy_number": "PWC1239278",
            "insured_name": "Yes We Do LLC",
            "carrier_name": "Associated Specialty Insurance Agency MGA",
            "applicant_id": 21588091,
            "assigned_csr_email": "eimy@streetsmart.insurance",
            "line_of_business": "Workers comp",
        },
    }
    bland = {
        "recording_url": "https://api.bland.ai/recordings/call_12345.mp3",
        "concatenated_transcript": "Agent: Hello from StreetSmart... Rep: The quote is issued.",
        "summary": "Representative Sarah confirmed renewal quote was issued yesterday at $2,450.00.",
    }
    mock_ezlynx = MagicMock()
    mock_ezlynx.add_note_to_discussion.return_value = {"success": True, "note_id": 998877}
    mock_ezlynx.upload_document.return_value = {"status": "success", "method": "api"}
    mock_voice = MagicMock()
    mock_voice.get_call.return_value = bland
    mock_voice.download_recording.side_effect = lambda url, dest: Path(dest).write_bytes(b"ID3") or True
    store = ProcessedRobieCallStore(tmp_path / "processed.sqlite")

    with patch("src.voice.call_completion.SessionLocal") as mock_session, \
         patch("src.voice.call_completion.GmailRenewalClient") as mock_gmail_cls:
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        mock_session.return_value = db
        mock_gmail = MagicMock()
        mock_gmail_cls.return_value = mock_gmail

        res = handle_completed_call(
            payload,
            voice_client=mock_voice,
            ezlynx_client=mock_ezlynx,
            processed_store=store,
        )

    assert res["ezlynx_posted"] is True
    assert res["note_id"] == 998877
    assert res["recording_uploaded"] is True
    assert res["assigned_csr"] == DEFAULT_ASSIGNED_CSR_NAME
    mock_voice.get_call.assert_called_once_with("call_test_12345")
    note_kwargs = mock_ezlynx.add_note_to_discussion.call_args.kwargs
    assert note_kwargs["policy_number"] == "PWC1239278"
    assert "Policy: #PWC1239278 (Workers comp - Associated Specialty Insurance Agency MGA)" in note_kwargs["note_text"]
    assert "Agent: Hello from StreetSmart" in note_kwargs["note_text"]
    assert "Robie was here" in note_kwargs["note_text"]
    assert note_kwargs.get("label_to_apply") is None
    assert note_kwargs.get("label") is None
    assert store.has("998877") is True
    mock_gmail.send_email.assert_called_once()


def test_webhook_export_still_handles_complete_payload():
    """Existing webhook_server import path keeps posting notes with Robie markers."""
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
            "line_of_business": "Workers comp",
        },
    }

    with patch("src.voice.call_completion.EZLynxApiClient") as mock_ezlynx_cls, \
         patch("src.voice.call_completion.GmailRenewalClient") as mock_gmail_cls, \
         patch("src.voice.call_completion.fetch_bland_call_details", return_value={}), \
         patch("src.voice.call_completion.upload_call_artifacts", return_value={"recording_uploaded": False, "transcript_uploaded": False}), \
         patch("src.voice.call_completion.SessionLocal") as mock_session:
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        mock_session.return_value = db
        mock_client = MagicMock()
        mock_client.add_note_to_discussion.return_value = {
            "success": True,
            "note_id": 998877,
        }
        mock_ezlynx_cls.return_value = mock_client
        mock_gmail_cls.return_value = MagicMock()

        res = webhook_handle(payload)

    assert res["call_id"] == "call_test_12345"
    assert res["ezlynx_posted"] is True
    assert res["note_id"] == 998877
    call_kwargs = mock_client.add_note_to_discussion.call_args.kwargs
    assert "Policy: #PWC1239278 (Workers comp - Associated Specialty Insurance Agency MGA)" in call_kwargs["note_text"]
    assert "Robie was here" in call_kwargs["note_text"]
    assert "https://api.bland.ai/recordings/call_12345.mp3" in call_kwargs["note_text"]
