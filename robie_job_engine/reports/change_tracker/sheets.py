"""Google Sheets write path for the weekly change-tracker tab.

Auth follows the proven pattern from
``/opt/streetsmart-hermes/exports/streetsmart_agency_exports_sheets_push.py``:
ROBIE_GOOGLE_TOKEN_FILE (default /opt/streetsmart-hermes/.hermes/google_token.json)
user-OAuth token with the spreadsheets scope, refreshed on demand. The
``googleapiclient`` imports are lazy so unit tests never need the dependency.

STATUS (finding 2): conditional formatting (light-red Days Open >30) and the
Plain-Text number formats are UNPROVEN-but-standard Google Sheets API calls.
They are implemented exactly per the API reference, are exercised in --dry-run
via the request builders below, and are labeled UNPROVEN-UNTIL-LIVE in
ASSUMPTIONS.md until a live tab proves them.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .config import DEFAULT_CONFIG, STALE_FORMULA, ChangeTrackerConfig
from .transform import MissingSpreadsheetError, RefuseOverwriteError, week_tab_name

DEFAULT_TOKEN_FILE = "/opt/streetsmart-hermes/.hermes/google_token.json"
SPREADSHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"


def resolve_spreadsheet_id(config: ChangeTrackerConfig = DEFAULT_CONFIG) -> str:
    sheet_id = (config.spreadsheet_id or "").strip()
    if not sheet_id or sheet_id.startswith("PENDING_"):
        raise MissingSpreadsheetError(
            "the canonical 'New Change Request Tracker 2' spreadsheet id is unset "
            "(X1 pending). Set CHANGE_TRACKER_SPREADSHEET_ID or config.py "
            "CHANGE_TRACKER_SPREADSHEET_ID; refusing to guess which similarly-named "
            "copy is canonical"
        )
    return sheet_id


def build_service(token_file: str | None = None):
    """Build a Sheets v4 service from the user-OAuth token file."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    path = Path(token_file or os.environ.get("ROBIE_GOOGLE_TOKEN_FILE", "").strip()
                 or DEFAULT_TOKEN_FILE)
    if not path.is_file():
        raise FileNotFoundError(f"google token file not found: {path}")
    creds = Credentials.from_authorized_user_file(str(path))
    granted = set(creds.scopes or ())
    if SPREADSHEETS_SCOPE not in granted:
        raise PermissionError(
            f"token {path} lacks the spreadsheets scope; granted={sorted(granted)}"
        )
    if not creds.valid:
        creds.refresh(Request())
    return build("sheets", "v4", credentials=creds, cache_discovery=False), str(path)


def list_tabs(service, spreadsheet_id: str) -> dict[str, int]:
    """Map tab title -> sheetId."""
    meta = (
        service.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="sheets.properties")
        .execute()
    )
    return {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta.get("sheets", [])}


def tab_has_data(service, spreadsheet_id: str, tab: str) -> bool:
    resp = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=f"'{tab}'!A1:A")
        .execute()
    )
    return bool(resp.get("values"))


def ensure_week_tab(
    service, spreadsheet_id: str, tab: str, *, dry_run: bool
) -> dict[str, Any]:
    """Create the week tab if missing. Returns {tab, sheet_id, created}."""
    tabs = list_tabs(service, spreadsheet_id)
    if tab in tabs:
        return {"tab": tab, "sheet_id": tabs[tab], "created": False}
    if dry_run:
        print(f"DRY: would create tab {tab!r}")
        return {"tab": tab, "sheet_id": None, "created": False, "dry_run": True}
    resp = (
        service.spreadsheets()
        .batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": tab}}}]},
        )
        .execute()
    )
    sheet_id = resp["replies"][0]["addSheet"]["properties"]["sheetId"]
    return {"tab": tab, "sheet_id": sheet_id, "created": True}


def _team_header_row_indexes(matrix: list[list[str]]) -> list[int]:
    """1-based row numbers of team header rows (col B matches 'Team - names')."""
    indexes = []
    for i, row in enumerate(matrix[1:], start=2):  # skip header row
        b = (row[1] if len(row) > 1 else "").strip()
        if b and not any((row[j] if j < len(row) else "") for j in (0, 2, 3, 4, 9)):
            # B has text and the data columns are empty -> team header row.
            indexes.append(i)
    return indexes


def build_format_requests(
    matrix: list[list[str]], sheet_id: int, config: ChangeTrackerConfig = DEFAULT_CONFIG
) -> list[dict[str, Any]]:
    """Build the Sheets API formatting requests for a finished weekly tab.

    UNPROVEN-UNTIL-LIVE (finding 2): request shapes follow the Sheets API
    reference but have not been proven against a live tab.
      - Policy Number (J) -> Plain text (P9 LOCKED: re-applied every run,
        idempotent; NO other column gets this format).
      - Team header rows -> bold + light-blue fill.
      - =$C2>30 conditional format over A2:Q<last> -> light red 3 fill.
    """
    last_row = len(matrix)
    requests: list[dict[str, Any]] = []
    if sheet_id is None or last_row < 2:
        return requests

    # 1) Plain-text number format for J (Policy Number) only (P9 LOCKED).
    col = 9  # 0-based: J=9
    requests.append(
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "endRowIndex": last_row,
                    "startColumnIndex": col,
                    "endColumnIndex": col + 1,
                },
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "TEXT"}}},
                "fields": "userEnteredFormat.numberFormat",
            }
        }
    )

    # 2) Team header rows: bold + light-blue fill across A:Q.
    for row_1based in _team_header_row_indexes(matrix):
        requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": row_1based - 1,
                        "endRowIndex": row_1based,
                        "startColumnIndex": 0,
                        "endColumnIndex": 17,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "textFormat": {"bold": True},
                            "backgroundColor": _hex_to_color(config.team_header_fill),
                        }
                    },
                    "fields": "userEnteredFormat(textFormat,backgroundColor)",
                }
            }
        )

    # 3) Stale-flag conditional format: =$C2>30 over A2:Q<last>, light red 3.
    requests.append(
        {
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [
                        {
                            "sheetId": sheet_id,
                            "startRowIndex": 1,
                            "endRowIndex": last_row,
                            "startColumnIndex": 0,
                            "endColumnIndex": 17,
                        }
                    ],
                    "booleanRule": {
                        "condition": {
                            "type": "CUSTOM_FORMULA",
                            "values": [{"userEnteredValue": STALE_FORMULA}],
                        },
                        "format": {
                            "backgroundColor": _hex_to_color(config.stale_fill),
                        },
                    },
                },
                "index": 0,
            }
        }
    )
    return requests


def _hex_to_color(hex_color: str) -> dict[str, float]:
    h = hex_color.lstrip("#")
    return {
        "red": int(h[0:2], 16) / 255.0,
        "green": int(h[2:4], 16) / 255.0,
        "blue": int(h[4:6], 16) / 255.0,
    }


def write_week_tab(
    service,
    spreadsheet_id: str,
    tab: str,
    matrix: list[list[str]],
    *,
    dry_run: bool,
    force: bool = False,
    config: ChangeTrackerConfig = DEFAULT_CONFIG,
) -> dict[str, Any]:
    """Write the finished weekly tab atomically.

    Fail-closed / idempotent (X3): if the tab already exists with data rows,
    refuse unless force=True. The whole tab is built in memory first (no
    half-built paste state like the manual process), then clear + rewrite +
    formats in one call sequence.
    """
    result: dict[str, Any] = {
        "tab": tab,
        "matrix_rows": len(matrix),
        "data_rows": max(0, len(matrix) - 1),
        "wrote": False,
    }
    if dry_run:
        # Placeholder sheet id: dry-run only counts/plans the requests;
        # no request is ever sent.
        format_requests = build_format_requests(matrix, 0, config)
        result["format_requests_planned"] = len(format_requests)
        result["format_sheet_id_placeholder"] = True
        print(f"DRY: would write tab {tab!r}: {len(matrix)} rows "
              f"({result['data_rows']} data), {len(format_requests)} format requests — NO WRITES")
        result["dry_run"] = True
        return result

    ensured = ensure_week_tab(service, spreadsheet_id, tab, dry_run=False)
    sheet_id = ensured["sheet_id"]
    if not ensured["created"] and tab_has_data(service, spreadsheet_id, tab):
        if not force:
            raise RefuseOverwriteError(
                f"tab {tab!r} already holds data; refusing to overwrite without "
                "--force (idempotency, X3). Re-run with --force to rebuild it."
            )

    format_requests = build_format_requests(matrix, sheet_id, config)
    result["format_requests_planned"] = len(format_requests)

    service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id, range=f"'{tab}'!A:ZZ", body={}
    ).execute()
    resp = (
        service.spreadsheets()
        .values()
        .update(
            spreadsheetId=spreadsheet_id,
            range=f"'{tab}'!A1",
            valueInputOption="RAW",
            body={"values": matrix},
        )
        .execute()
    )
    result["wrote"] = True
    result["updated_cells"] = resp.get("updatedCells")
    result["updated_range"] = resp.get("updatedRange")
    if format_requests:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id, body={"requests": format_requests}
        ).execute()
        result["formats_applied"] = len(format_requests)

    # Read-back: the written tab's row count must match the matrix (X3 health).
    rb = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=f"'{tab}'!A1:A")
        .execute()
    )
    read_rows = len(rb.get("values") or [])
    result["readback_rows"] = read_rows
    if read_rows != len(matrix):
        raise RuntimeError(
            f"write read-back failed: wrote {len(matrix)} rows, tab shows "
            f"{read_rows}; fail closed, investigate before re-running"
        )
    return result
