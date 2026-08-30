"""Evidence-backed EZLynx Retention and Submission Center audits.

The browser/export layer is intentionally separate from this module.  This
module consumes a CSV export, preserves the source row identity, and produces
deterministic exceptions without claiming that a browser action or customer
outcome occurred.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional


_CLOSED_SUBMISSION_STATUSES = {
    "bound",
    "closed",
    "declined",
    "lost",
    "withdrawn",
    "cancelled",
    "canceled",
    "completed",
}

_GENERIC_NOTE_PATTERNS = (
    r"^called(?: the)? (?:client|customer|insured)$",
    r"^left (?:a )?(?:message|voicemail|vm)$",
    r"^emailed(?: the)? (?:client|customer|insured|carrier|underwriter)$",
    r"^sent (?:an )?email$",
    r"^follow(?:ed)? up$",
    r"^working on it$",
    r"^spoke (?:to|with) (?:the )?(?:client|customer|insured)$",
    r"^reviewed$",
    r"^pending$",
)


def _normalise_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _row_value(row: Mapping[str, Any], *aliases: str) -> str:
    normalised = {_normalise_key(str(k)): v for k, v in row.items() if k is not None}
    for alias in aliases:
        value = normalised.get(_normalise_key(alias))
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _read_csv_rows(source: Path | str) -> list[dict[str, str]]:
    if isinstance(source, Path) or ("\n" not in str(source) and Path(str(source)).exists()):
        content = Path(source).read_text(encoding="utf-8-sig", errors="replace")
    else:
        content = str(source)
    return [dict(row) for row in csv.DictReader(io.StringIO(content))]


def parse_date(value: str) -> Optional[date]:
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(value[:10], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def parse_datetime(value: str) -> Optional[datetime]:
    value = (value or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in (
        "%m/%d/%Y %I:%M %p",
        "%m/%d/%Y %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%m/%d/%Y",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def vague_note_reasons(note: str) -> list[str]:
    """Return explainable reasons a note lacks actionable service evidence.

    This is a coaching signal only.  It does not prove the work did not occur.
    """

    clean = " ".join((note or "").split())
    if not clean:
        return ["no note present"]

    reasons: list[str] = []
    lower = clean.lower().strip(" .:-")
    if len(clean) < 20:
        reasons.append("note is shorter than 20 characters")
    if any(re.fullmatch(pattern, lower) for pattern in _GENERIC_NOTE_PATTERNS):
        reasons.append("note contains only a generic activity statement")

    has_next_step = bool(
        re.search(
            r"\b(next|retry|follow up (?:on|by)|due|waiting (?:for|on)|will |by \d|on \d|tomorrow|monday|tuesday|wednesday|thursday|friday)\b",
            lower,
        )
    )
    has_outcome = bool(
        re.search(
            r"\b(resolved|completed|confirmed|approved|declined|received|quoted|submitted|bound|customer selected|carrier advised)\b",
            lower,
        )
    )
    if not has_next_step and not has_outcome:
        reasons.append("note has no documented outcome or next step")
    return reasons


@dataclass(frozen=True)
class RetentionRecord:
    record_id: str
    account_id: str
    account_name: str
    owner: str
    expiration_date: Optional[date]
    last_activity_at: Optional[datetime]
    last_note: str
    status: str
    source_row_number: int


@dataclass(frozen=True)
class RetentionException:
    record_id: str
    account_id: str
    account_name: str
    owner: str
    expiration_date: Optional[str]
    days_to_expiration: Optional[int]
    days_since_touch: Optional[int]
    severity: str
    reasons: tuple[str, ...]
    note_quality_reasons: tuple[str, ...]
    source_row_number: int


@dataclass(frozen=True)
class SubmissionRecord:
    record_id: str
    submission_id: str
    account_name: str
    owner: str
    carrier: str
    created_at: Optional[datetime]
    last_activity_at: Optional[datetime]
    last_note: str
    status: str
    source_row_number: int


@dataclass(frozen=True)
class SubmissionException:
    record_id: str
    submission_id: str
    account_name: str
    owner: str
    carrier: str
    status: str
    age_days: Optional[int]
    days_since_touch: Optional[int]
    severity: str
    reasons: tuple[str, ...]
    note_quality_reasons: tuple[str, ...]
    source_row_number: int


def parse_retention_csv(source: Path | str) -> list[RetentionRecord]:
    records: list[RetentionRecord] = []
    for row_number, row in enumerate(_read_csv_rows(source), start=2):
        account_id = _row_value(row, "Account ID", "Applicant ID", "Customer ID", "ID")
        account_name = _row_value(row, "Account Name", "Customer Name", "Applicant Name", "Insured")
        owner = _row_value(row, "Owner", "Assigned To", "CSR", "Account Manager", "Agent") or "Unassigned"
        expiration = parse_date(_row_value(row, "Expiration Date", "Policy Expiration", "Renewal Date", "Expires"))
        last_activity = parse_datetime(
            _row_value(row, "Last Activity", "Last Activity Date", "Last Touch", "Last Contact", "Modified Date")
        )
        note = _row_value(row, "Last Note", "Latest Note", "Notes", "Activity Note", "Description")
        status = _row_value(row, "Status", "Renewal Status", "Policy Status") or "Unknown"
        record_id = _row_value(row, "Retention ID", "Record ID") or account_id or f"retention-row-{row_number}"
        records.append(
            RetentionRecord(
                record_id=record_id,
                account_id=account_id,
                account_name=account_name or "Unknown account",
                owner=owner,
                expiration_date=expiration,
                last_activity_at=last_activity,
                last_note=note,
                status=status,
                source_row_number=row_number,
            )
        )
    return records


def audit_retention_records(
    records: Iterable[RetentionRecord],
    *,
    as_of: datetime,
    untouched_days: int = 14,
    renewal_horizon_days: int = 90,
) -> list[RetentionException]:
    findings: list[RetentionException] = []
    today = as_of.date()
    for record in records:
        days_to_expiration = (record.expiration_date - today).days if record.expiration_date else None
        if days_to_expiration is not None and not (0 <= days_to_expiration <= renewal_horizon_days):
            continue
        days_since_touch = (
            max(0, int((as_of - record.last_activity_at.astimezone(as_of.tzinfo or timezone.utc)).total_seconds() // 86400))
            if record.last_activity_at
            else None
        )
        reasons: list[str] = []
        if record.last_activity_at is None:
            reasons.append("no last-touch timestamp in export")
        elif days_since_touch is not None and days_since_touch > untouched_days:
            reasons.append(f"no recorded touch for {days_since_touch} days")
        note_reasons = vague_note_reasons(record.last_note)
        if note_reasons:
            reasons.append("latest note may not document service sufficiently")
        if not reasons:
            continue

        severity = "high" if days_to_expiration is not None and days_to_expiration <= 30 else "medium"
        if days_to_expiration is None:
            severity = "unknown"
            reasons.append("expiration date missing or invalid")
        findings.append(
            RetentionException(
                record_id=record.record_id,
                account_id=record.account_id,
                account_name=record.account_name,
                owner=record.owner,
                expiration_date=record.expiration_date.isoformat() if record.expiration_date else None,
                days_to_expiration=days_to_expiration,
                days_since_touch=days_since_touch,
                severity=severity,
                reasons=tuple(reasons),
                note_quality_reasons=tuple(note_reasons),
                source_row_number=record.source_row_number,
            )
        )
    return sorted(
        findings,
        key=lambda item: (
            item.days_to_expiration is None,
            item.days_to_expiration if item.days_to_expiration is not None else 999999,
            -(item.days_since_touch or 0),
        ),
    )


def parse_submission_csv(source: Path | str) -> list[SubmissionRecord]:
    records: list[SubmissionRecord] = []
    for row_number, row in enumerate(_read_csv_rows(source), start=2):
        submission_id = _row_value(row, "Submission ID", "Submission #", "ID")
        account_name = _row_value(row, "Account Name", "Customer Name", "Applicant Name", "Insured")
        owner = _row_value(row, "Owner", "Assigned To", "Producer", "Account Manager", "CSR") or "Unassigned"
        carrier = _row_value(row, "Carrier", "Market", "Company")
        created = parse_datetime(_row_value(row, "Created Date", "Created At", "Submission Date", "Date Created"))
        last_activity = parse_datetime(
            _row_value(row, "Last Activity", "Last Activity Date", "Last Touch", "Modified Date", "Updated At")
        )
        note = _row_value(row, "Last Note", "Latest Note", "Notes", "Activity Note", "Description")
        status = _row_value(row, "Status", "Submission Status", "Stage") or "Unknown"
        record_id = _row_value(row, "Record ID") or submission_id or f"submission-row-{row_number}"
        records.append(
            SubmissionRecord(
                record_id=record_id,
                submission_id=submission_id,
                account_name=account_name or "Unknown account",
                owner=owner,
                carrier=carrier,
                created_at=created,
                last_activity_at=last_activity,
                last_note=note,
                status=status,
                source_row_number=row_number,
            )
        )
    return records


def audit_submission_records(
    records: Iterable[SubmissionRecord],
    *,
    as_of: datetime,
    open_age_days: int = 30,
    untouched_days: int = 14,
) -> list[SubmissionException]:
    findings: list[SubmissionException] = []
    for record in records:
        if record.status.strip().lower() in _CLOSED_SUBMISSION_STATUSES:
            continue
        age_days = (
            max(0, int((as_of - record.created_at.astimezone(as_of.tzinfo or timezone.utc)).total_seconds() // 86400))
            if record.created_at
            else None
        )
        if age_days is not None and age_days <= open_age_days:
            continue
        days_since_touch = (
            max(0, int((as_of - record.last_activity_at.astimezone(as_of.tzinfo or timezone.utc)).total_seconds() // 86400))
            if record.last_activity_at
            else None
        )
        reasons: list[str] = []
        if age_days is None:
            reasons.append("creation date missing or invalid")
        else:
            reasons.append(f"submission open for {age_days} days")
        if record.last_activity_at is None:
            reasons.append("no last-touch timestamp in export")
        elif days_since_touch is not None and days_since_touch > untouched_days:
            reasons.append(f"no recorded touch for {days_since_touch} days")
        note_reasons = vague_note_reasons(record.last_note)
        if note_reasons:
            reasons.append("latest note may not document status or next step sufficiently")
        severity = "high" if age_days is not None and age_days > 60 else ("medium" if age_days is not None else "unknown")
        findings.append(
            SubmissionException(
                record_id=record.record_id,
                submission_id=record.submission_id,
                account_name=record.account_name,
                owner=record.owner,
                carrier=record.carrier,
                status=record.status,
                age_days=age_days,
                days_since_touch=days_since_touch,
                severity=severity,
                reasons=tuple(reasons),
                note_quality_reasons=tuple(note_reasons),
                source_row_number=record.source_row_number,
            )
        )
    return sorted(findings, key=lambda item: (-(item.age_days or -1), item.account_name.lower()))


def finding_dicts(findings: Iterable[RetentionException | SubmissionException]) -> list[dict[str, Any]]:
    return [asdict(item) for item in findings]
