#!/usr/bin/env python3
"""Parser for EZLynx task assignment report CSVs.

Tasks assigned to "Robie AI" in EZLynx are exported via a scheduled
Looker Look ("Robie AI - Task Check-In") that emails a CSV to
robie@streetsmart.insurance. This module parses those CSVs into task
records for the assignment worker.

The report also carries plain discussion notes. Notes carrying the
"Robie Call" activity label are call triggers: they are kept (even
with a blank assignee) and routed to the Bland call workflow by the
assignment worker. The label column is the signal — not the note text.

Real CSV schema (verified 2026-10-03/2026-10-04 from live deliveries):
- From: Applied Reporting <DoNotReply@appliedsystems.com>
- Subject: "Robie AI - Task Check-In"
- 32 columns, headers WITHOUT Looker view prefixes, newest-first
- Includes an "Activity Labels" column (e.g. "Robie Call")
- NOTE: The Looker filter is NOT baked into the saved Look — the CSV
  contains rows beyond Robie AI's queue. This parser keeps exactly:
  (Task Assigned To = Robie AI) OR ("Robie Call" in Activity Labels),
  as defense in depth.

Fail-closed: missing/malformed data raises, never silently ignored.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass


@dataclass(frozen=True)
class AssignedTask:
    """A single task assigned to Roby from the EZLynx report.

    Also covers "Robie Call" labeled discussion notes (blank assignee):
    those carry a synthetic task_id of the form
    ``note-<discussion_id>-<created_date>`` because plain notes have no
    Task ID. The synthetic ID is stable across report deliveries, so
    idempotency and checkpoint keys work unchanged.
    """
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
    labels: str = ""           # Activity Labels (e.g. "Robie Call")


class TaskReportParseError(ValueError):
    """Raised when the task report CSV is missing or malformed."""
    pass


# Real CSV headers from the live Looker export (verified 2026-10-03).
# Looker strips view prefixes in CSV exports.
# "Activity Labels" and "Created Date" are required: the label column
# drives the Robie Call trigger, and Created Date anchors the stable
# synthetic ID for labeled notes (which have no Task ID).
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
    "Activity Labels",
    "Created Date",
]

# The assignee name as it appears in EZLynx.
ROBIE_ASSIGNEE = "Robie AI"

# Activity-type values that represent real tasks (with Task IDs).
# Plain discussion notes ("Note") carry no Task ID.
TASK_ACTIVITY_TYPES = ("Task Creation Note", "Task Note")

# EZLynx activity label (case-insensitive) that triggers the Bland
# call workflow for a plain discussion note.
LABEL_ROBIE_CALL = "robie call"

# Prefix for synthetic task IDs minted for labeled notes.
NOTE_ID_PREFIX = "note-"


def is_note_task_id(task_id: str) -> bool:
    """True for synthetic IDs minted for labeled notes (no real Task ID)."""
    return str(task_id or "").startswith(NOTE_ID_PREFIX)


def synthetic_note_task_id(discussion_id: str, created_date: str) -> str:
    """Stable synthetic task ID for a labeled note.

    Discussion ID alone is not unique per note (one discussion holds
    many notes), so the note's Created Date anchors it. Both values
    come straight from the report row, so the ID is identical on every
    delivery of the same row — idempotency and checkpoint keys stay
    stable.
    """
    return f"{NOTE_ID_PREFIX}{discussion_id}-{created_date}"


def parse_task_report(csv_content: str) -> list[AssignedTask]:
    """Parse a task report CSV into AssignedTask records.

    Keeps exactly the rows Robie must act on:
      (Task Assigned To = "Robie AI") OR ("Robie Call" in Activity Labels).
    The report contains other rows; the Looker-side filter is not reliable,
    so this parser enforces the rule itself (defense in depth).

    Fail-closed: raises TaskReportParseError on empty content,
    missing headers, or ragged rows. Returns empty list (not error)
    when nothing matches — that is a healthy, quiet outcome.
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
        labels = (row.get("Activity Labels") or "").strip()
        activity_type = (row.get("Activity Type") or "").strip()

        is_robie_task = (assigned_to == ROBIE_ASSIGNEE)
        is_labeled_call_note = (LABEL_ROBIE_CALL in labels.lower())
        if not (is_robie_task or is_labeled_call_note):
            continue

        task_id = (row.get("Task ID") or "").strip()
        discussion_id = (row.get("Discussion ID") or "").strip()
        created_date = (row.get("Created Date") or "").strip()
        if not task_id:
            if activity_type in TASK_ACTIVITY_TYPES:
                raise TaskReportParseError(
                    f"Row {line_no} ({activity_type}) has no Task ID"
                )
            # Plain labeled note: mint a stable synthetic ID so
            # idempotency and checkpoint keys work unchanged.
            if not discussion_id or not created_date:
                raise TaskReportParseError(
                    f"Row {line_no} labeled note has no Discussion ID "
                    f"or Created Date"
                )
            task_id = synthetic_note_task_id(discussion_id, created_date)

        tasks.append(AssignedTask(
            task_id=task_id,
            title=activity_type,
            description=(row.get("Note") or "").strip(),
            applicant_id=(row.get("Applicant ID") or "").strip(),
            applicant_name=(row.get("Account Name") or "").strip(),
            assigned_to=assigned_to,
            due_date=(row.get("Task Due Date") or "").strip(),
            priority=(row.get("Task Priority") or "").strip(),
            created_date=(row.get("Task Created Date") or "").strip(),
            status=(row.get("Task Status") or "").strip(),
            discussion_id=discussion_id,
            last_modified=(row.get("Task Last Modified Date") or "").strip(),
            # Reassignment routing — optional columns, empty when absent.
            # Never fail the parse because a routing column is missing.
            created_by=(row.get("Task Created By") or "").strip(),
            assigned_producer=(row.get("Assigned Producer") or "").strip(),
            csr=(row.get("CSR") or "").strip(),
            labels=labels,
        ))

    return tasks
