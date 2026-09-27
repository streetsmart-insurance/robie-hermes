"""Tests for robie_job_engine.certificate_filing.

The DiscussionApiClient is faked: no test may touch the network or Secret
Manager. The fake models a discussion whose state changes only when
append_note is called.
"""

from datetime import datetime, timezone

import pytest

from robie_job_engine import certificate_filing as cf


class FakeClient:
    """Minimal DiscussionApiClient double backed by an in-memory discussion."""

    def __init__(self, *, title="Certificate of Insurance Example", note_count=2,
                 last_modified="2026-09-21T10:00:00Z", extra_discussions=()):
        self.state = {
            "discussionId": "111",
            "title": title,
            "noteCount": note_count,
            "mostRecentNoteId": "9001",
            "lastModified": last_modified,
        }
        self.extra = list(extra_discussions)
        self.appended = []

    # -- client surface used by certificate_filing --
    def get_discussions(self, account_id):
        rows = [dict(self.state)]
        rows.extend(self.extra)
        return rows

    def get_discussion(self, discussion_id):
        assert str(discussion_id) == self.state["discussionId"]
        return dict(self.state)

    def append_note(self, discussion_id, body):
        assert str(discussion_id) == self.state["discussionId"]
        self.appended.append(body)
        self.state["noteCount"] += 1
        self.state["mostRecentNoteId"] = str(int(self.state["mostRecentNoteId"]) + 1)
        self.state["lastModified"] = "2026-09-21T12:38:01Z"
        return {"noteId": self.state["mostRecentNoteId"]}


def item(**over):
    base = {
        "key": "ex1",
        "account_id": "150750271",
        "discussion_id": "111",
        "note": "Client requests updated docs. CSR to issue.",
    }
    base.update(over)
    return base


def test_explicit_id_files_and_verifies():
    client = FakeClient()
    (res,) = cf.file_certificate_notes(client, [item()])
    assert res["status"] == "filed"
    assert res["note_id"] == "9002"
    assert client.appended == ["Client requests updated docs. CSR to issue."]
    assert res["after"]["noteCount"] == 3


def test_title_match_resolves_single_discussion():
    client = FakeClient()
    (res,) = cf.file_certificate_notes(
        client, [item(discussion_id=None, title_match=["example"])]
    )
    assert res["status"] == "filed"
    assert res["discussion_id"] == "111"


def test_title_match_zero_or_many_fails_closed():
    client = FakeClient()
    (res,) = cf.file_certificate_notes(
        client, [item(discussion_id=None, title_match=["no-such-title"])]
    )
    assert res["status"] == "pending"
    assert client.appended == []

    dup = {"discussionId": "112", "title": "Certificate of Insurance Example Duplicate",
           "noteCount": 1, "mostRecentNoteId": "5", "lastModified": "2026-09-20T00:00:00Z"}
    client2 = FakeClient(extra_discussions=[dup])
    (res2,) = cf.file_certificate_notes(
        client2, [item(discussion_id=None, title_match=["example"])]
    )
    assert res2["status"] == "pending"
    assert "2 discussions" in res2["reason"]
    assert client2.appended == []


def test_duplicate_guard_skips_recently_modified():
    client = FakeClient(last_modified="2026-09-21T12:38:01Z")
    cutoff = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)
    (res,) = cf.file_certificate_notes(
        client, [item()], skip_if_modified_after=cutoff
    )
    assert res["status"] == "skipped"
    assert "duplicate" in res["reason"]
    assert client.appended == []


def test_duplicate_guard_allows_older_discussion():
    client = FakeClient(last_modified="2026-09-21T10:00:00Z")
    cutoff = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)
    (res,) = cf.file_certificate_notes(
        client, [item()], skip_if_modified_after=cutoff
    )
    assert res["status"] == "filed"


def test_failed_readback_is_filed_unverified_not_silent_success():
    class StuckClient(FakeClient):
        def append_note(self, discussion_id, body):
            self.appended.append(body)
            return {"noteId": "9001"}  # state never moves: lost write

    client = StuckClient()
    (res,) = cf.file_certificate_notes(client, [item()])
    assert res["status"] == "filed_unverified"
    assert "count_ok=False" in res["reason"]


def test_dry_run_writes_nothing():
    client = FakeClient()
    (res,) = cf.file_certificate_notes(client, [item()], dry_run=True)
    assert res["status"] == "skipped"
    assert client.appended == []
    assert "before" in res and "after" not in res


def test_empty_note_is_pending():
    client = FakeClient()
    (res,) = cf.file_certificate_notes(client, [item(note="   ")])
    assert res["status"] == "pending"
    assert client.appended == []


def test_one_bad_item_does_not_kill_batch():
    client = FakeClient()
    results = cf.file_certificate_notes(
        client, [item(key="bad", discussion_id=None), item(key="good")]
    )
    assert [r["status"] for r in results] == ["pending", "filed"]


def test_parse_dt_handles_z_and_offsets():
    assert cf.parse_dt("2026-09-21T12:38:01.17Z") is not None
    assert cf.parse_dt("2026-09-21T12:38:01.17+00:00") is not None
    assert cf.parse_dt("not a date") is None
    assert cf.parse_dt("") is None
    assert cf.parse_dt(None) is None
