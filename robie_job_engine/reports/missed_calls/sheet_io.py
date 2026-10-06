"""Google Sheets I/O for the Missed Calls Report 2026 workbook.

Auth reuses the proven repo pattern (publish_4359_liveness.py):
  ROBIE_GOOGLE_TOKEN_FILE -> authorized-user credentials, else ADC with the
  spreadsheets (+ drive, for title lookup) scopes.

WRITE SAFETY (M13/M14):
  * APPEND-ONLY. Existing rows are never modified or deleted. A phone number
    already present on the tab (column B, normalized) is skipped, so a
    human-filled row can never be overwritten and a number never appears twice
    from two runs.
  * Tab creation: when the dated tab does not exist, duplicate the most recent
    existing dated tab (M/D), rename it, and clear its data rows before
    writing -- per the process doc. When the tab already exists, rows are only
    appended.

UNVERIFIED (X1 answered, writes still untested): Sandeep confirmed the
canonical workbook on 2026-10-05
(https://docs.google.com/spreadsheets/d/1POQ9oAop1AOa6gWsaNVQX3I540WvvX0gmw9XNPK1L5Y/edit),
but every Sheets method here has still never run against the real workbook.
--dry-run (the default) never touches Sheets at all.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any, Optional

from . import date_rules
from .models import SheetRow
from .normalize import normalize_phone
from .rows import row_values

SHEETS_WRITE_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"

SPREADSHEET_TITLE = "Missed Calls Report 2026"

# X1 LOCKED 2026-10-05 (Sandeep): the canonical workbook.
CANONICAL_SPREADSHEET_ID = "1POQ9oAop1AOa6gWsaNVQX3I540WvvX0gmw9XNPK1L5Y"

HEADER = ["Department call received", "Phone Number", "Profile", "Was addressed?", "Updated by"]


class SheetError(RuntimeError):
    """Fail-closed Sheets error."""


def _service(drive: bool = False) -> Any:
    """Build a Sheets (and optionally Drive) API client. Lazy imports."""
    import google.auth
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    scopes = [SHEETS_WRITE_SCOPE] + ([DRIVE_READONLY_SCOPE] if drive else [])
    token_file = os.environ.get("ROBIE_GOOGLE_TOKEN_FILE", "").strip()
    if token_file:
        creds = Credentials.from_authorized_user_file(token_file, scopes=scopes)
    else:
        creds, _ = google.auth.default(scopes=scopes)
        refresh = getattr(creds, "refresh", None)
        if callable(refresh) and not getattr(creds, "valid", True):
            refresh(Request())
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    if not drive:
        return sheets
    drive_svc = build("drive", "v3", credentials=creds, cache_discovery=False)
    return sheets, drive_svc


class SheetIO:
    """Append-only writer for the dated tabs. dry_run=True writes nothing."""

    def __init__(
        self,
        spreadsheet_id: Optional[str] = None,
        spreadsheet_title: str = SPREADSHEET_TITLE,
        dry_run: bool = True,
    ) -> None:
        self.spreadsheet_id = spreadsheet_id
        self.spreadsheet_title = spreadsheet_title
        self.dry_run = dry_run
        self._sheets: Any = None
        self._drive: Any = None
        self._tabs_cache: Optional[list[dict[str, Any]]] = None

    # -- plumbing ------------------------------------------------------

    def _clients(self) -> tuple[Any, Any]:
        if self._sheets is None:
            self._sheets, self._drive = _service(drive=True)
        return self._sheets, self._drive

    def resolve_spreadsheet_id(self) -> str:
        """Spreadsheet id: explicit flag wins, else Drive title lookup."""
        if self.spreadsheet_id:
            return self.spreadsheet_id
        _, drive = self._clients()
        # Title lookup is a fallback (the canonical id is now the default);
        # this lookup path has not run against the real Drive.
        query = (
            f"name = '{self.spreadsheet_title}' "
            "and mimeType = 'application/vnd.google-apps.spreadsheet' "
            "and trashed = false"
        )
        resp = (
            drive.files()
            .list(q=query, fields="files(id, name)", pageSize=10)
            .execute()
        )
        files = resp.get("files", [])
        if not files:
            raise SheetError(
                f"spreadsheet titled {self.spreadsheet_title!r} not found in Drive"
            )
        if len(files) > 1:
            ids = ", ".join(f["id"] for f in files)
            raise SheetError(
                f"multiple spreadsheets titled {self.spreadsheet_title!r} "
                f"({ids}); pass --spreadsheet-id explicitly"
            )
        self.spreadsheet_id = files[0]["id"]
        return self.spreadsheet_id

    def _tabs(self) -> list[dict[str, Any]]:
        if self._tabs_cache is None:
            sheets, _ = self._clients()
            sid = self.resolve_spreadsheet_id()
            meta = (
                sheets.spreadsheets()
                .get(spreadsheetId=sid, fields="sheets.properties")
                .execute()
            )
            self._tabs_cache = [
                s["properties"] for s in meta.get("sheets", []) if "properties" in s
            ]
        return self._tabs_cache

    def _tab_id(self, title: str) -> Optional[int]:
        for props in self._tabs():
            if props.get("title") == title:
                return props.get("sheetId")
        return None

    def _most_recent_dated_tab(self, year_hint: int) -> Optional[tuple[str, int]]:
        """(title, sheetId) of the latest M/D tab, or None."""
        best: Optional[tuple[date, str, int]] = None
        for props in self._tabs():
            title = props.get("title", "")
            parsed = date_rules.parse_tab_name(title, year_hint)
            if parsed is None:
                continue
            candidate = (parsed, title, props.get("sheetId"))
            if best is None or candidate[0] > best[0]:
                best = candidate
        return (best[1], best[2]) if best else None

    # -- read existing rows (append-only guard) --------------------------

    def existing_phones(self, tab: str) -> set[str]:
        """Normalized phone digits already on the tab (column B)."""
        sheets, _ = self._clients()
        sid = self.resolve_spreadsheet_id()
        resp = (
            sheets.spreadsheets()
            .values()
            .get(spreadsheetId=sid, range=f"'{tab}'!B2:B")
            .execute()
        )
        phones: set[str] = set()
        for row in resp.get("values", []):
            if row:
                digits = normalize_phone(row[0])
                if digits:
                    phones.add(digits)
        return phones

    # -- tab creation ----------------------------------------------------

    def ensure_tab(self, tab: str, year_hint: int) -> str:
        """Ensure the dated tab exists; create it from the most recent date tab.

        Returns "exists" or "created".
        """
        if self._tab_id(tab) is not None:
            return "exists"
        source = self._most_recent_dated_tab(year_hint)
        if source is None:
            raise SheetError(
                f"tab {tab!r} missing and no dated tab to duplicate "
                "(refusing to create a blank tab; M14)"
            )
        src_title, src_id = source
        sheets, _ = self._clients()
        sid = self.resolve_spreadsheet_id()
        copied = (
            sheets.spreadsheets()
            .sheets()
            .copyTo(spreadsheetId=sid, sheetId=src_id, body={"destinationSpreadsheetId": sid})
            .execute()
        )
        new_id = copied.get("sheetId")
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=sid,
            body={
                "requests": [
                    {
                        "updateSheetProperties": {
                            "properties": {"sheetId": new_id, "title": tab},
                            "fields": "title",
                        }
                    }
                ]
            },
        ).execute()
        # Clear data rows (keep the header) per the process doc.
        sheets.spreadsheets().values().clear(
            spreadsheetId=sid, range=f"'{tab}'!A2:Z"
        ).execute()
        self._tabs_cache = None  # tab list changed
        return "created"

    # -- append ----------------------------------------------------------

    def append_rows(self, tab: str, rows: list[SheetRow]) -> int:
        """Append rows; returns the number of rows written (0 in dry-run)."""
        if not rows:
            return 0
        if self.dry_run:
            return 0
        sheets, _ = self._clients()
        sid = self.resolve_spreadsheet_id()
        body = {"values": [row_values(r) for r in rows]}
        sheets.spreadsheets().values().append(
            spreadsheetId=sid,
            range=f"'{tab}'!A:E",
            valueInputOption="USER_ENTERED",  # lets HYPERLINK formulas resolve
            insertDataOption="INSERT_ROWS",
            body=body,
        ).execute()
        return len(rows)
