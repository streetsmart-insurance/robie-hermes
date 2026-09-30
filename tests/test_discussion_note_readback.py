"""Discussion note confirmation matches the live EZLynx response shape.

On 2026-09-30, POST v8/discussions/{id}/notes returned 2xx with no note id.
GET v8/discussions/{id} returned metadata only. GET .../notes returned 405.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from robie_job_engine import ezlynx_discussions as disc
from robie_job_engine.discussion_note_ledger import (
    KNOWN_POSTED_DOCUMENT_IDS,
    main,
    record_known_posted_notes,
    record_posted_note,
)
from robie_job_engine.ezlynx_api_only_writes import (
    EzlynxNoteDocReadbackError,
    confirm_discussion_note,
    note_id_in_discussion,
)
from robie_job_engine.ezlynx_write_scope import EzlynxWriteScopeError

APPLICANT = "220250093"
TEST_APPLICANT = "26356199"
DISCUSSION = "819225260"
TITLE = "Additional Information - CHANGE ME"
FIELD_WORDS = ("noteCount", "mostRecentNoteId", "note_id", "NoteId", "DiscussionApi", "document_id")


class LiveShapeClient:
    """The response shapes seen on hermes-test-01. Notes list is HTTP 405."""

    def __init__(
        self,
        *,
        note_count: int = 7,
        latest: str = "700",
        after_count: int | None = None,
        after_latest: str | None = None,
        after_title: str | None = None,
        post_body: dict | None = None,
        discussion_id: str = DISCUSSION,
    ):
        self.note_count = note_count
        self.latest = latest
        self.after_count = note_count + 1 if after_count is None else after_count
        self.after_latest = "701" if after_latest is None else after_latest
        self.title = TITLE
        self.after_title = TITLE if after_title is None else after_title
        self.post_body = {} if post_body is None else post_body
        self.discussion_id = discussion_id
        self.posts = 0
        self.reads = 0
        self.note_lists = 0
        self.posted = False

    def get_discussions(self, applicant_id):
        return [{"discussionId": self.discussion_id, "title": self.title, "applicantId": applicant_id}]

    def get_discussion(self, discussion_id):
        self.reads += 1
        return {
            "discussionId": discussion_id,
            "applicantId": APPLICANT,
            "title": self.after_title if self.posted else self.title,
            "watcherUserIds": [],
            "organizationId": 1,
            "noteCount": self.after_count if self.posted else self.note_count,
            "created": "2026-01-01T00:00:00Z",
            "createdById": 1,
            "lastModified": "2026-09-30T15:00:00Z",
            "lastModifiedById": 1,
            "mostRecentNoteId": self.after_latest if self.posted else self.latest,
            "deleted": False,
        }

    def list_notes(self, discussion_id):
        self.note_lists += 1
        raise disc.DiscussionApiError(405, "HTTP 405")

    def append_note(self, discussion_id, text, note_type="Note"):
        self.posts += 1
        self.posted = True
        return dict(self.post_body)


def _file(client, text="NatGen cancellation notice was added. ROBIE was here", **kwargs):
    return disc.file_note_to_existing_discussion(client, APPLICANT, text, **kwargs)


def test_metadata_count_confirms_a_post_that_returns_no_note_id(tmp_path):
    client = LiveShapeClient()
    with pytest.raises(disc.DiscussionApiError) as listed:
        client.list_notes(DISCUSSION)
    assert listed.value.status == 405

    result = _file(client, ledger_path=tmp_path / "ledger.json", document_id="824463419")
    assert result["status"] == "filed"
    assert result["note_id"] == "701"
    assert result["read_back"] is True
    assert result["verified_by"] == "discussion"
    assert result["reason"] == "The note was added to the discussion."
    assert client.posts == 1
    assert client.reads == 2
    assert client.note_lists == 1  # the explicit 405 check above, not the filer
    for word in FIELD_WORDS:
        assert word not in result["reason"]


def test_rerun_uses_the_ledger_and_does_not_post_again(tmp_path):
    client = LiveShapeClient()
    ledger = tmp_path / "ledger.json"
    text = "Progressive memo dated 9/30/2026 was added. ROBIE was here"
    first = _file(client, text, ledger_path=ledger, document_id="501")
    assert first["status"] == "filed"
    assert client.posts == 1
    second = _file(client, text, ledger_path=ledger, document_id="501")
    assert second["status"] == "filed"
    assert second["idempotent"] is True
    assert second["note_id"] == "701"
    assert "already sent" in second["reason"]
    assert client.posts == 1
    assert client.note_lists == 0


def test_same_document_with_different_wording_is_not_posted_again(tmp_path):
    client = LiveShapeClient()
    ledger = tmp_path / "ledger.json"
    _file(client, "first wording. ROBIE was here", ledger_path=ledger, document_id="501")
    again = _file(client, "rewritten wording. ROBIE was here", ledger_path=ledger, document_id="501")
    assert again["idempotent"] is True
    assert client.posts == 1


def test_count_unchanged_stays_held_after_one_post(tmp_path):
    client = LiveShapeClient(after_count=7, after_latest="700", post_body={"noteId": "should-not-win"})
    result = _file(client, ledger_path=tmp_path / "ledger.json")
    assert result["status"] == "held"
    assert result["note_id"] is None
    assert result["read_back"] is False
    assert "same notes" in result["reason"]
    assert client.posts == 1
    for word in FIELD_WORDS:
        assert word not in result["reason"]


def test_count_jump_stays_held_and_does_not_post_twice(tmp_path):
    client = LiveShapeClient(after_count=9, after_latest="999")
    result = _file(client, ledger_path=tmp_path / "ledger.json")
    assert result["status"] == "held"
    assert "exactly one new note" in result["reason"]
    assert client.posts == 1


def test_latest_note_unchanged_stays_held(tmp_path):
    client = LiveShapeClient(after_count=8, after_latest="700")
    result = _file(client, ledger_path=tmp_path / "ledger.json")
    assert result["status"] == "held"
    assert "latest note did not change" in result["reason"]
    assert client.posts == 1


def test_title_change_stays_held(tmp_path):
    client = LiveShapeClient(after_title="Some other discussion")
    result = _file(client, ledger_path=tmp_path / "ledger.json")
    assert result["status"] == "held"
    assert "title changed" in result["reason"]
    assert client.posts == 1


def test_unread_discussion_is_not_posted(tmp_path):
    class Blind(LiveShapeClient):
        def get_discussion(self, discussion_id):
            raise disc.DiscussionApiError(None, "down")

    client = Blind()
    result = _file(client, ledger_path=tmp_path / "ledger.json")
    assert result["status"] == "held"
    assert "not sent" in result["reason"]
    assert client.posts == 0


def test_known_posted_documents_are_not_sent_again(tmp_path):
    client = LiveShapeClient()
    with patch(
        "robie_job_engine.ezlynx_write_scope.ALLOWED_EZLYNX_WRITE_APPLICANT_IDS",
        frozenset({TEST_APPLICANT}),
    ):
        for document_id in KNOWN_POSTED_DOCUMENT_IDS:
            result = disc.file_note_to_existing_discussion(
                client,
                TEST_APPLICANT,
                "NatGen cancellation notice dated 9/30/2026 for someone, policy ending in 1506, "
                "saved to this test account. ROBIE was here",
                document_id=document_id,
                ledger_path=tmp_path / "empty.json",
            )
            assert result["status"] == "filed"
            assert result["idempotent"] is True
            assert result["discussion_id"] == DISCUSSION
            assert "already sent" in result["reason"]
    assert client.posts == 0
    assert client.note_lists == 0
    assert not (tmp_path / "empty.json").exists()


def test_record_known_notes_writes_a_file_and_does_not_post(tmp_path, capsys):
    ledger = tmp_path / "ledger.json"
    rows = record_known_posted_notes(ledger)
    assert len(rows) == 6
    saved = json.loads(ledger.read_text(encoding="utf-8"))
    assert {row["document_id"] for row in saved["notes"]} == set(KNOWN_POSTED_DOCUMENT_IDS)
    assert all(row["note_text_sha256"] == "" for row in saved["notes"])
    assert main(["--ledger", str(ledger), "--record-known"]) == 0
    assert json.loads(capsys.readouterr().out) == {"recorded": 6, "posted": False}
    saved = json.loads(ledger.read_text(encoding="utf-8"))
    assert len(saved["notes"]) == 6
    extra = record_posted_note(
        TEST_APPLICANT,
        DISCUSSION,
        document_id="999",
        ledger_path=ledger,
    )
    assert extra["document_id"] == "999"
    assert "999" not in KNOWN_POSTED_DOCUMENT_IDS


def test_confirm_accepts_metadata_latest_note_id():
    record = {
        "discussionId": DISCUSSION,
        "title": TITLE,
        "noteCount": 9,
        "mostRecentNoteId": "701",
        "deleted": False,
    }
    assert note_id_in_discussion(record, "701") is True
    assert note_id_in_discussion(record, "700") is False

    class Getter:
        def get_discussion(self, discussion_id):
            return dict(record)

    confirmed = confirm_discussion_note(Getter(), DISCUSSION, "701")
    assert confirmed["note_id"] == "701"
    assert confirmed["read_back"] is True
    with pytest.raises(EzlynxNoteDocReadbackError):
        confirm_discussion_note(Getter(), DISCUSSION, "700")


def test_phone_number_and_other_applicant_still_write_nothing():
    client = LiveShapeClient()
    with pytest.raises(disc.DiscussionApiError, match="phone-number-like"):
        _file(client, "Call 603-769-3995")
    with patch(
        "robie_job_engine.ezlynx_write_scope.ALLOWED_EZLYNX_WRITE_APPLICANT_IDS",
        frozenset({APPLICANT}),
    ):
        with pytest.raises(EzlynxWriteScopeError):
            disc.file_note_to_existing_discussion(client, "999999999", "Filed note")
    assert client.posts == 0


def test_corrupt_ledger_does_not_post(tmp_path):
    ledger = tmp_path / "ledger.json"
    ledger.write_text("{", encoding="utf-8")
    client = LiveShapeClient()
    result = _file(client, ledger_path=ledger)
    assert result["status"] == "held"
    assert "nothing was sent" in result["reason"]
    assert client.posts == 0
