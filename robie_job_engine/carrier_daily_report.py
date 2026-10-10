"""Daily carrier pull report: status-sheet tab per day, one row per PDF (Carlo 2026-10-10).

After the daily pull uploads PDFs to ``Document Retrieval/<YYYY-MM-DD>/``,
this step:

1. copies the ``TEMPLATE`` tab of the Document Retrieval Status Sheet to a new
   tab for the day (``10/12.``, the title the filing stage already uses) when
   that tab is not there yet;
2. adds one row per uploaded PDF under its carrier's section: Insured Name,
   Policy Number, Department, Document Type, Memo Date, and a Comment that
   links to the PDF in Drive;
3. looks the policy number up through the EZLynx API (read only) and says in
   the Comment whether exactly one client has it. Zero or several matches are
   left for a person. Nothing is filed to EZLynx here.

A rerun updates the same row (same policy, document type, and memo date)
instead of adding a second one. Cells are written RAW; only the Comment cell
is a formula, and its text is quoted.
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .carrier_qa_drive import DRIVE_LEDGER_NAME, CarrierDriveLedger, drive_file_link
from .document_retrieval_filing import (
    STATUS_SHEET_ID,
    SHEETS_SCOPE,
    _INSURED_KEYS,
    _applicants_for_policy,
    plan_status_sheet_edit,
    status_tab_title,
)

TEMPLATE_TAB = "TEMPLATE"

# Column A section labels in TEMPLATE, compared after collapsing whitespace.
SHEET_SECTION_LABELS: dict[str, tuple[str, ...]] = {
    "geico": ("geico",),
    "asi": ("asi",),
    "progressive": ("progressive",),
    "progressive_bop": ("progressive bop/cgl", "progressive bop"),
    "farmersofsalem": ("farmers of salem - (fos portal)", "farmers of salem"),
    "guard": ("guard",),
    "kingstone": ("kingstone",),
    "travelers": ("travelers",),
    "uticafirst": ("utica first (ufirst now)", "utica first"),
    "natgen": ("natgen",),
    "nbic": ("nbic",),
    "safeco": ("safeco",),
    "universal": ("universal property",),
}

_DOC_TYPE_KEYS = ("document_type", "doc_type", "notice_type", "type")
_MEMO_DATE_KEYS = ("memo_date", "issued_date", "issued", "processed_date")
_DEPARTMENTS = {"personal": "Personal Lines", "commercial": "Commercial Lines"}

Matcher = Callable[[str], dict[str, Any]]


def _norm(value: object) -> str:
    return " ".join(str(value or "").split()).casefold()


def section_label(rows: list[list[str]], carrier: str) -> str | None:
    """The exact column A text of ``carrier``'s section in this tab, or None."""
    wanted = SHEET_SECTION_LABELS.get(carrier, ())
    for index, row in enumerate(rows):
        cell = str(row[0]) if row else ""
        if index == 0 and len(row) > 1 and _norm(row[1]) == "insured name":
            continue
        if _norm(cell) in wanted:
            return cell.strip()
    return None


def _first(entry: Mapping[str, Any], keys: Iterable[str]) -> str:
    return next((str(entry.get(k)).strip() for k in keys if str(entry.get(k) or "").strip()), "")


def _department(entry: Mapping[str, Any]) -> str:
    raw = _first(entry, ("department",))
    if raw:
        return raw
    return _DEPARTMENTS.get(_first(entry, ("line",)).casefold(), "")


def pack_items(carrier: str, pack: Path, ledger: Mapping[str, Any]) -> list[dict[str, str]]:
    """One item per pulled PDF in today's pack that is in Drive (per the ledger).

    Reads every ``manifest.json`` under the pack (``sources/`` excluded). Only
    entries with a policy number and a file on disk count. A field the
    manifest does not have stays blank; nothing is guessed.
    """
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    ledger_items = ledger.get("items") or {}
    if not pack.is_dir():
        return items
    for manifest_path in sorted(pack.rglob("manifest.json")):
        rel_dir = manifest_path.parent.relative_to(pack)
        if rel_dir.parts and rel_dir.parts[0] == "sources":
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(manifest, dict):
            continue
        fallback_date = _first(manifest, ("processed_date", "report_date")) or (
            manifest_path.parent.name if manifest_path.parent != pack else ""
        )
        for value in manifest.values():
            if not isinstance(value, list):
                continue
            for entry in value:
                if not isinstance(entry, dict):
                    continue
                filename = str(entry.get("filename") or "").strip()
                policy = str(entry.get("policy_number") or "").strip()
                if not filename or not policy:
                    continue
                if str(entry.get("disposition") or "pulled").strip().lower() != "pulled":
                    continue
                path = manifest_path.parent / filename
                if not path.is_file():
                    continue
                key = "/".join(path.relative_to(pack).parts)
                if key in seen:
                    continue
                seen.add(key)
                drive = ledger_items.get(key) or {}
                file_id = str(drive.get("drive_file_id") or "")
                items.append({
                    "carrier": carrier,
                    "key": key,
                    "insured_name": _first(entry, _INSURED_KEYS),
                    "policy_number": policy,
                    "department": _department(entry),
                    "document_type": _first(entry, _DOC_TYPE_KEYS),
                    "memo_date": _first(entry, _MEMO_DATE_KEYS) or fallback_date,
                    "drive_link": drive_file_link(file_id) if file_id else "",
                })
    return items


def match_policy(search: Callable[[str], Any], policy_number: str) -> dict[str, Any]:
    """EZLynx client(s) on this policy number. Read only. Never raises."""
    try:
        applicants = _applicants_for_policy(search(policy_number), policy_number)
    except Exception as exc:  # noqa: BLE001 - a lookup failure is a row for a person
        return {"status": "ERROR", "applicants": [], "reason": type(exc).__name__}
    if len(applicants) == 1:
        return {"status": "MATCHED", "applicants": applicants}
    return {"status": "NONE" if not applicants else "MULTIPLE", "applicants": applicants}


def match_text(match: Mapping[str, Any] | None) -> str:
    if not match:
        return "EZLynx not checked"
    status = match.get("status")
    if status == "MATCHED":
        return f"EZLynx client {match['applicants'][0]} has this policy (not filed yet)"
    if status == "MULTIPLE":
        return f"{len(match['applicants'])} EZLynx clients have this policy, needs a person"
    if status == "NONE":
        return "No EZLynx client has this policy, needs a person"
    return "EZLynx lookup failed, needs a person"


def _formula_text(text: str) -> str:
    return '"' + text.replace('"', '""') + '"'


def comment_cell(link: str, text: str) -> str:
    label = f"PDF in Drive. {text}" if link else f"Not in Drive. {text}"
    if not link:
        return label
    return f"=HYPERLINK({_formula_text(link)},{_formula_text(label)})"


def tab_link(sheet_id: int) -> str:
    return f"https://docs.google.com/spreadsheets/d/{STATUS_SHEET_ID}/edit#gid={sheet_id}"


class DailyStatusSheet:
    """Status Sheet client for the daily tab. Copies TEMPLATE; never deletes."""

    def __init__(self, service: Any, spreadsheet_id: str = STATUS_SHEET_ID) -> None:
        self._service = service
        self._id = spreadsheet_id

    def _sheets(self) -> list[dict[str, Any]]:
        meta = self._service.spreadsheets().get(spreadsheetId=self._id).execute()
        return [s.get("properties") or {} for s in meta.get("sheets") or []]

    def ensure_tab(self, day: date) -> tuple[str, int, bool]:
        """(title, sheetId, created) for the day's tab, copying TEMPLATE when needed."""
        title = status_tab_title(day)
        sheets = self._sheets()
        for props in sheets:
            if props.get("title") == title:
                return title, int(props["sheetId"]), False
        template = next((p for p in sheets if p.get("title") == TEMPLATE_TAB), None)
        if template is None:
            raise RuntimeError(f"Status sheet has no {TEMPLATE_TAB} tab")
        reply = self._service.spreadsheets().batchUpdate(
            spreadsheetId=self._id,
            body={"requests": [{"duplicateSheet": {
                "sourceSheetId": int(template["sheetId"]),
                "insertSheetIndex": int(template.get("index") or 0) + 1,
                "newSheetName": title,
            }}]},
        ).execute()
        props = reply["replies"][0]["duplicateSheet"]["properties"]
        return title, int(props["sheetId"]), True

    def read(self, tab: str) -> list[list[str]]:
        result = self._service.spreadsheets().values().get(
            spreadsheetId=self._id, range=f"'{tab}'!A1:G500",
        ).execute()
        return result.get("values") or []

    def insert_row(self, sheet_id: int, before_index: int) -> None:
        self._service.spreadsheets().batchUpdate(
            spreadsheetId=self._id,
            body={"requests": [{"insertDimension": {
                "range": {"sheetId": sheet_id, "dimension": "ROWS",
                          "startIndex": before_index, "endIndex": before_index + 1},
                "inheritFromBefore": True,
            }}]},
        ).execute()

    def write_row(self, tab: str, row_number: int, values: list[str], comment: str) -> None:
        values_api = self._service.spreadsheets().values()
        values_api.update(
            spreadsheetId=self._id, range=f"'{tab}'!A{row_number}:F{row_number}",
            valueInputOption="RAW", body={"values": [values[:6]]},
        ).execute()
        values_api.update(
            spreadsheetId=self._id, range=f"'{tab}'!G{row_number}",
            valueInputOption="USER_ENTERED", body={"values": [[comment]]},
        ).execute()


def write_items(sheet: DailyStatusSheet, tab: str, sheet_id: int, items: list[dict[str, Any]]) -> dict[str, Any]:
    """Write each item under its carrier section. Returns counts and held rows."""
    written, held = 0, []
    for item in items:
        rows = sheet.read(tab)
        label = section_label(rows, item["carrier"])
        if label is None:
            held.append({"key": item["key"], "reason": f"no {item['carrier']} section in the tab"})
            continue
        comment = comment_cell(item.get("drive_link", ""), match_text(item.get("match")))
        edit = plan_status_sheet_edit(
            rows, carrier=label, insured_name=item["insured_name"], policy_number=item["policy_number"],
            department=item["department"], document_type=item["document_type"],
            memo_date=item["memo_date"], comment=comment,
        )
        if edit.kind == "insert":
            sheet.insert_row(sheet_id, edit.row_index)
        sheet.write_row(tab, edit.row_index + 1, list(edit.values), comment)
        written += 1
    return {"written": written, "held": held}


def build_sheets_service() -> Any:
    """Sheets v4 client from this host's Drive token (robie@). Fails closed."""
    from .carrier_qa_drive import FALLBACK_TOKEN_ENV, TOKEN_ENV, require_upload_environment

    require_upload_environment()
    path = (os.environ.get(TOKEN_ENV) or os.environ.get(FALLBACK_TOKEN_ENV) or "").strip()
    if not path or not os.path.isfile(path):
        raise RuntimeError("Status sheet token is not configured on this host")
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    credentials = Credentials.from_authorized_user_file(path)
    if SHEETS_SCOPE not in set(credentials.scopes or ()):
        raise RuntimeError("Status sheet token lacks the Sheets scope")
    if not credentials.valid:
        credentials.refresh(Request())
    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def build_policy_search() -> Callable[[str], Any]:
    """EZLynx ``search_policy_by_number`` from this host's API config (read only)."""
    from .ezlynx_api import EzlynxApiClient, load_ezlynx_api_config

    return EzlynxApiClient(load_ezlynx_api_config()).search_policy_by_number


def run_report(
    *,
    day: date,
    root: Path,
    carriers: Iterable[str],
    sheet_factory: Callable[[], Any] | None = None,
    search_factory: Callable[[], Callable[[str], Any]] | None = None,
    match_clients: bool = True,
) -> dict[str, Any]:
    """Daily tab plus one row per uploaded PDF. Never raises."""
    items: list[dict[str, Any]] = []
    for carrier in carriers:
        carrier_root = root / "packs" / carrier
        try:
            ledger = CarrierDriveLedger(carrier_root).load() if (carrier_root / DRIVE_LEDGER_NAME).exists() else {}
        except Exception:  # noqa: BLE001 - an unreadable ledger only drops the links
            ledger = {}
        items.extend(pack_items(carrier, carrier_root / day.isoformat(), ledger))
    report: dict[str, Any] = {"status": "OK", "rows": len(items)}
    if match_clients and items:
        try:
            search = (search_factory or build_policy_search)()
        except Exception as exc:  # noqa: BLE001
            search = None
            report["match_error"] = type(exc).__name__
        cache: dict[str, dict[str, Any]] = {}
        for item in items:
            if search is None:
                item["match"] = {"status": "ERROR", "applicants": [], "reason": report["match_error"]}
                continue
            policy = item["policy_number"]
            if policy not in cache:
                cache[policy] = match_policy(search, policy)
            item["match"] = cache[policy]
    counts: dict[str, int] = {}
    for item in items:
        status = (item.get("match") or {}).get("status", "NOT_CHECKED")
        counts[status] = counts.get(status, 0) + 1
    report["matches"] = counts
    try:
        sheet = DailyStatusSheet((sheet_factory or build_sheets_service)())
        tab, sheet_id, created = sheet.ensure_tab(day)
        report.update(tab=tab, tab_link=tab_link(sheet_id), tab_created=created)
        report.update(write_items(sheet, tab, sheet_id, items))
    except Exception as exc:  # noqa: BLE001 - the sheet never stops the run
        report.update(status="HELD", reason=f"{type(exc).__name__}: {exc}"[:300])
    report["items"] = items
    return report


__all__ = [
    "DailyStatusSheet",
    "SHEET_SECTION_LABELS",
    "TEMPLATE_TAB",
    "comment_cell",
    "match_policy",
    "match_text",
    "pack_items",
    "run_report",
    "section_label",
    "write_items",
]
