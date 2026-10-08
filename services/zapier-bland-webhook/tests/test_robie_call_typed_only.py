"""HARD BLOCK (Carlo, Oct 7 2026): a Robie Call never dials a number on file.

It dials only a phone number typed in the note text. With none typed (or
more than one) it dials nothing, not even a dry-run placeholder, and asks
for the number in plain English. Deterministic campaigns (lead follow-up,
etc.) keep using the number on file.
"""
import pytest

from dispatcher import dispatch, typed_phones
from tests.test_freeform import APPLICANT, DISCUSSIONS, POLICIES, FakeEZ

ON_FILE = APPLICANT  # FakeEZ applicant carries the phone on file


class RecordingEZ(FakeEZ):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.notes = []

    def append_note(self, discussion_id, body):
        self.notes.append((discussion_id, body))
        return {"status": "success", "noteId": "N-1"}

    def upload_document(self, *args, **kwargs):
        return {"status": "skipped"}


class RecordingBland:
    def __init__(self):
        self.phones = []

    def is_available(self):
        return True

    def call_with_double_dial(self, phone, task, first_sentence, voicemail_message,
                              metadata=None, dry_run=False, halt_check=None):
        self.phones.append(phone)
        return {"attempts": [{"success": True, "payload": {"phone_number": phone}}],
                "dry_run": dry_run}

    def get_call(self, call_id):
        return {}


@pytest.mark.parametrize("text,expected", [
    ("Call at 908-555-0199 about the renewal", ["+19085550199"]),
    ("call her (908) 555-0199 today", ["+19085550199"]),
    ("cell: 908.555.0199", ["+19085550199"]),
    ("+1 908 555 0199", ["+19085550199"]),
    ("call at 9085550199", ["+19085550199"]),
    ("phone 19085550199", ["+19085550199"]),
    ("policy 7685786571 renews Nov 1", []),
    ("claim 4445556666", []),
    ("", []),
    ("Call 908-555-0199 or 732-555-0142", ["+19085550199", "+17325550142"]),
    ("908-555-0199 and again 908-555-0199", ["+19085550199"]),
])
def test_typed_phones(text, expected):
    assert typed_phones(text) == expected


@pytest.mark.parametrize("dry", [True, False])
def test_robie_call_without_a_typed_number_never_dials(dry):
    ez = RecordingEZ(applicant=APPLICANT, policies=POLICIES, discussions=DISCUSSIONS)
    bland = RecordingBland()
    result = dispatch(
        "123", "robie-call", "Robie Call", "", discussion_id="d1",
        note_body="Please call Jane about her renewal.", dry_run=dry, ez=ez, bland=bland,
    )
    assert bland.phones == []  # not the number on file, not +10000000000
    assert result["ok"] is False
    assert result["needs_typed_number"] is True
    assert "never dials the number on file" in result["error"]
    if dry:
        assert ez.notes == []
        assert result["ask_note_result"] == {"skipped": "dry_run"}
    else:
        assert len(ez.notes) == 1
        assert ez.notes[0][0] == "d1"
        assert "no phone number was typed in this note" in ez.notes[0][1]


def test_robie_call_with_two_numbers_asks_which_one():
    ez = RecordingEZ(applicant=APPLICANT, policies=POLICIES, discussions=DISCUSSIONS)
    bland = RecordingBland()
    result = dispatch(
        "123", "robie-call", "Robie Call", "", discussion_id="d1",
        note_body="Call 908-555-0199 or 732-555-0142", dry_run=False, ez=ez, bland=bland,
    )
    assert bland.phones == []
    assert "more than one phone number" in ez.notes[0][1]


@pytest.mark.parametrize("dry", [True, False])
def test_robie_call_dials_only_the_typed_number(dry):
    ez = RecordingEZ(applicant=APPLICANT, policies=POLICIES, discussions=DISCUSSIONS)
    bland = RecordingBland()
    result = dispatch(
        "123", "robie-call", "Robie Call", "", discussion_id="d1",
        note_body="Call at 908-555-0199 about the renewal.", dry_run=dry, ez=ez, bland=bland,
    )
    assert bland.phones == ["+19085550199"]
    assert result["phone_source"] == "typed in note"
