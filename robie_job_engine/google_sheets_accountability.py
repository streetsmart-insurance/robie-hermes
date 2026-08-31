"""Read explicitly allowlisted AppSheet backing-sheet columns with cloud ADC."""

from __future__ import annotations

import os
from typing import Any, Mapping


SHEETS_READONLY_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


def _sheets_client(service_account_email: str | None = None) -> Any:
    import google.auth
    from google.auth import iam
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    service_account_email = (
        service_account_email
        or os.environ.get("ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT", "")
    ).strip()
    if service_account_email:
        source, _ = google.auth.default(scopes=[CLOUD_PLATFORM_SCOPE])
        credentials = service_account.Credentials(
            signer=iam.Signer(Request(), source, service_account_email),
            service_account_email=service_account_email,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=[SHEETS_READONLY_SCOPE],
        )
    else:
        credentials, _ = google.auth.default(scopes=[SHEETS_READONLY_SCOPE])
    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def collect_allowlisted_tables(config: Mapping[str, Any], *, service: Any | None = None) -> dict[str, Any]:
    spreadsheet_id = str(config.get("spreadsheet_id") or "").strip()
    tables = dict(config.get("tables") or {})
    if not spreadsheet_id or not tables:
        return {"source_status": "missing spreadsheet_id or table allowlist"}
    client = service or _sheets_client()
    output: dict[str, Any] = {}
    for label, raw in sorted(tables.items()):
        table = dict(raw or {})
        range_name = str(table.get("range") or "").strip()
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


def role_registry_from_snapshot(snapshot: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    table_label = str(config.get("employee_role_table") or "").strip()
    table = ((snapshot.get("tables") or {}).get(table_label) or {}) if table_label else {}
    columns = dict(config.get("employee_role_columns") or {})
    name_column = str(columns.get("name") or "Name")
    role_column = str(columns.get("role") or "Role")
    email_column = str(columns.get("email") or "Email")
    employees = {}
    for row in table.get("rows", []) or []:
        name = str(row.get(name_column) or "").strip()
        role = str(row.get(role_column) or "").strip()
        if name and role:
            employees[name] = {"role": role, "email": str(row.get(email_column) or "").strip()}
    return {"source_status": table.get("source_status", "not supplied"), "employees": employees}
