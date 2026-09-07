"""Department-first accountability normalization for EZLynx Reports 5.0.

This module keeps row-level evidence and separates verified facts from roster
fallbacks.  It is designed for a prior-business-day team-lead digest.
"""

from __future__ import annotations

import csv
import io
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Mapping, Optional

from .center_audits import parse_datetime


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _value(row: Mapping[str, Any], *aliases: str) -> str:
    normalized = {_key(str(k)): str(v or "").strip() for k, v in row.items() if k is not None}
    for alias in aliases:
        if normalized.get(_key(alias)):
            return normalized[_key(alias)]
    return ""


def _rows(source: Path | str) -> list[dict[str, str]]:
    if isinstance(source, Path) or ("\n" not in str(source) and Path(str(source)).exists()):
        content = Path(source).read_text(encoding="utf-8-sig", errors="replace")
    else:
        content = str(source)
    return [dict(row) for row in csv.DictReader(io.StringIO(content))]


@dataclass(frozen=True)
class OverdueTask:
    task_id: str
    applicant_id: str
    account_name: str
    owner: str
    department: str
    department_evidence: str
    workstream_department: str
    activity_type: str
    task_status: str
    task_created_date: Optional[str]
    due_date: Optional[str]
    last_modified_date: Optional[str]
    overdue_days: int
    task_age_days: Optional[int]
    days_since_update: Optional[int]
    priority: str
    latest_note: str
    source_row_number: int


@dataclass(frozen=True)
class OverdueOffender:
    department: str
    owner: str
    overdue_count: int
    oldest_overdue_days: int
    median_overdue_days: float
    average_overdue_days: float
    high_priority_count: int
    accounts_affected: int


def parse_overdue_activity_detail(
    source: Path | str,
    *,
    as_of: date,
    employee_departments: Optional[Mapping[str, str]] = None,
) -> list[OverdueTask]:
    """Parse, validate, and deduplicate open overdue Task IDs.

    Reports 5.0 Activity Detail can repeat a task once per discussion row.  The
    newest evidence row is retained so documentation volume never inflates the
    accountability count.
    """

    roster = {_key(name): dept for name, dept in (employee_departments or {}).items()}
    candidates: dict[str, tuple[datetime, int, Mapping[str, Any]]] = {}
    for row_number, row in enumerate(_rows(source), start=2):
        status = _value(row, "Task Status", "Status")
        if status.casefold() not in {"open", "overdue", "past due"}:
            continue
        due_at = parse_datetime(_value(row, "Task Due Date", "Due Date", "Date Due"))
        if due_at is None or due_at.date() >= as_of:
            continue
        task_id = _value(row, "Task ID")
        if not task_id:
            task_id = "fallback:" + "|".join((
                _value(row, "Applicant ID"),
                _value(row, "Task Assigned To", "Assigned To"),
                due_at.date().isoformat(),
                _value(row, "Task Created Date", "Created Date"),
            ))
        evidence_at = (
            parse_datetime(_value(row, "Created Date", "Task Last Modified Date"))
            or parse_datetime(_value(row, "Task Last Modified Date"))
            or datetime.min.replace(tzinfo=timezone.utc)
        )
        prior = candidates.get(task_id)
        if prior is None or evidence_at > prior[0]:
            candidates[task_id] = (evidence_at, row_number, row)

    tasks: list[OverdueTask] = []
    for task_id, (_, row_number, row) in candidates.items():
        owner = _value(row, "Task Assigned To", "Assigned To", "CSR", "Producer") or "Unassigned"
        report_department = _value(row, "Department")
        roster_department = roster.get(_key(owner), "")
        if roster_department:
            department = roster_department
            evidence = "approved active employee roster"
        elif report_department:
            department = "UNVERIFIED"
            evidence = f"employee not matched to active roster; EZLynx workstream is {report_department}"
        else:
            department = "UNVERIFIED"
            evidence = "UNVERIFIED"
        due_at = parse_datetime(_value(row, "Task Due Date", "Due Date", "Date Due"))
        created_at = parse_datetime(_value(row, "Task Created Date", "Task Created", "Date Created"))
        modified_at = parse_datetime(_value(row, "Task Last Modified Date", "Modified Date"))
        tasks.append(OverdueTask(
            task_id=task_id,
            applicant_id=_value(row, "Applicant ID"),
            account_name=_value(row, "Account Name") or "Unknown account",
            owner=owner,
            department=department,
            department_evidence=evidence,
            workstream_department=report_department or "UNVERIFIED",
            activity_type=_value(row, "Activity Type") or "Unknown",
            task_status=_value(row, "Task Status", "Status") or "Unknown",
            task_created_date=created_at.date().isoformat() if created_at else None,
            due_date=due_at.date().isoformat() if due_at else None,
            last_modified_date=modified_at.date().isoformat() if modified_at else None,
            overdue_days=(as_of - due_at.date()).days if due_at else 0,
            task_age_days=(as_of - created_at.date()).days if created_at else None,
            days_since_update=(as_of - modified_at.date()).days if modified_at else None,
            priority=_value(row, "Task Priority", "Priority") or "Normal/UNVERIFIED",
            latest_note=_value(row, "Note", "Comment"),
            source_row_number=row_number,
        ))
    return sorted(tasks, key=lambda item: (-item.overdue_days, item.department.casefold(), item.owner.casefold()))


def rank_overdue_offenders(tasks: Iterable[OverdueTask]) -> list[OverdueOffender]:
    grouped: dict[tuple[str, str], list[OverdueTask]] = defaultdict(list)
    for task in tasks:
        grouped[(task.department, task.owner)].append(task)
    offenders: list[OverdueOffender] = []
    for (department, owner), group in grouped.items():
        ages = [task.overdue_days for task in group]
        offenders.append(OverdueOffender(
            department=department,
            owner=owner,
            overdue_count=len(group),
            oldest_overdue_days=max(ages),
            median_overdue_days=float(median(ages)),
            average_overdue_days=round(sum(ages) / len(ages), 1),
            high_priority_count=sum(1 for task in group if task.priority.casefold() in {"high", "urgent", "critical"}),
            accounts_affected=len({task.applicant_id or task.account_name.casefold() for task in group}),
        ))
    return sorted(offenders, key=lambda item: (item.department.casefold(), -item.overdue_count, -item.oldest_overdue_days, item.owner.casefold()))


def department_totals(tasks: Iterable[OverdueTask]) -> list[dict[str, Any]]:
    grouped: dict[str, list[OverdueTask]] = defaultdict(list)
    for task in tasks:
        grouped[task.department].append(task)
    return [
        {
            "department": department,
            "overdue_tasks": len(group),
            "employees_with_overdue_tasks": len({item.owner for item in group}),
            "oldest_overdue_days": max(item.overdue_days for item in group),
            "accounts_affected": len({item.applicant_id or item.account_name.casefold() for item in group}),
        }
        for department, group in sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0].casefold()))
    ]


def overdue_dicts(items: Iterable[OverdueTask | OverdueOffender]) -> list[dict[str, Any]]:
    return [asdict(item) for item in items]
