"""Task-worker notes: EZLynx returns no note id, so confirm by the one new note.

Regression (task 63591513): POST .../notes returns no note id, so every
task-worker note was held as "No durable destination note ID" even though
it landed. The note is now confirmed only when the discussion, read in full
before and after, shows exactly one new note id with exactly the text
Robie sent. Anything else stays held and is never reposted.
"""

from __future__ import annotations

from typing import Any

import pytest

from robie_job_engine.ezlynx_task_jobs import ensure_task_job
from robie_job_engine.store import JobStore
from robie_job_engine.task_assignment_worker import TaskAssignmentWorker, UnverifiedNoteError
from test_ezlynx_task_intake import make_task

DISCUSSION = "849945654"
BODY = "one intent"


class WithNotesClient:
    """Discussion API fake that, like EZLynx, returns no id from POST."""

    def __init__(self, existing: list[tuple[str, str]] | None = None, *, others_after_post: list[str] = (),
                 duplicate_post: bool = False, incomplete: bool = False):
        self.notes: list[dict[str, Any]] = [{"noteId": nid, "body": text} for nid, text in (existing or [])]
        self.posts: list[tuple[str, str]] = []
        self.others_after_post = list(others_after_post)
        self.duplicate_post = duplicate_post
        self.incomplete = incomplete
        self.reads_fail = False
        self._next = 900

    def _add(self, text: str) -> None:
        self._next += 1
        self.notes.append({"noteId": str(self._next), "body": text})

    def get_discussion_ids(self, applicant_id: str) -> list[str]:
        return [DISCUSSION]

    def append_note(self, discussion_id: str, body: str, applicant_id: str | None = None) -> dict:
        self.posts.append((discussion_id, body))
        self._add(body)
        if self.duplicate_post:
            self._add(body)
        for text in self.others_after_post:
            self._add(text)
        return {}

    def get_discussion(self, discussion_id: str) -> dict:
        latest = self.notes[-1]["noteId"] if self.notes else ""
        return {"title": "Task Note", "mostRecentNoteId": latest, "noteCount": len(self.notes)}

    def get_discussion_with_notes(self, discussion_id: str) -> dict:
        if self.reads_fail:
            raise ConnectionError("read failed")
        rows = self.notes[:-1] if self.incomplete and self.posts else self.notes
        return {"discussionId": discussion_id, "notes": [dict(r) for r in rows]}


@pytest.fixture
def context(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job, _ = ensure_task_job(store, make_task())
    return store, job


def post(client, store, job):
    return TaskAssignmentWorker(discussion_client=client)._post_note_verified(store, job, DISCUSSION, BODY)


def test_note_without_returned_id_is_confirmed_by_the_one_new_note(context):
    store, job = context
    client = WithNotesClient(existing=[("101", "older note")])
    assert post(client, store, job) == "901"
    assert client.posts == [(DISCUSSION, BODY)]


def test_an_older_note_with_the_same_words_is_not_taken(context):
    store, job = context
    client = WithNotesClient(existing=[("101", BODY)])
    assert post(client, store, job) == "901"


def test_another_writers_note_in_the_same_gap_is_not_taken(context):
    store, job = context
    client = WithNotesClient(existing=[("101", "older")], others_after_post=["someone else's note"])
    assert post(client, store, job) == "901"


def test_two_new_notes_with_this_text_are_held_and_not_reposted(context):
    store, job = context
    client = WithNotesClient(duplicate_post=True)
    with pytest.raises(UnverifiedNoteError, match="No durable destination note ID"):
        post(client, store, job)
    with pytest.raises(UnverifiedNoteError):
        post(client, JobStore(store.path), job)
    assert len(client.posts) == 1


def test_incomplete_read_after_the_post_is_held(context):
    store, job = context
    client = WithNotesClient(existing=[("101", "older")], incomplete=True)
    with pytest.raises(UnverifiedNoteError):
        post(client, store, job)
    assert len(client.posts) == 1


def test_resume_after_a_failed_check_confirms_without_posting_again(context):
    store, job = context
    client = WithNotesClient(existing=[("101", "older")])
    original = client.append_note

    def post_then_reads_fail(*args, **kwargs):
        result = original(*args, **kwargs)
        client.reads_fail = True
        return result

    client.append_note = post_then_reads_fail
    with pytest.raises(Exception):
        post(client, store, job)
    client.reads_fail = False
    assert post(client, JobStore(store.path), job) == "901"
    assert len(client.posts) == 1


def test_client_without_full_read_keeps_the_old_rule(context):
    from test_ezlynx_task_intake import FakeDiscussionClient

    store, job = context
    client = FakeDiscussionClient(title="", return_note_id=False)
    with pytest.raises(UnverifiedNoteError, match="No durable destination note ID"):
        post(client, store, job)
    assert len(client.posts) == 1



# ------------------------------------------------------------- verifier
def _verify(store, client, note_id: str, body: str = BODY):
    import hashlib

    from robie_job_engine.task_assignment_worker import TaskIntakeVerifier, _norm_text
    from test_ezlynx_task_intake import _verifying_job

    action = {"test_task": False, "category": "callback", "reassigned": None,
              "note": {"discussion_id": DISCUSSION, "note_id": note_id, "text": "handoff",
                       "body_norm_sha256": hashlib.sha256(_norm_text(body).encode()).hexdigest()}}
    job = _verifying_job(store, make_task(), action)
    return TaskIntakeVerifier(discussion_client=client).verify(job, action)


def test_verifier_confirms_our_note_when_a_later_note_is_newest(tmp_path):
    client = WithNotesClient(existing=[("901", BODY), ("902", "later note by someone else")])
    result = _verify(JobStore(tmp_path / "v.db"), client, "901")
    assert result.verified is True and result.evidence.observed["note_id"] == "901"


def test_verifier_rejects_a_non_newest_note_whose_text_differs(tmp_path):
    client = WithNotesClient(existing=[("901", "different words"), ("902", "later")])
    result = _verify(JobStore(tmp_path / "v.db"), client, "901")
    assert result.verified is False
