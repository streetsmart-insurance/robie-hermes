"""Two note bugs seen on hermes-poc-01 on 2026-10-09.

1. A task that names a discussion with no title ("no matching discussion").
   The untitled filter belongs to the guessing path only. Two empty titles
   are the same title when the post is confirmed.
2. robie-filer posted a note, then held it as "could not be told apart from
   another note": the metadata read has counts and ids but no note text, and
   the POST returns no note id. One extra read that includes note bodies
   confirms the note by its text. It is a GET; the note is never posted twice.
"""

from __future__ import annotations

import json

import pytest

from robie_job_engine import ezlynx_discussions as disc
from robie_job_engine import robie_call_handler as rch
from robie_job_engine.discussion_note_ledger import SENT_UNCONFIRMED

APPLICANT = "220250093"
UNTITLED_ID = "851287313"
TITLED_ID = "841872781"
NOTE = "Saved to Documents: Robie filer QA step 1 - safe to delete.txt"


class Client:
    """Live response shapes: metadata read has no bodies, POST returns nothing."""

    def __init__(self, *, discussions, title="", note_count=1, latest="1000",
                 new_latest="1001", with_notes="ok", post_body=None):
        self.discussions = discussions
        self.title = title
        self.note_count = note_count
        self.latest = latest
        self.new_latest = new_latest
        self.with_notes_mode = with_notes  # ok | other_text | missing | error | none
        self.post_body = {} if post_body is None else post_body
        self.posted_text = None
        self.posts = 0
        self.bodies_reads = 0
        self.id = discussions[0]["discussionId"] if discussions else UNTITLED_ID

    def get_discussions(self, applicant_id):
        return list(self.discussions)

    allow_unlisted = False

    def get_discussion(self, discussion_id):
        # A real read of an id that is not on this applicant is a 404.
        known = {str(d["discussionId"]) for d in self.discussions}
        if str(discussion_id) not in known and not self.allow_unlisted:
            raise disc.DiscussionApiError(404, "HTTP 404")
        after = self.posts > 0
        return {
            "discussionId": discussion_id,
            "applicantId": int(APPLICANT),
            "title": self.title,
            "noteCount": self.note_count + (1 if after else 0),
            "mostRecentNoteId": self.new_latest if after else self.latest,
            "deleted": False,
        }

    def append_note(self, discussion_id, text, note_type="Note"):
        self.posts += 1
        self.posted_text = text
        return dict(self.post_body)

    def get_discussion_with_notes(self, discussion_id):
        self.bodies_reads += 1
        if self.with_notes_mode == "error":
            raise disc.DiscussionApiError(500, "HTTP 500")
        notes = [{"noteId": self.latest, "body": "an older note"}]
        if self.posts and self.with_notes_mode == "ok":
            notes.append({"noteId": self.new_latest, "body": self.posted_text})
        elif self.posts and self.with_notes_mode == "other_text":
            notes.append({"noteId": self.new_latest, "body": "someone else wrote this"})
        elif self.posts and self.with_notes_mode == "missing":
            pass  # the new id is not in the list and no body matches
        return {"discussionId": discussion_id, "notes": notes}


def _untitled(discussion_id=UNTITLED_ID, **extra):
    return {"discussionId": discussion_id, "title": "", "applicantId": int(APPLICANT), **extra}


def _titled(discussion_id=TITLED_ID, title="Test Note", **extra):
    return {"discussionId": discussion_id, "title": title, "applicantId": int(APPLICANT), **extra}


def _file(client, text=NOTE, **kwargs):
    return disc.file_note_to_existing_discussion(client, APPLICANT, text, **kwargs)


# ---- 1. task-given untitled discussion --------------------------------------
def test_task_given_untitled_discussion_is_filed_and_confirmed(tmp_path):
    client = Client(discussions=[_untitled()], title="")
    result = _file(client, discussion_id=UNTITLED_ID, ledger_path=tmp_path / "l.json")
    assert result["status"] == "filed", result
    assert result["discussion_id"] == UNTITLED_ID
    assert result["note_id"] == "1001"
    assert result["verified_by"] == "text"
    assert result["read_back"] is True
    assert client.posts == 1
    saved = json.loads((tmp_path / "l.json").read_text(encoding="utf-8"))
    assert saved["notes"][0]["confirmation"] == "confirmed"


def test_task_given_discussion_must_exist_and_not_be_deleted(tmp_path):
    client = Client(discussions=[_untitled(deleted=True), _titled()])
    missing = _file(client, discussion_id=UNTITLED_ID, ledger_path=tmp_path / "a.json")
    assert missing["status"] == "pending"
    assert missing["reason"] == "no matching discussion"
    other = _file(client, discussion_id="999", ledger_path=tmp_path / "b.json")
    assert other["reason"] == "no matching discussion"
    assert client.posts == 0


def test_guessing_path_still_skips_untitled_discussions(tmp_path):
    client = Client(discussions=[_untitled()])
    result = _file(client, ledger_path=tmp_path / "l.json")  # no discussion id
    assert result["status"] == "pending"
    assert client.posts == 0
    both = Client(discussions=[_untitled(), _titled()], title="Test Note")
    chosen = _file(both, ledger_path=tmp_path / "m.json")
    assert chosen["discussion_id"] == TITLED_ID
    assert both.posts == 1


def test_two_empty_titles_are_unchanged_but_a_title_change_still_holds(tmp_path):
    before = disc.discussion_note_snapshot({"title": "", "noteCount": 1, "mostRecentNoteId": "1"})
    after = disc.discussion_note_snapshot({"title": "", "noteCount": 2, "mostRecentNoteId": "2"})
    assert disc._metadata_note_confirmation(before, after)[0] is True
    renamed = disc.discussion_note_snapshot({"title": "Renamed", "noteCount": 2, "mostRecentNoteId": "2"})
    ok, reason = disc._metadata_note_confirmation(before, renamed)
    assert ok is False and "title changed" in reason


# ---- 2. confirm by note body ------------------------------------------------
def test_posted_note_is_confirmed_by_its_body(tmp_path):
    client = Client(discussions=[_titled()], title="Test Note")
    result = _file(client, discussion_id=TITLED_ID, ledger_path=tmp_path / "l.json")
    assert result["status"] == "filed"
    assert result["verified_by"] == "text"
    assert result["note_id"] == "1001"
    assert client.posts == 1
    assert client.bodies_reads == 1
    saved = json.loads((tmp_path / "l.json").read_text(encoding="utf-8"))
    assert saved["notes"][0]["note_id"] == "1001"
    assert saved["notes"][0]["confirmation"] == "confirmed"


def test_note_whose_text_is_not_the_newest_is_still_unconfirmed(tmp_path):
    client = Client(discussions=[_titled()], title="Test Note", with_notes="other_text")
    result = _file(client, discussion_id=TITLED_ID, ledger_path=tmp_path / "l.json")
    assert result["status"] == "held"
    assert result["note_id"] is None
    assert result["confirmation"] == SENT_UNCONFIRMED
    assert "did not match" in result["reason"]
    assert client.posts == 1


@pytest.mark.parametrize("mode", ["missing", "error", "none"])
def test_a_note_that_cannot_be_found_is_reported_unconfirmed(tmp_path, mode):
    client = Client(discussions=[_titled()], title="Test Note", with_notes=mode)
    if mode == "none":
        client.get_discussion_with_notes = None  # an older client without the read
    result = _file(client, discussion_id=TITLED_ID, ledger_path=tmp_path / "l.json")
    assert result["status"] == "held"
    assert result["note_id"] is None
    assert result["read_back"] is False
    assert result["confirmation"] == SENT_UNCONFIRMED
    assert client.posts == 1


def test_an_older_identical_note_does_not_confirm_a_new_one(tmp_path):
    client = Client(discussions=[_titled()], title="Test Note", with_notes="missing")
    original = client.get_discussion_with_notes

    def with_old_copy(discussion_id):
        record = original(discussion_id)
        record["notes"].append({"noteId": "500", "body": NOTE})  # same text, old id
        return record

    client.get_discussion_with_notes = with_old_copy
    result = _file(client, discussion_id=TITLED_ID, ledger_path=tmp_path / "l.json")
    assert result["status"] == "held"
    assert result["note_id"] is None


def test_confirming_never_posts_again(tmp_path):
    ledger = tmp_path / "l.json"
    ok = Client(discussions=[_titled()], title="Test Note")
    first = _file(ok, discussion_id=TITLED_ID, ledger_path=ledger)
    again = _file(ok, discussion_id=TITLED_ID, ledger_path=ledger)
    assert first["status"] == "filed"
    # The same text again in the same day asks first; it does not post.
    assert again["status"] == "already_posted"
    assert ok.posts == 1
    assert ok.bodies_reads == 1

    held_ledger = tmp_path / "h.json"
    held = Client(discussions=[_titled()], title="Test Note", with_notes="missing")
    assert _file(held, discussion_id=TITLED_ID, ledger_path=held_ledger)["status"] == "held"
    repeat = _file(held, discussion_id=TITLED_ID, ledger_path=held_ledger)
    assert repeat["status"] == "already_posted"
    assert held.posts == 1


# ---- the Robie Call writeback shares the same code --------------------------
def test_robie_call_writeback_files_to_an_untitled_task_discussion(tmp_path, monkeypatch):
    real = disc.file_note_to_existing_discussion
    ledger = tmp_path / "call.json"
    monkeypatch.setattr(
        disc, "file_note_to_existing_discussion",
        lambda *a, **k: real(*a, ledger_path=ledger, **k),
    )
    client = Client(discussions=[_untitled()], title="")
    token = rch._outcome_discussion_id.set(UNTITLED_ID)
    try:
        result = rch._writeback_outcome_note(client, APPLICANT, NOTE)
    finally:
        rch._outcome_discussion_id.reset(token)
    assert result["status"] == "filed", result
    assert result["discussion_id"] == UNTITLED_ID
    assert result["note_id"] == "1001"
    assert client.posts == 1
