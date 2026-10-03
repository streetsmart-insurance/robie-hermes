#!/usr/bin/env python3
"""Parser for EZLynx task assignment report CSVs.

Tasks assigned to "Robie AI" in EZLynx are exported via a scheduled
Looker Look ("Robie AI - Task Check-In") that emails a CSV to
robie@streetsmart.insurance. This module parses those CSVs into task
records for the assignment worker.

Real CSV schema (verified 2026-10-03 from live test delivery):
- From: Applied Reporting <DoNotReply@appliedsystems.com>
- Subject: "Robie AI - Task Check-In"
- 32 columns, headers WITHOUT Looker view prefixes
- NOTE: The Looker filter (Task Assigned To = Robie AI) is NOT baked
  into the saved Look — the CSV contains ALL open tasks. This parser
  filters to Robie AI rows itself (defense in depth).

Fail-closed: missing/malformed data raises, never silently ignored.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass


@dataclass(frozen=True)
class AssignedTask:
    """A single task assigned to Roby from the EZLynx report."""
    task_id: str
    title: str            # Activity Type (e.g. "Task Note")
    description: str      # Note text
    applicant_id: str
    applicant_name: str   # Account Name
    assigned_to: str      # Task Assigned To
    due_date: str         # Task Due Date
    priority: str         # Task Priority
    created_date: str     # Task Created Date
    status: str           # Task Status
    discussion_id: str    # Discussion ID (for write-back)
    last_modified: str    # Task Last Modified Date (for idempotency)
    created_by: str = ""       # Task Created By (reassignment fallback 1)
    assigned_producer: str = ""  # Assigned Producer (reassignment fallback 2)
    csr: str = ""              # CSR (reassignment fallback 3)


class TaskReportParseError(ValueError):
    """Raised when the task report CSV is missing or malformed."""
    pass


# Real CSV headers from the live Looker export (verified 2026-10-03).
# Looker strips view prefixes in CSV exports.
REQUIRED_HEADERS = [
    "Task ID",
    "Applicant ID",
    "Account Name",
    "Task Assigned To",
    "Task Status",
    "Task Due Date",
    "Task Priority",
    "Task Created Date",
    "Task Last Modified Date",
    "Note",
    "Activity Type",
    "Discussion ID",
]

# The assignee name as it appears in EZLynx.
ROBIE_ASSIGNEE = "Robie AI"


def parse_task_report(csv_content: str) -> list[AssignedTask]:
    """Parse a task report CSV into AssignedTask records.

    Filters to tasks assigned to Robie AI (the report contains all
    open tasks; the Looker-side filter is not reliable).

    Fail-closed: raises TaskReportParseError on empty content,
    missing headers, or ragged rows. Returns empty list (not error)
    when no tasks are assigned to Robie AI — that is a healthy,
    quiet outcome.
    """
    if not csv_content or not csv_content.strip():
        raise TaskReportParseError("Empty CSV content")

    # utf-8-sig strips a BOM if present
    reader = csv.DictReader(io.StringIO(csv_content.strip()))
    headers = reader.fieldnames or []

    missing = [h for h in REQUIRED_HEADERS if h not in headers]
    if missing:
        raise TaskReportParseError(
            f"Missing expected headers: {missing}. Got: {headers}"
        )

    tasks: list[AssignedTask] = []
    for line_no, row in enumerate(reader, start=2):
        # Fail-closed on ragged rows
        if None in row.values():
            raise TaskReportParseError(
                f"Ragged row at line {line_no}: {row}"
            )

        assigned_to = (row.get("Task Assigned To") or "").strip()
        if assigned_to != ROBIE_ASSIGNEE:
            continue

        task_id = (row.get("Task ID") or "").strip()
        if not task_id:
            raise TaskReportParseError(
                f"Row {line_no} assigned to Robie AI has no Task ID"
            )

        tasks.append(AssignedTask(
            task_id=task_id,
            title=(row.get("Activity Type") or "").strip(),
            description=(row.get("Note") or "").strip(),
            applicant_id=(row.get("Applicant ID") or "").strip(),
            applicant_name=(row.get("Account Name") or "").strip(),
            assigned_to=assigned_to,
            due_date=(row.get("Task Due Date") or "").strip(),
            priority=(row.get("Task Priority") or "").strip(),
            created_date=(row.get("Task Created Date") or "").strip(),
            status=(row.get("Task Status") or "").strip(),
            discussion_id=(row.get("Discussion ID") or "").strip(),
            last_modified=(row.get("Task Last Modified Date") or "").strip(),
            # Reassignment routing — optional columns, empty when absent.
            # Never fail the parse because a routing column is missing.
            created_by=(row.get("Task Created By") or "").strip(),
            assigned_producer=(row.get("Assigned Producer") or "").strip(),
            csr=(row.get("CSR") or "").strip(),
        ))

    return tasks
