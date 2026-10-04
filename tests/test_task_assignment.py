#!/usr/bin/env python3
"""Tests for the EZLynx task report parser and assignment worker.

Uses the REAL Looker CSV schema (verified 2026-10-03 from the live
"Robie AI - Task Check-In" test delivery).
"""

import pytest

from robie_job_engine.ezlynx_task_report import (
    AssignedTask,
    TaskReportParseError,
    parse_task_report,
)
from robie_job_engine.task_assignment_worker import TaskAssignmentWorker


# Real CSV fixture — matches the live Looker export format.
# Headers are WITHOUT Looker view prefixes (Looker strips them in CSV).
REAL_CSV = """Applicant ID,Account Name,Task Assigned To,Branch,Activity Type,Note Created by,Task Status,Assigned Producer,CSR,Task Created By,Created Date,Task Due Date,Task Last Modified Date,Task Last Modified By,Note,Comment,Task Closed By,Policy Master ID,Task Priority,Sticky,Task Created By ID,Task Created Date,Task ID,Task Closed Date,Policy Number,Producer Code,Producer Code Override,Activity Labels,Lead Source,Discussion ID,Department,Service Team
25486692,Jake N Ferrara,Robie AI,Streetsmart Insurance,Task Note,Carlo Ferrara,Open,Carlo Ferrara,,Carlo Ferrara,2026-10-03T09:24:00,2026-10-05,2026-10-03,Carlo Ferrara,Test task for Roby - please ignore,,0,0,Normal,0,123,2026-10-03,63429523,,,,,,Google,849945654,,
220250093,ROBIE Test LLC,Robie AI,Streetsmart Insurance,Task Note,Carlo Ferrara,Open,Carlo Ferrara,,Carlo Ferrara,2026-10-03T08:00:00,2026-10-03,2026-10-03,Carlo Ferrara,Please call the client about renewal,,0,0,High,0,123,2026-10-03,63425064,,,,,,Google,849932997,,
48006672,Joseph & Emily Calhoun,Ashley Huntley,Streetsmart Insurance,Task Note,Ashley Huntley,Open,Ashley Huntley,Daniela Aguilar,Ashley Huntley,2026-10-03T09:58:07,2026-10-06,2026-10-03,Ashley Huntley,Fire claim loss run attached,,0,0,Normal,0,456,2026-09-23,63129743,,,,,,Google,847305170,,
"""

REAL_HEADERS = [
    "Applicant ID", "Account Name", "Task Assigned To", "Branch",
    "Activity Type", "Note Created by", "Task Status", "Assigned Producer",
    "CSR", "Task Created By", "Created Date", "Task Due Date",
    "Task Last Modified Date", "Task Last Modified By", "Note", "Comment",
    "Task Closed By", "Policy Master ID", "Task Priority", "Sticky",
    "Task Created By ID", "Task Created Date", "Task ID", "Task Closed Date",
    "Policy Number", "Producer Code", "Producer Code Override",
    "Activity Labels", "Lead Source", "Discussion ID", "Department",
    "Service Team",
]


class TestParseRealCsv:
    def test_parses_real_looker_csv(self):
        tasks = parse_task_report(REAL_CSV)
        # Only the 2 Robie AI rows (Ashley Huntley's row is filtered out)
        assert len(tasks) == 2
        assert all(t.assigned_to == "Robie AI" for t in tasks)

    def test_real_field_mapping(self):
        tasks = parse_task_report(REAL_CSV)
        task = next(t for t in tasks if t.task_id == "63429523")
        assert task.applicant_id == "25486692"
        assert task.applicant_name == "Jake N Ferrara"
        assert task.description == "Test task for Roby - please ignore"
        assert task.discussion_id == "849945654"
        assert task.status == "Open"
        assert task.priority == "Normal"
        assert task.due_date == "2026-10-05"
        assert task.last_modified == "2026-10-03"

    def test_filters_non_robie_tasks(self):
        tasks = parse_task_report(REAL_CSV)
        task_ids = {t.task_id for t in tasks}
        assert "63129743" not in task_ids  # Ashley Huntley's task

    def test_zero_robie_tasks_is_quiet_not_error(self):
        no_robie = REAL_CSV.replace("Robie AI", "Someone Else")
        tasks = parse_task_report(no_robie)
        assert tasks == []

    def test_empty_csv_raises(self):
        with pytest.raises(TaskReportParseError):
            parse_task_report("")

    def test_missing_headers_raises(self):
        with pytest.raises(TaskReportParseError, match="Missing expected headers"):
            parse_task_report("Foo,Bar\n1,2\n")

    def test_missing_task_id_raises(self):
        bad = REAL_CSV.replace("63429523", "", 1)
        with pytest.raises(TaskReportParseError, match="no Task ID"):
            parse_task_report(bad)


class FakeDiscussionClient:
    def __init__(self):
        self.notes: list[tuple[str, str]] = []

    def append_note(self, discussion_id: str, body: str):
        self.notes.append((discussion_id, body))


class TestWorker:
    def test_acknowledges_and_categorizes(self):
        tasks = parse_task_report(REAL_CSV)
        client = FakeDiscussionClient()
        worker = TaskAssignmentWorker(discussion_client=client)
        results = worker.process_tasks(tasks)

        assert len(results) == 2
        # Callback task (call the client) gets categorized
        cb = next(r for r in results if r.task_id == "63425064")
        assert cb.action == "categorized"
        assert "callback" in cb.detail

        # Notes went to the RIGHT discussions (from CSV, not guessed)
        discussion_ids = {d for d, _ in client.notes}
        assert "849945654" in discussion_ids
        assert "849932997" in discussion_ids
        # Never posted to Ashley Huntley's task discussion
        assert "847305170" not in discussion_ids

    def test_idempotent_on_repeat_report(self):
        tasks = parse_task_report(REAL_CSV)
        client = FakeDiscussionClient()
        worker = TaskAssignmentWorker(discussion_client=client)

        first = worker.process_tasks(tasks)
        assert all(r.action in ("categorized", "flagged_for_human") for r in first)

        # Same report again — all skipped, no duplicate notes
        notes_before = len(client.notes)
        second = worker.process_tasks(tasks)
        assert all(r.action == "skipped_seen" for r in second)
        assert len(client.notes) == notes_before

    def test_modified_task_reprocessed(self):
        tasks = parse_task_report(REAL_CSV)
        client = FakeDiscussionClient()
        worker = TaskAssignmentWorker(discussion_client=client)
        worker.process_tasks(tasks)

        # Task modified (new last_modified) → processed again
        modified_csv = REAL_CSV.replace(
            "63429523,,,,,,Google,849945654,,",
            "63429523,,,,,,Google,849945654,,",
        ).replace(
            "2026-10-03,Carlo Ferrara,Test task for Roby",
            "2026-10-04,Carlo Ferrara,Test task for Roby",
            1,
        )
        tasks2 = parse_task_report(modified_csv)
        results2 = worker.process_tasks(tasks2)
        reprocessed = [r for r in results2 if r.action != "skipped_seen"]
        assert len(reprocessed) == 1
        assert reprocessed[0].task_id == "63429523"

    def test_empty_task_list_is_quiet(self):
        worker = TaskAssignmentWorker(discussion_client=FakeDiscussionClient())
        assert worker.process_tasks([]) == []

    def test_unknown_task_flagged_for_human(self):
        csv_unknown = REAL_CSV.replace(
            "Test task for Roby - please ignore",
            "Xyzzy plugh frobnicate the wobble",
            1,
        )
        tasks = parse_task_report(csv_unknown)
        client = FakeDiscussionClient()
        worker = TaskAssignmentWorker(discussion_client=client)
        results = worker.process_tasks(tasks)

        flagged = next(r for r in results if r.task_id == "63429523")
        assert flagged.action == "flagged_for_human"

    def test_dry_run_posts_nothing(self):
        tasks = parse_task_report(REAL_CSV)
        worker = TaskAssignmentWorker(discussion_client=None)
        results = worker.process_tasks(tasks)
        assert len(results) == 2  # still processes, just logs
