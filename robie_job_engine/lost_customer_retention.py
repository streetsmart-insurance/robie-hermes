"""Monthly lost-customer retention reporting with fail-closed evidence rules.

Magellan Match and Magellan Sentiment are corroborating evidence only. When
enrichment is enabled, values are refreshed from a read-only snapshot already
written by accountability Magellan collect or from a JSON list shaped like
phone-watchdog ``parse_magellan_data`` input. This module does not log into
the Magellan portal.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from .magellan_sad_identity import (
    DEFAULT_AGENCY_DIDS,
    is_agency_did,
    is_usable_client_name,
    normalize_phone_digits,
    select_client_phone,
)


SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
UNKNOWN = "Unknown"
CANONICAL_DEPARTMENTS = (
    "Personal Lines", "Commercial Lines", "Trucking and Transportation",
)
DEPARTMENT_RECIPIENT_KEYS = {
    "Personal Lines": "personal",
    "Commercial Lines": "commercial",
    "Trucking and Transportation": "trucking",
}
EXECUTIVE_DEPARTMENT = "Executive Team"
SOP_RESULTS = ("Followed", "Partially Followed", "Not Followed", "Cannot Verify")
SOP_SOURCES = {
    "Cancellations": "1gpDHSl6xm_RTF9d4Htyd9i8zj7eIWebvIDjFJJSlQzo",
    "Client Cancellation Request": "1rvAYtS1HY9hdX6Is0Z0Llt6FhizipoZOyZUbSyRkyhc",
    "Remarketing and Rewriting": "1rFsVS4DzN7x3xL1aZS4qGU5pPQFHeULYROsXsn0i9lw",
    "Reinstatements": "1nDze_axPNH7tqXr5GPnhoRP7j-eplAbmzkRcrSNZarQ",
    "Manual Renewals": "1NSHFdtgKYXeF6OnJRg9Qlse_yiHUoaXTYPi0X1OwLr4",
    "VIP Service Standards": "1YA70LaB0vmmRfKZZLFHMu2w8h8OrLGxrPG_h858oAis",
}
REQUIRED_REVIEW_HEADERS = (
    "Month", "Applicant ID", "Account Name", "Policy Count", "Lines of Business",
    "Policy Numbers", "Department", "CSR", "Assigned Agent", "Annualized Premium",
    "Account Status Classification", "Evidence-Supported Cause", "Evidence Summary",
    "Responsibility Lane", "Preventable?", "Confidence", "Magellan Match",
    "Magellan Sentiment", "Recovery Opportunity", "Recommended Account Action",
    "Systemic Prevention",
)
NO_MAGELLAN_MATCH = "No matched record"
NO_MAGELLAN_SENTIMENT = "Not available"
MAGELLAN_MATCH_COLUMN = REQUIRED_REVIEW_HEADERS.index("Magellan Match")
MAGELLAN_SENTIMENT_COLUMN = REQUIRED_REVIEW_HEADERS.index("Magellan Sentiment")
_EASTERN = ZoneInfo("America/New_York")
_AVAILABLE_MAGELLAN_STATUSES = frozenset({"available", "complete", "verified"})
_MAGELLAN_DIRECTION_LIMITATION = (
    "Phone-watchdog Magellan call JSON (phone_number, customer_name, sentiment_score, "
    "sentiment_label, key_findings, coaching_notes) has no call direction, so a match "
    "cannot say whether the customer called or the agency called. Direction is recorded "
    "only when the snapshot has from/to numbers or an explicit direction field, using the "
    "accountability agency-DID rule. Accountability collect snapshots are Sad-filtered, so "
    "they cannot prove a non-sad call Magellan excluded. Undated calls are not attached "
    "to a month-scoped account row. No matched record means the loaded snapshots that "
    "cover that month did not match; it does not prove Magellan never saw the customer. "
    "Magellan remains corroborating evidence only and never changes Evidence-Supported Cause."
)


def normalize(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def normalize_name(value: Any) -> str:
    name = normalize(value)
    if "," in name:
        last, first = [normalize(x) for x in name.split(",", 1)]
        name = f"{first} {last}"
    return re.sub(r"[^a-z0-9]", "", name.casefold())


def employee_directory(values: Sequence[Sequence[Any]]) -> dict[str, dict[str, str]]:
    if not values:
        raise ValueError("AppSheet Employees tab is empty")
    headers = [normalize(v) for v in values[0]]
    needed = ("Name", "Email", "Department", "Employment Status", "Position")
    missing = [h for h in needed if h not in headers]
    if missing:
        raise ValueError(f"AppSheet Employees missing headers: {', '.join(missing)}")
    result = {}
    for row in values[1:]:
        item = {h: normalize(row[i]) if i < len(row) else "" for i, h in enumerate(headers)}
        key = normalize_name(item["Name"])
        if key and item["Employment Status"].casefold() == "active":
            result[key] = item
    return result


def fallback_department(lines_of_business: Any) -> str:
    lob = normalize(lines_of_business).casefold()
    if any(x in lob for x in ("trucking", "truckers", "motor carrier", "transportation")):
        return "Trucking and Transportation"
    if any(x in lob for x in ("personal", "homeowner", "flood", "dwelling", "umbrella")):
        return "Personal Lines"
    return "Commercial Lines"


def resolve_department(item: Mapping[str, str], directory: Mapping[str, Mapping[str, str]]) -> tuple[str, str, bool]:
    assigned = directory.get(normalize_name(item.get("Assigned Agent")))
    csr = directory.get(normalize_name(item.get("CSR")))
    chosen = csr if assigned and assigned.get("Department") == EXECUTIVE_DEPARTMENT else assigned
    if not chosen or chosen.get("Department") not in CANONICAL_DEPARTMENTS:
        chosen = csr if csr and csr.get("Department") in CANONICAL_DEPARTMENTS else None
    if chosen:
        return chosen["Department"], f"AppSheet Employees: {chosen['Name']}", False
    return fallback_department(item.get("Lines of Business")), "LOB fallback — employee unmatched", True


def tenure_months(as_of: datetime, start: datetime | None) -> int | None:
    if start is None:
        return None
    return max(0, (as_of.year - start.year) * 12 + as_of.month - start.month - (as_of.day < start.day))


def tenure_band(months: int | None) -> str:
    if months is None:
        return "Cannot Verify"
    if months < 12:
        return "<1 year"
    if months < 36:
        return "1–3 years"
    if months < 60:
        return "3–5 years"
    return "5+ years"


def conservative_sop_audit(item: Mapping[str, str]) -> dict[str, str]:
    evidence = normalize(item.get("Evidence Summary"))
    folded = evidence.casefold()
    result = {key: "Cannot Verify" for key in (
        "SPLICE Call", "Email", "Text", "Postal Mail / Bad Contact",
        "Department Label", "Cancellation Notice / Reason", "Written Authorization",
        "EFT / Payment Rescue", "$5k+ Two Outreaches", "$10k+ AM Alert",
        "Licensed Handoff", "Carrier-Confirmed Reinstatement",
        "No Premature Renewal/Coverage Confirmation",
    )}
    if "signed cancellation request" in folded or "signed lpr" in folded:
        result["Written Authorization"] = "Followed"
    if "cancellation notice" in folded and ("reason" in folded or item.get("Evidence-Supported Cause")):
        result["Cancellation Notice / Reason"] = "Partially Followed"
    if "mistaken renewal confirmation" in folded or "incorrectly told" in folded:
        result["No Premature Renewal/Coverage Confirmation"] = "Not Followed"
    if "carrier confirm" in folded and "reinstat" in folded:
        result["Carrier-Confirmed Reinstatement"] = "Followed"
    result["Overall SOP Result"] = (
        "Not Followed" if "Not Followed" in result.values()
        else "Partially Followed" if any(v in {"Followed", "Partially Followed"} for v in result.values())
        else "Cannot Verify"
    )
    result["Evidence Citation"] = (
        f"3-Month Account Review | {item.get('Month')} | Applicant {item.get('Applicant ID')} | "
        f"Evidence Summary: {evidence or 'No supporting evidence recorded'}"
    )
    return result


def money(value: Any) -> float:
    cleaned = re.sub(r"[^0-9.()-]", "", normalize(value)).replace("(", "-").replace(")", "")
    try:
        return float(cleaned or 0)
    except ValueError:
        return 0.0


def review_sheet_rows(values: Sequence[Sequence[Any]]) -> list[tuple[int, dict[str, str]]]:
    """Return ``(sheet_row_number, item)`` pairs for account-review rows.

    Sheet row numbers are 1-based and include the header row, so the first
    data row is 2. Extra columns after the approved header prefix are kept.
    """
    if not values:
        return []
    headers = [normalize(v) for v in values[0]]
    if tuple(headers[: len(REQUIRED_REVIEW_HEADERS)]) != REQUIRED_REVIEW_HEADERS:
        raise ValueError("3-Month Account Review headers do not match the approved contract")
    output = []
    for offset, row in enumerate(values[1:], start=2):
        item = {header: normalize(row[i]) if i < len(row) else "" for i, header in enumerate(headers)}
        if item.get("Month") and item.get("Applicant ID"):
            if not item.get("Evidence-Supported Cause") or "not found" in item["Evidence-Supported Cause"].casefold():
                item["Evidence-Supported Cause"] = UNKNOWN
            output.append((offset, item))
    return output


def records(values: Sequence[Sequence[Any]]) -> list[dict[str, str]]:
    return [item for _, item in review_sheet_rows(values)]


def _column_letter(index: int) -> str:
    number = index + 1
    letters = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def magellan_cell_updates(rows: Sequence[tuple[int, Mapping[str, str]]]) -> list[dict[str, Any]]:
    """Sheets value updates for Magellan Match and Magellan Sentiment only."""
    match_cell = _column_letter(MAGELLAN_MATCH_COLUMN)
    sentiment_cell = _column_letter(MAGELLAN_SENTIMENT_COLUMN)
    updates = []
    for row_number, item in rows:
        updates.append({
            "range": f"'3-Month Account Review'!{match_cell}{row_number}",
            "values": [[item.get("Magellan Match") or NO_MAGELLAN_MATCH]],
        })
        updates.append({
            "range": f"'3-Month Account Review'!{sentiment_cell}{row_number}",
            "values": [[item.get("Magellan Sentiment") or NO_MAGELLAN_SENTIMENT]],
        })
    return updates


def magellan_write_back_enabled(config: Mapping[str, Any], *, dry_run: bool, status: str) -> bool:
    """Sheet refresh runs only for an available snapshot on a non-dry run."""
    magellan = config.get("magellan") or {}
    if not isinstance(magellan, Mapping):
        return False
    if dry_run or status != "available" or not magellan.get("enabled"):
        return False
    return magellan.get("write_back", True) is True


@dataclass(frozen=True)
class MagellanCallEvidence:
    """One Magellan call normalized from a snapshot the accountability path already writes."""

    call_id: str
    phone: str
    customer_name: str
    applicant_id: str
    sentiment_label: str
    sentiment_score: float | None
    findings: tuple[str, ...]
    note: str
    occurred_on: date | None
    direction: str
    schema: str


@dataclass(frozen=True)
class MagellanSource:
    status: str
    reason: str
    calls: tuple[MagellanCallEvidence, ...] = ()
    files: tuple[str, ...] = ()
    sad_scoped: bool = False
    covered_months: frozenset[tuple[int, int]] = frozenset()

    def summary(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "files": list(self.files),
            "calls_loaded": len(self.calls),
            "calls_with_direction": sum(1 for call in self.calls if call.direction),
            "sad_scoped_snapshot": self.sad_scoped,
            "covered_months": [f"{year:04d}-{month:02d}" for year, month in sorted(self.covered_months)],
            "limitation": _MAGELLAN_DIRECTION_LIMITATION,
        }


def _display_name(value: Any) -> str:
    name = normalize(value)
    if "," in name:
        last, first = [normalize(part) for part in name.split(",", 1)]
        if first and last:
            name = f"{first} {last}"
    return name


def _name_key(value: Any) -> str:
    return normalize_name(_display_name(value))


def _name_eligible(value: Any) -> bool:
    text = _display_name(value)
    words = [word for word in text.split(" ") if word]
    return len(words) >= 2 and is_usable_client_name(text)


def _parse_call_date(raw: Any) -> date | None:
    text = normalize(raw)
    if not text:
        return None
    parsed: datetime | None = None
    iso = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        for fmt in ("%m/%d/%Y %I:%M %p", "%m/%d/%Y"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                parsed = None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_EASTERN)
    else:
        parsed = parsed.astimezone(_EASTERN)
    return parsed.date()


def _month_bounds(label: Any) -> tuple[date, date] | None:
    try:
        start = datetime.strptime(normalize(label), "%B %Y").date()
    except ValueError:
        return None
    if start.month == 12:
        return start, date(start.year + 1, 1, 1)
    return start, date(start.year, start.month + 1, 1)


def _in_month(call: MagellanCallEvidence, bounds: tuple[date, date] | None) -> bool:
    if bounds is None or call.occurred_on is None:
        return False
    start, end = bounds
    return start <= call.occurred_on < end


def _findings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        text = normalize(value)
        return (text,) if text else ()
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        return ()
    found = []
    for item in value:
        text = normalize(item)
        if text and text not in found:
            found.append(text)
    return tuple(found)


def _sentiment_score(value: Any) -> float | None:
    if value is None or normalize(value) == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _direction(item: Mapping[str, Any], agency_dids: Iterable[str]) -> str:
    explicit = normalize(item.get("direction") or item.get("call_direction")).casefold()
    if explicit.startswith("in"):
        return "inbound"
    if explicit.startswith("out"):
        return "outbound"
    from_raw = item.get("from_number") or item.get("from_phone") or item.get("From")
    to_raw = item.get("to_number") or item.get("to_phone") or item.get("To")
    if not normalize(from_raw) or not normalize(to_raw):
        return ""
    from_agency = is_agency_did(from_raw, agency_dids)
    to_agency = is_agency_did(to_raw, agency_dids)
    if from_agency and not to_agency:
        return "outbound"
    if to_agency and not from_agency:
        return "inbound"
    return ""


def _schema_name(item: Mapping[str, Any]) -> str:
    if any(key in item for key in ("phone_number", "sentiment_label", "sentiment_score", "key_findings")):
        return "phone_watchdog"
    return "accountability_snapshot"


def _normalize_magellan_item(
    item: Mapping[str, Any], agency_dids: Iterable[str],
) -> MagellanCallEvidence | None:
    if not isinstance(item, Mapping):
        return None
    phone = select_client_phone(
        from_phone=item.get("from_phone") or item.get("from_number") or item.get("From"),
        to_phone=item.get("to_phone") or item.get("to_number") or item.get("To"),
        client_phone=item.get("client_phone") or item.get("phone_number") or item.get("phone") or item.get("caller_phone"),
        caller_phone=item.get("caller_phone_masked"),
        agency_dids=agency_dids,
    )
    digits = normalize_phone_digits(phone)
    if len(digits) != 10 or is_agency_did(digits, agency_dids):
        digits = ""
    name = normalize(
        item.get("customer_name")
        or item.get("client_name")
        or item.get("caller_name")
        or item.get("account_name")
        or item.get("account")
        or ""
    )
    if not is_usable_client_name(name):
        name = ""
    applicant = normalize(
        item.get("applicant_id") or item.get("Applicant ID") or item.get("ams_account_id") or ""
    )
    label = item.get("sentiment_label")
    if label is None:
        label = item.get("sentiment")
    if label is None:
        label = item.get("Sentiment")
    findings = _findings(item.get("key_findings") if "key_findings" in item else item.get("tags") or item.get("Topics"))
    note = normalize(item.get("coaching_notes") or "")
    call_id = normalize(item.get("call_id") or item.get("id") or "")
    occurred = _parse_call_date(item.get("timestamp") or item.get("occurred_at") or item.get("date_time"))
    if not any((call_id, digits, name, applicant, normalize(label), findings, note)):
        return None
    return MagellanCallEvidence(
        call_id=call_id,
        phone=digits,
        customer_name=name,
        applicant_id=applicant,
        sentiment_label=normalize(label),
        sentiment_score=_sentiment_score(item.get("sentiment_score")),
        findings=findings,
        note=note,
        occurred_on=occurred,
        direction=_direction(item, agency_dids),
        schema=_schema_name(item),
    )


def _payload_items(payload: Any) -> tuple[str, list[Any], bool]:
    """Return ``(status, items, sad_scoped)`` for one snapshot JSON value."""
    if isinstance(payload, list):
        return "available", payload, False
    if not isinstance(payload, dict):
        return "unavailable", [], False
    status = normalize(payload.get("source_status")).casefold()
    if status and status not in _AVAILABLE_MAGELLAN_STATUSES:
        return "unavailable", [], False
    if "pages_complete" in payload and payload.get("pages_complete") is not True:
        return "unavailable", [], False
    collector_sad = "magellan authenticated dashboard" in normalize(payload.get("source")).casefold()
    sad_only_key = "records" not in payload and "calls" not in payload and isinstance(payload.get("sad_calls"), list)
    for key in ("records", "calls", "sad_calls"):
        value = payload.get(key)
        if isinstance(value, list) and value:
            return "available", value, collector_sad or sad_only_key or key == "sad_calls"
    for key in ("records", "calls", "sad_calls"):
        if isinstance(payload.get(key), list):
            return "available", payload[key], collector_sad or sad_only_key or key == "sad_calls"
    return "unavailable", [], False


def _agency_dids(config: Mapping[str, Any]) -> frozenset[str]:
    extra = config.get("agency_dids") or ()
    found = set(DEFAULT_AGENCY_DIDS)
    if isinstance(extra, str):
        extra = [extra]
    for item in extra:
        digits = normalize_phone_digits(item)
        if digits:
            found.add(digits)
    return frozenset(found)


def _snapshot_files(config: Mapping[str, Any]) -> tuple[list[Path], list[str]]:
    files: list[Path] = []
    errors: list[str] = []
    raw_path = normalize(config.get("snapshot_path"))
    if raw_path:
        path = Path(raw_path)
        if path.is_file():
            files.append(path)
        else:
            errors.append(f"snapshot_path missing: {path}")
    raw_dir = normalize(config.get("snapshot_dir"))
    if raw_dir:
        directory = Path(raw_dir)
        pattern = normalize(config.get("snapshot_glob")) or "magellan-*.json"
        if "/" in pattern or "\\" in pattern or ".." in pattern:
            errors.append("snapshot_glob must be a file name pattern")
        elif directory.is_dir():
            files.extend(sorted(item for item in directory.glob(pattern) if item.is_file()))
        else:
            errors.append(f"snapshot_dir missing: {directory}")
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in files:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return unique, errors


def _covered_month(raw: Any, covered: set[tuple[int, int]]) -> None:
    day = raw if isinstance(raw, date) else _parse_call_date(raw)
    if day is not None:
        covered.add((day.year, day.month))


def load_magellan_calls(
    path: Path, agency_dids: Iterable[str],
) -> tuple[list[MagellanCallEvidence], bool, str, set[tuple[int, int]]]:
    """Parse one snapshot.

    Returns ``(calls, sad_scoped, error, covered_months)``. A non-empty error
    means the file must not be cited.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [], False, f"{path.name}: {exc.__class__.__name__}", set()
    status, items, sad_scoped = _payload_items(payload)
    if status != "available":
        return [], sad_scoped, f"{path.name}: unverified Magellan snapshot", set()
    calls = []
    seen: set[tuple[str, str, str, str, str]] = set()
    covered: set[tuple[int, int]] = set()
    filename_day = re.search(r"magellan-(\d{4}-\d{2}-\d{2})", path.name)
    if filename_day:
        _covered_month(filename_day.group(1), covered)
    if isinstance(payload, dict):
        _covered_month(payload.get("target_date"), covered)
    for item in items:
        if not isinstance(item, Mapping):
            continue
        call = _normalize_magellan_item(item, agency_dids)
        if call is None:
            continue
        key = (
            call.call_id, call.phone, call.customer_name, call.sentiment_label,
            call.occurred_on.isoformat() if call.occurred_on else "",
        )
        if key in seen:
            continue
        seen.add(key)
        calls.append(call)
        _covered_month(call.occurred_on, covered)
    return calls, sad_scoped, "", covered


def load_magellan_source(config: Mapping[str, Any] | None) -> MagellanSource:
    """Load optional Magellan snapshots. Disabled and missing sources invent nothing."""
    if not isinstance(config, Mapping) or not config.get("enabled"):
        return MagellanSource(status="disabled", reason="magellan enrichment disabled")
    files, errors = _snapshot_files(config)
    if errors or not files:
        detail = "; ".join(errors) or "no Magellan snapshot files found"
        return MagellanSource(status="unavailable", reason=detail, files=tuple(str(path) for path in files))
    dids = _agency_dids(config)
    calls: list[MagellanCallEvidence] = []
    used: list[str] = []
    failures: list[str] = []
    sad_scoped = False
    covered: set[tuple[int, int]] = set()
    seen: set[tuple[str, str, str, str, str]] = set()
    for path in files:
        parsed, file_sad, error, file_covered = load_magellan_calls(path, dids)
        if error:
            failures.append(error)
            continue
        sad_scoped = sad_scoped or file_sad
        covered.update(file_covered)
        used.append(str(path))
        for call in parsed:
            key = (
                call.call_id, call.phone, call.customer_name, call.sentiment_label,
                call.occurred_on.isoformat() if call.occurred_on else "",
            )
            if key in seen:
                continue
            seen.add(key)
            calls.append(call)
    if failures or not used:
        return MagellanSource(
            status="unavailable",
            reason="; ".join(failures) or "Magellan snapshot unreadable",
            files=tuple(str(path) for path in files),
        )
    return MagellanSource(
        status="available",
        reason="",
        calls=tuple(calls),
        files=tuple(used),
        sad_scoped=sad_scoped,
        covered_months=frozenset(covered),
    )


def _phone_headers(headers: Sequence[str]) -> list[int]:
    indexes = []
    for index, header in enumerate(headers):
        folded = header.casefold()
        if "phone" in folded or folded in {"cell", "mobile"}:
            indexes.append(index)
    return indexes


def _applicant_header(headers: Sequence[str]) -> int | None:
    for index, header in enumerate(headers):
        if header.casefold() in {"applicant id", "applicantid", "applicant_id"}:
            return index
    return None


def account_phones_by_applicant(monthly_sheets: Iterable[Sequence[Sequence[Any]]]) -> dict[str, set[str]]:
    """Phones from monthly tabs when a header actually names a phone column.

    Applicant IDs without a phone header contribute nothing. Agency DIDs are
    not treated as customer numbers.
    """
    found: dict[str, set[str]] = defaultdict(set)
    for values in monthly_sheets:
        rows = list(values)
        header_index = None
        headers: list[str] = []
        for index, row in enumerate(rows[:15]):
            candidate = [normalize(cell) for cell in row]
            if _applicant_header(candidate) is None or not _phone_headers(candidate):
                continue
            header_index = index
            headers = candidate
            break
        if header_index is None:
            continue
        applicant_index = _applicant_header(headers)
        phone_indexes = _phone_headers(headers)
        assert applicant_index is not None
        for row in rows[header_index + 1:]:
            applicant = normalize(row[applicant_index]) if applicant_index < len(row) else ""
            if not applicant.isdigit():
                continue
            for index in phone_indexes:
                if index >= len(row):
                    continue
                digits = normalize_phone_digits(row[index])
                if len(digits) == 10 and not is_agency_did(digits):
                    found[applicant].add(digits)
    return dict(found)


def _row_phones(item: Mapping[str, str], phones_by_applicant: Mapping[str, set[str]]) -> set[str]:
    phones = set(phones_by_applicant.get(normalize(item.get("Applicant ID")), ()))
    for key, value in item.items():
        folded = normalize(key).casefold()
        if "phone" in folded or folded in {"cell", "mobile"}:
            digits = normalize_phone_digits(value)
            if len(digits) == 10 and not is_agency_did(digits):
                phones.add(digits)
    return phones


def _risk_tokens(call: MagellanCallEvidence) -> list[str]:
    label = call.sentiment_label.casefold()
    findings = " ".join(call.findings).casefold()
    blob = f"{label} {findings}"
    sad = "sad" in label or label in {"negative", "frown"} or (
        call.sentiment_score is not None and call.sentiment_score < 0
    )
    at_risk = any(token in blob for token in ("at-risk", "at risk", "cancellation")) or (
        call.sentiment_score is not None and call.sentiment_score < 0
    )
    frustrated = "frustrat" in blob
    tokens = []
    if sad:
        tokens.append("SAD")
    if at_risk:
        tokens.append("at-risk")
    if frustrated:
        tokens.append("frustration")
    return tokens


def _evidence_snippet(call: MagellanCallEvidence) -> str:
    bits = [normalize(item) for item in call.findings if normalize(item)]
    text = ", ".join(bits) or call.note
    text = normalize(text)[:80].strip(" ,;")
    if not text:
        return ""
    if call.call_id:
        return f"evidence: {text} ({call.call_id})"
    return f"evidence: {text}"


def format_magellan_sentiment(calls: Sequence[MagellanCallEvidence]) -> str:
    if not calls:
        return NO_MAGELLAN_SENTIMENT
    directions = []
    if any(call.direction == "inbound" for call in calls):
        directions.append("customer called")
    if any(call.direction == "outbound" for call in calls):
        directions.append("agency called")
    primary = max(
        calls,
        key=lambda call: (
            len(_risk_tokens(call)),
            call.occurred_on.toordinal() if call.occurred_on else 0,
            call.call_id,
        ),
    )
    parts: list[str] = []
    if directions:
        parts.append(" and ".join(directions))
    risk = _risk_tokens(primary)
    if risk:
        parts.append(" / ".join(risk))
    if primary.sentiment_label:
        parts.append(primary.sentiment_label)
    snippet = _evidence_snippet(primary)
    if snippet:
        parts.append(snippet)
    if primary.occurred_on:
        parts.append(primary.occurred_on.isoformat())
    text = "; ".join(parts)
    return text[:240] if text else NO_MAGELLAN_SENTIMENT


def format_magellan_match(method: str, calls: Sequence[MagellanCallEvidence]) -> str:
    noun = "call" if len(calls) == 1 else "calls"
    return f"Matched by {method} ({len(calls)} {noun})"


def enrich_review_with_magellan(
    items: Sequence[dict[str, str]],
    source: MagellanSource,
    phones_by_applicant: Mapping[str, set[str]] | None = None,
) -> dict[str, Any]:
    """Refresh Magellan columns from a snapshot. Unavailable sources leave the sheet text."""
    summary = source.summary()
    summary["matched_accounts"] = 0
    summary["rows"] = []
    if source.status != "available":
        return summary
    phones_by_applicant = phones_by_applicant or {}
    phone_owners: dict[str, set[str]] = defaultdict(set)
    name_owners: dict[str, set[str]] = defaultdict(set)
    for item in items:
        applicant = normalize(item.get("Applicant ID"))
        for phone in _row_phones(item, phones_by_applicant):
            phone_owners[phone].add(applicant)
        if _name_eligible(item.get("Account Name")):
            name_owners[_name_key(item.get("Account Name"))].add(applicant)
    ambiguous_phones = {phone for phone, owners in phone_owners.items() if len(owners) > 1}
    ambiguous_names = {name for name, owners in name_owners.items() if len(owners) > 1}
    matched = 0
    rows = []
    for item in items:
        bounds = _month_bounds(item.get("Month"))
        in_month = [call for call in source.calls if _in_month(call, bounds)]
        raw_phones = _row_phones(item, phones_by_applicant)
        account_phones = raw_phones - ambiguous_phones
        phone_hits = [call for call in in_month if call.phone and call.phone in account_phones]
        applicant = normalize(item.get("Applicant ID"))
        applicant_hits = [call for call in in_month if call.applicant_id and call.applicant_id == applicant]
        method = ""
        chosen: list[MagellanCallEvidence] = []
        if phone_hits:
            method = "phone"
            chosen = phone_hits
            seen = {(call.call_id, call.phone, call.occurred_on) for call in chosen}
            for call in applicant_hits:
                key = (call.call_id, call.phone, call.occurred_on)
                if key not in seen:
                    chosen.append(call)
                    seen.add(key)
        elif applicant_hits:
            method = "applicant ID"
            chosen = applicant_hits
        elif not raw_phones:
            name = _name_key(item.get("Account Name"))
            if name and name not in ambiguous_names and _name_eligible(item.get("Account Name")):
                named = [call for call in in_month if _name_key(call.customer_name) == name]
                distinct_phones = {call.phone for call in named if call.phone}
                if named and len(distinct_phones) <= 1:
                    method = "account name"
                    chosen = named
        if chosen:
            item["Magellan Match"] = format_magellan_match(method, chosen)
            item["Magellan Sentiment"] = format_magellan_sentiment(chosen)
            matched += 1
        else:
            method = ""
            month_key = (bounds[0].year, bounds[0].month) if bounds else None
            if month_key is not None and month_key in source.covered_months:
                item["Magellan Match"] = NO_MAGELLAN_MATCH
                item["Magellan Sentiment"] = NO_MAGELLAN_SENTIMENT
            else:
                if not normalize(item.get("Magellan Match")):
                    item["Magellan Match"] = NO_MAGELLAN_MATCH
                if not normalize(item.get("Magellan Sentiment")):
                    item["Magellan Sentiment"] = NO_MAGELLAN_SENTIMENT
        rows.append({
            "month": item.get("Month", ""),
            "applicant_id": applicant,
            "account_name": item.get("Account Name", ""),
            "magellan_match": item.get("Magellan Match", ""),
            "magellan_sentiment": item.get("Magellan Sentiment", ""),
            "method": method,
        })
    summary["matched_accounts"] = matched
    summary["rows"] = rows
    return summary


def validate_monthly_source(values: Sequence[Sequence[Any]], month: str) -> dict[str, Any]:
    rows = []
    for row in values:
        first = normalize(row[0] if row else "")
        if first.isdigit() and len(row) > 12 and normalize(row[9]):
            rows.append(row)
    keys = ["|".join((normalize(r[0]).casefold(), normalize(r[9]).casefold(), normalize(r[12]).casefold())) for r in rows]
    if len(keys) != len(set(keys)):
        raise ValueError(f"{month}: duplicate policy transaction keys remain")
    departments = Counter(normalize(r[8]) or UNKNOWN for r in rows)
    return {
        "month": month,
        "policy_rows": len(rows),
        "accounts": len({normalize(r[0]) for r in rows}),
        "departments": dict(sorted(departments.items())),
        "sha256": hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
    }


def summarize(items: Iterable[Mapping[str, str]]) -> dict[str, Any]:
    grouped: dict[str, dict[str, Any]] = {}
    for item in items:
        key = f"{item.get('Month')}|{item.get('Department') or UNKNOWN}"
        group = grouped.setdefault(key, {
            "month": item.get("Month"), "department": item.get("Department") or UNKNOWN,
            "accounts": 0, "policies": 0, "premium": 0.0, "classifications": Counter(),
            "causes": Counter(), "magellan_matches": 0,
        })
        group["accounts"] += 1
        group["policies"] += int(money(item.get("Policy Count")))
        group["premium"] += money(item.get("Annualized Premium"))
        group["classifications"][item.get("Account Status Classification") or UNKNOWN] += 1
        group["causes"][item.get("Evidence-Supported Cause") or UNKNOWN] += 1
        if normalize(item.get("Magellan Match")).casefold() not in {"", "no matched record", "not available"}:
            group["magellan_matches"] += 1
    result = []
    for group in grouped.values():
        group["classifications"] = dict(group["classifications"])
        group["causes"] = dict(group["causes"])
        group["premium"] = round(group["premium"], 2)
        result.append(group)
    return {"groups": sorted(result, key=lambda x: (x["month"], x["department"]))}


def department_email(department: str, items: Sequence[Mapping[str, str]], *, run_id: str) -> str:
    lines = [
        f"StreetSmart Lost Customer Retention Review — {department}", "",
        "June–August 2026 validated backfill", f"Run ID: {run_id}", "",
    ]
    if not items:
        lines += ["No account-level records mapped to this department for the validated period."]
        return "\n".join(lines)
    totals = summarize(items)["groups"]
    lines.append("Monthly totals:")
    for group in totals:
        lines.append(f"- {group['month']}: {group['accounts']} accounts / {group['policies']} policies / ${group['premium']:,.2f}")
    lines += ["", "Account-level findings:"]
    for item in items:
        lines += [
            "",
            f"- {item['Month']} | Applicant {item['Applicant ID']} | {item['Account Name']}",
            f"  Policies: {item['Policy Count']} | LOB: {item['Lines of Business']} | Premium: {item['Annualized Premium']}",
            f"  Status: {item['Account Status Classification']} | Cause: {item['Evidence-Supported Cause'] or UNKNOWN}",
            f"  Evidence: {item['Evidence Summary'] or 'No supporting evidence found.'}",
            f"  Magellan: {item['Magellan Match'] or 'No matched record'} / {item['Magellan Sentiment'] or 'Not available'}",
            f"  Department mapping: {item.get('_department_source', 'pre-mapped workbook')}"+
            (" [FALLBACK EXCEPTION]" if item.get("_department_exception") else ""),
            f"  SOP audit: {conservative_sop_audit(item)['Overall SOP Result']} | {conservative_sop_audit(item)['Evidence Citation']}",
            f"  Recommended action: {item['Recommended Account Action'] or 'Review and document a supported cause.'}",
        ]
    lines += ["", "Assignment alone is not evidence of employee fault. Unsupported causes remain Unknown."]
    return "\n".join(lines)


def executive_email(items: Sequence[Mapping[str, str]], *, run_id: str) -> str:
    summary = summarize(items)["groups"]
    total_accounts = len(items)
    total_policies = sum(int(money(x.get("Policy Count"))) for x in items)
    total_premium = sum(money(x.get("Annualized Premium")) for x in items)
    statuses = Counter(x.get("Account Status Classification") or UNKNOWN for x in items)
    lines = [
        "StreetSmart Lost Customer Retention Review — Executive Trends", "",
        "June–August 2026 validated backfill", f"Run ID: {run_id}",
        f"Overall: {total_accounts} account-months / {total_policies} policies / ${total_premium:,.2f} flagged annualized premium", "",
        "Department/month totals:",
    ]
    for group in summary:
        lines.append(f"- {group['month']} — {group['department']}: {group['accounts']} accounts / {group['policies']} policies / ${group['premium']:,.2f}")
    lines += ["", "Corrected account classifications:"]
    for label, count in statuses.most_common():
        lines.append(f"- {label}: {count}")
    lines += [
        "", "Controls verified:",
        "- Raw policy rows are consolidated to account-month findings.",
        "- Rewrites, reinstatements and partial-policy losses remain separate from true customer loss.",
        "- Unsupported causes are Unknown.",
        "- Assignment alone is never treated as employee fault.",
        "- Magellan is corroborating evidence only when an account match exists.",
        "", "This is a scheduled monthly-updated Google Sheet, not a continuously updating dashboard.",
    ]
    return "\n".join(lines)


@dataclass(frozen=True)
class MessageSpec:
    key: str
    to: tuple[str, ...]
    subject: str
    body: str


def build_messages(items: Sequence[Mapping[str, str]], recipients: Mapping[str, str], run_id: str) -> list[MessageSpec]:
    by_department: dict[str, list[Mapping[str, str]]] = defaultdict(list)
    for item in items:
        by_department[item.get("Department") or UNKNOWN].append(item)
    mapping = tuple((department, key) for department, key in DEPARTMENT_RECIPIENT_KEYS.items())
    messages = []
    for department, key in mapping:
        messages.append(MessageSpec(
            key=f"department:{key}", to=(recipients[key],),
            subject=f"Lost Customer Retention Review — {department} — June–August 2026",
            body=department_email(department, by_department.get(department, []), run_id=run_id),
        ))
    messages.append(MessageSpec(
        key="executive", to=(recipients["carlo"], recipients["jake"]),
        subject="Lost Customer Retention Review — Executive Trends — June–August 2026",
        body=executive_email(items, run_id=run_id),
    ))
    return messages


def send_message(gmail: Any, sender: str, spec: MessageSpec) -> dict[str, Any]:
    message = EmailMessage()
    message["To"] = ", ".join(spec.to)
    message["From"] = sender
    message["Subject"] = spec.subject
    message["X-ROBIE-Idempotency-Key"] = spec.key
    message.set_content(spec.body)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    sent = gmail.users().messages().send(userId="me", body={"raw": raw}).execute()
    return {"key": spec.key, "to": list(spec.to), "message_id": sent.get("id"), "thread_id": sent.get("threadId")}


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"runs": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    temp.replace(path)
