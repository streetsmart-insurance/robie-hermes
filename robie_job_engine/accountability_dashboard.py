"""Aggregate-only publication of a verified weekly report to Google Sheets."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
DEFAULT_LABELS = {
    "Reporting period": "reporting_period",
    "Submission Center": "submissions",
    "Policy changes": "policy_changes",
    "Certificates / COIs": "certificates",
    "Renewals / retention": "retention",
    "Service documentation": "service",
    "Gmail response aging": "gmail",
    "Sales inactivity": "sales",
    "Churn risk": "churn",
}


class DashboardPublicationError(RuntimeError):
    """The aggregate dashboard could not be updated and verified safely."""


def _client() -> Any:
    import google.auth
    from googleapiclient.discovery import build
    credentials, _ = google.auth.default(scopes=[SHEETS_SCOPE])
    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def _metric(report: str, label: str) -> str:
    match = re.search(rf"^• {re.escape(label)}:\s*(.+?)\s*$", report, re.MULTILINE)
    if not match:
        raise DashboardPublicationError(f"verified report is missing aggregate metric: {label}")
    value = match.group(1).strip()
    if not value or "UNVERIFIED" in value.upper():
        raise DashboardPublicationError(f"aggregate metric is not verified: {label}")
    return value


def _label_rows(values: list[list[Any]], labels: Mapping[str, str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for row_index, row in enumerate(values, start=1):
        first = str(row[0]).strip() if row else ""
        if first in labels:
            key = labels[first]
            if key in result:
                raise DashboardPublicationError(f"dashboard label is not unique: {first}")
            result[key] = row_index
    missing = [label for label, key in labels.items() if key not in result]
    if missing:
        raise DashboardPublicationError(f"dashboard is missing required labels: {', '.join(missing)}")
    return result


def publish_weekly_dashboard(
    report_path: Path, *, run_at: datetime, config: Mapping[str, Any], service: Any | None = None
) -> dict[str, Any]:
    report = report_path.read_text(encoding="utf-8", errors="strict")
    if "⚠️ *DATA LIMITATIONS*" in report:
        raise DashboardPublicationError("dashboard publication refused for an evidence-limited report")
    spreadsheet_id = str(config.get("spreadsheet_id") or "").strip()
    sheet_name = str(config.get("sheet_name") or "").strip()
    inspection_range = str(config.get("inspection_range") or "A1:L120").strip()
    environment_label = str(config.get("environment_label") or "TEST").strip().upper()
    if not spreadsheet_id or not sheet_name:
        raise DashboardPublicationError("dashboard requires spreadsheet_id and sheet_name")
    labels = dict(DEFAULT_LABELS)
    labels.update({str(k): str(v) for k, v in dict(config.get("labels") or {}).items()})
    client = service or _client()
    values_api = client.spreadsheets().values()
    observed = values_api.get(
        spreadsheetId=spreadsheet_id, range=f"'{sheet_name}'!{inspection_range}", majorDimension="ROWS"
    ).execute().get("values", [])
    rows = _label_rows(observed, labels)
    metrics = {
        "submissions": _metric(report, "Open Submissions Over 30 Days"),
        "policy_changes": (
            f"{_metric(report, 'Policy Changes Over 7 Days')}; "
            f"{_metric(report, 'Policy Change Blocker Split')}"
        ),
        "certificates": _metric(report, "COI / Endorsement Exceptions"),
        "retention": (
            f"retention {_metric(report, 'Retention Accounts Requiring Review')}; "
            f"expiration gaps {_metric(report, 'Expiration / Renewal Gaps')}"
        ),
        "service": _metric(report, "Overdue Tasks"),
        "gmail": _metric(report, "Email Threads >24h Awaiting Employee"),
        "sales": _metric(report, "Sales Opportunities With No Recent Touch"),
        "churn": f"Magellan at-risk calls {_metric(report, 'Magellan At-Risk Calls')}; corroboration required",
    }
    period = run_at.astimezone().date().isoformat()
    writes: dict[str, str] = {
        f"B{rows['reporting_period']}": period,
        f"C{rows['reporting_period']}": f"{environment_label} VERIFIED — aggregate only",
    }
    for key, value in metrics.items():
        writes[f"D{rows[key]}"] = f"VERIFIED — {value}"
    data = [{"range": f"'{sheet_name}'!{cell}", "values": [[value]]} for cell, value in sorted(writes.items())]
    values_api.batchUpdate(
        spreadsheetId=spreadsheet_id, body={"valueInputOption": "RAW", "data": data}
    ).execute()
    reread_ranges = [f"'{sheet_name}'!{cell}" for cell in sorted(writes)]
    reread = values_api.batchGet(
        spreadsheetId=spreadsheet_id, ranges=reread_ranges, majorDimension="ROWS"
    ).execute().get("valueRanges", [])
    observed_values = {
        item.get("range", "").rsplit("!", 1)[-1]: str(((item.get("values") or [[""]])[0] or [""])[0])
        for item in reread
    }
    for cell, expected in writes.items():
        if observed_values.get(cell) != expected:
            raise DashboardPublicationError(f"dashboard reread verification failed at {cell}")
    return {
        "spreadsheet_id": spreadsheet_id,
        "sheet_name": sheet_name,
        "environment_label": environment_label,
        "updated_cells": sorted(writes),
        "value_sha256": {
            cell: hashlib.sha256(value.encode("utf-8")).hexdigest() for cell, value in sorted(writes.items())
        },
        "report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
        "verified": True,
    }


def verify_weekly_dashboard(receipt: Mapping[str, Any], *, service: Any | None = None) -> tuple[bool, dict[str, Any]]:
    spreadsheet_id = str(receipt.get("spreadsheet_id") or "").strip()
    sheet_name = str(receipt.get("sheet_name") or "").strip()
    expected = {str(k): str(v) for k, v in dict(receipt.get("value_sha256") or {}).items()}
    cells = [str(value) for value in receipt.get("updated_cells", [])]
    if not spreadsheet_id or not sheet_name or not cells or set(cells) != set(expected):
        return False, {"verified": False, "reason": "dashboard receipt is incomplete"}
    client = service or _client()
    ranges = [f"'{sheet_name}'!{cell}" for cell in sorted(cells)]
    response = client.spreadsheets().values().batchGet(
        spreadsheetId=spreadsheet_id, ranges=ranges, majorDimension="ROWS"
    ).execute()
    observed_hashes: dict[str, str] = {}
    for item in response.get("valueRanges", []) or []:
        cell = str(item.get("range") or "").rsplit("!", 1)[-1]
        value = str(((item.get("values") or [[""]])[0] or [""])[0])
        observed_hashes[cell] = hashlib.sha256(value.encode("utf-8")).hexdigest()
    verified = observed_hashes == expected
    return verified, {
        "verified": verified,
        "spreadsheet_id_sha256": hashlib.sha256(spreadsheet_id.encode("utf-8")).hexdigest(),
        "sheet_name": sheet_name,
        "updated_cells": sorted(cells),
        "matched_cells": sum(observed_hashes.get(cell) == digest for cell, digest in expected.items()),
    }
