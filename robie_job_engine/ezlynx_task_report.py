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
import logging
from dataclasses import dataclass
from datetime import datetime

from .report_clock import report_created_et

logger = logging.getLogger("ezlynx_task_report")

# Looker caps the export at 500 data rows, newest first.
REPORT_ROW_CAP = 500


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
    activity_labels: str = ""  # Activity Labels (Robie Call / workflow labels)
    created_at: str = ""       # Created Date, naive America/Chicago
    created_at_et: str = ""    # Created Date converted to America/New_York
    # "task": a task assigned to Robie AI (the original path).
    # "label": a row that carries a Robie call label but is not assigned to
    # Robie AI, usually a plain EZLynx note (Carlo's rule, Oct 7 2026:
    # staff just add the label). These rows are never reassigned.
    source: str = "task"


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
# Robie's own notes are never a request, even if a label shows on them.
ROBIE_NOTE_AUTHOR = "Robie AI"
NOTE_LABEL_PICKUP_ENV = "ROBIE_NOTE_LABEL_PICKUP"
SOURCE_TASK = "task"
SOURCE_LABEL = "label"


def note_label_pickup_enabled(env=None) -> bool:
    """Labeled notes are picked up unless ROBIE_NOTE_LABEL_PICKUP=0."""
    import os

    source = os.environ if env is None else env
    return str(source.get(NOTE_LABEL_PICKUP_ENV) or "").strip() != "0"


def label_row_id(discussion_id: str, created_at: str) -> str:
    """Stable numeric id for a labeled row that has no Task ID.

    "9" + Discussion ID + Created Date digits (to the second). Real EZLynx
    task ids are 8 digits, so this cannot collide with one, and it stays
    numeric so every existing id check still applies. Empty when either
    part is missing (the row is then skipped: Robie cannot write back).
    """
    disc = "".join(ch for ch in str(discussion_id or "") if ch.isdigit())
    stamp = "".join(ch for ch in str(created_at or "")[:19] if ch.isdigit())
    if not disc or len(stamp) < 12:
        return ""
    return f"9{disc}{stamp}"


def _call_label_key(labels: str) -> str:
    """Which call label this row carries, or "" when none.

    Uses the same exact-label rules as the intake (call_pickup), so a
    label that would not dial is not picked up either.
    """
    from .call_pickup import classify_call_request

    decision = classify_call_request(labels or "", "")
    if decision.action == "freeform":
        return "robie-call"
    if decision.action == "workflow":
        return decision.workflow_id or "workflow"
    return ""


@dataclass(frozen=True)
class TaskReportParse:
    tasks: list[AssignedTask]
    row_count: int
    newest_created_et: str = ""


def parse_task_report(csv_content: str) -> list[AssignedTask]:
    """Parse a task report CSV into AssignedTask records."""
    return parse_task_report_detail(csv_content).tasks


def parse_task_report_detail(
    csv_content: str, *, include_labeled_notes: bool | None = None,
) -> TaskReportParse:
    """Parse a task report CSV into AssignedTask records.

    Filters to tasks assigned to Robie AI (the report contains all
    open tasks; the Looker-side filter is not reliable).

    Fail-closed: raises TaskReportParseError on empty content,
    missing headers, or ragged rows. Returns empty list (not error)
    when no tasks are assigned to Robie AI — that is a healthy,
    quiet outcome.

    Labeled rows (Carlo's rule, Oct 7 2026): a row that is NOT assigned to
    Robie AI but carries a Robie call label in Activity Labels (usually a
    plain note) is kept too, with source="label". Its id is the real Task
    ID when the row has one, otherwise label_row_id(). Robie's own notes
    are skipped. One row per applicant, discussion and label is kept (the
    newest, since the report is newest first). Turn this off with
    ROBIE_NOTE_LABEL_PICKUP=0.

    Created Date is America/Chicago and is stored again as Eastern.
    Exactly 500 data rows logs a truncation warning.
    """
    if include_labeled_notes is None:
        include_labeled_notes = note_label_pickup_enabled()
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
    row_count = 0
    newest_et: datetime | None = None
    label_seen: set[tuple[str, str, str]] = set()
    robie_task_ids: set[str] = set()
    robie_discussions: set[tuple[str, str]] = set()
    for line_no, row in enumerate(reader, start=2):
        row_count += 1
        # Fail-closed on ragged rows
        if None in row.values():
            raise TaskReportParseError(
                f"Ragged row at line {line_no}: {row}"
            )

        row_created = report_created_et(
            (row.get("Created Date") or "").strip()
            or (row.get("Task Created Date") or "").strip()
        )
        if row_created is not None and (newest_et is None or row_created > newest_et):
            newest_et = row_created

        assigned_to = (row.get("Task Assigned To") or "").strip()
        if assigned_to != ROBIE_ASSIGNEE:
            if include_labeled_notes:
                labeled = _labeled_row(row, label_seen)
                if labeled is not None:
                    tasks.append(labeled)
            continue

        task_id = (row.get("Task ID") or "").strip()
        if not task_id:
            raise TaskReportParseError(
                f"Row {line_no} assigned to Robie AI has no Task ID"
            )

        robie_task_ids.add(task_id)
        robie_discussions.add((
            (row.get("Applicant ID") or "").strip(),
            (row.get("Discussion ID") or "").strip(),
        ))
        created_at = (row.get("Created Date") or "").strip()
        created_date = (row.get("Task Created Date") or "").strip()
        created_et = report_created_et(created_at or created_date)
        tasks.append(AssignedTask(
            task_id=task_id,
            title=(row.get("Activity Type") or "").strip(),
            description=(row.get("Note") or "").strip(),
            applicant_id=(row.get("Applicant ID") or "").strip(),
            applicant_name=(row.get("Account Name") or "").strip(),
            assigned_to=assigned_to,
            due_date=(row.get("Task Due Date") or "").strip(),
            priority=(row.get("Task Priority") or "").strip(),
            created_date=created_date,
            status=(row.get("Task Status") or "").strip(),
            discussion_id=(row.get("Discussion ID") or "").strip(),
            last_modified=(row.get("Task Last Modified Date") or "").strip(),
            # Reassignment routing — optional columns, empty when absent.
            # Never fail the parse because a routing column is missing.
            created_by=(row.get("Task Created By") or "").strip(),
            assigned_producer=(row.get("Assigned Producer") or "").strip(),
            csr=(row.get("CSR") or "").strip(),
            activity_labels=(row.get("Activity Labels") or "").strip(),
            created_at=created_at,
            created_at_et=created_et.isoformat() if created_et else "",
        ))

    # A labeled row whose Task ID is also a Robie AI task row is that
    # task, handled by the original path. Keep one. A labeled note on the
    # same client discussion as a Robie AI task in this report is left to
    # that task too: the call dedupe keys on note text, so both paths could
    # otherwise dial the same client the same day.
    kept: list[AssignedTask] = []
    for task in tasks:
        if task.source == SOURCE_LABEL and (
            task.task_id in robie_task_ids
            or (task.applicant_id, task.discussion_id) in robie_discussions
        ):
            logger.info(
                "labeled row %s left to the Robie AI task on discussion %s",
                task.task_id, task.discussion_id,
            )
            continue
        kept.append(task)
    tasks = kept

    if row_count == REPORT_ROW_CAP:
        logger.warning(
            "task report has exactly %s rows, newest first; older rows may "
            "have been truncated",
            REPORT_ROW_CAP,
        )

    return TaskReportParse(
        tasks=tasks,
        row_count=row_count,
        newest_created_et=newest_et.isoformat() if newest_et else "",
    )


def _labeled_row(
    row: dict, seen: set[tuple[str, str, str]],
) -> AssignedTask | None:
    """A labeled row not assigned to Robie AI, or None to skip it."""
    labels = (row.get("Activity Labels") or "").strip()
    if not labels:
        return None
    label = _call_label_key(labels)
    if not label:
        return None
    author = (row.get("Note Created by") or "").strip()
    if author.casefold() == ROBIE_NOTE_AUTHOR.casefold():
        return None
    applicant_id = (row.get("Applicant ID") or "").strip()
    discussion_id = (row.get("Discussion ID") or "").strip()
    if not applicant_id or not discussion_id:
        logger.warning(
            "labeled row skipped: no applicant or discussion id (label %s)", label,
        )
        return None
    created_at = (row.get("Created Date") or "").strip()
    created_date = (row.get("Task Created Date") or "").strip()
    real_task_id = (row.get("Task ID") or "").strip()
    row_id = real_task_id if real_task_id.isdigit() else label_row_id(
        discussion_id, created_at or created_date,
    )
    if not row_id:
        logger.warning(
            "labeled row skipped: no usable Created Date (discussion %s)", discussion_id,
        )
        return None
    key = (applicant_id, discussion_id, label)
    if key in seen:
        return None
    seen.add(key)
    created_et = report_created_et(created_at or created_date)
    return AssignedTask(
        task_id=row_id,
        title=(row.get("Activity Type") or "").strip(),
        description=(row.get("Note") or "").strip(),
        applicant_id=applicant_id,
        applicant_name=(row.get("Account Name") or "").strip(),
        assigned_to="",
        due_date=(row.get("Task Due Date") or "").strip(),
        priority=(row.get("Task Priority") or "").strip(),
        created_date=created_date or created_at,
        status=(row.get("Task Status") or "").strip(),
        discussion_id=discussion_id,
        last_modified=(row.get("Task Last Modified Date") or "").strip() or created_at,
        created_by=(row.get("Note Created by") or row.get("Task Created By") or "").strip(),
        assigned_producer=(row.get("Assigned Producer") or "").strip(),
        csr=(row.get("CSR") or "").strip(),
        activity_labels=labels,
        created_at=created_at,
        created_at_et=created_et.isoformat() if created_et else "",
        source=SOURCE_LABEL,
    )
