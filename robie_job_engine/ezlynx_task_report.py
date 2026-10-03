#!/usr/bin/env python3
"""Parser for EZLynx task assignment report CSVs.

Tasks assigned to "Robie AI" in EZLynx are exported via a scheduled
Look that emails a CSV to robie@streetsmart.insurance. This module
parses those CSVs into task records for the assignment worker.

The report follows the same pattern as the 4359/4247/4246/4372 reports:
- From: Applied Reporting <DoNotReply@appliedsystems.com>
- Scheduled email with CSV attachment
- Fail-closed: missing/malformed data raises, never silently ignored.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class AssignedTask:
    """A single task assigned to Roby from the EZLynx report."""
    task_id: str
    title: str
    description: str
    applicant_id: str
    applicant_name: str
    assigned_to: str
    due_date: str
    priority: str
    created_date: str
    status: str


class TaskReportParseError(ValueError):
    """Raised when the task report CSV is missing or malformed."""
    pass


# Expected CSV headers for the task assignment report.
# These will be verified against the real export once the Look is created.
EXPECTED_HEADERS = [
    "Task ID",
    "Task Title",
    "Task Description",
    "Applicant ID",
    "Applicant Name",
    "Assigned To",
    "Due Date",
    "Priority",
    "Created Date",
    "Status",
]


def parse_task_report(csv_content: str) -> list[AssignedTask]:
    """Parse a task report CSV into AssignedTask records.

    Fail-closed: raises TaskReportParseError on missing headers,
    ragged rows, or zero data rows. Tasks not assigned to Roby
    are filtered out (the report should already filter, but we
    double-check).
    """
    if not csv_content or not csv_content.strip():
        raise TaskReportParseError("Empty CSV content")

    reader = csv.DictReader(io.StringIO(csv_content))
    headers = reader.fieldnames or []

    # Verify headers match expected (fail-closed on mismatch)
    missing = [h for h in EXPECTED_HEADERS if h not in headers]
    if missing:
        raise TaskReportParseError(
            f"Missing expected headers: {missing}. Got: {headers}"
        )

    tasks = []
    for row_num, row in enumerate(reader, start=2):
        # Skip blank rows
        if not any((v or "").strip() for v in row.values()):
            continue

        # Fail on ragged rows (missing required fields)
        task_id = (row.get("Task ID") or "").strip()
        if not task_id:
            raise TaskReportParseError(
                f"Row {row_num}: blank Task ID"
            )

        assigned_to = (row.get("Assigned To") or "").strip()
        # Only process tasks assigned to Roby (case-insensitive)
        if "robie" not in assigned_to.lower() and "roby" not in assigned_to.lower():
            continue

        tasks.append(AssignedTask(
            task_id=task_id,
            title=(row.get("Task Title") or "").strip(),
            description=(row.get("Task Description") or "").strip(),
            applicant_id=(row.get("Applicant ID") or "").strip(),
            applicant_name=(row.get("Applicant Name") or "").strip(),
            assigned_to=assigned_to,
            due_date=(row.get("Due Date") or "").strip(),
            priority=(row.get("Priority") or "").strip(),
            created_date=(row.get("Created Date") or "").strip(),
            status=(row.get("Status") or "").strip(),
        ))

    if not tasks:
        raise TaskReportParseError("Zero task rows for Roby in report")

    return tasks


def tasks_to_json(tasks: list[AssignedTask]) -> list[dict[str, Any]]:
    """Convert tasks to JSON-serializable dicts for the worker."""
    return [
        {
            "task_id": t.task_id,
            "title": t.title,
            "description": t.description,
            "applicant_id": t.applicant_id,
            "applicant_name": t.applicant_name,
            "assigned_to": t.assigned_to,
            "due_date": t.due_date,
            "priority": t.priority,
            "created_date": t.created_date,
            "status": t.status,
        }
        for t in tasks
    ]
