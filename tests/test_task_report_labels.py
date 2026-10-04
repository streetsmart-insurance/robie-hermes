#!/usr/bin/env python3
"""Tests for Robie Call labeled-note handling in the Task Check-In report.

Covers the label -> Bland call path:
- ezlynx_task_report.parse_task_report keeps "Robie Call" labeled notes
  (blank assignee) in addition to Robie AI assigned tasks.
- Labeled notes get stable synthetic task IDs (no Task ID on plain notes).
- task_assignment_worker routes labeled notes to the callback (Bland)
  workflow via the label, not just keywords.
- Labels survive the job payload round-trip.

Uses the REAL Looker CSV schema (32 columns, verified 2026-10-04 from
live "Robie AI - Task Check-In" deliveries).
"""

import csv
import io

import pytest

from robie_job_engine.ezlynx_task_report import (
    LABEL_ROBIE_CALL,
    AssignedTask,
    TaskReportParseError,
    is_note_task_id,
    parse_task_report,
    synthetic_note_task_id,
)
from robie_job_engine.ezlynx_task_jobs import job_payload_for_task
from robie_job_engine.task_assignment_worker import (
    LABEL_WORKFLOWS,
    TaskAssignmentWorker,
    _task_from_payload,
)


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


def make_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=REAL_HEADERS)
    writer.writeheader()
    for row in rows:
        writer.writerow({h: row.get(h, "") for h in REAL_HEADERS})
    return buf.getvalue()


def robie_task_row(**overrides):
    row = {
        "Applicant ID": "25486692",
        "Account Name": "Jake N Ferrara",
        "Task Assigned To": "Robie AI",
        "Branch": "Streetsmart Insurance",
        "Activity Type": "Task Note",
        "Note Created by": "Carlo Ferrara",
        "Task Status": "Open",
        "Task Created By": "Carlo Ferrara",
        "Created Date": "2026-10-03T09:24:00",
        "Task Last Modified Date": "2026-10-03",
        "Note": "Please call the client about renewal",
        "Task Priority": "Normal",
        "Task Created Date": "2026-10-03",
        "Task ID": "63429523",
        "Activity Labels": "",
        "Discussion ID": "849945654",
    }
    row.update(overrides)
    return row


def labeled_note_row(**overrides):
    row = {
        "Applicant ID": "26356199",
        "Account Name": "Buster Brown & test test",
        "Task Assigned To": "",
        "Branch": "Streetsmart Insurance",
        "Activity Type": "Note",
        "Note Created by": "Carlo Ferrara",
        "Task Status": "",
        "Task Created By": "Carlo Ferrara",
        "Created Date": "2026-10-04T08:51:28.120000",
        "Task Last Modified Date": "2026-10-04T08:51:28.120000",
        "Note": "Please call the client back today.",
        "Task Priority": "Normal",
        "Task Created Date": "2026-10-04",
        "Task ID": "",
        "Activity Labels": "Robie Call",
        "Discussion ID": "849843660",
    }
    row.update(overrides)
    return row


class TestLabeledNoteParsing:
    def test_labeled_note_is_kept(self):
        tasks = parse_task_report(make_csv([labeled_note_row()]))
        assert len(tasks) == 1
        note = tasks[0]
        assert note.labels == "Robie Call"
        assert note.assigned_to == ""
        assert note.applicant_id == "26356199"
        assert note.discussion_id == "849843660"

    def test_labeled_note_gets_stable_synthetic_id(self):
        tasks = parse_task_report(make_csv([labeled_note_row()]))
        note = tasks[0]
        assert is_note_task_id(note.task_id)
        assert note.task_id == "note-849843660-2026-10-04T08:51:28.120000"
        # Same row on the next delivery -> same ID (idempotency holds).
        again = parse_task_report(make_csv([labeled_note_row()]))
        assert again[0].task_id == note.task_id

    def test_synthetic_id_helper(self):
        assert synthetic_note_task_id("1", "2026-10-04") == "note-1-2026-10-04"
        assert not is_note_task_id("63429523")

    def test_unlabeled_plain_note_is_dropped(self):
        tasks = parse_task_report(make_csv([
            labeled_note_row(**{"Activity Labels": "", "Discussion ID": "1"}),
        ]))
        assert tasks == []

    def test_unmapped_label_is_dropped(self):
        tasks = parse_task_report(make_csv([
            labeled_note_row(**{"Activity Labels": "Some Other Label",
                              "Discussion ID": "2"}),
        ]))
        assert tasks == []

    def test_robie_task_without_label_still_kept(self):
        tasks = parse_task_report(make_csv([robie_task_row()]))
        assert len(tasks) == 1
        assert tasks[0].task_id == "63429523"
        assert tasks[0].labels == ""

    def test_robie_task_with_label_kept(self):
        tasks = parse_task_report(make_csv([
            robie_task_row(**{"Activity Labels": "Robie Call"}),
        ]))
        assert len(tasks) == 1
        assert tasks[0].task_id == "63429523"
        assert tasks[0].labels == "Robie Call"

    def test_mixed_report_keeps_exactly_the_right_rows(self):
        tasks = parse_task_report(make_csv([
            robie_task_row(),
            labeled_note_row(),
            labeled_note_row(**{"Activity Labels": "", "Discussion ID": "3"}),
            robie_task_row(**{"Task Assigned To": "Ashley Huntley",
                            "Task ID": "999", "Discussion ID": "4"}),
        ]))
        assert len(tasks) == 2
        assert {t.task_id for t in tasks} == {
            "63429523", "note-849843660-2026-10-04T08:51:28.120000"}

    def test_missing_labels_header_raises(self):
        headers = [h for h in REAL_HEADERS if h != "Activity Labels"]
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=headers)
        writer.writeheader()
        with pytest.raises(TaskReportParseError, match="Activity Labels"):
            parse_task_report(buf.getvalue())

    def test_task_note_without_task_id_still_raises(self):
        with pytest.raises(TaskReportParseError, match="no Task ID"):
            parse_task_report(make_csv([
                robie_task_row(**{"Task ID": ""}),
            ]))

    def test_labeled_note_without_discussion_id_raises(self):
        with pytest.raises(TaskReportParseError,
                           match="no Discussion ID"):
            parse_task_report(make_csv([
                labeled_note_row(**{"Discussion ID": ""}),
            ]))

    def test_label_match_is_case_insensitive(self):
        tasks = parse_task_report(make_csv([
            labeled_note_row(**{"Activity Labels": "robie call"}),
        ]))
        assert len(tasks) == 1


class FakeDiscussionClient:
    def __init__(self):
        self.notes = []

    def append_note(self, discussion_id, body):
        self.notes.append((discussion_id, body))
        return "n1"


class TestLabelRouting:
    def _worker(self):
        return TaskAssignmentWorker(discussion_client=FakeDiscussionClient())

    def test_label_workflows_maps_robie_call(self):
        assert LABEL_WORKFLOWS["robie call"] == "callback"

    def test_labeled_note_categorized_callback_without_keywords(self):
        worker = self._worker()
        task = AssignedTask(
            task_id="note-849843660-2026-10-04T08:51:28.120000",
            title="Note",
            description="Just a status update, no action words.",
            applicant_id="26356199",
            applicant_name="Buster Brown & test test",
            assigned_to="",
            due_date="",
            priority="Normal",
            created_date="2026-10-04",
            status="",
            discussion_id="849843660",
            last_modified="2026-10-04",
            labels="Robie Call",
        )
        assert worker._categorize_task(task) == "callback"

    def test_label_beats_keyword_routing(self):
        # A labeled note whose text looks like a document request still
        # goes to the call workflow: the label is the signal.
        worker = self._worker()
        task = AssignedTask(
            task_id="note-1-2026-10-04",
            title="Note",
            description="Please upload the renewal documents.",
            applicant_id="26356199",
            applicant_name="Buster Brown & test test",
            assigned_to="",
            due_date="",
            priority="Normal",
            created_date="2026-10-04",
            status="",
            discussion_id="1",
            last_modified="2026-10-04",
            labels="Robie Call",
        )
        assert worker._categorize_task(task) == "callback"

    def test_unlabeled_task_still_keyword_routed(self):
        worker = self._worker()
        task = AssignedTask(
            task_id="63429523",
            title="Task Note",
            description="Please call the client about renewal",
            applicant_id="25486692",
            applicant_name="Jake N Ferrara",
            assigned_to="Robie AI",
            due_date="",
            priority="Normal",
            created_date="2026-10-03",
            status="Open",
            discussion_id="849945654",
            last_modified="2026-10-03",
            labels="",
        )
        assert worker._categorize_task(task) == "callback"

    def test_labels_survive_payload_round_trip(self):
        tasks = parse_task_report(make_csv([labeled_note_row()]))
        payload = job_payload_for_task(tasks[0])
        assert payload["labels"] == "Robie Call"
        rebuilt = _task_from_payload(payload)
        assert rebuilt.labels == "Robie Call"
        assert rebuilt.task_id == tasks[0].task_id
