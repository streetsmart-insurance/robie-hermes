"""Note write-back follow-ups to #823, #825 and #831.

#823 files an outcome note to a discussion the task names, even untitled.
#825 pins the same rule for the Ascend preview and the task worker.
#831 confirms the TASK WORKER's note by the one new id with the text sent.

Left over:
1. The intake's hold note ("too old" / "could not tell when") still needed the
   POST response to carry a note id. EZLynx returns none, so a hold note that
   landed was recorded as "attempted, no note id" (reads as uncertain).
2. A task's discussion id that the applicant's list omits (a discussion made
   moments ago, or an id written as 851486023.0) was "no matching discussion".
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from robie_job_engine import ezlynx_discussions as disc
from robie_job_engine import ezlynx_task_intake as intake
from tests.test_note_untitled_and_body_confirmation import (
    APPLICANT,
    NOTE,
    UNTITLED_ID,
    Client as BaseClient,
    _file,
    _titled,
    _untitled,
)

TASK = SimpleNamespace(task_id="90026158", discussion_id=UNTITLED_ID, applicant_id=APPLICANT)


class HoldClient(BaseClient):
    """The live shapes, plus the applicant id the write guard passes through."""

    def __init__(self, discussions=None, **kw):
        super().__init__(discussions=discussions or [_untitled()], **kw)

    def append_note(self, discussion_id, text, note_type="Note", applicant_id=None):
        return super().append_note(discussion_id, text, note_type)

    def get_discussion_ids(self, applicant_id):
        return [d["discussionId"] for d in self.discussions]


# ---- 1. hold note ----------------------------------------------------------
@pytest.mark.parametrize("reason", ["too_old", "unparseable"])
def test_a_hold_note_that_landed_is_confirmed_without_a_response_id(reason):
    client = HoldClient()
    assert intake._post_hold_note(client, TASK, reason) is True
    assert client.posts == 1  # one POST, never repeated by the confirmation


def test_hold_note_stays_unconfirmed_when_the_new_note_is_someone_elses():
    client = HoldClient(with_notes="other_text")
    assert intake._post_hold_note(client, TASK, "too_old") is False
    assert client.posts == 1


def test_hold_note_stays_unconfirmed_when_the_discussion_cannot_be_read_whole():
    client = HoldClient(with_notes="error")
    assert intake._post_hold_note(client, TASK, "too_old") is False
    assert client.posts == 1


def test_an_older_identical_hold_note_does_not_confirm_a_new_one():
    client = HoldClient(with_notes="missing")
    # The old note already says exactly what the hold says.
    original = client.get_discussion_with_notes

    def older_identical(discussion_id):
        record = original(discussion_id)
        record["notes"][0]["body"] = intake._HOLD_TOO_OLD
        return record

    client.get_discussion_with_notes = older_identical
    assert intake._post_hold_note(client, TASK, "too_old") is False


def test_two_new_notes_with_the_same_text_are_not_confirmed():
    client = HoldClient()
    original = client.get_discussion_with_notes

    def doubled(discussion_id):
        record = original(discussion_id)
        record["notes"].append({"noteId": "1002", "body": client.posted_text})
        return record

    client.get_discussion_with_notes = doubled
    assert intake._post_hold_note(client, TASK, "too_old") is False


def test_a_response_with_a_note_id_is_still_accepted_without_any_read():
    class Plain:
        posts = 0

        def get_discussion_ids(self, applicant_id):
            return [UNTITLED_ID]

        def append_note(self, discussion_id, body, note_type="Note", applicant_id=None):
            Plain.posts += 1
            return {"noteId": "N-1"}

    assert intake._post_hold_note(Plain(), TASK, "too_old") is True
    assert Plain.posts == 1


def test_a_client_that_cannot_prove_the_prior_notes_stays_unconfirmed():
    class NoReads:
        def get_discussion_ids(self, applicant_id):
            return [UNTITLED_ID]

        def append_note(self, discussion_id, body, note_type="Note", applicant_id=None):
            return {}

    assert intake._post_hold_note(NoReads(), TASK, "too_old") is False


def test_hold_note_for_another_applicants_discussion_is_still_refused():
    client = HoldClient()
    other = SimpleNamespace(task_id="1", discussion_id="999", applicant_id=APPLICANT)
    assert intake._post_hold_note(client, other, "too_old") is False
    assert client.posts == 0


# ---- 2. pinned discussion the list omits -------------------------------------
class ListLags(BaseClient):
    """by-applicant does not show the discussion yet; a direct read does."""

    def __init__(self, *, owner=APPLICANT, deleted=False, **kw):
        super().__init__(discussions=[_titled()], **kw)
        self.owner, self.deleted = owner, deleted
        self.id = UNTITLED_ID
        self.allow_unlisted = True  # the list lags; a direct read still answers

    def get_discussion(self, discussion_id):
        record = super().get_discussion(discussion_id)
        record["applicantId"] = int(self.owner)
        record["deleted"] = self.deleted
        return record


def test_a_pinned_discussion_missing_from_the_list_is_read_directly(tmp_path):
    client = ListLags()
    result = _file(client, discussion_id=UNTITLED_ID, ledger_path=tmp_path / "l.json")
    assert result["status"] == "filed", result
    assert result["discussion_id"] == UNTITLED_ID and client.posts == 1


def test_a_direct_read_of_someone_elses_discussion_is_not_a_match(tmp_path):
    client = ListLags(owner="123456")
    result = _file(client, discussion_id=UNTITLED_ID, ledger_path=tmp_path / "l.json")
    assert result["status"] == "pending" and result["reason"] == "no matching discussion"
    assert client.posts == 0


def test_a_direct_read_of_a_deleted_discussion_is_not_a_match(tmp_path):
    client = ListLags(deleted=True)
    result = _file(client, discussion_id=UNTITLED_ID, ledger_path=tmp_path / "l.json")
    assert result["reason"] == "no matching discussion" and client.posts == 0


def test_a_direct_read_that_fails_or_has_no_owner_is_not_a_match(tmp_path):
    failing = ListLags()
    failing.get_discussion = lambda _id: (_ for _ in ()).throw(disc.DiscussionApiError(500, "x"))
    assert _file(failing, discussion_id=UNTITLED_ID, ledger_path=tmp_path / "a.json")["status"] == "pending"
    anonymous = ListLags()
    original = anonymous.get_discussion
    anonymous.get_discussion = lambda i: {k: v for k, v in original(i).items() if k != "applicantId"}
    assert _file(anonymous, discussion_id=UNTITLED_ID, ledger_path=tmp_path / "b.json")["status"] == "pending"
    assert failing.posts == 0 and anonymous.posts == 0


def test_the_direct_read_is_not_made_before_the_post_when_the_list_matches(tmp_path):
    reads = []

    class Counting(BaseClient):
        def get_discussion(self, discussion_id):
            reads.append(self.posts)
            return super().get_discussion(discussion_id)

    client = Counting(discussions=[_untitled()])
    result = _file(client, discussion_id=UNTITLED_ID, ledger_path=tmp_path / "l.json")
    assert result["status"] == "filed"
    assert reads and reads[0] == 0  # the normal pre-post metadata read, nothing extra
    assert reads.count(0) == 1


@pytest.mark.parametrize("written", ["851287313.0", "851,287,313", " 851287313 "])
def test_an_id_written_as_a_float_or_with_commas_still_matches(tmp_path, written):
    client = BaseClient(discussions=[_untitled()])
    result = _file(client, discussion_id=written, ledger_path=tmp_path / "l.json")
    assert result["status"] == "filed" and result["discussion_id"] == UNTITLED_ID


def test_two_list_rows_with_the_id_stay_ambiguous_and_post_nothing(tmp_path):
    client = BaseClient(discussions=[_untitled(), _untitled()])
    result = _file(client, discussion_id=UNTITLED_ID, ledger_path=tmp_path / "l.json")
    assert result["reason"] == "no matching discussion" and client.posts == 0


def test_the_guessing_path_is_unchanged(tmp_path):
    client = BaseClient(discussions=[_untitled()])
    assert _file(client, ledger_path=tmp_path / "l.json")["status"] == "pending"
    assert client.posts == 0


# ---- the intake records a confirmed hold note as posted -----------------------
def test_run_intake_marks_a_confirmed_hold_note_hitl_not_attempted(tmp_path, monkeypatch):
    from robie_job_engine.ezlynx_seen_tasks import SeenTaskStore
    from robie_job_engine.ezlynx_task_intake import run_intake
    from tests.test_task_intake_review_blockers import _Bland, _install_fakes, _report, _task

    db = tmp_path / "jobs.db"
    SeenTaskStore(str(db)).observe(["prior"], report_digest="prior")
    client = HoldClient(discussions=[_untitled(discussion_id="70026158")])
    _install_fakes(monkeypatch, client, _Bland())
    old = _task(
        task_id="90026158", created_at="2026-10-01T09:00:00",
        created_at_et="2026-10-01T10:00:00-04:00", created_date="2026-10-01",
    )
    monkeypatch.setattr(
        "robie_job_engine.ezlynx_task_intake.fetch_latest_task_report",
        lambda _s: _report(old),
    )
    assert run_intake(db_path=str(db)) == 0
    assert SeenTaskStore(str(db)).statuses()["90026158"] == "hitl"
    assert client.posts == 1
    assert run_intake(db_path=str(db)) == 0  # the next tick never posts again
    assert client.posts == 1


def test_a_discussion_the_list_shows_as_deleted_is_never_second_guessed(tmp_path):
    client = ListLags()
    client.discussions = [_untitled(deleted=True)]  # listed, and gone
    result = _file(client, discussion_id=UNTITLED_ID, ledger_path=tmp_path / "l.json")
    assert result["reason"] == "no matching discussion" and client.posts == 0
