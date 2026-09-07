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
    "policy_changes": TrackerDefinition("policy_changes", "Policy Change Request Tracker", "Service", "daily", ("Owner", "Assigned To", "Account Manager", "CSR", "Producer", "Agent"), ("Status",), ("Request Date", "Created Date", "Date Submitted"), ("Due Date", "Effective Date"), ("Notes", "Remarks", "Description"), ("Account Name", "Insured", "Client Name"), 7, "Which policy changes have exceeded seven days, who is blocking them, and who owns the next action?"),
    "referrals": TrackerDefinition("referrals", "Referrals Report - Last Week", "Sales", "weekly", ("Producer", "Assigned Agent"), ("Opportunity Status", "Opportunity Status Category"), ("Opportunity Created Date",), ("X - Date",), ("Notes",), ("Account Name",), 7, "Which referrals need follow-up, and who generated wins?"),
    "missed_calls": TrackerDefinition("missed_calls", "Missed Calls Report 2026", "All departments", "daily", ("Employee", "Rep", "Assigned To", "Queue"), ("Callback Status", "Status"), ("Missed Date", "Call Date", "Date"), ("Callback Due", "Due Date"), ("Notes", "Callback Note"), ("Caller", "Phone Number", "Client"), 1, "Were clients called back within SLA?"),
    "coi_endorsements": TrackerDefinition(
        "coi_endorsements",
        "2026 Pending COIs/Endorsements",
        "Service",
        "daily",
        ("Owner", "Assigned To", "Account Manager", "CSR", "Producer", "Agent assigned to the task."),
        ("Status",),
        ("Request Date", "Created Date", "Date COI was Requested", "Date"),
        ("Due Date", "Needed By"),
        ("Notes", "Remarks", "Requirements/Endo"),
        ("Account Name", "Insured", "Client", "Profile"),
        1,
        "Which certificates or endorsements are late or blocked, and what does EZLynx show?",
    ),
    "expirations": TrackerDefinition("expirations", "Expiration Report", "Retention", "weekly", ("Agent", "Owner", "Account Manager", "CSR", "Producer"), ("Status", "Renewal Status"), ("Created Date",), ("Renewal Date", "Expiration Date"), ("Remarks", "Notes", "Last Outreach"), ("File Name/URL", "Account Name", "Insured"), 14, "Which upcoming renewals lack documented producer/CSR outreach and a next step in EZLynx?"),
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
    opened_date: Optional[str]
    age_days: Optional[int]
    due_date: Optional[str]
    severity: str
    reasons: tuple[str, ...]
    source_row_number: int
    blocker_party: str = "unclear"
    next_action: str = "Review the EZLynx account and document the next step"
    notification_target: str = "Unassigned"
    ezlynx_reference: str = "UNVERIFIED"


def _blocker_party(status: str, notes: str) -> str:
    text = f"{status} {notes}".casefold()
    if re.search(r"\b(carrier|underwriter|uw|company|market)\b", text):
        return "carrier"
    if re.search(r"\b(client|customer|insured|applicant|signature|documents?)\b", text):
        return "client"
    if re.search(r"\b(internal|csr|producer|agent|our team|processing|not submitted|not sent)\b", text):
        return "StreetSmart"
    return "unclear"


def _next_action(definition: TrackerDefinition, blocker: str) -> str:
    if definition.key == "policy_changes":
        return (
            "Escalate to the carrier and record the carrier response/next follow-up in EZLynx"
            if blocker == "carrier"
            else "Producer/CSR must review the change, contact the required party, and update EZLynx"
        )
    if definition.key == "expirations":
        return "Producer/CSR must document renewal outreach, current disposition, and next contact date in EZLynx"
    if definition.key == "coi_endorsements":
        return "CSR/producer must document the account blocker, delivery status, and next action in EZLynx"
    return "Review the EZLynx account and document the next step"


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
                opened_date=report_date.isoformat(),
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
        blocker = _blocker_party(status, notes)
        owner = _value(row, definition.owner_columns) or "Unassigned"
        ezlynx_reference = _value(
            row,
            ("EZLynx Account ID", "EZLynx URL", "Applicant ID", "Account ID", "File Name/URL", "Link"),
        ) or "UNVERIFIED"
        reasons: list[str] = []
        if due and due < as_of:
            reasons.append(f"due date passed by {(as_of - due).days} days")
        if age_days is not None and age_days > definition.aging_days:
            reasons.append(f"open for {age_days} days (threshold {definition.aging_days})")
        if status == "Unknown":
            reasons.append("status missing")
        if definition.key in {"policy_changes", "expirations", "coi_endorsements"} and ezlynx_reference == "UNVERIFIED":
            reasons.append("EZLynx account link/ID missing; source-of-truth match is UNVERIFIED")
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
            owner=owner,
            status=status,
            opened_date=opened.isoformat() if opened else None,
            age_days=age_days,
            due_date=due.isoformat() if due else None,
            severity=severity,
            reasons=tuple(dict.fromkeys(reasons)),
            source_row_number=row_number,
            blocker_party=blocker,
            next_action=_next_action(definition, blocker),
            notification_target=owner,
            ezlynx_reference=ezlynx_reference,
        ))
    return findings


def voicemail_email_attestations(source: Path | str) -> list[dict[str, Any]]:
    """Return every employee's latest tracker entry, including reported zeroes."""

    result: list[dict[str, Any]] = []
    definition = TRACKER_DEFINITIONS["voicemail_email"]
    for row_number, row in enumerate(_rows(source), start=2):
        dated = [(parsed, column) for column in row if (parsed := parse_date(str(column))) is not None]
        if not dated:
            continue
        report_date, column = max(dated)
        raw = str(row.get(column, "") or "").strip()
        result.append(
            {
                "employee": _value(row, definition.owner_columns) or "Unassigned",
                "email": _value(row, ("Email",)).casefold(),
                "reported_unresolved": int(raw) if raw.isdigit() else None,
                "report_date": report_date.isoformat(),
                "source_row_number": row_number,
            }
        )
    return result


def findings_as_dicts(findings: Iterable[TrackerException]) -> list[dict[str, Any]]:
    return [asdict(item) for item in findings]
