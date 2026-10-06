"""Google Sheets I/O for the Weekly Expiration List report.

Auth reuses the proven pattern from
streetsmart_agency_exports_sheets_push.py:
  1) ROBIE_GOOGLE_TOKEN_FILE (default /opt/streetsmart-hermes/.hermes/google_token.json), user OAuth
  2) Application Default Credentials when the token file is absent.

googleapiclient is imported lazily so unit tests run without it.

Write strategy (E13 LOCKED 2026-10-05):
  * Every run writes to a NEW dated tab "<base> YYYY-MM-DD" (base defaults
    to config.TAB_NAME_BASE, "Expiration List").
  * The new tab is created by DUPLICATING Sheet1 (the read-only format
    template). Sheet1 is NEVER written to, cleared, or deleted.
  * Values + formatting are written to the new tab only — never to Sheet1
    and never to any other tab.
  * Fail-closed: if a tab with the dated name already exists, the run
    aborts UNLESS --force is passed (which may overwrite ONLY that
    same-named dated tab — never Sheet1 or another tab).
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from . import config


class SheetsError(Exception):
    """Fail-closed error for any Sheets failure."""


def fingerprint(values: list[list[str]]) -> str:
    """Stable fingerprint of A:E content (kept for state records)."""
    canonical = json.dumps(
        [[str(c) for c in row[:5]] for row in values], sort_keys=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def connect():
    """Build the Sheets v4 service. Raises SheetsError when unavailable."""
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise SheetsError("googleapiclient is not installed") from exc
    token_path = os.environ.get(config.GOOGLE_TOKEN_ENV, "").strip() or config.DEFAULT_GOOGLE_TOKEN_FILE
    try:
        if os.path.exists(token_path):
            from google.oauth2.credentials import Credentials

            creds = Credentials.from_authorized_user_file(token_path)
            return build("sheets", "v4", credentials=creds, cache_discovery=False)
        from google.auth import default as google_auth_default

        creds, _ = google_auth_default(scopes=["https://www.googleapis.com/auth/spreadsheets"])
        return build("sheets", "v4", credentials=creds, cache_discovery=False)
    except Exception as exc:
        raise SheetsError(f"Google Sheets auth failed: {type(exc).__name__}") from exc


def get_sheet_id(service: Any, spreadsheet_id: str, tab: str) -> int | None:
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for sheet in meta.get("sheets", []):
        props = sheet.get("properties", {})
        if props.get("title") == tab:
            return props.get("sheetId")
    return None


def duplicate_tab(
    service: Any, spreadsheet_id: str, source_tab: str, new_title: str
) -> int:
    """Duplicate `source_tab` into `new_title` (copyTo + rename). E13:
    the weekly tab is born by duplicating Sheet1, the read-only template.
    Fail-closed when the source tab is missing."""
    source_id = get_sheet_id(service, spreadsheet_id, source_tab)
    if source_id is None:
        raise SheetsError(
            f"template tab '{source_tab}' not found; aborting (Sheet1 is the read-only template)"
        )
    try:
        service.spreadsheets().sheets().copyTo(
            spreadsheetId=spreadsheet_id,
            sheetId=source_id,
            body={"destinationSpreadsheetId": spreadsheet_id},
        ).execute()
        copy_id = get_sheet_id(service, spreadsheet_id, f"Copy of {source_tab}")
        if copy_id is None:
            raise SheetsError("duplicated tab not found after copyTo")
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {
                        "updateSheetProperties": {
                            "properties": {"sheetId": copy_id, "title": new_title},
                            "fields": "title",
                        }
                    }
                ]
            },
        ).execute()
        return copy_id
    except SheetsError:
        raise
    except Exception as exc:
        raise SheetsError(f"tab duplication failed: {type(exc).__name__}") from exc


def clear_a_to_e(service: Any, spreadsheet_id: str, tab: str, row_count: int) -> None:
    """Clear values AND formats on A:E only (never columns F+). Used only
    on the dated tab when --force overwrites it."""
    end = max(row_count, 1) + 1
    service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id, range=f"'{tab}'!A1:E{end}", body={}
    ).execute()
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            "requests": [
                {
                    "updateCells": {
                        "range": {
                            "sheetId": get_sheet_id(service, spreadsheet_id, tab),
                            "startRowIndex": 0,
                            "endRowIndex": end,
                            "startColumnIndex": 0,
                            "endColumnIndex": 5,
                        },
                        "fields": "userEnteredFormat",
                    }
                }
            ]
        },
    ).execute()


def write_values(
    service: Any, spreadsheet_id: str, tab: str, rows: list[list[str]]
) -> None:
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"'{tab}'!A1",
        valueInputOption="USER_ENTERED",
        body={"values": rows},
    ).execute()


def apply_format_requests(
    service: Any, spreadsheet_id: str, requests: list[dict]
) -> None:
    if not requests:
        return
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id, body={"requests": requests}
    ).execute()
