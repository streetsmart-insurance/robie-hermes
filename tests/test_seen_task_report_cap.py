"""500-row reports drop older tasks. Seen tasks are not new and not re-dialed."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from robie_job_engine.ezlynx_seen_tasks import SeenTaskStore
from robie_job_engine.ezlynx_task_inbox import IngestedReport
from robie_job_engine.ezlynx_task_jobs import ensure_task_job
from robie_job_engine.ezlynx_task_report import (
    REPORT_ROW_CAP,
    AssignedTask,
    parse_task_report,
    parse_task_report_detail,
)
from robie_job_engine.models import JobStatus
from robie_job_engine.store import JobStore

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "robie_ai_task_check_in_scrubbed.csv"

HEADER = (
    "Task ID,Applicant ID,Account Name,Task Assigned To,Task Status,"
    "Task Due Date,Task Priority,Task Created Date,Task Last Modified Date,"
    "Note,Activity Type,Discussion ID,Created Date,Activity Labels"
)


def _row(index: int, *, assigned: str, label: str, created: str) -> str:
    return (
        f"{900000000 + index},{800000000 + index},Synthetic Person {index},{assigned},"
        f"Open,2026-10-06,Normal,2026-10-05,2026-10-05,"
        f"Synthetic note {index},Task Note,{700000000 + index},{created},{label}"
    )


def test_scrubbed_fixture_converts_central_created_date_and_live_label():
    text = FIXTURE.read_text(encoding="utf-8")
    assert "Ferrara" not in text
    assert "Calhoun" not in text
    tasks = parse_task_report(text)
    assert [task.task_id for task in tasks] == ["90029523", "90025064", "90025065"]
    lead = tasks[0]
    assert lead.activity_labels == "Robie lead follow-up"
    assert lead.created_at == "2026-10-05T09:11:00"
    assert lead.created_at_et.startswith("2026-10-05T10:11:00")
    assert lead.applicant_name == "Avery Sample"
    assert tasks[1].activity_labels == "Robie Call"
    assert tasks[1].created_at_et.startswith("2026-10-05T08:59:00")


def test_exactly_500_rows_logs_a_truncation_warning(caplog):
    rows = [
        _row(i, assigned="Robie AI" if i == 0 else "Someone Else",
             label="Robie lead follow-up", created="2026-10-05T09:11:00")
        for i in range(REPORT_ROW_CAP)
    ]
    text = HEADER + "\n" + "\n".join(rows) + "\n"
    with caplog.at_level(logging.WARNING, logger="ezlynx_task_report"):
        parsed = parse_task_report_detail(text)
    assert parsed.row_count == REPORT_ROW_CAP
    assert len(parsed.tasks) == 1
    assert any("truncated" in record.message for record in caplog.records)


def test_499_rows_do_not_warn(caplog):
    rows = [
        _row(i, assigned="Someone Else", label="", created="2026-10-05T09:11:00")
        for i in range(REPORT_ROW_CAP - 1)
    ]
    text = HEADER + "\n" + "\n".join(rows) + "\n"
    with caplog.at_level(logging.WARNING, logger="ezlynx_task_report"):
        parsed = parse_task_report_detail(text)
    assert parsed.row_count == REPORT_ROW_CAP - 1
    assert not any("truncated" in record.message for record in caplog.records)


def _task(**overrides) -> AssignedTask:
    base = dict(
        task_id="90029523",
        title="Task Note",
        description="Synthetic lead follow-up. No client data.",
        applicant_id="900100001",
        applicant_name="Avery Sample",
        assigned_to="Robie AI",
        due_date="2026-10-06",
        priority="Normal",
        created_date="2026-10-05",
        status="Open",
        discussion_id="900945654",
        last_modified="2026-10-05T09:11:00",
        created_by="Pat Example",
        activity_labels="Robie lead follow-up",
        created_at="2026-10-05T09:11:00",
        created_at_et="2026-10-05T10:11:00-04:00",
    )
    base.update(overrides)
    return AssignedTask(**base)


def test_seen_store_keeps_a_dropped_task_from_looking_new(tmp_path):
    store = SeenTaskStore(str(tmp_path / "jobs.db"))
    new_ids, dropped = store.observe(["9001", "9002"], report_digest="first")
    assert new_ids == {"9001", "9002"}
    assert dropped == set()
    new_ids, dropped = store.observe(["9002"], report_digest="second")
    assert new_ids == set()
    assert dropped == {"9001"}
    assert "9001" in store.known_ids()


def test_previously_seen_terminal_task_is_not_reopened(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    jobs = JobStore(str(db))
    task = _task()
    job, created = ensure_task_job(jobs, task)
    assert created is True
    with jobs.connect() as conn:
        conn.execute(
            "UPDATE jobs SET status=? WHERE id=?",
            (JobStatus.COMPLETE.value, job["id"]),
        )
    SeenTaskStore(str(db)).observe([task.task_id], report_digest="old")

    changed = _task(last_modified="2026-10-05T12:00:00")
    report = IngestedReport(
        message_id="msg-synthetic",
        filename="Robie_AI_-_Task_Check-In_synthetic.csv",
        digest="digest-new",
        received_at=(datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        tasks=(changed,),
        row_count=1,
    )

    monkeypatch.setattr(
        "robie_job_engine.report_email_source.build_default_gmail_service",
        lambda: object(),
    )
    monkeypatch.setattr(
        "robie_job_engine.ezlynx_task_intake.fetch_latest_task_report",
        lambda _service: report,
    )
    from robie_job_engine.ezlynx_task_intake import run_intake

    assert run_intake(db_path=str(db), dry_run=True) == 0
    fresh = JobStore(str(db)).get_job(job["id"])
    assert fresh["status"] == JobStatus.COMPLETE.value
    with JobStore(str(db)).connect() as conn:
        row = conn.execute(
            """SELECT status, newest_created_et, row_count, digest
               FROM ezlynx_task_intake_heartbeats ORDER BY id DESC LIMIT 1"""
        ).fetchone()
    assert row["status"] == "ok"
    assert row["newest_created_et"].startswith("2026-10-05T10:11:00")
    assert row["row_count"] == 1
    assert row["digest"] == "digest-new"
