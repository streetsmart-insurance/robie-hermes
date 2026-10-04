#!/usr/bin/env python3
"""Tests for the EZLynx task intake: durable jobs, honest notes, retries,
reassignment routing, independent verification, inbox envelope, health.

Fakes stand in for Gmail/EZLynx; a REAL JobStore on a temp DB proves the
durability contract (restarts must not duplicate work — an in-memory
fake cannot prove that).
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from robie_job_engine import ezlynx_task_jobs as jobs_mod
from robie_job_engine.ezlynx_task_inbox import (
    EXPECTED_SENDER,
    EXPECTED_SUBJECT,
    TaskInboxError,
    fetch_latest_task_report,
)
from robie_job_engine.ezlynx_task_intake_health import check_intake
from robie_job_engine.ezlynx_task_report import AssignedTask, parse_task_report
from robie_job_engine.models import JobStatus
from robie_job_engine.store import JobStore
from robie_job_engine.task_assignment_worker import (
    NeedsHuman,
    TaskAssignmentWorker,
    TaskIntakeVerifier,
    UnverifiedNoteError,
    is_test_task,
    reassign_target,
)


# ---------------------------------------------------------------- fakes

CSV_HEADERS = (
    "Task ID,Applicant ID,Account Name,Task Assigned To,Task Status,"
    "Task Due Date,Task Priority,Task Created Date,Task Last Modified Date,"
    "Note,Activity Type,Discussion ID,Task Created By,Assigned Producer,CSR"
)


def make_csv(rows: list[str]) -> str:
    return CSV_HEADERS + "\n" + "\n".join(rows) + "\n"


def make_task(**overrides: Any) -> AssignedTask:
    base = dict(
        task_id="63429523",
        title="Task Note",
        description="Please call the client about their quote.",
        applicant_id="25486692",
        applicant_name="Jake N Ferrara",
        assigned_to="Robie AI",
        due_date="2026-10-05",
        priority="Normal",
        created_date="2026-10-03",
        status="Open",
        discussion_id="849945654",
        last_modified="2026-10-03T10:00:00",
        created_by="Carlo Ferrara",
        assigned_producer="",
        csr="",
    )
    base.update(overrides)
    return AssignedTask(**base)


class FakeDiscussionClient:
    """Discussion API fake with read-back snapshots."""

    def __init__(
        self,
        *,
        fail_times: int = 0,
        confirm: bool = True,
        api_note_id: str = "note-123",
        return_note_id: bool = True,
        title: str = "Task Note",
        extra_notes: int = 0,
        already_posted: bool = False,
    ):
        self.posts: list[tuple[str, str]] = []
        self.fail_times = fail_times
        self.confirm = confirm
        self.api_note_id = api_note_id
        self.return_note_id = return_note_id
        self.title = title
        self.extra_notes = extra_notes  # simulate concurrent writers
        self._latest = "note-000"
        self._count = 5
        self.force_latest: str | None = None
        self._posted = already_posted

    def append_note(self, discussion_id: str, body: str) -> dict[str, Any]:
        self.posts.append((discussion_id, body))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionError("transient network blip")
        self._posted = True
        return {"note_id": self.api_note_id} if self.return_note_id else {}

    def _snapshot(self) -> dict[str, Any]:
        if not self._posted:
            return {"title": self.title, "mostRecentNoteId": self._latest,
                    "noteCount": self._count}
        latest = self.api_note_id if self.confirm else self._latest
        if self.force_latest is not None:
            latest = self.force_latest
        count = self._count + (1 if self.confirm else 0) + self.extra_notes
        return {"title": self.title, "mostRecentNoteId": latest, "noteCount": count}

    def get_discussion(self, discussion_id: str) -> dict[str, Any]:
        return self._snapshot()


class FakeReassigner:
    def __init__(self, assignee: str = "Carlo Ferrara"):
        self.calls: list[tuple[str, str, str]] = []
        self.read_calls: list[tuple[str, str, str]] = []
        self.assignee = assignee

    def reassign(self, task_id: str, applicant_id: str, new_assignee: str,
                 description: str = "", expected_assignee: str = "Robie AI") -> str:
        self.calls.append((task_id, applicant_id, new_assignee))
        self.last_description = description
        return new_assignee

    def read_assignee(self, task_id: str, applicant_id: str,
                      description: str = "") -> str:
        self.read_calls.append((task_id, applicant_id, description))
        return self.assignee


@pytest.fixture()
def store():
    with tempfile.TemporaryDirectory() as tmp:
        yield JobStore(str(Path(tmp) / "jobs.db"))


def _job_payload(task: AssignedTask) -> dict[str, Any]:
    return jobs_mod.job_payload_for_task(task)


# ---------------------------------------------------------------- parser

def test_parse_routing_columns():
    row = (
        '63429523,25486692,Jake N Ferrara,Robie AI,Open,2026-10-05,Normal,'
        '2026-10-03,2026-10-03T10:00:00,"Call about quote",Task Note,849945654,'
        'Carlo Ferrara,,Jazmin Molina'
    )
    tasks = parse_task_report(make_csv([row]))
    assert len(tasks) == 1
    assert tasks[0].created_by == "Carlo Ferrara"
    assert tasks[0].assigned_producer == ""
    assert tasks[0].csr == "Jazmin Molina"


def test_parse_without_routing_columns_ok():
    headers = CSV_HEADERS.replace(",Task Created By,Assigned Producer,CSR", "")
    row = (
        '63429523,25486692,Jake N Ferrara,Robie AI,Open,2026-10-05,Normal,'
        '2026-10-03,2026-10-03T10:00:00,"Call about quote",Task Note,849945654'
    )
    tasks = parse_task_report(headers + "\n" + row + "\n")
    assert tasks[0].created_by == "" and tasks[0].csr == ""


# ---------------------------------------------------------------- durable jobs

def test_idempotency_key_stable():
    assert jobs_mod.task_idempotency_key("63429523") == "ezlynx-task:63429523"
    assert jobs_mod.task_idempotency_key(" 63429523 ") == "ezlynx-task:63429523"


def test_ensure_task_job_creates_pending_with_ids(store):
    task = make_task()
    job, created = jobs_mod.ensure_task_job(store, task)
    assert created is True
    assert job["status"] == JobStatus.PENDING.value
    payload = job["payload"]
    assert payload["task_id"] == "63429523"
    assert payload["applicant_id"] == "25486692"
    assert payload["discussion_id"] == "849945654"
    assert payload["locator"] == "ezlynx-discussion:849945654"


def test_ensure_task_job_idempotent_across_restarts(store, tmp_path):
    """Simulate a restart: new JobStore on the same DB file."""
    db = tmp_path / "jobs.db"
    s1 = JobStore(str(db))
    task = make_task()
    job1, created1 = jobs_mod.ensure_task_job(s1, task)
    assert created1 is True

    s2 = JobStore(str(db))  # "restart"
    job2, created2 = jobs_mod.ensure_task_job(s2, task)
    assert created2 is False
    assert job2["id"] == job1["id"]

    with s2.connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE idempotency_key=?",
            (jobs_mod.task_idempotency_key("63429523"),),
        ).fetchone()[0]
    assert count == 1


def test_ensure_task_job_reopens_terminal_on_change(store):
    task = make_task()
    job, _ = jobs_mod.ensure_task_job(store, task)
    store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
    store.transition(job["id"], JobStatus.VERIFYING, expected={JobStatus.RUNNING})
    # (skip the guard: mark complete directly via SQL for the fixture)
    with store.connect() as conn:
        conn.execute("UPDATE jobs SET status=? WHERE id=?", (JobStatus.COMPLETE.value, job["id"]))

    changed = make_task(last_modified="2026-10-03T11:00:00")
    job2, created = jobs_mod.ensure_task_job(store, changed)
    assert created is False
    assert job2["id"] == job["id"]
    assert job2["status"] == JobStatus.PENDING.value
    assert job2["payload"]["last_modified"] == "2026-10-03T11:00:00"


def test_ensure_task_job_waiting_keeps_status(store):
    task = make_task()
    job, _ = jobs_mod.ensure_task_job(store, task)
    store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
    store.transition(
        job["id"], JobStatus.AWAITING_HUMAN_INPUT,
        expected={JobStatus.RUNNING}, resume_status=JobStatus.PENDING, release_lease=True,
    )
    changed = make_task(last_modified="2026-10-03T12:00:00")
    job2, _ = jobs_mod.ensure_task_job(store, changed)
    assert job2["status"] == JobStatus.AWAITING_HUMAN_INPUT.value
    assert job2["payload"]["last_modified"] == "2026-10-03T12:00:00"


# ---------------------------------------------------------------- routing

def test_reassign_target_precedence():
    t, f = reassign_target(make_task(created_by="Carlo Ferrara", assigned_producer="Mike Sosa", csr="Jazmin Molina"))
    assert (t, f) == ("Carlo Ferrara", "task_created_by")
    t, f = reassign_target(make_task(created_by="", assigned_producer="Mike Sosa", csr="Jazmin Molina"))
    assert (t, f) == ("Mike Sosa", "assigned_producer")
    t, f = reassign_target(make_task(created_by="", assigned_producer="", csr="Jazmin Molina"))
    assert (t, f) == ("Jazmin Molina", "csr")
    t, f = reassign_target(make_task(created_by=" ", assigned_producer="", csr=""))
    assert (t, f) == (None, None)


def test_is_test_task():
    assert is_test_task(make_task(description="Test task for Roby - please ignore..."))
    assert not is_test_task(make_task(description="Please call the client about their quote."))


# ---------------------------------------------------------------- worker

def _pending_job(store, task: AssignedTask) -> dict[str, Any]:
    job, _ = jobs_mod.ensure_task_job(store, task)
    return job


def test_worker_single_honest_note_gate_off(store):
    """Gate off: one note, no 'working on it' claim, waits for a human."""
    client = FakeDiscussionClient()
    worker = TaskAssignmentWorker(discussion_client=client, reassign_enabled=False)
    job = _pending_job(store, make_task())

    result = worker.process_job(store, job)

    assert result["status"] == JobStatus.AWAITING_HUMAN_INPUT.value
    assert len(client.posts) == 1
    body = client.posts[0][1]
    assert "working on it" not in body.lower()
    assert "Carlo Ferrara" in body  # names the handoff target
    assert "reassign" in body.lower()


def test_worker_reassigns_gate_on(store):
    client = FakeDiscussionClient()
    reassigner = FakeReassigner(assignee="Carlo Ferrara")
    worker = TaskAssignmentWorker(
        discussion_client=client, task_reassigner=reassigner, reassign_enabled=True
    )
    job = _pending_job(store, make_task())

    result = worker.process_job(store, job)

    assert result["status"] == JobStatus.VERIFYING.value
    assert reassigner.calls == [("63429523", "25486692", "Carlo Ferrara")]
    # The task description is passed through so the CDP flow can search for it.
    assert reassigner.last_description == "Please call the client about their quote."
    assert len(client.posts) == 1
    assert "Carlo Ferrara" in client.posts[0][1]
    checkpoint = store.get_checkpoint(job["id"], "action")
    assert checkpoint["reassigned"]["verified_assignee"] == "Carlo Ferrara"
    assert checkpoint["note"]["note_id"] == "note-123"


def test_worker_no_target_posts_note_and_waits(store):
    client = FakeDiscussionClient()
    worker = TaskAssignmentWorker(discussion_client=client, reassign_enabled=True,
                                  task_reassigner=FakeReassigner())
    job = _pending_job(store, make_task(created_by="", assigned_producer="", csr=""))

    result = worker.process_job(store, job)

    assert result["status"] == JobStatus.AWAITING_HUMAN_INPUT.value
    assert len(client.posts) == 1
    assert "couldn't tell who to send it back to" in client.posts[0][1]
    assert "no creator, producer, or csr" in client.posts[0][1].lower()


def test_worker_test_task_left_alone(store):
    client = FakeDiscussionClient()
    reassigner = FakeReassigner()
    worker = TaskAssignmentWorker(
        discussion_client=client, task_reassigner=reassigner, reassign_enabled=True
    )
    job = _pending_job(store, make_task(description="Test task for Roby - please ignore..."))

    result = worker.process_job(store, job)

    assert result["status"] == JobStatus.VERIFYING.value
    assert reassigner.calls == []
    assert len(client.posts) == 1
    assert "leaving it alone" in client.posts[0][1].lower()


def test_note_timeout_not_retried(store):
    client = FakeDiscussionClient(fail_times=2)
    worker = TaskAssignmentWorker(discussion_client=client)
    job, _ = jobs_mod.ensure_task_job(store, make_task())
    with pytest.raises(UnverifiedNoteError):
        worker._post_note_verified(store, job, "849945654", "hello")
    with pytest.raises(UnverifiedNoteError):
        worker._post_note_verified(store, job, "849945654", "hello")
    assert len(client.posts) == 1


def test_note_metadata_without_id_is_uncertain(store):
    client = FakeDiscussionClient(return_note_id=False)
    worker = TaskAssignmentWorker(discussion_client=client)
    job, _ = jobs_mod.ensure_task_job(store, make_task())
    with pytest.raises(UnverifiedNoteError):
        worker._post_note_verified(store, job, "849945654", "hello")
    assert len(client.posts) == 1


def test_note_unverified_never_reposts(store):
    client = FakeDiscussionClient(confirm=False)
    worker = TaskAssignmentWorker(discussion_client=client)
    job, _ = jobs_mod.ensure_task_job(store, make_task())
    with pytest.raises(UnverifiedNoteError):
        worker._post_note_verified(store, job, "849945654", "hello")
    with pytest.raises(UnverifiedNoteError):
        worker._post_note_verified(store, job, "849945654", "hello")
    assert len(client.posts) == 1


# ---------------------------------------------------------------- verifier

def _verifying_job(store, task: AssignedTask, action: dict[str, Any]) -> dict[str, Any]:
    job, _ = jobs_mod.ensure_task_job(store, task)
    store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
    store.checkpoint(job["id"], "action", action)
    return store.transition(job["id"], JobStatus.VERIFYING, expected={JobStatus.RUNNING})


def _action_note_only(note_id: str = "note-123") -> dict[str, Any]:
    return {
        "test_task": False,
        "category": "callback",
        "note": {"discussion_id": "849945654", "note_id": note_id, "text": "handoff"},
        "reassigned": None,
    }


def test_verifier_confirms_note(store):
    client = FakeDiscussionClient(already_posted=True)  # read-back shows note-123 as latest
    verifier = TaskIntakeVerifier(discussion_client=client)
    job = _verifying_job(store, make_task(), _action_note_only())

    result = verifier.verify(job, _action_note_only())

    assert result.verified is True
    assert result.evidence.expected["note_id"] == "note-123"
    assert result.evidence.observed["note_id"] == "note-123"
    assert result.evidence.authoritative is True
    assert result.evidence.locator == "ezlynx-discussion:849945654"


def test_verifier_rejects_latest_mismatch(store):
    client = FakeDiscussionClient(already_posted=True)
    client.force_latest = "note-999"  # someone else posted after us
    verifier = TaskIntakeVerifier(discussion_client=client)
    job = _verifying_job(store, make_task(), _action_note_only())

    result = verifier.verify(job, _action_note_only())

    assert result.verified is False
    assert result.retryable is True


def test_engine_completes_only_with_evidence(store):
    """End-to-end through the engine's authorized COMPLETE path."""
    from robie_job_engine.engine import JobEngine

    client = FakeDiscussionClient(already_posted=True)
    verifier = TaskIntakeVerifier(discussion_client=client)
    engine = JobEngine(store, {}, {jobs_mod.ACTION_TYPE: verifier})

    action = _action_note_only()
    job = _verifying_job(store, make_task(), action)
    final = engine._verify(job, action)

    assert final["status"] == JobStatus.COMPLETE.value
    evidence = store.list_evidence(job["id"])
    assert any(e["verified"] for e in evidence)


def test_engine_refuses_complete_without_evidence(store):
    """A verifier that cannot confirm -> UNVERIFIED, never COMPLETE."""
    from robie_job_engine.engine import JobEngine

    client = FakeDiscussionClient(confirm=False)
    verifier = TaskIntakeVerifier(discussion_client=client)
    engine = JobEngine(store, {}, {jobs_mod.ACTION_TYPE: verifier})

    action = _action_note_only()
    job = _verifying_job(store, make_task(), action)
    final = engine._verify(job, action)

    assert final["status"] in (JobStatus.UNVERIFIED.value, JobStatus.RETRY_WAIT.value)
    assert final["status"] != JobStatus.COMPLETE.value


# ---------------------------------------------------------------- inbox

def _gmail_message(*, sender: str, subject: str, filename: str, csv_text: str,
                   internal_date: str = "1720000000000") -> dict[str, Any]:
    import base64

    data = base64.urlsafe_b64encode(csv_text.encode()).decode()
    return {
        "id": "msg-1",
        "internalDate": internal_date,
        "payload": {
            "headers": [
                {"name": "From", "value": f"Applied Reporting <{sender}>"},
                {"name": "Subject", "value": subject},
            ],
            "parts": [
                {
                    "filename": filename,
                    "body": {"data": data},
                }
            ],
        },
    }


class _FakeAttachments:
    def get(self, *, userId, messageId, id):
        raise AssertionError("should not be called for inline data")


class _FakeMessages:
    def __init__(self, messages: list[dict[str, Any]]):
        self._messages = {m["id"]: m for m in messages}

    def list(self, **kwargs):
        class R:
            def execute(inner):
                return {"messages": [{"id": m_id} for m_id in self._messages]}
        return R()

    def get(self, *, userId, id, format):
        class R:
            def execute(inner):
                return self._messages[id]
        return R()

    def attachments(self):
        return _FakeAttachments()


class _FakeService:
    def __init__(self, messages: list[dict[str, Any]]):
        self._messages = _FakeMessages(messages)

    def users(self):
        return self

    def messages(self):
        return self._messages


def _good_csv() -> str:
    return make_csv([
        '63429523,25486692,Jake N Ferrara,Robie AI,Open,2026-10-05,Normal,'
        '2026-10-03,2026-10-03T10:00:00,"Call about quote",Task Note,849945654,'
        'Carlo Ferrara,,'
    ])


def test_inbox_accepts_exact_envelope():
    service = _FakeService([_gmail_message(
        sender=EXPECTED_SENDER, subject=EXPECTED_SUBJECT,
        filename="Robie_AI_-_Task_Check-In_2026-10-03T1141.csv", csv_text=_good_csv(),
    )])
    report = fetch_latest_task_report(service)
    assert report is not None
    assert report.message_id == "msg-1"
    assert len(report.tasks) == 1
    assert report.tasks[0].task_id == "63429523"


def test_inbox_skips_wrong_sender_then_raises():
    service = _FakeService([_gmail_message(
        sender="someone@evil.com", subject=EXPECTED_SUBJECT,
        filename="Robie_AI_-_Task_Check-In_2026-10-03T1141.csv", csv_text=_good_csv(),
    )])
    with pytest.raises(TaskInboxError):
        fetch_latest_task_report(service)


def test_inbox_rejects_multiple_csvs():
    import base64

    data = base64.urlsafe_b64encode(_good_csv().encode()).decode()
    message = _gmail_message(
        sender=EXPECTED_SENDER, subject=EXPECTED_SUBJECT,
        filename="Robie_AI_-_Task_Check-In_2026-10-03T1141.csv", csv_text=_good_csv(),
    )
    message["payload"]["parts"].append(
        {"filename": "Robie_AI_-_Task_Check-In_2026-10-03T1142.csv", "body": {"data": data}}
    )
    with pytest.raises(TaskInboxError):
        fetch_latest_task_report(_FakeService([message]))


def test_inbox_rejects_bad_filename():
    service = _FakeService([_gmail_message(
        sender=EXPECTED_SENDER, subject=EXPECTED_SUBJECT,
        filename="random.csv", csv_text=_good_csv(),
    )])
    with pytest.raises(TaskInboxError):
        fetch_latest_task_report(service)


def test_inbox_none_when_empty():
    assert fetch_latest_task_report(_FakeService([])) is None


# ---------------------------------------------------------------- health check

def _record_run(store: JobStore, *, message_id: str, status: str, minutes_ago: float):
    created = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    with store.connect() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS ezlynx_task_intake_runs (
                message_id TEXT PRIMARY KEY, digest TEXT NOT NULL, filename TEXT NOT NULL,
                task_count INTEGER NOT NULL, jobs_created INTEGER NOT NULL,
                status TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL)"""
        )
        conn.execute(
            "INSERT INTO ezlynx_task_intake_runs VALUES (?,?,?,?,?,?,?,?)",
            (message_id, "abc", "f.csv", 3, 1, status, "", created),
        )


def test_health_healthy_quiet(store, monkeypatch):
    import robie_job_engine.ezlynx_task_intake_health as health

    _record_run(store, message_id="m1", status="ok", minutes_ago=10)
    monkeypatch.setattr(health, "default_db_path", lambda: store.path)
    assert check_intake() == []


def test_health_flags_stale_run(store, monkeypatch):
    import robie_job_engine.ezlynx_task_intake_health as health

    _record_run(store, message_id="m1", status="ok", minutes_ago=200)
    monkeypatch.setattr(health, "default_db_path", lambda: store.path)
    problems = check_intake()
    assert any("no successful intake run" in p for p in problems)


def test_health_flags_failed_job(store, monkeypatch):
    import robie_job_engine.ezlynx_task_intake_health as health

    _record_run(store, message_id="m1", status="ok", minutes_ago=10)
    job, _ = jobs_mod.ensure_task_job(store, make_task())
    store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
    store.transition(job["id"], JobStatus.FAILED, expected={JobStatus.RUNNING},
                     error="boom", release_lease=True)
    monkeypatch.setattr(health, "default_db_path", lambda: store.path)
    problems = check_intake()
    assert any("FAILED" in p for p in problems)
