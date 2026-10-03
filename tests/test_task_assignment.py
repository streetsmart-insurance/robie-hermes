#!/usr/bin/env python3
"""Tests for EZLynx task report parser and assignment worker."""

import unittest
from unittest.mock import MagicMock

import sys
sys.path.insert(0, '/tmp')

from ezlynx_task_report import (
    parse_task_report,
    TaskReportParseError,
    EXPECTED_HEADERS,
)
from task_assignment_worker import TaskAssignmentWorker


SAMPLE_CSV = """Task ID,Task Title,Task Description,Applicant ID,Applicant Name,Assigned To,Due Date,Priority,Created Date,Status
TASK-001,Call back client,Please call about renewal,25486692,Jake Ferrara,Robie AI,2026-10-05,High,2026-10-03,Open
TASK-002,Upload documents,Need loss runs PDF,25486692,Jake Ferrara,Robie AI,2026-10-06,Medium,2026-10-03,Open
TASK-003,Review quote,Check premium calculation,12345,Other Client,Carlo Ferrara,2026-10-07,Low,2026-10-03,Open
"""


class TestParseTaskReport(unittest.TestCase):
    def test_parses_roby_tasks(self):
        tasks = parse_task_report(SAMPLE_CSV)
        # Only Roby's tasks (TASK-001, TASK-002), not Carlo's (TASK-003)
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0].task_id, "TASK-001")
        self.assertEqual(tasks[0].assigned_to, "Robie AI")
        self.assertEqual(tasks[1].task_id, "TASK-002")

    def test_empty_csv_raises(self):
        with self.assertRaises(TaskReportParseError):
            parse_task_report("")

    def test_missing_headers_raises(self):
        bad_csv = "Wrong,Headers\nval1,val2\n"
        with self.assertRaises(TaskReportParseError):
            parse_task_report(bad_csv)

    def test_blank_task_id_raises(self):
        bad_csv = (
            "Task ID,Task Title,Task Description,Applicant ID,Applicant Name,"
            "Assigned To,Due Date,Priority,Created Date,Status\n"
            ",No ID task,desc,123,Name,Robie AI,2026-10-05,High,2026-10-03,Open\n"
        )
        with self.assertRaises(TaskReportParseError):
            parse_task_report(bad_csv)

    def test_zero_roby_tasks_raises(self):
        no_roby = (
            "Task ID,Task Title,Task Description,Applicant ID,Applicant Name,"
            "Assigned To,Due Date,Priority,Created Date,Status\n"
            "TASK-999,Other task,desc,123,Name,Carlo Ferrara,2026-10-05,High,2026-10-03,Open\n"
        )
        with self.assertRaises(TaskReportParseError):
            parse_task_report(no_roby)


class TestTaskAssignmentWorker(unittest.TestCase):
    def test_categorize_callback(self):
        worker = TaskAssignmentWorker(discussion_client=None)
        from ezlynx_task_report import AssignedTask
        task = AssignedTask(
            task_id="T1", title="Call back client",
            description="Please phone the client",
            applicant_id="123", applicant_name="Test",
            assigned_to="Robie AI", due_date="2026-10-05",
            priority="High", created_date="2026-10-03", status="Open",
        )
        self.assertEqual(worker._categorize_task(task), "callback")

    def test_categorize_unknown(self):
        worker = TaskAssignmentWorker(discussion_client=None)
        from ezlynx_task_report import AssignedTask
        task = AssignedTask(
            task_id="T2", title="Do something vague",
            description="Handle this somehow",
            applicant_id="123", applicant_name="Test",
            assigned_to="Robie AI", due_date="2026-10-05",
            priority="High", created_date="2026-10-03", status="Open",
        )
        self.assertEqual(worker._categorize_task(task), "unknown")

    def test_process_tasks_dry_run(self):
        worker = TaskAssignmentWorker(discussion_client=None)  # Dry-run, no client
        tasks = parse_task_report(SAMPLE_CSV)
        results = worker.process_tasks(tasks)
        self.assertEqual(len(results), 2)
        # First task is callback (clear), second is document (clear)
        actions = [r.action for r in results]
        self.assertIn("worked", actions)


if __name__ == '__main__':
    unittest.main()
