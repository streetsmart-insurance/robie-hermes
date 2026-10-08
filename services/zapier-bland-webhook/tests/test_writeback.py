"""Tests for EZLynx writeback formatting (Carlo's standing rule).

- Every call writes a concise Notes API entry + the MP3 via Documents API.
- Notes must be plain English and extremely concise.
- The Discussion API refuses bodies with phone-number-like values, so the
  formatter must never emit literal phone numbers.
- Every note ends with "Robie was here".
"""
import re
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from writeback import format_call_note, pick_writeback_discussion

PHONE_RE = re.compile(r"(\+?1?[\s\-.]?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4})")


def _summary(**kw):
    base = {"mode": "LIVE_BLAND_AI", "attempts": [],
            "voicemail_hit": False, "redialed": False}
    base.update(kw)
    # Production always populates final_status for successful POSTs (via
    # _wait_for_terminal). Fixtures without one get a realistic default so
    # the "connected" verdict matches production behavior.
    for a in base["attempts"]:
        if a.get("success") and "final_status" not in a:
            a["final_status"] = {"status": "completed",
                                 "answered_by": "human",
                                 "duration": 95}
    return base


def test_note_states_success_first_and_is_plain_english():
    note = format_call_note(
        "Jane Doe", "robie-audit", "Robie audit",
        _summary(attempts=[{"call_id": "abc-123", "success": True}],
                 outcome="completed", duration_seconds=95,
                 recording_url="https://x/y.mp3",
                 transcript_summary="Client confirmed renewal."),
    )
    assert note.rstrip().endswith("Robie was here")
    assert not PHONE_RE.search(note), f"phone number leaked into note: {note}"
    assert "abc-123" not in note  # call IDs are not staff-facing
    assert "Bland call id" not in note
    assert "The call was successful." in note
    assert "Called Jane Doe about an audit follow-up." in note
    assert "What was discussed: Client confirmed renewal." in note
    assert "The call recording is saved in the applicant's Documents tab." in note


def test_note_covers_redial_plain_english():
    note = format_call_note(
        "Jane Doe", "robie-audit", "Robie audit",
        _summary(attempts=[{"call_id": "a1", "success": True},
                           {"call_id": "a2", "success": True}],
                 voicemail_hit=True, redialed=True),
    )
    assert "The call was successful." in note
    assert "voicemail" in note.lower()
    assert "called back" in note.lower()
    assert "double-dial" not in note.lower()


def test_note_failed_call_plain_english():
    note = format_call_note(
        "Jane Doe", "robie-audit", "Robie audit",
        _summary(attempts=[{"success": False, "error": "the number was disconnected"}]),
    )
    assert "Attempted to call Jane Doe about an audit follow-up." in note
    assert "The call was NOT successful:" in note
    assert "the number was disconnected" in note
    assert "No message was left." in note
    assert note.rstrip().endswith("Robie was here")


def test_note_spoke_to_person_with_duration():
    note = format_call_note(
        "Jane Doe", "robie-audit", "Robie audit",
        _summary(attempts=[{"success": True,
                            "final_status": {"answered_by": "human", "duration": 180}}]),
    )
    assert "The call was successful." in note
    assert "Spoke with Jane for 3 minutes." in note


def test_note_scrubs_phone_from_transcript():
    note = format_call_note(
        "Jane Doe", "robie-audit", "Robie audit",
        _summary(attempts=[{"success": True}],
                 transcript_summary="Client gave callback 732-462-8343 today."),
    )
    assert not PHONE_RE.search(note), f"phone leaked: {note}"
    assert "732-462-8343" not in note
    assert "[phone number]" in note


def test_dry_run_note_says_no_real_call():
    note = format_call_note("Jane Doe", "robie-call", "Robie Call",
                            _summary(mode="DRY_RUN"))
    assert "DRY RUN" in note
    assert "no real call was placed" in note


def test_pick_writeback_discussion_prefers_trigger():
    discs = [{"id": "d9", "lastActivityDate": "2026-10-02"},
             {"id": "d1", "lastActivityDate": "2026-09-01"}]
    assert pick_writeback_discussion(discs, trigger_discussion_id="d1") == "d1"


def test_pick_writeback_discussion_falls_back_to_most_recent():
    discs = [{"id": "d9", "lastActivityDate": "2026-10-02"},
             {"id": "d1", "lastActivityDate": "2026-09-01"}]
    assert pick_writeback_discussion(discs) == "d9"
    assert pick_writeback_discussion([]) is None
