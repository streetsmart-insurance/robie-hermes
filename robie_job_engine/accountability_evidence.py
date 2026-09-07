"""Cross-channel service evidence for the StreetSmart accountability reports.

The functions in this module are deliberately deterministic and read-only.
They reconcile RingCentral sessions with privacy-limited EZLynx activity
metadata, preserve source-row references, and avoid treating a queue member leg
as a separate customer failure when another member answered the same session.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from .center_audits import parse_date, parse_datetime, vague_note_reasons
from .productivity import RingCentralCall, normalize_phone


_CLOSED = {"closed", "complete", "completed", "done", "cancelled", "canceled"}
_MISSED_RESULTS = {"missed", "voicemail", "no answer", "refused", "rejected"}
_EASTERN = ZoneInfo("America/New_York")


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _value(row: Mapping[str, Any], *aliases: str) -> str:
    normalized = {_key(str(key)): value for key, value in row.items() if key is not None}
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


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def mask_phone(value: str) -> str:
    digits = normalize_phone(value)
    return f"***-***-{digits[-4:]}" if len(digits) >= 4 else "unavailable"


@dataclass(frozen=True)
class ActivityEvidence:
    evidence_id: str
    occurred_at: datetime
    employee: str
    channel: str
    direction: str
    outcome: str
    phone: str
    account_name: str
    account_reference: str
    account_type: str
    account_status: str
    assigned_producer: str
    assigned_csr: str
    communication_mode: str
    source_row_number: int


@dataclass(frozen=True)
class ServiceResolution:
    incident_id: str
    parent_call_id: str
    missed_at: str
    caller_phone_masked: str
    account_name: str
    account_reference: str
    account_type: str
    account_status: str
    business_hours_flag: bool
    assigned_employee: str
    failed_destination: str
    assigned_producer: str
    assigned_csr: str
    queue: str
    resolution: str
    final_classification: str
    resolved_at: str | None
    resolved_by: str | None
    response_minutes: float | None
    evidence_source: str
    evidence_id: str
    source_call_ids: tuple[str, ...]
    automated_text_detected: bool
    caller_had_to_redial: bool
    status: str


def parse_activity_evidence(source: Path | str) -> list[ActivityEvidence]:
    """Parse activity metadata without retaining note bodies or subjects."""

    events: list[ActivityEvidence] = []
    for row_number, row in enumerate(_rows(source), start=2):
        occurred_text = _value(
            row,
            "Activity Date", "Date Created", "Created Date", "Created At",
            "Modified Date", "Date/Time", "Timestamp",
        )
        occurred = parse_datetime(occurred_text)
        if occurred is None:
            continue
        explicit_timezone = bool(re.search(r"(?:Z|[+-]\d{2}:?\d{2})\s*$", occurred_text, re.IGNORECASE))
        occurred_at = (
            occurred
            if explicit_timezone and occurred.tzinfo
            else occurred.replace(tzinfo=None).replace(tzinfo=_EASTERN)
        )
        activity_type = _value(row, "Activity Type", "Type", "Action", "Category") or "Activity"
        direction = _value(row, "Direction", "Communication Direction").casefold()
        lowered = activity_type.casefold()
        if not direction:
            direction = "inbound" if "inbound" in lowered or "received" in lowered else "outbound"
        channel = "other"
        for candidate in ("text", "email", "call", "voicemail", "chat"):
            if candidate in lowered:
                channel = candidate
                break
        outcome = _value(row, "Outcome", "Result", "Status") or activity_type
        automation_hint = " ".join((
            activity_type,
            _value(row, "Created By", "User", "Author", "CSR", "Employee"),
            _value(row, "Automation", "Automated", "Source", "Origin"),
        )).casefold()
        communication_mode = (
            "automated"
            if any(token in automation_hint for token in ("automat", "workflow", "campaign", "system generated"))
            else "manual"
        )
        phone = normalize_phone(_value(
            row,
            "Phone", "Phone Number", "Customer Phone", "Applicant Phone",
            "From", "To", "Caller ID",
        ))
        if not phone:
            continue
        evidence_id = _value(row, "Activity ID", "Discussion ID", "Record ID", "ID") or f"activity-row-{row_number}"
        events.append(ActivityEvidence(
            evidence_id=evidence_id,
            occurred_at=occurred_at,
            employee=_value(row, "Created By", "User", "Author", "CSR", "Employee") or "Unassigned",
            channel=channel,
            direction=direction,
            outcome=outcome,
            phone=phone,
            account_name=_value(row, "Account Name", "Applicant", "Insured", "Customer Name"),
            account_reference=_value(row, "EZLynx URL", "Account ID", "Applicant ID", "Account Link"),
            account_type=_value(row, "Account Type", "Applicant Type", "Customer Type", "Client Type"),
            account_status=_value(row, "Account Status", "Applicant Status", "Customer Status", "Client Status"),
            assigned_producer=_value(row, "Assigned Producer", "Producer", "Account Producer"),
            assigned_csr=_value(row, "Assigned CSR", "CSR", "Account Manager", "Assigned User"),
            communication_mode=communication_mode,
            source_row_number=row_number,
        ))
    return sorted(events, key=lambda item: item.occurred_at)


def audit_task_details(source: Path | str, *, as_of: date) -> dict[str, Any]:
    """Return source-backed open/overdue task exceptions and postponement clues."""

    exceptions: list[dict[str, Any]] = []
    reviewed = 0
    open_total = 0
    for row_number, row in enumerate(_rows(source), start=2):
        if not any(str(value or "").strip() for value in row.values()):
            continue
        reviewed += 1
        status = _value(row, "Status", "Task Status") or "Unknown"
        if status.casefold().strip() in _CLOSED:
            continue
        open_total += 1
        due = parse_date(_value(row, "Due Date", "Date Due", "Task Due Date"))
        opened = parse_date(_value(row, "Created Date", "Date Created", "Opened Date"))
        explicit_overdue = any(token in status.casefold() for token in ("overdue", "past due"))
        overdue = explicit_overdue or bool(due and due < as_of)
        if not overdue:
            continue
        postpone_raw = _value(row, "Postpone Count", "Snooze Count", "Times Postponed", "Push Count")
        try:
            postpone_count = int(postpone_raw or 0)
        except ValueError:
            postpone_count = 0
        notes = _value(row, "Notes", "Description", "Latest Note", "Remarks")
        reasons = [
            f"due date passed by {(as_of - due).days} days" if due else "status marks task overdue but due date is unavailable"
        ]
        if postpone_count:
            reasons.append(f"due date postponed {postpone_count} time(s)")
        note_reasons = vague_note_reasons(notes)
        if note_reasons:
            reasons.append("latest task note lacks a documented outcome or next step")
        exceptions.append({
            "task_id": _value(row, "Task ID", "Record ID", "ID") or f"task-row-{row_number}",
            "title": _value(row, "Task Type", "Title", "Task", "Subject") or "Untitled task",
            "owner": _value(row, "Assigned To", "User", "CSR", "Producer") or "Unassigned",
            "account_name": _value(row, "Account Name", "Applicant", "Insured", "Customer Name") or "Unknown account",
            "account_reference": _value(row, "EZLynx URL", "Account ID", "Applicant ID", "Account Link") or "UNVERIFIED",
            "status": status,
            "priority": _value(row, "Priority") or "Unspecified",
            "opened_date": opened.isoformat() if opened else None,
            "due_date": due.isoformat() if due else None,
            "days_overdue": (as_of - due).days if due else None,
            "postpone_count": postpone_count,
            "reasons": reasons,
            "source_row_number": row_number,
        })
    return {
        "records_reviewed": reviewed,
        "open_total": open_total,
        "overdue_total": len(exceptions),
        "exceptions": sorted(
            exceptions,
            key=lambda item: (item["days_overdue"] is None, -(item["days_overdue"] or -1), item["owner"].casefold()),
        ),
    }


def _session_key(call: RingCentralCall, index: int) -> str:
    return call.call_id or f"row-{index}"


def _is_human_leg(call: RingCentralCall) -> bool:
    employee = call.employee_name.strip()
    return bool(
        employee
        and employee not in {"Unassigned", "Queue"}
        and "ai receptionist" not in employee.casefold()
        and (not call.queue_name or employee.casefold() != call.queue_name.casefold())
    )


def _business_minutes_between(start: datetime, end: datetime, holidays: set[date] | None = None) -> float:
    """Count Monday-Friday 9:00-17:00 America/New_York minutes."""

    cursor = (_aware(start) or start).astimezone(_EASTERN)
    finish = (_aware(end) or end).astimezone(_EASTERN)
    if finish <= cursor:
        return 0.0
    total = 0.0
    holidays = holidays or set()
    day = cursor.date()
    while day <= finish.date():
        if day.weekday() < 5 and day not in holidays:
            window_start = datetime.combine(day, time(9), tzinfo=_EASTERN)
            window_end = datetime.combine(day, time(17), tzinfo=_EASTERN)
            overlap_start = max(cursor, window_start)
            overlap_end = min(finish, window_end)
            if overlap_end > overlap_start:
                total += (overlap_end - overlap_start).total_seconds() / 60
        day += timedelta(days=1)
    return total


def _in_business_hours(value: datetime) -> bool:
    local = (_aware(value) or value).astimezone(_EASTERN)
    return local.weekday() < 5 and time(9) <= local.time().replace(tzinfo=None) < time(17)


def _next_business_start(value: datetime, holidays: set[date]) -> datetime:
    local = (_aware(value) or value).astimezone(_EASTERN)
    day = local.date()
    if local.weekday() < 5 and day not in holidays and local.time().replace(tzinfo=None) < time(9):
        return datetime.combine(day, time(9), tzinfo=_EASTERN)
    day += timedelta(days=1)
    while day.weekday() >= 5 or day in holidays:
        day += timedelta(days=1)
    return datetime.combine(day, time(9), tzinfo=_EASTERN)


def reconcile_service_calls(
    calls: Iterable[RingCentralCall],
    *,
    as_of: datetime,
    activity_events: Iterable[ActivityEvidence] = (),
    sla_minutes: int = 30,
    business_holidays: Iterable[date] = (),
) -> list[ServiceResolution]:
    """Reconcile missed customer sessions across phone and EZLynx metadata."""

    ordered = sorted(calls, key=lambda item: item.start_time)
    sessions: dict[str, list[RingCentralCall]] = {}
    for index, call in enumerate(ordered):
        sessions.setdefault(_session_key(call, index), []).append(call)
    later_inbound = [
        call for call in ordered
        if call.direction == "Inbound" and call.result == "Call connected" and call.from_number and _is_human_leg(call)
    ]
    activities = sorted(activity_events, key=lambda item: item.occurred_at)
    now = _aware(as_of) or as_of
    holidays = set(business_holidays)
    results: list[ServiceResolution] = []

    for session_id, legs in sessions.items():
        inbound_legs = [call for call in legs if call.direction == "Inbound"]
        if not inbound_legs or any(call.result == "Call connected" and _is_human_leg(call) for call in inbound_legs):
            continue
        missed = [call for call in inbound_legs if call.result.casefold() in _MISSED_RESULTS]
        if not missed:
            continue
        first = min(missed, key=lambda item: item.start_time)
        caller = first.from_number
        if not caller:
            continue
        missed_at = _aware(first.start_time) or first.start_time
        business_hours_flag = _in_business_hours(missed_at) and missed_at.astimezone(_EASTERN).date() not in holidays
        sla_start = missed_at if business_hours_flag else _next_business_start(missed_at, holidays)
        resolution = "UNRESOLVED"
        resolved_at: datetime | None = None
        resolved_by: str | None = None
        evidence_source = "RingCentral"
        evidence_id = session_id
        account_activity = next((item for item in activities if item.phone == caller), None)
        assigned_producer = account_activity.assigned_producer if account_activity else ""
        assigned_csr = account_activity.assigned_csr if account_activity else ""
        automated_text_detected = False
        caller_had_to_redial = False

        callback = next((
            call for call in ordered
            if call.direction == "Outbound"
            and call.to_number == caller
            and _is_human_leg(call)
            and (_aware(call.start_time) or call.start_time) >= missed_at
        ), None)
        if callback:
            resolved_at = _aware(callback.start_time) or callback.start_time
            resolved_by = callback.employee_name
            callback_kind = "CONNECTED" if callback.result == "Call connected" else "ATTEMPTED"
            resolution = (
                f"CALLBACK_{callback_kind}_BY_ASSIGNED_REP"
                if callback.employee_name.casefold() == first.employee_name.casefold()
                else f"CALLBACK_{callback_kind}_BY_TEAMMATE"
            )
            evidence_id = callback.call_id
        else:
            connected_later = next((
                call for call in later_inbound
                if call.from_number == caller and (_aware(call.start_time) or call.start_time) > missed_at
            ), None)
            if connected_later:
                resolved_at = _aware(connected_later.start_time) or connected_later.start_time
                resolved_by = connected_later.answered_by or connected_later.employee_name
                resolution = "CLIENT_REACHED_AGENCY_LATER"
                evidence_id = connected_later.call_id
                caller_had_to_redial = True
            else:
                matching_activities = [
                    item for item in activities
                    if item.phone == caller
                    and item.occurred_at >= missed_at
                    and item.direction != "inbound"
                    and item.channel in {"text", "email", "call", "chat"}
                ]
                automated_text_detected = any(
                    item.channel == "text" and item.communication_mode == "automated"
                    for item in matching_activities
                )
                activity = next((
                    item for item in matching_activities
                    if item.communication_mode == "manual"
                ), None)
                if activity:
                    resolved_at = activity.occurred_at
                    resolved_by = activity.employee
                    resolution = "EZLYNX_RESPONSE_DOCUMENTED"
                    evidence_source = "EZLynx Activity"
                    evidence_id = activity.evidence_id

        elapsed = _business_minutes_between(sla_start, now, holidays)
        status = "RESOLVED" if resolved_at else ("UNRESOLVED" if elapsed >= sla_minutes else "PENDING_SLA")
        response_minutes = round(_business_minutes_between(sla_start, resolved_at, holidays), 1) if resolved_at else None
        if status == "PENDING_SLA":
            final_classification = "PENDING_SLA"
        elif account_activity is None:
            final_classification = "UNVERIFIED_CALLER"
        elif caller_had_to_redial:
            final_classification = "NO_CALLBACK_CLIENT_REDIALED"
        elif evidence_source == "EZLynx Activity":
            final_classification = "NON_CALL_RESPONSE"
        elif resolved_at:
            final_classification = "TIMELY_CALLBACK" if (response_minutes or 0) <= sla_minutes else "LATE_CALLBACK"
        else:
            final_classification = "TRUE_NO_RESPONSE"
        failed_destination = first.queue_name or first.employee_name or "UNVERIFIED"
        accountable_employee = (
            assigned_csr
            or (first.employee_name if not first.queue_name else "Queue ownership UNVERIFIED")
        )
        results.append(ServiceResolution(
            incident_id=session_id,
            parent_call_id=session_id,
            missed_at=missed_at.isoformat(),
            caller_phone_masked=mask_phone(caller),
            account_name=(account_activity.account_name if account_activity else "") or "UNVERIFIED",
            account_reference=(account_activity.account_reference if account_activity else "") or "UNVERIFIED",
            account_type=(account_activity.account_type if account_activity else "") or "UNVERIFIED",
            account_status=(account_activity.account_status if account_activity else "") or "UNVERIFIED",
            business_hours_flag=business_hours_flag,
            assigned_employee=accountable_employee,
            failed_destination=failed_destination,
            assigned_producer=assigned_producer or "UNVERIFIED",
            assigned_csr=assigned_csr or "UNVERIFIED",
            queue=first.queue_name,
            resolution=resolution if resolved_at else status,
            final_classification=final_classification,
            resolved_at=resolved_at.isoformat() if resolved_at else None,
            resolved_by=resolved_by,
            response_minutes=response_minutes,
            evidence_source=evidence_source,
            evidence_id=evidence_id,
            source_call_ids=tuple(dict.fromkeys(call.call_id for call in legs if call.call_id)),
            automated_text_detected=automated_text_detected,
            caller_had_to_redial=caller_had_to_redial,
            status=status,
        ))
    return sorted(results, key=lambda item: item.missed_at)


def resolution_dicts(items: Iterable[ServiceResolution]) -> list[dict[str, Any]]:
    return [asdict(item) for item in items]
