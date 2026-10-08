"""Carlo's label rule (Oct 7 2026) and Jake's go-live limits.

Staff just add a Robie label to an EZLynx note. Robie Call dials only a
number typed in the note, never the number on file, and asks for the number
when none is typed. Robie Lead Follow Up uses the number on file. A daily
cap (5 by default for day one) holds extra calls with a plain-English note.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from robie_job_engine.call_pickup import DailyCallCapStore, daily_call_cap
from robie_job_engine.ezlynx_seen_tasks import SeenTaskStore
from robie_job_engine.ezlynx_task_intake import run_intake
from robie_job_engine.ezlynx_task_jobs import remember_note_label_pickup
from robie_job_engine.ezlynx_task_report import (
    label_row_id,
    note_label_pickup_enabled,
    parse_task_report_detail,
)
from robie_job_engine.store import JobStore

import test_task_intake_review_blockers as base

HEADER = (
    "Task ID,Applicant ID,Account Name,Task Assigned To,Task Status,"
    "Task Due Date,Task Priority,Task Created Date,Task Last Modified Date,"
    "Note,Activity Type,Discussion ID,Created Date,Activity Labels,Note Created by,"
    "Assigned Producer"
)
ON_FILE = "+17325550142"


def _note_row(index: int, *, label: str, note: str, created: str,
              author: str = "Pat Example", task_id: str = "",
              assigned: str = "", discussion: str | None = None) -> str:
    disc = discussion or str(70030000 + index)
    return (
        f"{task_id},{80030000 + index},Synthetic Person {index},{assigned},,,,,,"
        f"\"{note}\",Note,{disc},{created},{label},{author},Pat Example"
    )


def _csv(*rows: str) -> str:
    return HEADER + "\n" + "\n".join(rows) + "\n"


# ---------------------------------------------------------------- parsing


def test_labeled_note_without_a_task_gets_a_stable_numeric_id():
    text = _csv(_note_row(1, label="Robie Call", note="Call at 908-555-0199",
                          created="2026-10-05T08:30:15"))
    tasks = parse_task_report_detail(text, include_labeled_notes=True).tasks
    assert len(tasks) == 1
    task = tasks[0]
    assert task.source == "label"
    assert task.assigned_to == ""
    assert task.task_id == label_row_id("70030001", "2026-10-05T08:30:15")
    assert task.task_id == "97003000120261005083015"
    assert task.task_id.isdigit()
    assert task.description == "Call at 908-555-0199"
    assert task.created_by == "Pat Example"
    assert task.created_at_et.startswith("2026-10-05T09:30:15")


def test_robie_notes_unlabeled_and_unknown_labels_are_not_requests():
    text = _csv(
        _note_row(1, label="Robie Call", note="Robie called.", created="2026-10-05T08:30:00",
                  author="Robie AI"),
        _note_row(2, label="", note="Please call.", created="2026-10-05T08:31:00"),
        _note_row(3, label="Robie Callz", note="Call at 908-555-0199",
                  created="2026-10-05T08:32:00"),
        _note_row(4, label="Robie Call", note="no discussion", created="2026-10-05T08:33:00",
                  discussion="-"),
    )
    tasks = parse_task_report_detail(text, include_labeled_notes=True).tasks
    assert [t.task_id for t in tasks] == []


def test_one_request_per_discussion_and_label_newest_first():
    text = _csv(
        _note_row(1, label="Robie Call", note="newest", created="2026-10-05T09:00:00",
                  discussion="70039999"),
        _note_row(2, label="Robie Call", note="older", created="2026-10-05T08:00:00",
                  discussion="70039999"),
    )
    # Different applicants => different requests even on one discussion id.
    tasks = parse_task_report_detail(text, include_labeled_notes=True).tasks
    assert [t.description for t in tasks] == ["newest", "older"]
    same = _csv(
        _note_row(1, label="Robie Call", note="newest", created="2026-10-05T09:00:00",
                  discussion="70039999"),
        _note_row(1, label="Robie Call", note="older", created="2026-10-05T08:00:00",
                  discussion="70039999"),
    )
    tasks = parse_task_report_detail(same, include_labeled_notes=True).tasks
    assert [t.description for t in tasks] == ["newest"]


def test_a_labeled_row_that_is_also_a_robie_task_is_kept_once_as_a_task():
    text = _csv(
        _note_row(1, label="Robie Call", note="task", created="2026-10-05T09:00:00",
                  task_id="90031111", assigned="Robie AI"),
        _note_row(1, label="Robie Call", note="dup", created="2026-10-05T09:00:00",
                  task_id="90031111", assigned="Pat Example"),
    )
    tasks = parse_task_report_detail(text, include_labeled_notes=True).tasks
    assert [(t.task_id, t.source) for t in tasks] == [("90031111", "task")]


def test_note_pickup_can_be_turned_off():
    assert note_label_pickup_enabled({}) is True
    assert note_label_pickup_enabled({"ROBIE_NOTE_LABEL_PICKUP": "1"}) is True
    assert note_label_pickup_enabled({"ROBIE_NOTE_LABEL_PICKUP": "0"}) is False
    text = _csv(_note_row(1, label="Robie Call", note="x", created="2026-10-05T08:30:00"))
    assert parse_task_report_detail(text, include_labeled_notes=False).tasks == []


# ---------------------------------------------------------------- daily cap


def test_daily_cap_defaults_to_five_and_can_be_changed(tmp_path):
    assert daily_call_cap({}) == 5
    assert daily_call_cap({"ROBIE_CALL_DAILY_CAP": "25"}) == 25
    assert daily_call_cap({"ROBIE_CALL_DAILY_CAP": "0"}) is None
    store = DailyCallCapStore(str(tmp_path / "cap.sqlite"))
    for _ in range(5):
        store.record("2026-10-05")
    assert store.count("2026-10-05") == 5
    assert store.count("2026-10-06") == 0
    assert store.count("dry:2026-10-05") == 0


# ---------------------------------------------------------------- end to end


class _CapturingBland(base._VoicemailBland):
    def __init__(self):
        super().__init__()
        self.phones: list[str] = []

    def place_call_with_double_dial(self, *args, **kwargs):
        self.phones.append(str(args[0] if args else kwargs.get("phone")))
        return super().place_call_with_double_dial(*args, **kwargs)


class _CountingPhone(base._Phone):
    def __init__(self):
        self.looked: list[str] = []

    def get_phone(self, applicant_id):
        self.looked.append(str(applicant_id))
        return ON_FILE


class _AnyDiscussionNotes(base._FilingNotes):
    def get_discussion_ids(self, applicant_id):
        del applicant_id
        return [str(70030000 + i) for i in range(20)] + ["70026158"]


def _wire(tmp_path, monkeypatch, report_text: str, *, enabled_at: str):
    from robie_job_engine.robie_call_handler import _reset_module_state_for_tests

    db = tmp_path / "jobs.db"
    SeenTaskStore(str(db)).observe(["prior"], report_digest="prior")
    remember_note_label_pickup(JobStore(str(db)), now=enabled_at)
    notes = _AnyDiscussionNotes()
    bland = _CapturingBland()
    phone = _CountingPhone()
    clock = {"now": datetime(2026, 10, 5, 14, 5, tzinfo=timezone.utc)}  # Mon 10:05 ET
    base._install_fakes(monkeypatch, notes, bland)
    monkeypatch.setattr(
        "robie_job_engine.ezlynx_task_intake._intake_now", lambda: clock["now"],
    )
    monkeypatch.setattr(
        "robie_job_engine.bland_prod_wiring.build_call_dependencies",
        lambda **kwargs: (phone, bland, None, False),
    )
    monkeypatch.setattr(
        "robie_job_engine.robie_call_handler.RobieCallConfig", base._clocked_config(clock),
    )
    monkeypatch.setattr(
        "robie_job_engine.ezlynx_discussions.file_note_to_existing_discussion",
        base._file_note,
    )
    monkeypatch.setenv("ROBIE_PHONE_LIVE_CALLS", "1")
    monkeypatch.delenv("ROBIE_CALL_DAILY_CAP", raising=False)
    _reset_module_state_for_tests()
    parsed = parse_task_report_detail(report_text, include_labeled_notes=True)
    report = base._report(*parsed.tasks, message_id="m1", digest="d1")
    monkeypatch.setattr(
        "robie_job_engine.ezlynx_task_intake.fetch_latest_task_report", base._fetch(report),
    )
    return db, notes, bland, phone


def test_robie_call_note_with_a_typed_number_dials_that_number(tmp_path, monkeypatch):
    text = _csv(_note_row(1, label="Robie Call",
                          note="Please call about the renewal. Call at 908-555-0199.",
                          created="2026-10-05T08:30:00"))
    db, notes, bland, phone = _wire(tmp_path, monkeypatch, text,
                                    enabled_at="2026-10-05T13:00:00+00:00")
    assert run_intake(db_path=str(db)) == 0
    assert bland.phones == ["+19085550199"]
    assert phone.looked == []


def test_robie_call_note_without_a_number_asks_and_does_not_dial(tmp_path, monkeypatch):
    text = _csv(_note_row(1, label="Robie Call", note="Please call about the renewal.",
                          created="2026-10-05T08:30:00"))
    db, notes, bland, phone = _wire(tmp_path, monkeypatch, text,
                                    enabled_at="2026-10-05T13:00:00+00:00")
    assert run_intake(db_path=str(db)) == 0
    assert bland.phones == []
    assert phone.looked == []
    assert len(notes.notes) == 1
    body = notes.notes[0][1]
    assert "no phone number was typed in this note" in body
    assert "never the number on file" in body
    assert "Add a new note with the Robie Call label with the number to call." in body


def test_lead_follow_up_note_uses_the_number_on_file(tmp_path, monkeypatch):
    text = _csv(_note_row(1, label="Robie Lead Follow Up",
                          note="Lead asked about a homeowners quote. Call at 908-555-0199.",
                          created="2026-10-05T08:30:00"))
    db, notes, bland, phone = _wire(tmp_path, monkeypatch, text,
                                    enabled_at="2026-10-05T13:00:00+00:00")
    assert run_intake(db_path=str(db)) == 0
    assert bland.phones == [ON_FILE]
    assert phone.looked == ["80030001"]


def test_labeled_notes_from_before_pickup_was_turned_on_never_dial(tmp_path, monkeypatch):
    text = _csv(_note_row(1, label="Robie Call", note="Call at 908-555-0199.",
                          created="2026-10-02T08:30:00"))
    db, notes, bland, phone = _wire(tmp_path, monkeypatch, text,
                                    enabled_at="2026-10-05T13:00:00+00:00")
    assert run_intake(db_path=str(db)) == 0
    assert bland.phones == []
    assert notes.notes == []
    assert set(SeenTaskStore(str(db)).statuses().values()) <= {"baseline", "seen"}


def test_sixth_call_of_the_day_is_held_with_a_plain_note(tmp_path, monkeypatch):
    rows = [
        _note_row(i, label="Robie Call", note=f"Call at 908-555-01{i:02d}.",
                  created=f"2026-10-05T08:{30 + i:02d}:00")
        for i in range(1, 7)
    ]
    db, notes, bland, phone = _wire(tmp_path, monkeypatch, _csv(*rows),
                                    enabled_at="2026-10-05T13:00:00+00:00")
    for _ in range(4):
        assert run_intake(db_path=str(db)) == 0
    assert len(bland.phones) == 5
    held = [body for _d, body in notes.notes if "today's limit of 5" in body]
    assert len(held) == 1
    assert "Robie did not call because today's limit of 5 automated calls" in held[0]
