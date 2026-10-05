"""Review blockers: labels, backlog, dry-run, cap, and the all-row clock."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from robie_job_engine.ezlynx_seen_tasks import SeenTaskStore
from robie_job_engine.ezlynx_task_inbox import IngestedReport
from robie_job_engine.ezlynx_task_jobs import ensure_task_job, job_is_dialable
from robie_job_engine.ezlynx_task_report import AssignedTask, parse_task_report_detail
from robie_job_engine.ezlynx_task_intake import run_intake, select_intake_work
from robie_job_engine.store import JobStore
from robie_job_engine.task_assignment_worker import NeedsHuman, TaskAssignmentWorker

MONDAY = datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)  # 10:00 ET

HEADER = (
    "Task ID,Applicant ID,Account Name,Task Assigned To,Task Status,"
    "Task Due Date,Task Priority,Task Created Date,Task Last Modified Date,"
    "Note,Activity Type,Discussion ID,Created Date,Activity Labels"
)


def _task(**overrides) -> AssignedTask:
    base = dict(
        task_id="90026158",
        title="Task Note",
        description="Please call about the renewal.",
        applicant_id="80026158",
        applicant_name="Avery Sample",
        assigned_to="Robie AI",
        due_date="2026-10-06",
        priority="Normal",
        created_date="2026-10-05",
        status="Open",
        discussion_id="70026158",
        last_modified="2026-10-05T09:00:00",
        created_by="Pat Example",
        activity_labels="Robie Call",
        created_at="2026-10-05T09:00:00",
        created_at_et="2026-10-05T10:00:00-04:00",
    )
    base.update(overrides)
    return AssignedTask(**base)


def _report(
    *tasks: AssignedTask,
    message_id: str = "msg-1",
    digest: str = "digest-1",
    received_at: str = "2026-10-05T14:00:00Z",
):
    newest = ""
    for task in tasks:
        if task.created_at_et and task.created_at_et > newest:
            newest = task.created_at_et
    return IngestedReport(
        message_id=message_id,
        filename="Robie_AI_-_Task_Check-In_synthetic.csv",
        digest=digest,
        received_at=received_at,
        tasks=tuple(tasks),
        row_count=len(tasks),
        newest_created_et=newest,
    )


class _Notes:
    def __init__(self):
        self.notes: list[tuple[str, str]] = []

    def get_discussion_ids(self, applicant_id):
        del applicant_id
        # The discussions these fixtures file against. A real client lists
        # the applicant's discussions; WRITE_SCOPE=all does not skip that.
        return ["70026158", "70020002", "70020003", "70020004", "70020005"]

    def append_note(self, discussion_id, body, note_type="Note", applicant_id=None):
        del note_type, applicant_id
        self.notes.append((discussion_id, body))
        return {"noteId": f"N-{len(self.notes)}"}


class _Bland:
    def __init__(self):
        self.dials = 0
        self.execute = False

    def place_call_with_double_dial(self, *args, **kwargs):
        del args, kwargs
        self.dials += 1
        return {"success": False, "error": "test bland", "call_ids": []}

    def recent_calls(self, phone, since_seconds=1800):
        del phone, since_seconds
        return {"ok": False, "calls": []}

    def get_call_status(self, call_id):
        del call_id
        return {"ok": False, "status": "unknown"}


class _Phone:
    def get_phone(self, applicant_id):
        del applicant_id
        return "+17325550142"


def _install_fakes(monkeypatch, notes: _Notes, bland: _Bland):
    # The intake unit sets both. All-clients still has to prove the discussion.
    monkeypatch.setenv("ROBIE_EZLYNX_WRITE_SCOPE", "all")
    monkeypatch.setenv("ROBIE_PLAYGROUND", "1")
    monkeypatch.setattr(
        "robie_job_engine.ezlynx_task_intake._intake_now", lambda: MONDAY,
    )
    monkeypatch.setattr(
        "robie_job_engine.ezlynx_task_intake._build_discussion_client", lambda: notes,
    )
    monkeypatch.setattr(
        "robie_job_engine.bland_prod_wiring.build_call_dependencies",
        lambda **kwargs: (_Phone(), bland, None, True),
    )
    monkeypatch.setattr(
        "robie_job_engine.report_email_source.build_default_gmail_service",
        lambda: object(),
    )


def _fetch(report):
    def fetch(_service):
        return report
    return fetch


def test_newest_created_et_includes_non_robie_rows():
    text = "\n".join([
        HEADER,
        "9001,8001,Avery Sample,Robie AI,Open,2026-10-06,Normal,2026-10-05,"
        "2026-10-05,Note,Task Note,7001,2026-10-05T09:11:00,Robie Call",
        "9002,8002,Casey Other,Casey Other,Open,2026-10-06,Normal,2026-10-05,"
        "2026-10-05,Note,Task Note,7002,2026-10-05T15:00:00,",
    ]) + "\n"
    parsed = parse_task_report_detail(text)
    assert [task.task_id for task in parsed.tasks] == ["9001"]
    assert parsed.newest_created_et.startswith("2026-10-05T16:00:00")


def test_unlabeled_do_not_call_and_splice_are_not_callbacks():
    worker = TaskAssignmentWorker(discussion_client=object())
    unlabeled = _task(activity_labels="", description="Please call John about his renewal.")
    do_not = _task(task_id="2", activity_labels="", description="Do not call or contact anyone.")
    splice = _task(task_id="3", activity_labels="Robie audit", description="Audit the file.")
    labeled = _task(task_id="4", activity_labels="Robie lead follow-up")
    assert worker._categorize_task(unlabeled) != "callback"
    assert worker._categorize_task(do_not) != "callback"
    assert worker._categorize_task(splice) != "callback"
    assert worker._categorize_task(labeled) == "callback"


def test_dry_run_writes_no_jobs_and_leaves_the_first_live_run_as_baseline(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    notes = _Notes()
    bland = _Bland()
    _install_fakes(monkeypatch, notes, bland)
    report = _report(_task())
    monkeypatch.setattr(
        "robie_job_engine.ezlynx_task_intake.fetch_latest_task_report", _fetch(report),
    )
    assert run_intake(db_path=str(db), dry_run=True) == 0
    assert notes.notes == []
    assert bland.dials == 0
    assert SeenTaskStore(str(db)).is_empty()
    with JobStore(str(db)).connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0

    assert run_intake(db_path=str(db), dry_run=False) == 0
    assert SeenTaskStore(str(db)).statuses() == {"90026158": "baseline"}
    assert notes.notes == []
    assert bland.dials == 0
    with JobStore(str(db)).connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_missing_database_baselines_and_dials_nothing(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    notes = _Notes()
    bland = _Bland()
    _install_fakes(monkeypatch, notes, bland)
    monkeypatch.setattr(
        "robie_job_engine.ezlynx_task_intake.fetch_latest_task_report",
        _fetch(_report(_task(), _task(task_id="90022622"), _task(task_id="90025899"))),
    )
    assert run_intake(db_path=str(db)) == 0
    assert set(SeenTaskStore(str(db)).statuses()) == {"90026158", "90022622", "90025899"}
    assert set(SeenTaskStore(str(db)).statuses().values()) == {"baseline"}
    assert bland.dials == 0
    with JobStore(str(db)).connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_old_and_unparseable_tasks_get_one_hold_note_and_never_dial(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    SeenTaskStore(str(db)).observe(["prior"], report_digest="prior")
    notes = _Notes()
    bland = _Bland()
    _install_fakes(monkeypatch, notes, bland)
    old = _task(
        task_id="90026158",
        created_at="2026-10-01T09:00:00",
        created_at_et="2026-10-01T10:00:00-04:00",
        created_date="2026-10-01",
    )
    friday = _task(
        task_id="90020002",
        created_at="2026-10-02T09:00:00",
        created_at_et="2026-10-02T10:00:00-04:00",
        created_date="2026-10-02",
        discussion_id="70020002",
    )
    weekend = _task(
        task_id="90020003",
        created_at="2026-10-03T09:00:00",
        created_at_et="2026-10-03T10:00:00-04:00",
        created_date="2026-10-03",
        discussion_id="70020003",
    )
    sunday = _task(
        task_id="90020005",
        created_at="2026-10-04T09:00:00",
        created_at_et="2026-10-04T10:00:00-04:00",
        created_date="2026-10-04",
        discussion_id="70020005",
    )
    broken = _task(
        task_id="90020004",
        created_at="not-a-date",
        created_at_et="",
        created_date="",
        discussion_id="70020004",
    )
    monkeypatch.setenv("ROBIE_PHONE_LIVE_CALLS", "1")
    monkeypatch.setattr(
        "robie_job_engine.ezlynx_task_intake.fetch_latest_task_report",
        _fetch(_report(old, friday, weekend, sunday, broken)),
    )
    assert run_intake(db_path=str(db)) == 0
    bodies = {discussion: body for discussion, body in notes.notes}
    assert set(bodies) == {"70026158", "70020004"}
    assert "older than the calling window" in bodies["70026158"]
    assert "could not be read" in bodies["70020004"]
    assert "could not be read" not in bodies["70026158"]
    assert "older than the calling window" not in bodies["70020004"]
    assert all("732" not in body and "555" not in body for body in bodies.values())
    assert bland.dials == 0
    statuses = SeenTaskStore(str(db)).statuses()
    assert statuses["90026158"] == "hitl"
    assert statuses["90020004"] == "hitl"
    store = JobStore(str(db))
    with store.connect() as conn:
        job_ids = [str(row[0]) for row in conn.execute("SELECT id FROM jobs")]
    dialed = {store.get_job(job_id)["payload"]["task_id"] for job_id in job_ids}
    assert dialed == {"90020002", "90020003", "90020005"}
    assert all(store.get_job(job_id)["payload"]["dialable"] is True for job_id in job_ids)

    monkeypatch.setattr(
        "robie_job_engine.ezlynx_task_intake.fetch_latest_task_report",
        _fetch(_report(
            old, friday, weekend, sunday, broken, message_id="msg-2", digest="digest-2",
        )),
    )
    before = len(notes.notes)
    assert run_intake(db_path=str(db)) == 0
    assert len(notes.notes) == before
    assert bland.dials == 0


def test_hold_note_without_an_id_is_not_repeated(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    SeenTaskStore(str(db)).observe(["prior"], report_digest="prior")

    class _Silent:
        def __init__(self):
            self.notes: list[tuple[str, str]] = []

        def get_discussion_ids(self, applicant_id):
            del applicant_id
            return ["70026158", "70020004"]

        def append_note(self, discussion_id, body, note_type="Note", applicant_id=None):
            del note_type, applicant_id
            self.notes.append((discussion_id, body))
            return {}

    notes = _Silent()
    bland = _Bland()
    _install_fakes(monkeypatch, notes, bland)
    old = _task(
        task_id="90026158",
        created_at="2026-10-01T09:00:00",
        created_at_et="2026-10-01T10:00:00-04:00",
        created_date="2026-10-01",
    )
    broken = _task(
        task_id="90020004",
        created_at="not-a-date",
        created_at_et="",
        created_date="",
        discussion_id="70020004",
    )
    monkeypatch.setattr(
        "robie_job_engine.ezlynx_task_intake.fetch_latest_task_report",
        _fetch(_report(old, broken)),
    )
    assert run_intake(db_path=str(db)) == 0
    bodies = {discussion: body for discussion, body in notes.notes}
    assert "older than the calling window" in bodies["70026158"]
    assert "could not be read" in bodies["70020004"]
    statuses = SeenTaskStore(str(db)).statuses()
    assert statuses["90026158"] == "hitl_attempted"
    assert statuses["90020004"] == "hitl_attempted"

    monkeypatch.setattr(
        "robie_job_engine.ezlynx_task_intake.fetch_latest_task_report",
        _fetch(_report(old, broken, message_id="msg-2", digest="digest-2")),
    )
    assert run_intake(db_path=str(db)) == 0
    assert len(notes.notes) == 2
    assert bland.dials == 0


def test_production_dry_run_resolves_scope_and_writes_nothing(tmp_path, monkeypatch):
    from robie_job_engine.ezlynx_write_scope import all_clients_scope_honored

    db = tmp_path / "jobs.db"
    notes = _Notes()
    bland = _Bland()
    _install_fakes(monkeypatch, notes, bland)
    monkeypatch.setenv("ROBIE_ENV", "PRODUCTION")
    monkeypatch.setenv("ROBIE_EZLYNX_WRITE_SCOPE", "all")
    monkeypatch.setenv("ROBIE_PLAYGROUND", "1")
    assert all_clients_scope_honored() is True
    monkeypatch.setattr(
        "robie_job_engine.ezlynx_task_intake.fetch_latest_task_report",
        _fetch(_report(_task(applicant_id="80026158"))),
    )
    assert run_intake(db_path=str(db), dry_run=True) == 0
    assert notes.notes == []
    assert bland.dials == 0
    assert SeenTaskStore(str(db)).is_empty()
    with JobStore(str(db)).connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        beat = conn.execute(
            "SELECT status, error FROM ezlynx_task_intake_heartbeats"
        ).fetchone()
    assert beat[0] == "ok"
    assert beat[1] in ("", None)

    monkeypatch.delenv("ROBIE_EZLYNX_WRITE_SCOPE", raising=False)
    monkeypatch.delenv("ROBIE_PLAYGROUND", raising=False)
    assert all_clients_scope_honored() is False
    monkeypatch.setattr(
        "robie_job_engine.ezlynx_task_intake.fetch_latest_task_report",
        _fetch(_report(_task(applicant_id="80026158"), message_id="msg-2", digest="digest-2")),
    )
    assert run_intake(db_path=str(db), dry_run=True) == 2
    assert notes.notes == []
    assert SeenTaskStore(str(db)).is_empty()
    with JobStore(str(db)).connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        failed = conn.execute(
            "SELECT status, error FROM ezlynx_task_intake_heartbeats ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert failed[0] == "failed"
    assert "write scope did not resolve" in failed[1]


def test_non_live_job_is_never_dialed_later(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    store = JobStore(str(db))
    task = _task()
    job, created = ensure_task_job(
        store, task, live=False, queued_at="2026-10-04T12:00:00+00:00",
    )
    assert created is True
    assert job_is_dialable(job["payload"]) is False
    later, _created = ensure_task_job(
        store, _task(last_modified="2026-10-05T12:00:00"),
        live=True, queued_at="2026-10-05T14:00:00+00:00",
        live_enabled_at="2026-10-05T13:00:00+00:00",
    )
    assert later["payload"]["dialable"] is False
    assert job_is_dialable({
        "dialable": True,
        "queued_at": "2026-10-04T12:00:00+00:00",
        "live_enabled_at": "2026-10-05T13:00:00+00:00",
    }) is False

    bland = _Bland()
    worker = TaskAssignmentWorker(
        discussion_client=_Notes(),
        phone_lookup=_Phone(),
        bland_client=bland,
        call_dry_run=False,
    )
    with pytest.raises(NeedsHuman):
        worker._do_work(store, later)
    assert bland.dials == 0


def test_cap_processes_the_newest_eligible_tasks_and_alerts():
    tasks = [
        _task(
            task_id=str(900000000 + index),
            created_at=f"2026-10-05T{8 + (index % 2):02d}:{index % 60:02d}:00",
            created_at_et=f"2026-10-05T{9 + (index % 2):02d}:{index % 60:02d}:00-04:00",
            activity_labels="",
            description="File the dec page.",
        )
        for index in range(30)
    ]
    chosen, hold, overflow, alert = select_intake_work(
        tasks, {}, now=MONDAY, max_tasks=25, max_age_hours=None,
    )
    assert hold == []
    assert len(chosen) == 25
    assert len(overflow) == 5
    assert alert.startswith("Batch cap:")
    overflow_ids = set(overflow)
    assert {task.task_id for task in chosen}.isdisjoint(overflow_ids)
    assert min(task.created_at_et for task in chosen) >= max(
        task.created_at_et for task in tasks if task.task_id in overflow_ids
    )


class _FilingNotes(_Notes):
    def __init__(self):
        super().__init__()
        self.records: dict[str, dict] = {}

    def append_note(self, discussion_id, body, note_type="Note", applicant_id=None):
        del note_type, applicant_id
        self.notes.append((discussion_id, body))
        note_id = f"N-{len(self.notes)}"
        self.records[str(discussion_id)] = {
            "Title": "Task Note",
            "noteCount": 1,
            "mostRecentNoteId": note_id,
            "notes": [{"noteId": note_id, "body": body}],
        }
        return {"noteId": note_id}

    def get_discussion(self, discussion_id):
        return self.records[str(discussion_id)]


class _VoicemailBland(_Bland):
    def place_call_with_double_dial(self, *args, **kwargs):
        del args, kwargs
        self.dials += 1
        return {
            "success": True,
            "call_ids": [f"c-{self.dials}"],
            "attempts": [{
                "attempt": 1,
                "success": True,
                "final_status": {"status": "completed", "answered_by": "voicemail"},
            }],
            "voicemail_hit": True,
            "redialed": True,
            "recording_url": None,
            "error": None,
        }

    def get_call_status(self, call_id):
        del call_id
        return {
            "ok": True,
            "status": "completed",
            "answered_by": "voicemail",
            "duration_s": 25,
        }


def _file_note(client, applicant_id, body, **kwargs):
    del kwargs
    discussion_id = "70026158"
    created = client.append_note(discussion_id, body, applicant_id=applicant_id)
    return {
        "status": "filed",
        "note_id": created["noteId"],
        "discussion_id": discussion_id,
        "applicant_id": applicant_id,
    }


def _clocked_config(clock):
    from robie_job_engine.robie_call_handler import RobieCallConfig

    def build(*args, **kwargs):
        kwargs.setdefault("now", clock["now"])
        kwargs.setdefault("outcome_poll_interval_s", 0)
        kwargs.setdefault("outcome_poll_tries", 2)
        return RobieCallConfig(*args, **kwargs)

    return build


def test_called_task_gets_no_later_hold_note_and_no_later_dial(tmp_path, monkeypatch):
    """Monday's voicemail is done. Tuesday and Wednesday do nothing."""
    from robie_job_engine.models import JobStatus
    from robie_job_engine.robie_call_handler import _reset_module_state_for_tests

    db = tmp_path / "jobs.db"
    SeenTaskStore(str(db)).observe(["prior"], report_digest="prior")
    notes = _FilingNotes()
    bland = _VoicemailBland()
    clock = {"now": datetime(2026, 10, 5, 13, 5, tzinfo=timezone.utc)}  # Mon 9:05 ET
    _install_fakes(monkeypatch, notes, bland)
    monkeypatch.setattr(
        "robie_job_engine.ezlynx_task_intake._intake_now", lambda: clock["now"],
    )
    monkeypatch.setattr(
        "robie_job_engine.bland_prod_wiring.build_call_dependencies",
        lambda **kwargs: (_Phone(), bland, None, False),
    )
    monkeypatch.setattr(
        "robie_job_engine.robie_call_handler.RobieCallConfig", _clocked_config(clock),
    )
    monkeypatch.setattr(
        "robie_job_engine.ezlynx_discussions.file_note_to_existing_discussion",
        _file_note,
    )
    monkeypatch.setenv("ROBIE_PHONE_LIVE_CALLS", "1")
    _reset_module_state_for_tests()
    saturday = _task(
        created_at="2026-10-03T09:05:00",
        created_at_et="2026-10-03T10:05:00-04:00",
        created_date="2026-10-03",
        description="Please call the client about the renewal. Call at 732-555-0142.",
        assigned_producer="Pat Example",
    )
    monkeypatch.setattr(
        "robie_job_engine.ezlynx_task_intake.fetch_latest_task_report",
        _fetch(_report(saturday, message_id="mon", digest="mon")),
    )
    assert run_intake(db_path=str(db)) == 0
    assert bland.dials == 1
    assert len(notes.notes) == 1
    body = notes.notes[0][1]
    assert "No answer, left a voicemail." in body
    assert "Please call the client" not in body
    assert "I did not place a call" not in body
    assert ".." not in body
    store = JobStore(str(db))
    with store.connect() as conn:
        job_id = conn.execute("SELECT id FROM jobs").fetchone()[0]
    assert JobStatus(store.get_job(str(job_id))["status"]) == JobStatus.COMPLETE
    assert SeenTaskStore(str(db)).statuses()["90026158"] == "seen"

    for message_id, moment in (
        ("tue", datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc)),
        ("wed", datetime(2026, 10, 7, 14, 0, tzinfo=timezone.utc)),
    ):
        clock["now"] = moment
        monkeypatch.setattr(
            "robie_job_engine.ezlynx_task_intake.fetch_latest_task_report",
            _fetch(_report(
                saturday, message_id=message_id, digest=message_id,
                received_at=moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )),
        )
        assert run_intake(db_path=str(db)) == 0
        assert bland.dials == 1
        assert notes.notes == [(notes.notes[0][0], notes.notes[0][1])]
        assert "I did not place a call" not in notes.notes[0][1]


def test_lease_on_test_dials_nothing_then_one_call_when_it_returns(tmp_path, monkeypatch):
    import json

    from robie_job_engine.models import JobStatus
    from robie_job_engine.robie_call_handler import _reset_module_state_for_tests

    db = tmp_path / "jobs.db"
    SeenTaskStore(str(db)).observe(["prior"], report_digest="prior")
    notes = _FilingNotes()
    bland = _VoicemailBland()
    clock = {"now": datetime(2026, 10, 5, 13, 5, tzinfo=timezone.utc)}
    holder = {"name": "TEST"}
    _install_fakes(monkeypatch, notes, bland)
    monkeypatch.setattr(
        "robie_job_engine.ezlynx_task_intake._intake_now", lambda: clock["now"],
    )
    monkeypatch.setattr(
        "robie_job_engine.bland_prod_wiring.build_call_dependencies",
        lambda **kwargs: (_Phone(), bland, None, False),
    )
    monkeypatch.setattr(
        "robie_job_engine.robie_call_handler.RobieCallConfig", _clocked_config(clock),
    )
    monkeypatch.setattr(
        "robie_job_engine.ezlynx_discussions.file_note_to_existing_discussion",
        _file_note,
    )
    monkeypatch.setenv("ROBIE_PHONE_LIVE_CALLS", "1")
    monkeypatch.setenv("ROBIE_EZLYNX_DRIVER_GATE_REQUIRED", "1")
    monkeypatch.setenv("ROBIE_EZLYNX_DRIVER_HOLDER", "PRODUCTION")

    def reader():
        return json.dumps({
            "version": 1,
            "state": "IN",
            "holder": holder["name"],
            "expires_at": "2027-01-01T00:00:00+00:00",
        })

    monkeypatch.setattr("robie_job_engine.ezlynx_driver_gate.read_metadata", reader)
    _reset_module_state_for_tests()
    task = _task(
        created_at="2026-10-05T08:00:00",
        created_at_et="2026-10-05T09:00:00-04:00",
        created_date="2026-10-05",
        description="Please call the client about the renewal. Call at 732-555-0142.",
        assigned_producer="Pat Example",
    )
    monkeypatch.setattr(
        "robie_job_engine.ezlynx_task_intake.fetch_latest_task_report",
        _fetch(_report(task, message_id="tick-1", digest="tick-1")),
    )
    assert run_intake(db_path=str(db)) == 0
    assert bland.dials == 0
    assert notes.notes == []
    statuses = SeenTaskStore(str(db)).statuses()
    assert statuses["90026158"] == "seen"
    assert "hitl" not in statuses.values()
    assert "hitl_attempted" not in statuses.values()
    store = JobStore(str(db))
    with store.connect() as conn:
        rows = list(conn.execute("SELECT id, status FROM jobs"))
    assert len(rows) == 1
    assert rows[0][1] == JobStatus.PENDING.value

    assert run_intake(db_path=str(db)) == 0
    assert bland.dials == 0
    assert notes.notes == []
    assert store.get_job(str(rows[0][0]))["status"] == JobStatus.PENDING.value

    holder["name"] = "PRODUCTION"
    assert run_intake(db_path=str(db)) == 0
    assert bland.dials == 1
    assert len(notes.notes) == 1
    assert "No answer, left a voicemail." in notes.notes[0][1]
    assert store.get_job(str(rows[0][0]))["status"] == JobStatus.COMPLETE.value


def test_lease_refusal_before_a_hold_note_stays_retryable(tmp_path, monkeypatch):
    from robie_job_engine.ezlynx_driver_gate import REFUSED, EzlynxDriverGateRefused

    db = tmp_path / "jobs.db"
    SeenTaskStore(str(db)).observe(["prior"], report_digest="prior")

    class _Refused(_Notes):
        def append_note(self, discussion_id, body, note_type="Note", applicant_id=None):
            del discussion_id, body, note_type, applicant_id
            raise EzlynxDriverGateRefused(f"{REFUSED}: driver belongs to TEST")

    refused = _Refused()
    bland = _Bland()
    _install_fakes(monkeypatch, refused, bland)
    old = _task(
        created_at="2026-10-01T09:00:00",
        created_at_et="2026-10-01T10:00:00-04:00",
        created_date="2026-10-01",
    )
    monkeypatch.setattr(
        "robie_job_engine.ezlynx_task_intake.fetch_latest_task_report",
        _fetch(_report(old)),
    )
    assert run_intake(db_path=str(db)) == 0
    assert refused.notes == []
    assert bland.dials == 0
    assert SeenTaskStore(str(db)).statuses()["90026158"] == "deferred"

    notes = _Notes()
    monkeypatch.setattr(
        "robie_job_engine.ezlynx_task_intake._build_discussion_client", lambda: notes,
    )
    monkeypatch.setattr(
        "robie_job_engine.ezlynx_task_intake.fetch_latest_task_report",
        _fetch(_report(old, message_id="msg-1", digest="digest-1")),
    )
    assert run_intake(db_path=str(db)) == 0
    assert len(notes.notes) == 1
    assert "older than the calling window" in notes.notes[0][1]
    assert SeenTaskStore(str(db)).statuses()["90026158"] == "hitl"
    assert bland.dials == 0


def test_lease_outage_holds_tasks_that_aged_out_before_the_lease_returns(
    tmp_path, monkeypatch,
):
    """Lease on TEST Monday and Tuesday. Wednesday holds Saturday and Monday.

    Neither task is dialed. Saturday is still inside Monday's window, and
    Monday's task is still inside Tuesday's window, so the hold has to
    happen when the pending jobs are about to be dialed.
    """
    import json

    from robie_job_engine.models import JobStatus
    from robie_job_engine.robie_call_handler import _reset_module_state_for_tests

    db = tmp_path / "jobs.db"
    SeenTaskStore(str(db)).observe(["prior"], report_digest="prior")
    notes = _Notes()
    bland = _VoicemailBland()
    clock = {"now": datetime(2026, 10, 5, 13, 5, tzinfo=timezone.utc)}  # Mon 9:05 ET
    holder = {"name": "TEST"}
    _install_fakes(monkeypatch, notes, bland)
    monkeypatch.setattr(
        "robie_job_engine.ezlynx_task_intake._intake_now", lambda: clock["now"],
    )
    monkeypatch.setattr(
        "robie_job_engine.bland_prod_wiring.build_call_dependencies",
        lambda **kwargs: (_Phone(), bland, None, False),
    )
    monkeypatch.setattr(
        "robie_job_engine.robie_call_handler.RobieCallConfig", _clocked_config(clock),
    )
    monkeypatch.setenv("ROBIE_PHONE_LIVE_CALLS", "1")
    monkeypatch.setenv("ROBIE_EZLYNX_DRIVER_GATE_REQUIRED", "1")
    monkeypatch.setenv("ROBIE_EZLYNX_DRIVER_HOLDER", "PRODUCTION")

    def reader():
        return json.dumps({
            "version": 1,
            "state": "IN",
            "holder": holder["name"],
            "expires_at": "2027-01-01T00:00:00+00:00",
        })

    monkeypatch.setattr("robie_job_engine.ezlynx_driver_gate.read_metadata", reader)
    _reset_module_state_for_tests()
    saturday = _task(
        task_id="90026158",
        discussion_id="70026158",
        applicant_id="80026158",
        created_at="2026-10-03T09:05:00",
        created_at_et="2026-10-03T10:05:00-04:00",
        created_date="2026-10-03",
        description="Please call the client about the renewal.",
        assigned_producer="Pat Example",
    )
    monday = _task(
        task_id="90020002",
        discussion_id="70020002",
        applicant_id="80020002",
        applicant_name="Casey Sample",
        created_at="2026-10-05T08:00:00",
        created_at_et="2026-10-05T09:00:00-04:00",
        created_date="2026-10-05",
        description="Please call the client about the new vehicle.",
        assigned_producer="Pat Example",
    )
    for message_id, moment in (
        ("mon", datetime(2026, 10, 5, 13, 5, tzinfo=timezone.utc)),
        ("tue", datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc)),
    ):
        clock["now"] = moment
        monkeypatch.setattr(
            "robie_job_engine.ezlynx_task_intake.fetch_latest_task_report",
            _fetch(_report(
                saturday, monday, message_id=message_id, digest=message_id,
                received_at=moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )),
        )
        assert run_intake(db_path=str(db)) == 0
        assert bland.dials == 0
        assert notes.notes == []

    holder["name"] = "PRODUCTION"
    clock["now"] = datetime(2026, 10, 7, 14, 0, tzinfo=timezone.utc)  # Wed 10:00 ET
    monkeypatch.setattr(
        "robie_job_engine.ezlynx_task_intake.fetch_latest_task_report",
        _fetch(_report(
            saturday, monday, message_id="wed", digest="wed",
            received_at=clock["now"].strftime("%Y-%m-%dT%H:%M:%SZ"),
        )),
    )
    assert run_intake(db_path=str(db)) == 0
    assert bland.dials == 0
    assert len(notes.notes) == 2
    assert {discussion for discussion, _body in notes.notes} == {"70026158", "70020002"}
    assert all("older than the calling window" in body for _discussion, body in notes.notes)
    store = JobStore(str(db))
    with store.connect() as conn:
        statuses = [row[0] for row in conn.execute("SELECT status FROM jobs")]
    assert statuses
    assert set(statuses) == {JobStatus.AWAITING_HUMAN_INPUT.value}


def test_unlabeled_tasks_stay_untouched_and_gate_off_posts_no_note(tmp_path, monkeypatch):
    """The unit's intake must not file a gate-off note or touch an unlabeled task."""
    from robie_job_engine.models import JobStatus
    from robie_job_engine.robie_call_handler import _reset_module_state_for_tests
    from robie_job_engine.task_assignment_worker import TaskAssignmentWorker

    db = tmp_path / "jobs.db"
    notes = _Notes()
    bland = _Bland()
    clock = {"now": MONDAY}
    _install_fakes(monkeypatch, notes, bland)
    monkeypatch.setattr(
        "robie_job_engine.bland_prod_wiring.build_call_dependencies",
        lambda **kwargs: (_Phone(), bland, None, False),
    )
    monkeypatch.setattr(
        "robie_job_engine.robie_call_handler.RobieCallConfig", _clocked_config(clock),
    )
    monkeypatch.setenv("ROBIE_PHONE_LIVE_CALLS", "1")
    _reset_module_state_for_tests()
    labeled = _task(
        description="Please call the client about the renewal.",
        assigned_producer="Pat Example",
    )
    unlabeled = _task(
        task_id="90030001",
        applicant_id="80030001",
        discussion_id="70030001",
        activity_labels="",
        description="Please review the declarations page.",
        created_by="Pat Example",
        assigned_producer="Pat Example",
    )
    monkeypatch.setattr(
        "robie_job_engine.ezlynx_task_intake.fetch_latest_task_report",
        _fetch(_report(labeled, unlabeled)),
    )
    assert run_intake(db_path=str(db)) == 0
    assert bland.dials == 0
    assert notes.notes == []
    assert SeenTaskStore(str(db)).statuses() == {"90026158": "baseline"}
    assert "90030001" not in SeenTaskStore(str(db)).statuses()
    with JobStore(str(db)).connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0

    # The gate-off branch itself posts nothing, even if a job is already waiting.
    store = JobStore(str(db))
    job, _created = ensure_task_job(
        store, unlabeled, live=True,
        queued_at="2026-10-05T14:00:00+00:00",
        live_enabled_at="2026-10-05T13:00:00+00:00",
    )
    notes.owned = ["70030001"]

    def _owned(applicant_id):
        del applicant_id
        return list(notes.owned)

    notes.get_discussion_ids = _owned
    worker = TaskAssignmentWorker(
        discussion_client=notes, reassign_enabled=False, task_reassigner=None,
    )
    finished = worker.process_job(store, job)
    assert notes.notes == []
    assert JobStatus(finished["status"]) == JobStatus.AWAITING_HUMAN_INPUT
    assert "no note was posted" in (finished.get("last_error") or "").lower()


def test_baseline_and_first_seen_hold_survive_older_snapshots(tmp_path, monkeypatch):
    """#774 does not reopen an older snapshot, and #776 still baselines and holds once."""
    from robie_job_engine.models import JobStatus

    db = tmp_path / "jobs.db"
    notes = _Notes()
    bland = _Bland()
    _install_fakes(monkeypatch, notes, bland)
    monkeypatch.setenv("ROBIE_PHONE_LIVE_CALLS", "1")
    monkeypatch.setattr(
        "robie_job_engine.bland_prod_wiring.build_call_dependencies",
        lambda **kwargs: (_Phone(), bland, None, False),
    )
    labeled = _task(
        description="Please call the client about the renewal.",
        assigned_producer="Pat Example",
    )
    monkeypatch.setattr(
        "robie_job_engine.ezlynx_task_intake.fetch_latest_task_report",
        _fetch(_report(labeled)),
    )
    assert run_intake(db_path=str(db)) == 0
    assert bland.dials == 0
    assert notes.notes == []
    assert SeenTaskStore(str(db)).statuses() == {"90026158": "baseline"}
    with JobStore(str(db)).connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0

    # An older snapshot of a finished call does not reopen the job.
    store = JobStore(str(db))
    finished_task = _task(last_modified="2026-10-05T12:00:00")
    job, created = ensure_task_job(
        store, finished_task, live=True,
        queued_at="2026-10-05T14:00:00+00:00",
        live_enabled_at="2026-10-05T13:00:00+00:00",
    )
    assert created is True
    with store.connect() as conn:
        conn.execute(
            "UPDATE jobs SET status=? WHERE id=?",
            (JobStatus.COMPLETE.value, job["id"]),
        )
    older, reopened = ensure_task_job(
        store, _task(last_modified="2026-10-05T09:00:00"),
        report_received_at="2026-10-05T14:00:00Z",
        confirm_returned=lambda _task: True,
    )
    assert reopened is False
    assert older["status"] == JobStatus.COMPLETE.value
    assert int(older["payload"].get("round") or 0) == 0

    # The next delivery is fresh and in-window, but the task is already
    # baselined and already has a job, so it is not held and not dialed.
    monkeypatch.setattr(
        "robie_job_engine.ezlynx_task_intake.fetch_latest_task_report",
        _fetch(_report(labeled, message_id="later", digest="later")),
    )
    assert run_intake(db_path=str(db)) == 0
    assert bland.dials == 0
    assert notes.notes == []
    assert store.get_job(job["id"])["status"] == JobStatus.COMPLETE.value


def test_stale_report_does_not_dial_an_in_window_labeled_call(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    notes = _Notes()
    bland = _Bland()
    _install_fakes(monkeypatch, notes, bland)
    monkeypatch.setenv("ROBIE_PHONE_LIVE_CALLS", "1")
    monkeypatch.setattr(
        "robie_job_engine.bland_prod_wiring.build_call_dependencies",
        lambda **kwargs: (_Phone(), bland, None, False),
    )
    stale_at = (MONDAY - timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
    task = _task(
        description="Please call the client about the renewal.",
        assigned_producer="Pat Example",
    )
    report = _report(task)
    report = type(report)(
        message_id=report.message_id,
        filename=report.filename,
        digest=report.digest,
        received_at=stale_at,
        tasks=report.tasks,
        row_count=report.row_count,
        newest_created_et=report.newest_created_et,
    )
    monkeypatch.setattr(
        "robie_job_engine.ezlynx_task_intake.fetch_latest_task_report",
        _fetch(report),
    )
    assert run_intake(db_path=str(db)) == 2
    assert bland.dials == 0
    assert notes.notes == []
    assert SeenTaskStore(str(db)).is_empty()
    with JobStore(str(db)).connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        status = conn.execute(
            "SELECT status FROM ezlynx_task_intake_runs"
        ).fetchone()[0]
    assert status == "stale"


def test_outcome_note_requires_the_discussion_to_belong_to_the_applicant(monkeypatch):
    from robie_job_engine.ezlynx_discussions import file_note_to_existing_discussion

    monkeypatch.setenv("ROBIE_EZLYNX_WRITE_SCOPE", "all")
    monkeypatch.setenv("ROBIE_PLAYGROUND", "1")

    class _Client:
        def __init__(self, ids):
            self.ids = list(ids)
            self.posts: list[str] = []

        def get_discussions(self, applicant_id):
            del applicant_id
            return [{"discussionId": item, "title": "Task Note"} for item in self.ids]

        def get_discussion_ids(self, applicant_id):
            del applicant_id
            return list(self.ids)

        def append_note(self, discussion_id, body, **kwargs):
            del discussion_id, kwargs
            self.posts.append(body)
            return {"noteId": "N-1"}

    foreign = _Client(["999"])
    refused = file_note_to_existing_discussion(
        foreign, "80026158", "They answered and I talked to them.",
        discussion_id="70026158",
    )
    assert refused["status"] == "pending"
    assert foreign.posts == []

    owned = _Client(["70026158"])
    accepted = file_note_to_existing_discussion(
        owned, "80026158", "They answered and I talked to them.",
        discussion_id="70026158",
    )
    assert accepted["status"] != "pending" or accepted.get("reason_code") != "no matching discussion"
    assert accepted.get("reason") != "no matching discussion"


def test_intake_unit_does_not_enable_reassignment_or_fill_the_field_contract():
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    unit = (root / "deploy/systemd/robie-task-intake.service").read_text(encoding="utf-8")
    assert "EZLYNX_TASK_REASSIGN_ENABLED=" not in unit
    assert "Environment=EZLYNX_TASK_REASSIGN_ENABLED" not in unit
    assert "ROBIE_TASK_FIELD_CONTRACT_PATH=" not in unit
    contract = json.loads(
        (root / "deploy/ezlynx_task_field_contract.json").read_text(encoding="utf-8")
    )
    assert contract["fields"] == {}
    assert contract["status"] == "UNVERIFIED"


def test_dry_run_of_a_stale_report_says_what_it_would_do(tmp_path, monkeypatch, caplog):
    import logging

    db = tmp_path / "jobs.db"
    notes = _Notes()
    bland = _Bland()
    _install_fakes(monkeypatch, notes, bland)
    stale_at = (MONDAY - timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
    task = _task(description="Please call the client about the renewal.")
    report = _report(task)
    report = type(report)(
        message_id=report.message_id,
        filename=report.filename,
        digest=report.digest,
        received_at=stale_at,
        tasks=report.tasks,
        row_count=report.row_count,
        newest_created_et=report.newest_created_et,
    )
    monkeypatch.setattr(
        "robie_job_engine.ezlynx_task_intake.fetch_latest_task_report",
        _fetch(report),
    )
    with caplog.at_level(logging.INFO, logger="ezlynx_task_intake"):
        assert run_intake(db_path=str(db), dry_run=True) == 2
    assert "refusing to start new work" in caplog.text
    assert "[DRY-RUN]" in caplog.text
    assert "No jobs, no EZLynx writes, no worker" in caplog.text
    assert notes.notes == []
    assert bland.dials == 0
    with JobStore(str(db)).connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM ezlynx_task_intake_runs").fetchone()[0] == 0


def test_pending_work_rereads_job_status_before_acting(tmp_path, monkeypatch):
    from robie_job_engine.ezlynx_task_jobs import _find_by_idempotency_key, task_idempotency_key
    from robie_job_engine.models import JobStatus

    db = tmp_path / "jobs.db"
    notes = _Notes()
    bland = _Bland()
    _install_fakes(monkeypatch, notes, bland)
    JobStore(str(db))
    SeenTaskStore(str(db)).observe(["seed-task"], report_digest="seed")
    acted: list[str] = []
    pair = {"90026158", "90026159"}

    def wrapped(self, store, job):
        del self
        task_id = str((job.get("payload") or {}).get("task_id") or "")
        acted.append(task_id)
        for other_id in pair - {task_id}:
            other = _find_by_idempotency_key(store, task_idempotency_key(other_id))
            assert other is not None
            with store.transaction() as conn:
                conn.execute(
                    "UPDATE jobs SET status=? WHERE id=?",
                    (JobStatus.FAILED.value, other["id"]),
                )
        return {"ok": True}

    monkeypatch.setattr(TaskAssignmentWorker, "process_job", wrapped)
    # 09:00 Central is 10:00 ET, the same moment as the test clock.
    # 08:00 Central is earlier that morning. A later clock time is in
    # the future and is held instead of queued.
    newer = _task(task_id="90026158", created_at="2026-10-05T09:00:00")
    older = _task(
        task_id="90026159",
        discussion_id="70020002",
        applicant_id="80026159",
        created_at="2026-10-05T08:00:00",
    )
    monkeypatch.setattr(
        "robie_job_engine.ezlynx_task_intake.fetch_latest_task_report",
        _fetch(_report(newer, older)),
    )
    assert run_intake(db_path=str(db)) == 0
    assert len(acted) == 1
    assert acted[0] in pair
