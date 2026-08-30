"""Normalize department tracker exports into evidence-backed exceptions.

Each CSV remains the source of truth.  The rules below surface aging, ownership,
and missing follow-up without treating an incomplete row as proof of employee
misconduct.  Every finding includes its original source row number.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from .center_audits import parse_date, vague_note_reasons


_CLOSED = {"closed", "complete", "completed", "done", "paid", "released", "renewed", "won", "cancelled", "canceled"}


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _value(row: Mapping[str, Any], aliases: Iterable[str]) -> str:
    normalized = {_key(str(k)): v for k, v in row.items() if k is not None}
    for alias in aliases:
        value = normalized.get(_key(alias))
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _rows(source: Path | str) -> list[dict[str, str]]:
    if isinstance(source, Path) or ("\n" not in str(source) and Path(str(source)).exists()):
        content = Path(source).read_text(encoding="utf-8-sig", errors="replace")
    else:
        content = str(source)
    return [dict(row) for row in csv.DictReader(io.StringIO(content))]


@dataclass(frozen=True)
class TrackerDefinition:
    key: str
    display_name: str
    department: str
    cadence: str
    owner_columns: tuple[str, ...]
    status_columns: tuple[str, ...]
    opened_columns: tuple[str, ...]
    due_columns: tuple[str, ...]
    note_columns: tuple[str, ...]
    account_columns: tuple[str, ...]
    aging_days: int
    question: str
    missing_required_columns: tuple[tuple[str, ...], ...] = ()


TRACKER_DEFINITIONS: dict[str, TrackerDefinition] = {
    "bor": TrackerDefinition("bor", "BOR Tracker", "Commercial / Trucking", "weekly", ("Owner", "Assigned To", "Agent"), ("Status",), ("Date Submitted", "Submitted Date"), ("Effective Date", "Due Date"), ("Additional notes", "Additinal notes", "Notes"), ("Ezlynx URL/Client Name", "Client Name", "Account Name"), 14, "Which BORs are still pending or blocked?"),
    "pending_payouts": TrackerDefinition("pending_payouts", "Pending Payouts", "Accounting", "weekly", ("Owner", "Assigned To"), ("Status", "Paid"), ("Created Date", "Received Date"), ("Payable Due Date", "Due Date"), ("Notes", "Issue", "Missing Data"), ("Applicant", "Account Name", "Insured"), 7, "Which payments are blocked by missing policy numbers, invoices, or data?", (("Policy No.", "Policy Number"), ("Invoice No.", "Invoice Number"))),
    "policy_changes": TrackerDefinition("policy_changes", "Policy Change Request Tracker", "Service", "daily", ("Owner", "Assigned To", "Account Manager", "Agent"), ("Status",), ("Request Date", "Created Date", "Date Submitted"), ("Due Date", "Effective Date"), ("Notes", "Remarks", "Description"), ("Account Name", "Insured", "Client Name"), 2, "Which policy changes need immediate turnaround?"),
    "referrals": TrackerDefinition("referrals", "Referrals Report - Last Week", "Sales", "weekly", ("Producer", "Assigned Agent"), ("Opportunity Status", "Opportunity Status Category"), ("Opportunity Created Date",), ("X - Date",), ("Notes",), ("Account Name",), 7, "Which referrals need follow-up, and who generated wins?"),
    "missed_calls": TrackerDefinition("missed_calls", "Missed Calls Report 2026", "All departments", "daily", ("Employee", "Rep", "Assigned To", "Queue"), ("Callback Status", "Status"), ("Missed Date", "Call Date", "Date"), ("Callback Due", "Due Date"), ("Notes", "Callback Note"), ("Caller", "Phone Number", "Client"), 1, "Were clients called back within SLA?"),
    "coi_endorsements": TrackerDefinition("coi_endorsements", "2026 Pending COIs/Endorsements", "Service", "daily", ("Owner", "Assigned To", "Account Manager"), ("Status",), ("Request Date", "Created Date"), ("Due Date", "Needed By"), ("Notes", "Remarks"), ("Account Name", "Insured", "Client"), 1, "Which certificates or endorsements are late or blocked?"),
    "expirations": TrackerDefinition("expirations", "Expiration Report", "Retention", "weekly", ("Agent", "Owner", "Account Manager"), ("Status", "Renewal Status"), ("Created Date",), ("Renewal Date", "Expiration Date"), ("Remarks", "Notes"), ("File Name/URL", "Account Name", "Insured"), 14, "Which upcoming renewals lack a documented plan?"),
    "voicemail_email": TrackerDefinition("voicemail_email", "StreetSmart Voicemail/Email Tracker", "All departments", "weekly", ("Names Members", "Names (39) Members", "Employee", "Name"), ("Status",), ("Report Date",), ("Due Date",), ("Notes",), ("Email", "Employee", "Name"), 7, "Who has an unresolved weekly voicemail/email workload?"),
    "case_studies": TrackerDefinition("case_studies", "Case Studies", "All departments", "monthly", ("Owner", "Producer", "Account Manager"), ("Status",), ("Date", "Created Date"), ("Due Date",), ("Summary", "Notes", "Case Study"), ("Account Name", "Client"), 30, "Which verified wins or saved renewals can be shared with the team?"),
    "policy_transactions": TrackerDefinition("policy_transactions", "Policy Transaction - Audit & Cancellation", "Accounting / Service", "weekly", ("Owner", "Assigned To", "Agent"), ("Status",), ("Transaction Date", "Request Date", "Created Date"), ("Due Date", "Effective Date"), ("Notes", "Remarks", "Audit Notes"), ("Account Name", "Applicant", "Insured"), 7, "Which audits or cancellations remain incomplete or need escalation?"),
}


@dataclass(frozen=True)
class TrackerException:
    tracker_key: str
    tracker_name: str
    department: str
    account: str
    owner: str
    status: str
    age_days: Optional[int]
    due_date: Optional[str]
    severity: str
    reasons: tuple[str, ...]
    source_row_number: int


def audit_tracker_csv(
    source: Path | str,
    definition: TrackerDefinition,
    *,
    as_of: date,
) -> list[TrackerException]:
    findings: list[TrackerException] = []
    for row_number, row in enumerate(_rows(source), start=2):
        if not any(str(v or "").strip() for v in row.values()):
            continue
        if definition.key == "voicemail_email":
            dated_columns = [
                (parsed, column)
                for column in row
                if (parsed := parse_date(str(column))) is not None
            ]
            if not dated_columns:
                continue
            report_date, column = max(dated_columns)
            raw_count = str(row.get(column, "") or "").strip()
            if raw_count.isdigit() and int(raw_count) == 0:
                continue
            reasons = (
                (f"latest weekly unresolved count is {int(raw_count)}",)
                if raw_count.isdigit()
                else (f"weekly tracker value is missing or non-numeric for {report_date.isoformat()}",)
            )
            findings.append(TrackerException(
                tracker_key=definition.key,
                tracker_name=definition.display_name,
                department=definition.department,
                account=_value(row, definition.account_columns) or "Unknown employee/email",
                owner=_value(row, definition.owner_columns) or "Unassigned",
                status="Weekly count",
                age_days=max(0, (as_of - report_date).days),
                due_date=None,
                severity="medium" if raw_count.isdigit() else "unknown",
                reasons=reasons,
                source_row_number=row_number,
            ))
            continue
        status = _value(row, definition.status_columns) or "Unknown"
        if status.lower().strip() in _CLOSED:
            continue
        opened = parse_date(_value(row, definition.opened_columns))
        due = parse_date(_value(row, definition.due_columns))
        age_days = max(0, (as_of - opened).days) if opened else None
        notes = _value(row, definition.note_columns)
        reasons: list[str] = []
        if due and due < as_of:
            reasons.append(f"due date passed by {(as_of - due).days} days")
        if age_days is not None and age_days > definition.aging_days:
            reasons.append(f"open for {age_days} days (threshold {definition.aging_days})")
        if status == "Unknown":
            reasons.append("status missing")
        for alias_group in definition.missing_required_columns:
            if not _value(row, alias_group):
                reasons.append(f"missing required field: {alias_group[0]}")
        note_reasons = vague_note_reasons(notes)
        if note_reasons and (reasons or age_days is None):
            reasons.append("notes lack a documented outcome or next step")
        if not reasons:
            continue
        severity = "high" if due and due < as_of else "medium"
        if age_days is None and not due:
            severity = "unknown"
            reasons.append("opened/due date unavailable; aging not verified")
        findings.append(TrackerException(
            tracker_key=definition.key,
            tracker_name=definition.display_name,
            department=definition.department,
            account=_value(row, definition.account_columns) or "Unknown account/item",
            owner=_value(row, definition.owner_columns) or "Unassigned",
            status=status,
            age_days=age_days,
            due_date=due.isoformat() if due else None,
            severity=severity,
            reasons=tuple(dict.fromkeys(reasons)),
            source_row_number=row_number,
        ))
    return findings


def findings_as_dicts(findings: Iterable[TrackerException]) -> list[dict[str, Any]]:
    return [asdict(item) for item in findings]
