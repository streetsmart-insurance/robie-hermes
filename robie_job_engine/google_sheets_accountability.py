"""Read explicitly allowlisted AppSheet backing-sheet columns with cloud ADC."""

from __future__ import annotations

import csv
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from .business_calendar import previous_business_day


SHEETS_READONLY_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
REQUIRED_ROSTER_SCOPES = (SHEETS_READONLY_SCOPE, DRIVE_READONLY_SCOPE)
TEST_ACCOUNTABILITY_SA = (
    "robie-test-drive-reader@streetsmart-hermes-poc.iam.gserviceaccount.com"
)


class SheetsRosterAccessError(RuntimeError):
    """Fail-closed roster read. Never invent producer emails."""


def required_roster_scopes() -> tuple[str, ...]:
    """OAuth scopes the Test/cloud ADC identity must present for the roster."""
    return REQUIRED_ROSTER_SCOPES


def classify_sheets_auth_error(exc: BaseException) -> SheetsRosterAccessError:
    """Name the missing Sheets grant. Do not invent a producer directory."""
    text = f"{type(exc).__name__}: {exc}"
    upper = text.upper()
    if "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in upper or (
        "INSUFFICIENT" in upper and "SCOPE" in upper
    ):
        return SheetsRosterAccessError(
            "approved active-employee roster is unavailable: "
            "ACCESS_TOKEN_SCOPE_INSUFFICIENT. The Test/cloud ADC identity "
            f"(typically {TEST_ACCOUNTABILITY_SA}, or whichever identity the "
            "accountability manifest / VM uses) must request "
            f"{SHEETS_READONLY_SCOPE}. Shared Drive-hosted sheets also need "
            f"{DRIVE_READONLY_SCOPE}. Carlo must grant those OAuth scopes on "
            "the VM-attached SA (cloud-platform or the explicit Sheets+Drive "
            "scopes) and share the roster spreadsheet with that SA as Viewer. "
            "Never invent producer emails."
        )
    if "403" in text or "PERMISSION_DENIED" in upper:
        return SheetsRosterAccessError(
            "approved active-employee roster is forbidden (403). Share the "
            "allowlisted spreadsheet with the Test accountability SA "
            f"({TEST_ACCOUNTABILITY_SA} or the identity the manifest/ADC "
            "uses) as Viewer. Never invent producer emails."
        )
    return SheetsRosterAccessError(
        "approved active-employee roster could not be read "
        f"({type(exc).__name__}). Never invent producer emails."
    )


def _sheets_client() -> Any:
    import google.auth
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    try:
        # Request only the Sheets API scope on the token so a Production SA
        # that already has spreadsheets.readonly is not broken by also
        # demanding drive.readonly. Shared Drive hosting is documented as a
        # Carlo grant; SCOPE_INSUFFICIENT names both required scopes.
        credentials, _ = google.auth.default(scopes=[SHEETS_READONLY_SCOPE])
        if getattr(credentials, "requires_scopes", False):
            credentials = credentials.with_scopes([SHEETS_READONLY_SCOPE])
        refresh = getattr(credentials, "refresh", None)
        if callable(refresh) and not getattr(credentials, "valid", True):
            refresh(Request())
        return build("sheets", "v4", credentials=credentials, cache_discovery=False)
    except SheetsRosterAccessError:
        raise
    except Exception as exc:
        raise classify_sheets_auth_error(exc) from exc


def previous_business_week_tab(value: date, *, holiday_calendar: str | None = None) -> str:
    """Return Sandeep's Monday-Sunday tab label for the prior business day."""

    target = previous_business_day(value, holiday_calendar=holiday_calendar)
    monday = target - timedelta(days=target.weekday())
    sunday = monday + timedelta(days=6)
    return f"{monday.month}/{monday.day}-{sunday.month}/{sunday.day}"


def _resolved_range(value: str, *, as_of: datetime | None = None, holiday_calendar: str | None = None) -> str:
    """Resolve narrow date tokens without allowing a broad spreadsheet scan."""

    observed = as_of or datetime.now(ZoneInfo("America/New_York"))
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=ZoneInfo("America/New_York"))
    local = observed.astimezone(ZoneInfo("America/New_York"))
    return (
        value.replace("{month}", local.strftime("%B"))
        .replace("{year}", local.strftime("%Y"))
        .replace("{previous_business_week}", previous_business_week_tab(local.date(), holiday_calendar=holiday_calendar))
    )


def collect_allowlisted_tables(
    config: Mapping[str, Any],
    *,
    service: Any | None = None,
    as_of: datetime | None = None,
    holiday_calendar: str | None = None,
) -> dict[str, Any]:
    spreadsheet_id = str(config.get("spreadsheet_id") or "").strip()
    tables = dict(config.get("tables") or {})
    if not spreadsheet_id or not tables:
        return {"source_status": "missing spreadsheet_id or table allowlist"}
    client = service or _sheets_client()
    output: dict[str, Any] = {}
    for label, raw in sorted(tables.items()):
        table = dict(raw or {})
        range_name = _resolved_range(str(table.get("range") or "").strip(), as_of=as_of, holiday_calendar=holiday_calendar)
        allowed = [str(item).strip() for item in table.get("allowed_columns", []) if str(item).strip()]
        if not range_name or not allowed:
            output[str(label)] = {"source_status": "missing range or allowed_columns", "rows": []}
            continue
        values = client.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=range_name,
            majorDimension="ROWS",
        ).execute().get("values", [])
        headers = [str(item).strip() for item in (values[0] if values else [])]
        positions = {name: headers.index(name) for name in allowed if name in headers}
        rows = []
        for raw_row in values[1:]:
            row = {name: raw_row[index] if index < len(raw_row) else "" for name, index in positions.items()}
            if any(str(value).strip() for value in row.values()):
                rows.append(row)
        output[str(label)] = {
            "source_status": "available" if len(positions) == len(allowed) else "partial: allowlisted column missing",
            "range": range_name,
            "columns": list(positions),
            "row_count": len(rows),
            "rows": rows,
        }
    return {
        "source_status": "available",
        "scope": SHEETS_READONLY_SCOPE,
        "spreadsheet_id": spreadsheet_id,
        "total_rows": sum(item.get("row_count", 0) for item in output.values()),
        "tables": output,
    }


def write_allowlisted_table_csv(snapshot: Mapping[str, Any], table_label: str, destination: Path) -> Path:
    """Write one already-allowlisted table for the existing tracker auditor."""

    table = dict(((snapshot.get("tables") or {}).get(table_label) or {}))
    columns = [str(value) for value in table.get("columns", [])]
    if table.get("source_status") != "available" or not columns:
        raise ValueError(f"Google Sheet tracker {table_label!r} is incomplete")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in table.get("rows", []) or []:
            writer.writerow({column: row.get(column, "") for column in columns})
    return destination


def role_registry_from_snapshot(snapshot: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    table_label = str(config.get("employee_role_table") or "").strip()
    table = ((snapshot.get("tables") or {}).get(table_label) or {}) if table_label else {}
    columns = dict(config.get("employee_role_columns") or {})
    name_column = str(columns.get("name") or "Name")
    role_column = str(columns.get("role") or "Role")
    email_column = str(columns.get("email") or "Email")
    department_column = str(columns.get("department") or "Department")
    status_column = str(columns.get("status") or "Active")
    manager_column = str(columns.get("manager") or "Department Head")
    active_value = str(columns.get("active_value") or "Active").strip().casefold()
    employees = {}
    for row in table.get("rows", []) or []:
        name = str(row.get(name_column) or "").strip()
        role = str(row.get(role_column) or "").strip()
        status = str(row.get(status_column) or "").strip()
        if status_column in row and active_value and status.casefold() != active_value:
            continue
        if name and role:
            employees[name] = {
                "role": role,
                "email": str(row.get(email_column) or "").strip(),
                "department": str(row.get(department_column) or "").strip(),
                "manager": str(row.get(manager_column) or "").strip(),
                "status": status or "UNVERIFIED",
            }
    return {"source_status": table.get("source_status", "not supplied"), "employees": employees}
