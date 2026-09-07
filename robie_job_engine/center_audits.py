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
from dataclasses import asdict, dataclass, replace
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
_WEEKLY_CLOSED_SUBMISSION_STATUSES = {"closed - not sold", "closed - bound"}

_CLOSED_SALES_STATUSES = {
    "bound",
    "closed",
    "dead",
    "lost",
    "won",
    "sold",
    "finalized",
    "declined",
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
    title: str = ""
    submission_url: str = ""
    quote_due_date: Optional[date] = None
    effective_date: Optional[date] = None
    overdue_marker: Optional[bool] = None


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
    title: str = ""
    submission_url: str = ""
    quote_due_date: Optional[str] = None
    effective_date: Optional[str] = None
    overdue_days: Optional[int] = None


@dataclass(frozen=True)
class SalesRecord:
    record_id: str
    opportunity_id: str
    applicant_id: str
    account_name: str
    producer: str
    lead_source: str
    department: str
    stage: str
    created_at: Optional[datetime]
    last_activity_at: Optional[datetime]
    last_note: str
    last_touch_evidence: str
    source_row_number: int


@dataclass(frozen=True)
class SalesException:
    record_id: str
    opportunity_id: str
    applicant_id: str
    account_name: str
    producer: str
    lead_source: str
    department: str
    stage: str
    opportunity_created_date: Optional[str]
    last_touch_date: Optional[str]
    last_touch_evidence: str
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
            if age_days is not None and age_days <= untouched_days:
                continue
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
        owner = _row_value(row, "Assigned Producer", "Owner", "Assigned To", "Producer", "Account Manager", "CSR") or "Unassigned"
        carrier = _row_value(row, "Carrier", "Market", "Company")
        created = parse_datetime(_row_value(row, "Created Date", "Created At", "Submission Date", "Date Created"))
        last_activity = parse_datetime(
            _row_value(row, "Last Activity", "Last Activity Date", "Last Touch", "Modified Date", "Updated At")
        )
        note = _row_value(row, "Last Note", "Latest Note", "Notes", "Activity Note", "Description")
        status = _row_value(row, "Status", "Submission Status", "Stage") or "Unknown"
        title = _row_value(row, "Submission Title", "Title", "Submission")
        submission_url = _row_value(row, "Submission URL", "Submission Link", "URL", "Link")
        quote_due_date = parse_date(_row_value(row, "Quote Due Date", "Quote Due", "Due Date"))
        effective_date = parse_date(_row_value(row, "Effective Date", "Policy Effective Date"))
        marker_text = _row_value(row, "Overdue", "Overdue Marker", "Quote Due Status", "Due Status")
        marker_normalized = marker_text.casefold().strip()
        if marker_normalized in {"yes", "true", "1", "red", "overdue", "past due"}:
            overdue_marker: Optional[bool] = True
        elif marker_normalized in {"no", "false", "0", "current", "not overdue"}:
            overdue_marker = False
        else:
            overdue_marker = None
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
                title=title or account_name or submission_id or f"Submission row {row_number}",
                submission_url=submission_url,
                quote_due_date=quote_due_date,
                effective_date=effective_date,
                overdue_marker=overdue_marker,
            )
        )
    return records


def audit_overdue_submission_records(
    records: Iterable[SubmissionRecord], *, as_of: datetime
) -> list[SubmissionException]:
    """Apply the approved weekly red-marker and day-31 rule exactly."""
    findings: list[SubmissionException] = []
    seen_urls: set[str] = set()
    run_date = as_of.date()
    for record in records:
        if record.status.strip().casefold() in _WEEKLY_CLOSED_SUBMISSION_STATUSES:
            continue
        missing: list[str] = []
        if record.overdue_marker is None:
            missing.append("red/overdue marker")
        if record.quote_due_date is None:
            missing.append("Quote Due Date")
        if not record.submission_url:
            missing.append("full submission URL")
        if not record.owner or record.owner == "Unassigned":
            missing.append("Assigned Producer")
        if missing:
            raise ValueError(
                f"submission row {record.source_row_number} lacks required live evidence: {', '.join(missing)}"
            )
        if record.submission_url in seen_urls:
            continue
        seen_urls.add(record.submission_url)
        overdue_days = (run_date - record.quote_due_date).days
        if record.overdue_marker is not True or overdue_days <= 30:
            continue
        if record.effective_date is None:
            raise ValueError(f"qualifying submission row {record.source_row_number} lacks Effective Date")
        findings.append(SubmissionException(
            record_id=record.record_id,
            submission_id=record.submission_id,
            account_name=record.account_name,
            owner=record.owner,
            carrier=record.carrier,
            status=record.status,
            age_days=overdue_days,
            days_since_touch=None,
            severity="high" if overdue_days > 60 else "medium",
            reasons=(f"Quote Due Date is red and {overdue_days} days overdue",),
            note_quality_reasons=(),
            source_row_number=record.source_row_number,
            title=record.title,
            submission_url=record.submission_url,
            quote_due_date=record.quote_due_date.isoformat(),
            effective_date=record.effective_date.isoformat(),
            overdue_days=overdue_days,
        ))
    return sorted(findings, key=lambda item: (item.owner.casefold(), -(item.overdue_days or 0), item.title.casefold()))


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


def parse_sales_csv(source: Path | str) -> list[SalesRecord]:
    """Parse an EZLynx Sales Center export without assuming one fixed header set."""

    records: list[SalesRecord] = []
    for row_number, row in enumerate(_read_csv_rows(source), start=2):
        opportunity_id = _row_value(row, "Opportunity ID", "Sales ID", "Quote ID", "ID")
        applicant_id = _row_value(row, "Applicant ID", "Account ID", "Customer ID")
        account_name = _row_value(row, "Account Name", "Applicant Name", "Customer Name", "Insured")
        # Sales Center assignment is intentionally authoritative.  Do not silently
        # substitute the producer stored on the customer account.
        producer = _row_value(row, "Assigned Producer", "Sales Center Assigned Producer") or "Unassigned"
        if producer == "Unassigned" and not _row_value(row, "Assigned Producer", "Sales Center Assigned Producer"):
            # Compatibility for older exports that genuinely label this field
            # Producer, while keeping the source limitation explicit in evidence.
            producer = _row_value(row, "Producer", "Owner", "Assigned To", "Sales Rep", "Agent") or "Unassigned"
        lead_source = _row_value(row, "Lead Source", "Lead Channel", "Source") or "UNVERIFIED"
        department = _row_value(row, "Department") or "UNVERIFIED"
        stage = _row_value(row, "Stage", "Pipeline Stage", "Opportunity Status", "Status") or "Unknown"
        created_at = parse_datetime(_row_value(row, "Created Date", "Created At", "Opportunity Created Date", "Date Created"))
        last_activity_at = parse_datetime(
            _row_value(row, "Last Activity", "Last Activity Date", "Last Touch", "Last Contact", "Modified Date", "Updated At")
        )
        last_note = _row_value(row, "Last Note", "Latest Note", "Notes", "Activity Note", "Description")
        record_id = _row_value(row, "Record ID") or opportunity_id or f"sales-row-{row_number}"
        records.append(
            SalesRecord(
                record_id=record_id,
                opportunity_id=opportunity_id,
                applicant_id=applicant_id,
                account_name=account_name or "Unknown account",
                producer=producer,
                lead_source=lead_source,
                department=department,
                stage=stage,
                created_at=created_at,
                last_activity_at=last_activity_at,
                last_note=last_note,
                last_touch_evidence=("sales export" if last_activity_at else "UNVERIFIED"),
                source_row_number=row_number,
            )
        )
    return records


def enrich_sales_last_touches(
    records: Iterable[SalesRecord],
    activity_source: Path | str,
    *,
    window_days: int = 7,
) -> list[SalesRecord]:
    """Join Sales Center rows to the newest Activity Detail event by Applicant ID.

    A missing match proves only that no activity was present in the supplied
    validation window.  It does not invent an exact last-touch date.
    """

    newest: dict[str, tuple[datetime, str]] = {}
    for row in _read_csv_rows(activity_source):
        applicant_id = _row_value(row, "Applicant ID", "Account ID", "Customer ID")
        created = parse_datetime(_row_value(row, "Created Date", "Activity Created Date", "Note Created Date"))
        if not applicant_id or created is None:
            continue
        note = _row_value(row, "Note", "Comment", "Description")
        prior = newest.get(applicant_id)
        if prior is None or created > prior[0]:
            newest[applicant_id] = (created, note)

    enriched: list[SalesRecord] = []
    for record in records:
        match = newest.get(record.applicant_id)
        if match:
            enriched.append(replace(
                record,
                last_activity_at=match[0],
                last_note=match[1] or record.last_note,
                last_touch_evidence=f"Activity Detail exact match by Applicant ID ({window_days}-day window)",
            ))
        elif record.last_activity_at:
            enriched.append(record)
        else:
            enriched.append(replace(
                record,
                last_touch_evidence=f"no Activity Detail match in {window_days}-day validation window; exact last touch UNVERIFIED",
            ))
    return enriched


def audit_sales_records(
    records: Iterable[SalesRecord],
    *,
    as_of: datetime,
    untouched_days: int = 5,
) -> list[SalesException]:
    """Surface open producer opportunities with no documented recent activity."""

    findings: list[SalesException] = []
    for record in records:
        if record.stage.strip().lower() in _CLOSED_SALES_STATUSES:
            continue
        age_days = (
            max(0, int((as_of - record.created_at.astimezone(as_of.tzinfo or timezone.utc)).total_seconds() // 86400))
            if record.created_at
            else None
        )
        days_since_touch = (
            max(0, int((as_of - record.last_activity_at.astimezone(as_of.tzinfo or timezone.utc)).total_seconds() // 86400))
            if record.last_activity_at
            else None
        )
        reasons: list[str] = []
        if record.last_activity_at is None:
            reasons.append("no last-touch timestamp in export")
        elif days_since_touch is not None and days_since_touch > untouched_days:
            reasons.append(f"no recorded touch for {days_since_touch} days (threshold {untouched_days})")
        if record.producer == "Unassigned":
            reasons.append("producer is unassigned")
        if record.stage == "Unknown":
            reasons.append("pipeline stage is missing")
        if not reasons:
            continue
        note_reasons = vague_note_reasons(record.last_note)
        if note_reasons:
            reasons.append("latest note may not document an outcome or next step sufficiently")
        severity = "high" if days_since_touch is not None and days_since_touch > untouched_days * 2 else "medium"
        if days_since_touch is None:
            severity = "unknown"
        findings.append(
            SalesException(
                record_id=record.record_id,
                opportunity_id=record.opportunity_id,
                applicant_id=record.applicant_id,
                account_name=record.account_name,
                producer=record.producer,
                lead_source=record.lead_source,
                department=record.department,
                stage=record.stage,
                opportunity_created_date=record.created_at.date().isoformat() if record.created_at else None,
                last_touch_date=record.last_activity_at.date().isoformat() if record.last_activity_at else None,
                last_touch_evidence=record.last_touch_evidence,
                age_days=age_days,
                days_since_touch=days_since_touch,
                severity=severity,
                reasons=tuple(reasons),
                note_quality_reasons=tuple(note_reasons),
                source_row_number=record.source_row_number,
            )
        )
    return sorted(
        findings,
        key=lambda item: (item.days_since_touch is None, -(item.days_since_touch or -1), item.account_name.lower()),
    )


def finding_dicts(findings: Iterable[RetentionException | SubmissionException | SalesException]) -> list[dict[str, Any]]:
    return [asdict(item) for item in findings]
