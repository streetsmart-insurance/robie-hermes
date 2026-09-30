"""Test-only Progressive Businessowner/Contractor GL pending-cancel pull.

Manual path (Nicole, not certified against a live DOM): For Agents Only login,
Manage Policies, Businessowner/Contractor GL in a new window, View Reports,
Pending Cancel for Nonpayment. For each policy on that report, search it on
the FAO shell, open Documents → Policy, and save the Notice of Non Payment
whose date matches the report date as ``[Policy Number] - NOC - Non Payment.pdf``.

A blank report writes the screenshot and an empty pack (zero PDFs). A listed
policy with no matching notice is HELD on that row; other policies continue.
The pack is PULLED only when a PNG exists and every listed policy has one
saved NOC. This module does not log in, submit OTP, upload to EZLynx, file
notes, create tasks, deploy, or install a timer.

Agent context is the shared FAO rule: literal ``CA33617``, ``(33617)``, bare
``33617``, and login ``33617c`` are one StreetSmart agency. Any other agency
holds. If the attached tab is not FAO Home / Manage Policies Home —
``/``, ``/home``, ``/managepolicies`` or ``/landingpages/managepolicies``
(optional ``/home``, optional trailing slash) — for example Communications
/ underwritinglegacy, the pull clicks the existing Manage Policies Home
header control before that assert. Already on one of those URLs does not
click that Home control. That step does not ask Gemini. A missing or
ambiguous Home control holds.

When the shell was already on that Home, the later Manage Policies click is
skipped. The hidden header control's accessible name is ``Manage Policies
Home``, and exact ``Manage Policies`` is not on the landing. The pull then
opens one new window from the one exact link or button named
``Businessowner/Contractor GL`` or ``Go to Businessowner/Contractor GL
policy search``. Zero or several of those names hold. A tab that was not
already on Home still clicks Manage Policies, then exact
``Businessowner/Contractor GL``, after Home opens.

The window that click opens is often a dead For Agents Only HPLanding page
whose only control is ``Close this window``. The Businessowner application
itself is ``https://bop.americanstrategic.com``. The pull attaches to that
application page, or to the one frame on that host inside the landing window.
It closes the landing page when the application is a different page. A failed
close does not hold. HPLanding with no BOP application holds, and report
controls are not clicked there.

After the application is attached, the pull waits until a known reports
control is visible, or until the network is idle. It does not sleep a fixed
interval, and it does not look for controls before that. An older page still
uses ``View Reports`` / ``VIEW REPORTS`` and then ``Pending Cancel for
Nonpayment``. The reports page seen on hermes-test-01 on 2026-09-30 has no
View Reports control. It has ``Export Pending Cancel for Non-Payment Pdf``
and ``Export Pending Cancel for Non-Payment Xls``. The PDF export is tried
first. Before that download, the pull sets the report range. A live check
of the earlier select-based attempt held: there is no combobox named
Report Dates. The control is a dropdown button whose text is ``Select
Date Range`` (``#dropdownMenu2`` only when that button name is absent and
the id is unique). The pull opens that menu, fills ``.report-start`` and
``.report-end`` with the process date in the format the input type needs,
clicks Apply, and reads the values or the button text back. It waits for
the network to go idle, then exports. A missing control, a value that does
not stick, or a page that does not finish loading holds, and the export
is not downloaded. A list PDF yields insured, policy number, and cancel
date. An empty or truncated download is a hold, not an empty report. This
click only downloads a report. It does not bind, cancel, or move money.
This module never files to EZLynx.

Accessible names are the playbook, not a certified live DOM. Zero or multiple
matches hold. Live FAO on hermes-test-01 is UNVERIFIED.
"""
from __future__ import annotations

import argparse
import ast
import base64
import csv
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.parse
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, time
from html.parser import HTMLParser
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .intake_core import IntakeHold, SourceArchive, SourceItem, require_test
from .progressive_agent_context import (
    DEFAULT_AGENT_CODE,
    agent_codes_in_text,
    assert_agent_context,
    require_agent_code,
)
from .progressive_retrieval import SCOPES


BOP_SCOPE = "bop_pending_cancel_nonpayment"
DEFAULT_CDP_URL = "http://127.0.0.1:9222"
DOWNLOAD_TIMEOUT_MS = 8000
# expect_popup returns the HPLanding window as soon as it opens. The BOP
# application at https://bop.americanstrategic.com/ shows up after that.
BOP_APP_ATTACH_TIMEOUT_MS = 20000
# How long to wait for a reports control to become visible, or for the
# network to go idle. This is a Playwright wait budget, not a sleep.
BOP_REPORTS_READY_TIMEOUT_MS = 20000
# Report exports are larger than one notice. save_as still waits until the
# browser finishes the file; this is only the start/finish budget.
BOP_EXPORT_TIMEOUT_MS = 30000
LEDGER_NAME = "bop-noc-ledger.json"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
DEFAULT_OUTPUT_ROOT = Path(
    "/opt/streetsmart-hermes-test/robie-job-engine/data/artifacts/carrier-pull-qa/progressive-bop"
)
# Shared Drive "Robie Carrier Pull QA (Nicole)". The Progressive BOP child is
# created on upload. Folder upload is TODO: GoogleDriveUploader writes one
# video/webm into a known folder and is not a QA-folder helper.
DRIVE_QA_PARENT_ID = "1cLEpR-0T6KdiVjcdAr0qpGTO447MetI2"
DRIVE_BOP_CHILD_NAME = "Progressive BOP"
DRIVE_QA_FOLDER_NAME = "Robie Carrier Pull QA (Nicole)/Progressive BOP"
DRIVE_UPLOAD_UNAVAILABLE = (
    "Drive upload of the Progressive BOP QA pack is not available; "
    "GoogleDriveUploader writes one video/webm and does not create the "
    "Progressive BOP child under parent "
    f"{DRIVE_QA_PARENT_ID}. Refusing to report the pack as uploaded"
)
_POLICY_NUMBER = re.compile(r"^\d{6,12}$")
_POLICY_LINE = re.compile(
    r"policy(?:\s*(?:number|no\.?|#))?\s*[:#-]?\s*(\d{6,12})(?!\d)",
    re.IGNORECASE,
)
_ROW_START = re.compile(r"^(\d{6,12})(?!\d)\b")
_DATE_IN_TEXT = re.compile(r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})\b")
_REPORT_DATE_LABEL = re.compile(
    r"report date\s*[:\-]?\s*(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})",
    re.IGNORECASE,
)
_BLANK_TEXT = re.compile(
    r"\b(no (?:policies|policy|records|record|data|results)|none found|0 policies)\b",
    re.IGNORECASE,
)
_NOTICE = re.compile(r"notice of non[-\s]?payment", re.IGNORECASE)
_REMOTE_PDF = re.compile(r"https?://[^\s\"'<>]+?\.pdf(?:\?[^\s\"'<>]*)?", re.IGNORECASE)
_EASTERN = ZoneInfo("America/New_York")

_POLICY_HEADERS = frozenset({
    "policy number", "policy #", "policy", "policy no", "policy no.",
})
_INSURED_HEADERS = frozenset({
    "insured", "insured name", "named insured",
})
_ROW_DATE_HEADERS = frozenset({
    "report date", "cancel date", "pending cancel date", "notice date",
    "nonpayment date", "transaction date", "date",
})
_DOC_NAME_HEADERS = frozenset({
    "document", "document name", "description", "form", "title", "name",
})
_DOC_DATE_HEADERS = frozenset({
    "date", "document date", "processed date", "created", "created date", "notice date",
})
# Live 2026-09-30 accessible names on the BOP reports page. The older generic
# names stay as fallbacks. Pdf is preferred over Xls.
_PENDING_CANCEL_PDF_EXPORT = "Export Pending Cancel for Non-Payment Pdf"
_PENDING_CANCEL_XLS_EXPORT = "Export Pending Cancel for Non-Payment Xls"
_EXCEL_EXPORTS = (
    _PENDING_CANCEL_XLS_EXPORT,
    "Excel",
    "Export to Excel",
    "Download Excel",
    "Export Excel",
)
_PDF_EXPORTS = (
    _PENDING_CANCEL_PDF_EXPORT,
    "PDF",
    "Export to PDF",
    "Download PDF",
    "Export PDF",
)
_PENDING_CANCEL_NAV = "Pending Cancel for Nonpayment"
_XLSX_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_OLE_MAGIC = b"\xd0\xcf\x11\xe0"
_SEARCH_FIELDS = (
    ("searchbox", "Search"),
    ("textbox", "Policy number"),
    ("textbox", "Policy Number"),
    ("searchbox", "Policy number"),
    ("textbox", "Search"),
)

_BLOB_JS = """async (url) => {
  const response = await fetch(url);
  const bytes = new Uint8Array(await response.arrayBuffer());
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}"""


class RowHold(IntakeHold):
    """This policy row is held. Other listed policies may still be pulled."""


@dataclass(frozen=True)
class PendingCancelPolicy:
    policy_number: str
    insured_name: str
    report_date: date

    @property
    def document_id(self) -> str:
        return f"bop-noc:{self.policy_number}:{self.report_date.isoformat()}"

    @property
    def filename(self) -> str:
        return noc_filename(self.policy_number)


@dataclass(frozen=True)
class PendingCancelReport:
    report_date: date
    observed_date: date | None
    policies: tuple[PendingCancelPolicy, ...]
    source: str
    blank: bool


@dataclass(frozen=True)
class ReportCapture:
    png: bytes
    report: PendingCancelReport | None
    error: str | None


@dataclass(frozen=True)
class PolicyDocument:
    name: str
    document_date: date | None
    row_index: int


@dataclass(frozen=True)
class PagePdfView:
    url: str
    pdfs: tuple[bytes, ...]


@dataclass(frozen=True)
class PdfObservation:
    downloads: tuple[bytes, ...]
    pages: tuple[PagePdfView, ...]


@dataclass(frozen=True)
class PolicyOutcome:
    policy_number: str
    insured_name: str
    document_id: str
    filename: str
    disposition: str
    reason: str | None = None
    sha256: str | None = None
    byte_count: int | None = None


def noc_filename(policy_number: str) -> str:
    policy = str(policy_number or "").strip()
    if not _POLICY_NUMBER.fullmatch(policy):
        raise IntakeHold("Pending Cancel policy number is missing or ambiguous")
    name = f"{policy} - NOC - Non Payment.pdf"
    if name != Path(name).name:
        raise IntakeHold("Pending Cancel policy number is missing or ambiguous")
    return name


def parse_report_date(value: str) -> date:
    raw = _norm(value)
    if " " in raw and ":" in raw:
        raw = raw.split()[0]
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            return date.fromisoformat(raw)
        if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", raw):
            return datetime.strptime(raw, "%m/%d/%Y").date()
    except ValueError as exc:
        raise IntakeHold("Pending Cancel report date is missing or ambiguous") from exc
    raise IntakeHold("Pending Cancel report date is missing or ambiguous")


def _header_indexes(headers: tuple[str, ...], *, kind: str) -> dict[str, int] | None:
    labels = tuple(_norm(header).casefold().rstrip(".") for header in headers)
    if not any(labels):
        return None
    nonempty = [label for label in labels if label]
    if len(nonempty) != len(set(nonempty)):
        return None
    if kind == "report":
        mapping = (("policy_number", _POLICY_HEADERS), ("insured_name", _INSURED_HEADERS), ("row_date", _ROW_DATE_HEADERS))
        required = "policy_number"
    elif kind == "documents":
        mapping = (("name", _DOC_NAME_HEADERS), ("document_date", _DOC_DATE_HEADERS))
        required = "name"
    else:
        raise IntakeHold("Pending Cancel report table is missing or ambiguous")
    indexes: dict[str, int] = {}
    for index, label in enumerate(labels):
        fields = [name for name, aliases in mapping if label in aliases]
        if len(fields) > 1:
            raise IntakeHold("Pending Cancel report table is missing or ambiguous")
        if not fields:
            continue
        if fields[0] in indexes:
            raise IntakeHold("Pending Cancel report table is missing or ambiguous")
        indexes[fields[0]] = index
    if required not in indexes:
        return None
    return indexes


def _cell_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    return _norm(str(value))


def _preamble_dates(rows: tuple[tuple[str, ...], ...] | list[tuple[str, ...]], header_at: int | None) -> set[date]:
    region = list(rows) if header_at is None else list(rows)[:header_at]
    found: set[date] = set()
    for row in region:
        joined = " ".join(row)
        for match in _REPORT_DATE_LABEL.findall(joined):
            found.add(parse_report_date(match))
        labels = [cell.casefold().rstrip(":") for cell in row]
        if "report date" in labels:
            index = labels.index("report date")
            if index + 1 < len(row) and row[index + 1]:
                found.add(parse_report_date(row[index + 1]))
    return found


def parse_report_rows(
    rows: tuple[tuple[Any, ...], ...] | list[tuple[Any, ...]],
    *,
    report_date: date,
    source: str,
) -> PendingCancelReport:
    materialized = [tuple(_cell_str(cell) for cell in row) for row in rows]
    while materialized and not any(materialized[-1]):
        materialized.pop()
    header_at = None
    for index, row in enumerate(materialized[:30]):
        if _header_indexes(row, kind="report") is not None:
            header_at = index
            break
    observed = _preamble_dates(materialized, header_at)
    if len(observed) > 1:
        raise IntakeHold("Pending Cancel report date is missing or ambiguous")
    observed_date = next(iter(observed)) if observed else None
    if observed_date is not None and observed_date != report_date:
        raise IntakeHold(
            "Pending Cancel report date "
            f"{observed_date.isoformat()} does not match {report_date.isoformat()}"
        )
    if header_at is None:
        blob = " ".join(" ".join(row) for row in materialized)
        if not _norm(blob) or _BLANK_TEXT.search(blob):
            return _blank_report(report_date, observed_date, source)
        raise IntakeHold("Pending Cancel report policies are missing or ambiguous")
    indexes = _header_indexes(materialized[header_at], kind="report")
    if indexes is None:
        raise IntakeHold("Pending Cancel report policies are missing or ambiguous")
    policies: list[PendingCancelPolicy] = []
    for row in materialized[header_at + 1:]:
        if not any(row):
            continue
        policy = row[indexes["policy_number"]] if indexes["policy_number"] < len(row) else ""
        if not policy:
            continue
        if not _POLICY_NUMBER.fullmatch(policy):
            raise IntakeHold("Pending Cancel policy number is missing or ambiguous")
        insured = ""
        if "insured_name" in indexes and indexes["insured_name"] < len(row):
            insured = row[indexes["insured_name"]]
        if "row_date" in indexes and indexes["row_date"] < len(row):
            raw_date = row[indexes["row_date"]]
            if not raw_date or parse_report_date(raw_date) != report_date:
                raise IntakeHold("Pending Cancel report date does not match the requested date")
        policies.append(PendingCancelPolicy(policy, insured, report_date))
    if len({item.policy_number for item in policies}) != len(policies):
        raise IntakeHold("Pending Cancel policy number is missing or ambiguous")
    return PendingCancelReport(
        report_date=report_date,
        observed_date=observed_date,
        policies=tuple(policies),
        source=source,
        blank=not policies,
    )


def _blank_report(report_date: date, observed_date: date | None, source: str) -> PendingCancelReport:
    return PendingCancelReport(
        report_date=report_date,
        observed_date=observed_date,
        policies=(),
        source=source,
        blank=True,
    )


def _empty_export_hold(label: str) -> IntakeHold:
    return IntakeHold(
        f"Pending Cancel export {label!r} downloaded an empty file (0 bytes). "
        "An empty download is not a report with no policies."
    )


def _truncated_export_hold(label: str) -> IntakeHold:
    return IntakeHold(
        f"Pending Cancel export {label!r} is truncated. "
        "A partial download is not a report with no policies."
    )


def _unfinished_export_hold(label: str, detail: str = "") -> IntakeHold:
    suffix = f" ({detail})" if detail else ""
    return IntakeHold(
        f"Pending Cancel export {label!r} did not finish downloading{suffix}. "
        "An unfinished download is not a report with no policies."
    )


def _assert_export_bytes(blob: bytes | bytearray | None, *, label: str) -> bytes:
    """Reject an empty or cut-off export. Never turn that file into 'no docs'."""
    if not isinstance(blob, (bytes, bytearray)) or len(blob) == 0:
        raise _empty_export_hold(label)
    raw = bytes(blob)
    if raw.startswith(b"%PDF") and b"%%EOF" not in raw:
        raise _truncated_export_hold(label)
    if raw.startswith(b"PK"):
        try:
            with zipfile.ZipFile(BytesIO(raw)) as zipped:
                bad = zipped.testzip()
        except zipfile.BadZipFile as exc:
            raise _truncated_export_hold(label) from exc
        if bad is not None:
            raise _truncated_export_hold(label)
    if raw.startswith(_OLE_MAGIC) and len(raw) < 512:
        raise _truncated_export_hold(label)
    sample = raw.lstrip()[:16].lower()
    if sample.startswith(b"<") and b"</table>" not in raw.lower() and b"</html>" not in raw.lower():
        raise _truncated_export_hold(label)
    return raw


def _column_index(letters: str) -> int:
    index = 0
    for char in letters:
        index = index * 26 + (ord(char) - 64)
    return index


def _xlsx_sheet_rows(blob: bytes) -> list[tuple[str, ...]]:
    """Read the one populated worksheet from an xlsx zip. No openpyxl."""
    ns = {"m": _XLSX_NS}
    text_tag = f"{{{_XLSX_NS}}}t"
    try:
        with zipfile.ZipFile(BytesIO(blob)) as zipped:
            names = set(zipped.namelist())
            sheets = sorted(
                name for name in names
                if name.startswith("xl/worksheets/") and name.endswith(".xml")
            )
            if not sheets:
                raise IntakeHold("Pending Cancel report workbook is missing or ambiguous")
            shared: list[str] = []
            if "xl/sharedStrings.xml" in names:
                root = ET.fromstring(zipped.read("xl/sharedStrings.xml"))
                for item in root.findall("m:si", ns):
                    shared.append("".join(node.text or "" for node in item.iter(text_tag)))
            populated: list[list[tuple[str, ...]]] = []
            for sheet_name in sheets:
                root = ET.fromstring(zipped.read(sheet_name))
                grid: dict[int, dict[int, str]] = {}
                for cell in root.findall(".//m:c", ns):
                    ref = str(cell.attrib.get("r") or "")
                    found = re.fullmatch(r"([A-Z]+)(\d+)", ref)
                    if not found:
                        continue
                    column = _column_index(found.group(1))
                    row_index = int(found.group(2))
                    kind = cell.attrib.get("t")
                    value_node = cell.find("m:v", ns)
                    if kind == "s" and value_node is not None and value_node.text:
                        value = shared[int(value_node.text)]
                    elif kind == "inlineStr":
                        value = "".join(node.text or "" for node in cell.iter(text_tag))
                    elif value_node is not None and value_node.text:
                        value = value_node.text
                    else:
                        value = ""
                    grid.setdefault(row_index, {})[column] = _norm(value)
                if not grid:
                    continue
                width = max(max(columns) for columns in grid.values())
                rows: list[tuple[str, ...]] = []
                for row_index in range(1, max(grid) + 1):
                    columns = grid.get(row_index, {})
                    rows.append(tuple(columns.get(column, "") for column in range(1, width + 1)))
                while rows and not any(rows[-1]):
                    rows.pop()
                if rows:
                    populated.append(rows)
    except IntakeHold:
        raise
    except Exception as exc:
        raise IntakeHold("Pending Cancel report workbook is missing or ambiguous") from exc
    if len(populated) != 1:
        raise IntakeHold("Pending Cancel report workbook is missing or ambiguous")
    return populated[0]


def _parse_xlsx_zip(blob: bytes, *, report_date: date) -> PendingCancelReport:
    return parse_report_rows(_xlsx_sheet_rows(blob), report_date=report_date, source="excel")


class _HtmlTableParser(HTMLParser):
    """Collect top-level HTML tables. Export-to-Xls from older sites is often HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[tuple[str, ...]]] = []
        self._depth = 0
        self._table: list[tuple[str, ...]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "table":
            if self._depth == 0:
                self._table = []
            self._depth += 1
        elif tag == "tr" and self._depth == 1:
            self._row = []
        elif tag in {"td", "th"} and self._row is not None and self._depth == 1:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(_norm("".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._depth == 1 and self._table is not None:
            self._table.append(tuple(self._row))
            self._row = None
        elif tag == "table" and self._depth:
            self._depth -= 1
            if self._depth == 0 and self._table is not None:
                self.tables.append(self._table)
                self._table = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def _looks_like_html(blob: bytes) -> bool:
    sample = blob.lstrip()[:400].lower()
    return sample.startswith(b"<html") or sample.startswith(b"<table") or b"<table" in sample


def _rows_from_html(blob: bytes) -> list[tuple[str, ...]]:
    parser = _HtmlTableParser()
    parser.feed(blob.decode("utf-8", errors="replace"))
    parser.close()
    usable = [table for table in parser.tables if any(_header_indexes(row, kind="report") for row in table[:5])]
    if len(usable) != 1:
        raise IntakeHold("Pending Cancel report workbook is missing or ambiguous")
    return usable[0]


def _rows_from_delimited(blob: bytes) -> list[tuple[str, ...]] | None:
    if blob.startswith((b"%PDF", b"PK", _OLE_MAGIC)) or _looks_like_html(blob):
        return None
    try:
        text = blob.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    sample = text[:4096]
    if "," not in sample and "\t" not in sample:
        return None
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t")
        rows = [
            tuple(_norm(cell) for cell in row)
            for row in csv.reader(StringIO(text), dialect)
            if any(_norm(cell) for cell in row)
        ]
    except csv.Error:
        return None
    if not rows or _header_indexes(rows[0], kind="report") is None:
        return None
    return rows


def parse_excel_report(blob: bytes, *, report_date: date) -> PendingCancelReport:
    if not blob:
        raise _empty_export_hold("Xls")
    try:
        import openpyxl
    except ImportError:
        return _parse_xlsx_zip(blob, report_date=report_date)
    try:
        workbook = openpyxl.load_workbook(BytesIO(blob), read_only=True, data_only=True)
    except IntakeHold:
        raise
    except Exception as exc:
        raise IntakeHold("Pending Cancel report workbook is missing or ambiguous") from exc
    try:
        populated = []
        for sheet in workbook.worksheets:
            rows = []
            for row in sheet.iter_rows(values_only=True):
                rows.append(tuple(row))
            while rows and not any(cell is not None and str(cell).strip() for cell in rows[-1]):
                rows.pop()
            if rows:
                populated.append(rows)
        if len(populated) != 1:
            raise IntakeHold("Pending Cancel report workbook is missing or ambiguous")
        return parse_report_rows(populated[0], report_date=report_date, source="excel")
    finally:
        workbook.close()


_POLICY_TOKEN = re.compile(r"(?<!\d)(\d{6,12})(?!\d)")


def _structured_list_rows(text: str) -> list[tuple[str, ...]] | None:
    """Rows that carry a policy number, an insured name, and one cancel date.

    Returns None when the text is not that kind of list, so the older
    policy-line reader still handles a notice that only names a policy.
    """
    parsed: list[tuple[str, str, str]] = []
    for raw_line in str(text or "").splitlines():
        line = _norm(raw_line)
        if not line:
            continue
        policy_match = _POLICY_TOKEN.search(line)
        dates = list(_DATE_IN_TEXT.finditer(line))
        if policy_match is None or not dates:
            continue
        if len(dates) != 1:
            raise IntakeHold("Pending Cancel report date is missing or ambiguous")
        policy = policy_match.group(1)
        insured = line.replace(dates[0].group(0), " ", 1)
        insured = re.sub(rf"(?<!\d){re.escape(policy)}(?!\d)", " ", insured, count=1)
        insured = _norm(insured).strip(" -|")
        if not insured:
            raise IntakeHold("Pending Cancel insured name is missing")
        parsed.append((policy, insured, dates[0].group(0)))
    if not parsed:
        return None
    return [("Policy Number", "Named Insured", "Cancel Date"), *parsed]


def parse_xls_export(blob: bytes, *, report_date: date) -> PendingCancelReport:
    """Read a Pending Cancel Xls export without requiring an Excel package.

    xlsx is a zip of XML (stdlib). Older 'Export Xls' buttons often send an
    HTML table or a CSV. A real BIFF .xls file is named in the hold; this
    release does not add a workbook library for a format that has not been
    seen as a non-empty file.
    """
    raw = _assert_export_bytes(blob, label="Xls")
    if raw.startswith(b"PK"):
        return parse_excel_report(raw, report_date=report_date)
    if _looks_like_html(raw):
        return parse_report_rows(_rows_from_html(raw), report_date=report_date, source="excel")
    delimited = _rows_from_delimited(raw)
    if delimited is not None:
        return parse_report_rows(delimited, report_date=report_date, source="excel")
    if raw.startswith(_OLE_MAGIC):
        raise IntakeHold(
            "Pending Cancel Xls export is an older Excel workbook (.xls). "
            "This release has no reader for that format. "
            "The file was not treated as a report with no policies."
        )
    raise IntakeHold(
        "Pending Cancel Xls export is not a list this pull can read. "
        "The file was not treated as a report with no policies."
    )


def policies_from_report_text(text: str, *, report_date: date, source: str = "pdf") -> PendingCancelReport:
    raw = str(text or "")
    observed = {parse_report_date(match) for match in _REPORT_DATE_LABEL.findall(raw)}
    if len(observed) > 1:
        raise IntakeHold("Pending Cancel report date is missing or ambiguous")
    observed_date = next(iter(observed)) if observed else None
    if observed_date is not None and observed_date != report_date:
        raise IntakeHold(
            "Pending Cancel report date "
            f"{observed_date.isoformat()} does not match {report_date.isoformat()}"
        )
    structured = _structured_list_rows(raw)
    if structured is not None:
        report = parse_report_rows(structured, report_date=report_date, source=source)
        if observed_date is not None and report.observed_date is None:
            return PendingCancelReport(
                report_date=report.report_date,
                observed_date=observed_date,
                policies=report.policies,
                source=report.source,
                blank=report.blank,
            )
        return report
    labeled = list(_POLICY_LINE.finditer(raw))
    found: list[tuple[str, str]] = []
    if labeled:
        for index, match in enumerate(labeled):
            end = labeled[index + 1].start() if index + 1 < len(labeled) else len(raw)
            tail = raw[match.end():end]
            insured = _norm(tail.splitlines()[0] if tail else "")
            found.append((match.group(1), insured[:80].strip(" -")))
    else:
        for line in raw.splitlines():
            line_n = _norm(line)
            match = _ROW_START.match(line_n)
            if not match:
                continue
            rest = _norm(line_n[match.end():])
            if not rest or rest[0].isdigit():
                continue
            found.append((match.group(1), rest[:80]))
    policies: list[PendingCancelPolicy] = []
    seen: set[str] = set()
    for number, insured in found:
        if not _POLICY_NUMBER.fullmatch(number) or number in seen:
            raise IntakeHold("Pending Cancel policy number is missing or ambiguous")
        seen.add(number)
        policies.append(PendingCancelPolicy(number, insured, report_date))
    blankish = bool(_BLANK_TEXT.search(raw))
    if blankish and policies:
        raise IntakeHold("Pending Cancel report is missing or ambiguous")
    if not policies:
        if source == "pdf" and not blankish:
            raise IntakeHold("Pending Cancel report policies are missing or ambiguous")
        if source != "pdf" and _norm(raw) and not blankish:
            raise IntakeHold("Pending Cancel report policies are missing or ambiguous")
        return _blank_report(report_date, observed_date, source)
    return PendingCancelReport(
        report_date=report_date,
        observed_date=observed_date,
        policies=tuple(policies),
        source=source,
        blank=False,
    )


def parse_pdf_report(blob: bytes, *, report_date: date) -> PendingCancelReport:
    raw = _assert_export_bytes(blob, label="Pdf")
    if not _is_pdf(raw):
        raise IntakeHold("Pending Cancel report is missing or ambiguous")
    try:
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(raw))
        if not reader.pages:
            raise IntakeHold("Pending Cancel report is missing or ambiguous")
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    except IntakeHold:
        raise
    except Exception as exc:
        detail = f"{type(exc).__name__} {exc}".casefold()
        if "eof" in detail or "truncat" in detail:
            raise _truncated_export_hold("Pdf") from exc
        raise IntakeHold("Pending Cancel report is missing or ambiguous") from exc
    return policies_from_report_text(text, report_date=report_date, source="pdf")


def report_from_extracted(
    *,
    tables: list[tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]],
    page_text: str,
    excel_bytes: bytes | None,
    pdf_bytes: bytes | None,
    report_date: date,
) -> PendingCancelReport:
    if excel_bytes is not None:
        excel_bytes = _assert_export_bytes(excel_bytes, label="Xls")
    if pdf_bytes is not None:
        pdf_bytes = _assert_export_bytes(pdf_bytes, label="Pdf")
    policy_tables = []
    for headers, rows in tables:
        if _header_indexes(headers, kind="report") is not None:
            policy_tables.append((headers, rows))
    if len(policy_tables) > 1:
        raise IntakeHold("Pending Cancel report table is missing or ambiguous")
    if len(policy_tables) == 1:
        headers, rows = policy_tables[0]
        return parse_report_rows((headers, *rows), report_date=report_date, source="table")
    if excel_bytes:
        return parse_excel_report(excel_bytes, report_date=report_date)
    if pdf_bytes:
        return parse_pdf_report(pdf_bytes, report_date=report_date)
    if _BLANK_TEXT.search(page_text or "") and not _contains_policy_number(page_text or ""):
        return _blank_report(report_date, None, "page")
    raise IntakeHold("Pending Cancel report is missing or ambiguous")


def _contains_policy_number(text: str) -> bool:
    if _POLICY_LINE.search(text or ""):
        return True
    for line in (text or "").splitlines():
        if _ROW_START.match(_norm(line)):
            return True
    return False


def _dates_in_text(value: str) -> set[date]:
    found: set[date] = set()
    for match in _DATE_IN_TEXT.findall(value or ""):
        found.add(parse_report_date(match))
    return found


def select_notice(documents: tuple[PolicyDocument, ...] | list[PolicyDocument], report_date: date) -> PolicyDocument:
    matches: list[PolicyDocument] = []
    for document in documents:
        if not _NOTICE.search(_norm(document.name)):
            continue
        named = _dates_in_text(document.name)
        if len(named) > 1:
            raise RowHold("Notice of Non Payment date is ambiguous")
        named_date = next(iter(named)) if named else None
        if document.document_date is not None and named_date is not None and document.document_date != named_date:
            raise RowHold("Notice of Non Payment date is ambiguous")
        when = document.document_date if document.document_date is not None else named_date
        if when is None:
            raise RowHold("Notice of Non Payment date is missing")
        if when == report_date:
            matches.append(document)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise RowHold(f"Notice of Non Payment matching {report_date.isoformat()} is missing")
    raise RowHold("Notice of Non Payment matching the report date is ambiguous")


def assess_bop_gate(
    *,
    policy_count: int,
    outcomes: tuple[PolicyOutcome, ...] | list[PolicyOutcome],
    pdf_count: int,
    screenshot_ok: bool,
) -> tuple[str, str | None]:
    """Return pack status and a hold reason.

    Successful NOC rows must equal saved PDFs. A blank report with a screenshot
    and zero PDFs is EMPTY. Any held row or count mismatch is HELD.
    """
    if not screenshot_ok:
        return "HELD", "Pending Cancel report screenshot is missing or not a PNG"
    successful = [row for row in outcomes if row.disposition in {"pulled", "already_present"}]
    held = [row for row in outcomes if row.disposition == "held"]
    if len(outcomes) != policy_count or len(successful) + len(held) != len(outcomes):
        return "HELD", "Pending Cancel policy count does not match row outcomes"
    if len(successful) != pdf_count:
        return "HELD", (
            "Successful NOC count does not match saved PDFs: "
            f"{len(successful)} NOC and {pdf_count} PDFs"
        )
    if policy_count == 0 and not held:
        return "EMPTY", None
    if held:
        return "HELD", "; ".join(f"{row.policy_number}: {row.reason or 'held'}" for row in held)
    if len(successful) != policy_count:
        return "HELD", "Successful NOC count does not match policies on the report"
    return "PULLED", None


def screenshot_name(day: date) -> str:
    name = f"pending-cancel-nonpayment-{day.isoformat()}.png"
    if name != Path(name).name:
        raise IntakeHold("Pending Cancel report screenshot is missing or not a PNG")
    return name


def require_png(blob: bytes | bytearray | None) -> bytes:
    if not isinstance(blob, (bytes, bytearray)) or not bytes(blob).startswith(_PNG_MAGIC):
        raise IntakeHold("Pending Cancel report screenshot is missing or not a PNG")
    return bytes(blob)


class LocalNocLedger:
    """Private named-PDF ledger. A conflicting file is kept and the pull holds."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def ensure_private(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink() or not self.root.is_dir() or self.root.stat().st_mode & 0o077:
            raise IntakeHold("BOP output directory must be private (0700)")

    def date_dir(self, day: date) -> Path:
        self.ensure_private()
        folder = self.root / day.isoformat()
        if folder.is_symlink():
            raise IntakeHold("BOP output directory must be private (0700)")
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not folder.is_dir() or folder.stat().st_mode & 0o077:
            raise IntakeHold("BOP output directory must be private (0700)")
        return folder

    def pdf_path(self, day: date, filename: str) -> Path:
        return self.date_dir(day) / self._basename(filename)

    def delivery_status(self, *, document_id: str, filename: str, report_date: date) -> bool:
        self.ensure_private()
        path = self.pdf_path(report_date, filename)
        entry = self._load()["items"].get(document_id)
        exists = path.exists()
        if entry is None and not exists:
            return False
        if (
            not isinstance(entry, dict)
            or not exists
            or path.is_symlink()
            or not path.is_file()
            or entry.get("filename") != filename
            or entry.get("report_date") != report_date.isoformat()
            or entry.get("sha256") != hashlib.sha256(path.read_bytes()).hexdigest()
        ):
            raise IntakeHold("Existing NOC file conflicts with the pull ledger")
        return True

    def record(self, source: SourceItem, *, report_date: date) -> Path:
        self.ensure_private()
        path = self.pdf_path(report_date, source.filename)
        if path.exists() or path.is_symlink():
            raise IntakeHold("Existing NOC file conflicts with the pull ledger")
        digest = hashlib.sha256(source.content).hexdigest()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(source.content)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            try:
                os.unlink(path)
            except OSError:
                pass
            raise
        data = self._load()
        if source.source_id in data["items"]:
            raise IntakeHold("Existing NOC file conflicts with the pull ledger")
        data["items"][source.source_id] = {
            "filename": source.filename,
            "sha256": digest,
            "bytes": len(source.content),
            "report_date": report_date.isoformat(),
            "policy_number": source.source_id.split(":")[1] if source.source_id.count(":") >= 2 else "",
        }
        self._write(data)
        return path

    def pdf_ids_for_date(self, day: date) -> set[str]:
        found: set[str] = set()
        for document_id, entry in self._load()["items"].items():
            if not isinstance(entry, dict) or entry.get("report_date") != day.isoformat():
                continue
            if self.delivery_status(
                document_id=str(document_id),
                filename=str(entry.get("filename") or ""),
                report_date=day,
            ):
                found.add(str(document_id))
        return found

    def save_screenshot(self, day: date, png: bytes) -> str:
        blob = require_png(png)
        folder = self.date_dir(day)
        name = screenshot_name(day)
        primary = folder / name
        if primary.exists():
            if primary.is_symlink() or not primary.is_file():
                raise IntakeHold("Pending Cancel report screenshot is missing or not a PNG")
            if primary.read_bytes() == blob:
                return str(primary)
            path = self._sibling_screenshot(folder, name)
        else:
            path = primary
        return str(self._write_bytes(path, blob))

    def _sibling_screenshot(self, folder: Path, name: str) -> Path:
        stem = self._basename(name)[:-4]
        for index in range(2, 100):
            candidate = folder / f"{stem}-{index}.png"
            if not candidate.exists() and not candidate.is_symlink():
                return candidate
        raise IntakeHold("Pending Cancel report screenshot is missing or not a PNG")

    def _write_bytes(self, path: Path, blob: bytes) -> Path:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(blob)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            try:
                os.unlink(path)
            except OSError:
                pass
            raise
        return path

    def _basename(self, filename: str) -> str:
        if not filename or filename != Path(filename).name or filename in {".", ".."} or "/" in filename:
            raise IntakeHold("NOC filename is missing or ambiguous")
        return filename

    def _ledger_path(self) -> Path:
        return self.root / LEDGER_NAME

    def _load(self) -> dict[str, Any]:
        path = self._ledger_path()
        if not path.exists():
            return {"version": 1, "items": {}}
        if path.is_symlink() or not path.is_file():
            raise IntakeHold("Existing NOC file conflicts with the pull ledger")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise IntakeHold("Existing NOC file conflicts with the pull ledger") from exc
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("items"), dict):
            raise IntakeHold("Existing NOC file conflicts with the pull ledger")
        return data

    def _write(self, data: dict[str, Any]) -> None:
        path = self._ledger_path()
        temporary = path.with_name(LEDGER_NAME + ".tmp")
        if temporary.exists() or temporary.is_symlink():
            raise IntakeHold("Existing NOC file conflicts with the pull ledger")
        payload = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        os.chmod(path, 0o600)


def _outcome_from_policy(policy: PendingCancelPolicy, disposition: str, *, reason: str | None = None, source: SourceItem | None = None) -> PolicyOutcome:
    return PolicyOutcome(
        policy_number=policy.policy_number,
        insured_name=policy.insured_name,
        document_id=policy.document_id,
        filename=policy.filename,
        disposition=disposition,
        reason=reason,
        sha256=source.digest if source is not None else None,
        byte_count=len(source.content) if source is not None else None,
    )


class BopPendingCancelPortal:
    """Pull Notice of Non Payment files for one Pending Cancel report date."""

    def __init__(self, browser: Any, ledger: LocalNocLedger, archive: SourceArchive, *, agent_code: str, report_date: date):
        self.browser = browser
        self.ledger = ledger
        self.archive = archive
        self.agent_code = require_agent_code(agent_code)
        self.report_date = report_date
        self.verification: dict[str, Any] | None = None
        self.outcomes: tuple[PolicyOutcome, ...] = ()

    def pull(self) -> dict[str, Any]:
        require_test()
        if BOP_SCOPE not in SCOPES:
            raise IntakeHold("An approved Progressive scope is required")
        capture = self.browser.load_report(self.report_date)
        png = require_png(getattr(capture, "png", None))
        shot = self.ledger.save_screenshot(self.report_date, png)
        report = getattr(capture, "report", None)
        error = getattr(capture, "error", None)
        if error or report is None:
            reason = str(error or "Pending Cancel report is missing or ambiguous")
            self._finish(status="HELD", held=reason, outcomes=(), policy_count=0, screenshot=shot, report_source=None)
            raise IntakeHold(reason)
        if report.report_date != self.report_date:
            reason = "Pending Cancel report date does not match the requested date"
            self._finish(status="HELD", held=reason, outcomes=(), policy_count=0, screenshot=shot, report_source=report.source)
            raise IntakeHold(reason)
        outcomes = self._pull_policies(report)
        status, held = assess_bop_gate(
            policy_count=len(report.policies),
            outcomes=outcomes,
            pdf_count=len(self.ledger.pdf_ids_for_date(self.report_date)),
            screenshot_ok=True,
        )
        self._finish(
            status=status,
            held=held,
            outcomes=outcomes,
            policy_count=len(report.policies),
            screenshot=shot,
            report_source=report.source,
        )
        if held:
            raise IntakeHold(held)
        return self.verification or {}

    def _pull_policies(self, report: PendingCancelReport) -> list[PolicyOutcome]:
        outcomes: list[PolicyOutcome] = []
        blocked: str | None = None
        for index, policy in enumerate(report.policies):
            if blocked:
                outcomes.append(_outcome_from_policy(policy, "held", reason=blocked))
                continue
            try:
                outcomes.append(self._one(policy))
            except RowHold as exc:
                outcomes.append(_outcome_from_policy(policy, "held", reason=str(exc)))
            except IntakeHold as exc:
                blocked = str(exc)
                outcomes.append(_outcome_from_policy(policy, "held", reason=blocked))
            blocked = blocked or getattr(self.browser, "shell_blocked", None)
            if blocked and outcomes[-1].disposition != "held":
                blocked = f"FAO search could not continue: {blocked}"
                for remaining in report.policies[index + 1:]:
                    outcomes.append(_outcome_from_policy(remaining, "held", reason=blocked))
                break
            if blocked and index + 1 < len(report.policies) and len(outcomes) == index + 1:
                for remaining in report.policies[index + 1:]:
                    outcomes.append(_outcome_from_policy(remaining, "held", reason=blocked))
                break
        return outcomes

    def _one(self, policy: PendingCancelPolicy) -> PolicyOutcome:
        if self.ledger.delivery_status(
            document_id=policy.document_id,
            filename=policy.filename,
            report_date=policy.report_date,
        ):
            stored = self.ledger._load()["items"][policy.document_id]
            return PolicyOutcome(
                policy_number=policy.policy_number,
                insured_name=policy.insured_name,
                document_id=policy.document_id,
                filename=policy.filename,
                disposition="already_present",
                sha256=str(stored.get("sha256") or ""),
                byte_count=int(stored.get("bytes") or 0),
            )
        try:
            content = self.browser.download_notice(policy)
        except RowHold:
            raise
        except IntakeHold:
            raise
        except Exception as exc:
            raise RowHold(f"Notice of Non Payment download failed ({type(exc).__name__})") from exc
        if not _is_pdf(content):
            raise RowHold("Notice of Non Payment download is not a PDF")
        source = SourceItem(
            system="progressive",
            source_account=self.agent_code,
            source_id=policy.document_id,
            source_url=f"https://www.foragentsonly.com/policy/{policy.policy_number}#noc={policy.report_date.isoformat()}",
            received_at=_received_at(policy.report_date),
            filename=policy.filename,
            content=bytes(content),
        )
        source.validate()
        self.archive.preserve(source)
        self.ledger.record(source, report_date=policy.report_date)
        return _outcome_from_policy(policy, "pulled", source=source)

    def _finish(
        self,
        *,
        status: str,
        held: str | None,
        outcomes: list[PolicyOutcome],
        policy_count: int,
        screenshot: str,
        report_source: str | None,
    ) -> None:
        self.outcomes = tuple(outcomes)
        folder = self.ledger.date_dir(self.report_date)
        successful = sum(1 for row in outcomes if row.disposition in {"pulled", "already_present"})
        pdfs = len(self.ledger.pdf_ids_for_date(self.report_date))
        manifest = {
            "carrier": "progressive",
            "product": "bop",
            "scope": BOP_SCOPE,
            "agent_code": self.agent_code,
            "report_date": self.report_date.isoformat(),
            "report_source": report_source,
            "status": status,
            "gate": "successful_noc_equals_pdfs",
            "policies_on_report": policy_count,
            "successful_noc": successful,
            "pdfs": pdfs,
            "screenshot": Path(screenshot).name,
            "screenshot_sha256": hashlib.sha256(Path(screenshot).read_bytes()).hexdigest(),
            "policies": [_outcome_dict(row) for row in outcomes],
            "held": held,
            "ezlynx": "not_run",
            "drive": _drive_destination(self.report_date),
        }
        _write_private_file(folder / "manifest.json", json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n")
        _write_private_file(folder / "README.md", _qa_readme(manifest).encode("utf-8"))
        self.verification = {
            "gate": manifest["gate"],
            "status": status,
            "policies_on_report": policy_count,
            "successful_noc": successful,
            "pdfs": pdfs,
            "screenshot": screenshot,
            "pack": str(folder),
            "held": held,
        }


def _outcome_dict(row: PolicyOutcome) -> dict[str, Any]:
    return {
        "policy_number": row.policy_number,
        "insured_name": row.insured_name,
        "document_id": row.document_id,
        "filename": row.filename,
        "disposition": row.disposition,
        "reason": row.reason,
        "sha256": row.sha256,
        "bytes": row.byte_count,
    }


def _qa_readme(manifest: dict[str, Any]) -> str:
    policies = list(manifest["policies"])
    lines = [
        f"# Progressive BOP pending-cancel QA — {manifest['report_date']}",
        "",
        "Carrier: progressive",
        "Product: Businessowner/Contractor GL",
        f"Agent: {manifest['agent_code']}",
        f"Report date: {manifest['report_date']}",
        f"Status: {manifest['status']}",
        (
            "Gate: policies on the Pending Cancel report with a successful NOC equal saved PDFs "
            f"({manifest['policies_on_report']} policies, {manifest['successful_noc']} NOC, {manifest['pdfs']} PDFs)"
        ),
        "",
        "## Seen",
    ]
    if policies:
        for policy in policies:
            insured = f" {policy['insured_name']}" if policy.get("insured_name") else ""
            lines.append(
                f"- {policy['policy_number']}{insured} — {policy['filename']} ({policy['disposition']})"
            )
    else:
        lines.append("- No policies on the Pending Cancel for Nonpayment report.")
    lines.extend(["", "## Pulled this run"])
    pulled = [policy["filename"] for policy in policies if policy["disposition"] == "pulled"]
    lines.extend([f"- {name}" for name in pulled] or ["- None"])
    lines.extend(["", "## Already present"])
    present = [policy["filename"] for policy in policies if policy["disposition"] == "already_present"]
    lines.extend([f"- {name}" for name in present] or ["- None"])
    lines.extend(["", "## Held"])
    held_rows = [policy for policy in policies if policy["disposition"] == "held"]
    if held_rows:
        lines.extend([f"- {policy['policy_number']}: {policy['reason']}" for policy in held_rows])
    elif manifest.get("held"):
        lines.append(f"- {manifest['held']}")
    else:
        lines.append("- None")
    lines.extend([
        "",
        f"Screenshot: {manifest['screenshot']}",
        "",
        "EZLynx: not_run",
        "Drive: not_run",
        f"Drive destination: {manifest['drive']['path']}",
        "",
    ])
    return "\n".join(lines)


def _drive_destination(day: date) -> dict[str, str]:
    return {
        "status": "not_run",
        "parent_id": DRIVE_QA_PARENT_ID,
        "child_name": DRIVE_BOP_CHILD_NAME,
        "folder_id": "",
        "path": f"{DRIVE_QA_FOLDER_NAME}/{day.isoformat()}/",
        "todo": (
            "Create the Progressive BOP child folder under the Nicole parent when it is missing, "
            "then upload this date folder. Folder upload is not implemented."
        ),
    }


def refuse_bop_drive_upload(ledger: LocalNocLedger, *, report_date: date, verification: dict[str, Any] | None = None) -> None:
    """TODO: create Progressive BOP if needed and upload the date folder. Fail closed."""
    folder = ledger.date_dir(report_date)
    manifest_path = folder / "manifest.json"
    readme_path = folder / "README.md"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        readme = readme_path.read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError) as exc:
        raise IntakeHold(DRIVE_UPLOAD_UNAVAILABLE) from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("drive"), dict):
        raise IntakeHold(DRIVE_UPLOAD_UNAVAILABLE)
    if "Drive: not_run\n" not in readme:
        raise IntakeHold(DRIVE_UPLOAD_UNAVAILABLE)
    manifest["drive"]["status"] = "HELD"
    manifest["drive"]["reason"] = DRIVE_UPLOAD_UNAVAILABLE
    _write_private_file(manifest_path, json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n")
    _write_private_file(
        readme_path,
        readme.replace("Drive: not_run\n", f"Drive: HELD — {DRIVE_UPLOAD_UNAVAILABLE}\n", 1).encode("utf-8"),
    )
    if verification is not None:
        verification["drive_upload"] = "HELD"
    raise IntakeHold(DRIVE_UPLOAD_UNAVAILABLE)


def _write_private_file(path: Path, payload: bytes) -> None:
    temporary = path.with_name(path.name + ".tmp")
    if temporary.exists() or temporary.is_symlink():
        raise IntakeHold("Existing NOC file conflicts with the pull ledger")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)


def assert_authenticated(page: Any) -> None:
    url = str(getattr(page, "url", "") or "")
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/").lower()
    if host.endswith("foragentsonlylogin.progressive.com") or path.endswith("/login"):
        raise IntakeHold("Progressive FAO session is not authenticated")
    if page.locator("input[type='password']").count() != 0:
        raise IntakeHold("Progressive FAO session is not authenticated")


# Existing FAO header contract (progressive_fao_memo / locators/progressive_fao.json).
# Not a new selector. Live accessible name is "Manage Policies Home".
MANAGE_POLICIES_CSS = 'a[data-at="header-nav__parent-link--manage-policies"]'
MANAGE_POLICIES_NAME = re.compile(r"^Manage Policies")
MAIN_NAVIGATION_NAME = "Main Navigation"
_SHELL_NAV_ROLES = ("link", "button")
# FAO shell Home, or the Manage Policies Home landing that header control opens.
# Live Manage Policies Home is /landingpages/managepolicies/ (optional /home).
# Communications / underwritinglegacy is processed-date results and is not this page.
_FAO_SHELL_HOME_URL = re.compile(
    r"^https://(?:[a-z0-9-]+\.)*foragentsonly\.com"
    r"(?:/(?:home|(?:landingpages/)?managepolicies(?:/home)?))?/?$",
    re.IGNORECASE,
)


class _HomeNotReady(Exception):
    """Home control is absent or hidden. Ambiguous controls are an IntakeHold."""

    def __init__(self, reason: str, css_count: int, role_count: int):
        self.reason = reason
        self.css_count = css_count
        self.role_count = role_count


def _safe_page_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    return urllib.parse.urlunsplit((parsed.scheme, parsed.hostname, parsed.path or "", "", ""))


def _tab_hint(safe_url: str) -> str:
    path = urllib.parse.urlsplit(safe_url).path.lower()
    if "underwritinglegacy" in path or "underwriting-legacy" in path or "underwriting_legacy" in path:
        return "underwritinglegacy"
    if "/communications" in path:
        return "communications"
    if "processeddateresults" in path:
        return "processeddateresults"
    if not safe_url:
        return "(url withheld)"
    return "not-fao-home"


def _home_hold(page: Any, *, css_count: int, role_count: int, detail: str) -> IntakeHold:
    """Fail closed. The counts are not click targets. No Gemini."""
    url = _safe_page_url(str(getattr(page, "url", "") or "")) or "(url withheld)"
    return IntakeHold(
        "Progressive FAO Home is missing or ambiguous; "
        f"{MANAGE_POLICIES_CSS} matched {css_count}; "
        f"Manage Policies Home matched {role_count}; "
        f"tab {_tab_hint(url)}; page {url}; {detail}"
    )


def _on_fao_shell_home(page: Any) -> bool:
    return _FAO_SHELL_HOME_URL.fullmatch(_safe_page_url(str(getattr(page, "url", "") or ""))) is not None


def _union(locators: list[Any]) -> Any:
    merged = locators[0]
    for locator in locators[1:]:
        merged = merged.or_(locator)
    return merged


def _role_locator(page: Any, name: str | re.Pattern, *, exact: bool) -> Any:
    return _union([
        page.get_by_role(role, name=name, exact=exact)
        for role in _SHELL_NAV_ROLES
    ])


def _is_visible_one(locator: Any) -> bool:
    if int(locator.count()) != 1:
        return False
    probe = getattr(locator, "is_visible", None)
    if not callable(probe):
        return True
    try:
        return bool(probe())
    except Exception:
        return False


def _locator_count(locator: Any) -> int:
    try:
        return int(locator.count())
    except Exception:
        return -1


def _resolve_visible_home(page: Any) -> Any:
    """One Manage Policies Home element. Zero, hidden, or several do not click."""
    css = page.locator(MANAGE_POLICIES_CSS)
    role = _role_locator(page, MANAGE_POLICIES_NAME, exact=False)
    css_count = _locator_count(css)
    role_count = _locator_count(role)
    if css_count < 0 or role_count < 0 or css_count > 1 or role_count > 1:
        raise _home_hold(
            page,
            css_count=css_count,
            role_count=role_count,
            detail="Home control was not clicked",
        )
    if css_count == 1 and role_count == 1 and _locator_count(css.and_(role)) != 1:
        raise _home_hold(
            page,
            css_count=css_count,
            role_count=role_count,
            detail="Home control queries are different elements; Home control was not clicked",
        )
    chosen = css if css_count == 1 else role if role_count == 1 else None
    if chosen is None:
        raise _HomeNotReady("missing", css_count, role_count)
    if not _is_visible_one(chosen):
        raise _HomeNotReady("hidden", css_count, role_count)
    return chosen


def _expand_main_navigation_once(page: Any, *, css_count: int, role_count: int) -> None:
    """Open the shell drawer once so a hidden Manage Policies Home link can show.

    The existing FAO header contract hides that link until Main Navigation is
    expanded. This click is not a Gemini rescue. A missing or second Main
    Navigation control holds.
    """
    matches = []
    for role in _SHELL_NAV_ROLES:
        locator = page.get_by_role(role, name=MAIN_NAVIGATION_NAME, exact=True)
        count = _locator_count(locator)
        if count:
            matches.append((count, locator))
    if len(matches) != 1 or matches[0][0] != 1 or not _is_visible_one(matches[0][1]):
        raise _home_hold(
            page,
            css_count=css_count,
            role_count=role_count,
            detail="Main Navigation is missing or ambiguous; FAO Home was not opened",
        )
    try:
        matches[0][1].click()
    except IntakeHold:
        raise
    except Exception as exc:
        raise _home_hold(
            page,
            css_count=css_count,
            role_count=role_count,
            detail="Main Navigation click did not complete; FAO Home was not opened",
        ) from exc


def ensure_fao_shell_home(page: Any) -> None:
    """Land on FAO Home / Manage Policies Home before the agent-context assert.

    A Communications / underwritinglegacy tab, or any other non-home FAO URL,
    is not where this pull reads the agency. The click uses the existing
    Manage Policies Home header control. It does not ask Gemini. Already on
    that landing is a no-op. Missing, hidden after one Main Navigation expand,
    or ambiguous holds with the scrubbed URL.
    """
    if _on_fao_shell_home(page):
        return
    try:
        target = _resolve_visible_home(page)
    except _HomeNotReady as miss:
        if miss.reason == "hidden":
            _expand_main_navigation_once(page, css_count=miss.css_count, role_count=miss.role_count)
            try:
                target = _resolve_visible_home(page)
            except _HomeNotReady as still:
                raise _home_hold(
                    page,
                    css_count=still.css_count,
                    role_count=still.role_count,
                    detail="Home control stayed missing or hidden; FAO Home was not opened",
                ) from still
        else:
            raise _home_hold(
                page,
                css_count=miss.css_count,
                role_count=miss.role_count,
                detail="Home control was not found; FAO Home was not opened",
            ) from miss
    try:
        target.click()
    except IntakeHold:
        raise
    except Exception as exc:
        raise _home_hold(
            page,
            css_count=_locator_count(page.locator(MANAGE_POLICIES_CSS)),
            role_count=_locator_count(_role_locator(page, MANAGE_POLICIES_NAME, exact=False)),
            detail="Home control click did not complete; FAO Home was not opened",
        ) from exc
    if not _on_fao_shell_home(page):
        raise _home_hold(
            page,
            css_count=_locator_count(page.locator(MANAGE_POLICIES_CSS)),
            role_count=_locator_count(_role_locator(page, MANAGE_POLICIES_NAME, exact=False)),
            detail="FAO Home did not open",
        )


def click_named(page: Any, name: str, *, roles: tuple[str, ...], error: type[IntakeHold] = IntakeHold) -> None:
    matches = []
    for role in roles:
        locator = page.get_by_role(role, name=name, exact=True)
        count = locator.count()
        if count:
            matches.append((count, locator))
    if len(matches) != 1 or matches[0][0] != 1:
        raise error(f"Progressive control {name!r} is missing or ambiguous")
    matches[0][1].click()


# Landing text read on prove4 while the shell was already on
# /landingpages/managepolicies/. Exact playbook name stays the other candidate.
# One match clicks. Zero or several hold. This is not a Gemini rescue.
_SHELL_HOME_GL_NAMES = (
    "Businessowner/Contractor GL",
    "Go to Businessowner/Contractor GL policy search",
)


def _click_shell_home_gl(page: Any) -> None:
    """One Businessowner/Contractor GL opener on FAO shell Home."""
    matches = []
    for name in _SHELL_HOME_GL_NAMES:
        for role in ("link", "button"):
            locator = page.get_by_role(role, name=name, exact=True)
            count = locator.count()
            if count:
                matches.append((count, locator))
    if len(matches) != 1 or matches[0][0] != 1:
        raise IntakeHold(
            "Progressive control 'Businessowner/Contractor GL' is missing or ambiguous"
        )
    matches[0][1].click()


def fill_policy_search(page: Any, policy_number: str) -> None:
    policy = str(policy_number or "").strip()
    if not _POLICY_NUMBER.fullmatch(policy):
        raise IntakeHold("Pending Cancel policy number is missing or ambiguous")
    matches = []
    for role, name in _SEARCH_FIELDS:
        locator = page.get_by_role(role, name=name, exact=True)
        count = locator.count()
        if count:
            matches.append((count, locator))
    if len(matches) != 1 or matches[0][0] != 1:
        raise IntakeHold("FAO policy search is missing or ambiguous")
    field = matches[0][1]
    field.fill(policy)
    observed = str(field.input_value() or "").strip()
    if observed != policy:
        raise IntakeHold("FAO policy search did not stick")


_BOP_APP_HOST = "bop.americanstrategic.com"
_VIEW_REPORTS_NAMES = ("View Reports", "VIEW REPORTS")
_CLOSE_THIS_WINDOW = re.compile(r"^close this window$", re.IGNORECASE)
_HP_LANDING_URL = re.compile(r"hplanding", re.IGNORECASE)
_CLOSE_THIS_WINDOW_TEXT = re.compile(r"close this window", re.IGNORECASE)


class _BopFrameSurface:
    """Page-shaped view of the BOP application frame inside HPLanding.

    Locators stay on the frame. Screenshot, download, and context stay on the
    owning page so a frame can still feed the report reader.
    """

    def __init__(self, frame: Any, owner: Any):
        self._frame = frame
        self._owner = owner
        self.url = str(getattr(frame, "url", "") or "")

    def get_by_role(self, *args: Any, **kwargs: Any) -> Any:
        return self._frame.get_by_role(*args, **kwargs)

    def locator(self, *args: Any, **kwargs: Any) -> Any:
        return self._frame.locator(*args, **kwargs)

    def screenshot(self, **kwargs: Any) -> Any:
        shot = getattr(self._frame, "screenshot", None)
        if callable(shot):
            return shot(**kwargs)
        return self._owner.screenshot(**kwargs)

    @property
    def context(self) -> Any:
        return getattr(self._owner, "context", None)

    def expect_download(self, *args: Any, **kwargs: Any) -> Any:
        return self._owner.expect_download(*args, **kwargs)

    def wait_for_load_state(self, *args: Any, **kwargs: Any) -> Any:
        for target in (self._frame, self._owner):
            waiter = getattr(target, "wait_for_load_state", None)
            if callable(waiter):
                return waiter(*args, **kwargs)
        raise TypeError("wait_for_load_state")


def _is_bop_app_url(url: str) -> bool:
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return False
    return host == _BOP_APP_HOST or host.endswith("." + _BOP_APP_HOST)


def _target_url(target: Any) -> str:
    return str(getattr(target, "url", "") or "")


def _is_hplanding_target(target: Any) -> bool:
    """Dead FAO landing. The BOP application is not this page."""
    if _HP_LANDING_URL.search(_target_url(target)):
        return True
    for role in ("button", "link"):
        try:
            locator = target.get_by_role(role, name=_CLOSE_THIS_WINDOW)
            if int(locator.count()) >= 1:
                return True
        except Exception:
            continue
    try:
        body = str(target.locator("body").inner_text() or "")
    except Exception:
        body = ""
    return _CLOSE_THIS_WINDOW_TEXT.search(body) is not None


def _context_pages(*owners: Any) -> list[Any]:
    found: list[Any] = []
    seen: set[int] = set()
    for owner in owners:
        if owner is None:
            continue
        context = getattr(owner, "context", None)
        pages = getattr(context, "pages", None) if context is not None else None
        if pages is None:
            continue
        try:
            items = list(pages)
        except Exception:
            continue
        for item in items:
            marker = id(item)
            if marker in seen:
                continue
            seen.add(marker)
            found.append(item)
    return found


def _child_frames(page: Any) -> list[Any]:
    raw = getattr(page, "frames", None)
    if not raw:
        return []
    try:
        frames = list(raw)
    except Exception:
        return []
    owner_url = _target_url(page)
    found = []
    for frame in frames:
        if frame is page:
            continue
        frame_url = _target_url(frame)
        if frame_url and frame_url == owner_url:
            continue
        found.append(frame)
    return found


def _bop_pages(shell: Any, popup: Any) -> list[Any]:
    return [page for page in _context_pages(shell, popup) if _is_bop_app_url(_target_url(page))]


def _bop_frames(popup: Any) -> list[tuple[Any, Any]]:
    if popup is None:
        return []
    return [
        (popup, frame)
        for frame in _child_frames(popup)
        if _is_bop_app_url(_target_url(frame))
    ]


def _close_if_possible(target: Any) -> None:
    """HPLanding close is best-effort. A failed close still leaves the BOP app attached."""
    closer = getattr(target, "close", None)
    if not callable(closer):
        return
    try:
        closer()
    except Exception:
        return


def _listen_for_pages(owners: tuple[Any, ...], opened: list[Any]) -> list[tuple[Any, Any]]:
    """Record pages that open while HPLanding is on screen. CDP context.pages can lag."""
    subscriptions: list[tuple[Any, Any]] = []
    seen_contexts: set[int] = set()

    def handler(new_page: Any) -> None:
        opened.append(new_page)

    for owner in owners:
        context = getattr(owner, "context", None) if owner is not None else None
        if context is None or id(context) in seen_contexts:
            continue
        seen_contexts.add(id(context))
        subscribe = getattr(context, "on", None)
        if not callable(subscribe):
            continue
        try:
            subscribe("page", handler)
        except Exception:
            continue
        subscriptions.append((context, handler))
    return subscriptions


def _forget_pages(subscriptions: list[tuple[Any, Any]]) -> None:
    for context, handler in subscriptions:
        remover = getattr(context, "remove_listener", None)
        if not callable(remover):
            continue
        try:
            remover("page", handler)
        except Exception:
            continue


def _collect_bop(shell: Any, popup: Any, opened: list[Any]) -> tuple[list[Any], list[tuple[Any, Any]]]:
    pages = _bop_pages(shell, popup)
    seen = {id(page) for page in pages}
    for item in opened:
        if id(item) in seen or not _is_bop_app_url(_target_url(item)):
            continue
        seen.add(id(item))
        pages.append(item)
    frames = _bop_frames(popup)
    for item in opened:
        frames.extend(_bop_frames(item))
    return pages, frames


def _await_bop_surface(shell: Any, popup: Any) -> tuple[list[Any], list[tuple[Any, Any]]]:
    """Wait until https://bop.americanstrategic.com/ exists. Do not keep HPLanding.

    ``expect_popup`` resolves on the first window, which is
    ``sbr*.foragentsonly.com/.../HPLanding.aspx`` ("Close this window").
    The VIEW REPORTS application is a later page on ``bop.americanstrategic.com``.
    """
    opened: list[Any] = []
    subscriptions = _listen_for_pages((shell, popup), opened)
    try:
        pages, frames = _collect_bop(shell, popup, opened)
        if pages or frames or popup is None or not _is_hplanding_target(popup):
            return pages, frames
        wait = getattr(shell, "wait_for_timeout", None)
        if not callable(wait):
            wait = getattr(popup, "wait_for_timeout", None)
        if not callable(wait):
            return pages, frames
        waited = 0
        step = 250
        while waited < BOP_APP_ATTACH_TIMEOUT_MS:
            try:
                wait(step)
            except Exception:
                break
            waited += step
            pages, frames = _collect_bop(shell, popup, opened)
            if pages or frames:
                return pages, frames
        return pages, frames
    finally:
        _forget_pages(subscriptions)


# Live 2026-09-30: HPLanding tries to open the Businessowner site on load,
# Chrome blocks that window (no click), and the page asks for a click on
# "Service Homeowners Policies" (the partner button, selected_partner=BOP).
# One click is allowed. The page then signs on through
# FieldInfoService.asmx/Authenticate and opens the partner window.
_PARTNER_BUTTON = "Service Homeowners Policies"
_POPUP_BLOCKER_TEXT = re.compile(r"pop-?up blocker", re.IGNORECASE)


def _retry_partner_sign_on(popup: Any) -> bool:
    try:
        body = str(popup.locator("body").inner_text() or "")
    except Exception:
        return False
    if not _POPUP_BLOCKER_TEXT.search(body):
        return False
    try:
        button = popup.get_by_role("button", name=_PARTNER_BUTTON, exact=True)
        if int(button.count()) != 1:
            return False
        button.click()
    except Exception:
        return False
    return True


def _attach_bop_application(shell: Any, popup: Any) -> Any:
    """Use the BOP application, not the dead HPLanding popup.

    A popup that is already the application is returned as-is. A non-landing
    popup stays the report window so an older in-window GL path still works.
    HPLanding attaches to the one ``bop.americanstrategic.com`` page or frame.
    """
    if popup is not None and _is_bop_app_url(_target_url(popup)):
        return popup
    if popup is None or not _is_hplanding_target(popup):
        if popup is None:
            raise IntakeHold("Businessowner/Contractor GL did not open a single new window")
        return popup
    pages, frames = _await_bop_surface(shell, popup)
    if not pages and not frames and _retry_partner_sign_on(popup):
        pages, frames = _await_bop_surface(shell, popup)
    if len(pages) + len(frames) > 1:
        raise IntakeHold("Businessowner/Contractor GL opened more than one BOP window")
    if len(pages) == 1:
        _close_if_possible(popup)
        return pages[0]
    if len(frames) == 1:
        _owner, frame = frames[0]
        return _BopFrameSurface(frame, _owner)
    raise IntakeHold(
        "Businessowner/Contractor GL opened HPLanding instead of the BOP application"
        " (Progressive's Businessowner site never opened after sign-on)"
    )


def _click_first_exact(
    page: Any,
    names: tuple[str, ...],
    roles: tuple[str, ...],
    label: str,
) -> None:
    """Click the first name that is one control. A later casing is only a fallback."""
    for name in names:
        matches = []
        for role in roles:
            locator = page.get_by_role(role, name=name, exact=True)
            count = locator.count()
            if count:
                matches.append((count, locator))
        if not matches:
            continue
        if len(matches) != 1 or matches[0][0] != 1:
            raise IntakeHold(f"Progressive control {label!r} is missing or ambiguous")
        matches[0][1].click()
        return
    raise IntakeHold(f"Progressive control {label!r} is missing or ambiguous")


MANAGE_POLICIES_LANDING_URL = "https://www.foragentsonly.com/landingpages/managepolicies/"
_MANAGE_POLICIES_LANDING_PATH = re.compile(r"^/landingpages/managepolicies(?:/home)?/?$", re.IGNORECASE)


def _on_manage_policies_landing(page: Any) -> bool:
    parsed = urllib.parse.urlsplit(_safe_page_url(str(getattr(page, "url", "") or "")))
    host = (parsed.hostname or "").casefold()
    return (host == "foragentsonly.com" or host.endswith(".foragentsonly.com")) and bool(
        _MANAGE_POLICIES_LANDING_PATH.match(parsed.path or "")
    )


def ensure_manage_policies_landing(page: Any) -> None:
    """Open Manage Policies Home, where the Businessowner opener is visible.

    Live 2026-09-30: on FAO /home/ both Businessowner/Contractor GL links sit
    in the closed navigation drawer (no visible match), while
    /landingpages/managepolicies/ shows one "Go to Businessowner/Contractor GL
    policy search" link. The landing is opened by its fixed FAO address on the
    same signed-in tab.
    """
    if _on_manage_policies_landing(page):
        return
    try:
        page.goto(MANAGE_POLICIES_LANDING_URL, wait_until="domcontentloaded", timeout=DOWNLOAD_TIMEOUT_MS)
    except Exception as exc:
        raise IntakeHold("Progressive Manage Policies page did not open") from exc
    assert_authenticated(page)
    if not _on_manage_policies_landing(page):
        raise IntakeHold("Progressive Manage Policies page did not open")


def open_businessowner_window(page: Any, *, on_shell_home: bool | None = None) -> Any:
    """Open Businessowner/Contractor GL in one new window from Manage Policies Home.

    ``on_shell_home`` is kept for callers; the opener is always clicked on the
    Manage Policies landing, where exactly one visible GL link is expected.
    """
    ensure_manage_policies_landing(page)
    try:
        with page.expect_popup(timeout=DOWNLOAD_TIMEOUT_MS) as popup:
            _click_shell_home_gl(page)
        opened = popup.value
    except IntakeHold:
        raise
    except Exception as exc:
        raise IntakeHold("Businessowner/Contractor GL did not open a single new window") from exc
    if opened is None:
        raise IntakeHold("Businessowner/Contractor GL did not open a single new window")
    return _attach_bop_application(page, opened)


def _ready_control_names() -> tuple[str, ...]:
    return _VIEW_REPORTS_NAMES + (
        _PENDING_CANCEL_NAV,
        _PENDING_CANCEL_PDF_EXPORT,
        _PENDING_CANCEL_XLS_EXPORT,
    )


def _count_named(page: Any, name: str) -> int:
    total = 0
    for role in ("link", "button"):
        count = _locator_count(page.get_by_role(role, name=name, exact=True))
        if count < 0:
            continue
        if count > 1:
            raise IntakeHold(f"Progressive control {name!r} is missing or ambiguous")
        total += count
    if total > 1:
        raise IntakeHold(f"Progressive control {name!r} is missing or ambiguous")
    return total


def _any_ready_control(page: Any) -> bool:
    return any(_count_named(page, name) == 1 for name in _ready_control_names())


def _combined_ready_locator(page: Any) -> Any | None:
    combined = None
    for name in _ready_control_names():
        for role in ("link", "button"):
            locator = page.get_by_role(role, name=name, exact=True)
            if combined is None:
                combined = locator
                continue
            combine = getattr(combined, "or_", None)
            if not callable(combine):
                return None
            combined = combine(locator)
    return combined


def wait_for_bop_reports_ready(page: Any) -> str:
    """Wait until a reports control is visible, or the network is idle.

    Returns ``controls`` when a known control is already there, or
    ``networkidle`` when that was the ready signal. A fixed sleep is not used.
    """
    if _any_ready_control(page):
        return "controls"
    combined = _combined_ready_locator(page)
    wait_for = getattr(combined, "wait_for", None) if combined is not None else None
    if callable(wait_for):
        try:
            wait_for(state="visible", timeout=BOP_REPORTS_READY_TIMEOUT_MS)
        except Exception as exc:
            if not _is_download_timeout(exc):
                raise IntakeHold("Progressive BOP reports page did not finish loading") from exc
        else:
            return "controls"
    idle = getattr(page, "wait_for_load_state", None)
    if callable(idle):
        try:
            idle("networkidle", timeout=BOP_REPORTS_READY_TIMEOUT_MS)
        except Exception as exc:
            raise IntakeHold("Progressive BOP reports page did not finish loading") from exc
        return "networkidle"
    raise IntakeHold("Progressive BOP reports page did not finish loading")


def _export_surface_present(page: Any) -> bool:
    return any(_count_named(page, name) == 1 for name in (_PENDING_CANCEL_PDF_EXPORT, _PENDING_CANCEL_XLS_EXPORT))


def _unique_named_control(page: Any, names: tuple[str, ...]) -> Any | None:
    matches = []
    for name in names:
        for role in ("link", "button"):
            locator = page.get_by_role(role, name=name, exact=True)
            count = _locator_count(locator)
            if count < 0 or count == 0:
                continue
            matches.append((count, locator, name))
    if not matches:
        return None
    if len(matches) != 1 or matches[0][0] != 1:
        label = matches[0][2] if len({item[2] for item in matches}) == 1 else "reports"
        raise IntakeHold(f"Progressive control {label!r} is missing or ambiguous")
    return matches[0][1]


def open_pending_cancel_report(report_page: Any) -> None:
    """Land on Pending Cancel. Do not look for controls before the page is ready.

    View Reports → Pending Cancel for Nonpayment remains the older page.
    The 2026-09-30 reports page has the Pending Cancel export buttons and no
    View Reports control. That page is already the report.
    """
    ready = wait_for_bop_reports_ready(report_page)
    view = _unique_named_control(report_page, _VIEW_REPORTS_NAMES)
    if view is None and _export_surface_present(report_page):
        return
    if view is not None:
        view.click()
        wait_for_bop_reports_ready(report_page)
        if _export_surface_present(report_page) and _unique_named_control(report_page, (_PENDING_CANCEL_NAV,)) is None:
            return
        click_named(report_page, _PENDING_CANCEL_NAV, roles=("link", "button"))
        wait_for_bop_reports_ready(report_page)
        return
    pending = _unique_named_control(report_page, (_PENDING_CANCEL_NAV,))
    if pending is not None:
        pending.click()
        wait_for_bop_reports_ready(report_page)
        return
    if ready == "networkidle":
        raise IntakeHold(
            "Progressive BOP reports page has no View Reports control and no Pending Cancel export"
        )
    raise IntakeHold("Progressive BOP reports page did not finish loading")


def navigate_to_pending_cancel(page: Any, agent_code: str) -> Any:
    """Shell FAO tab → Businessowner/Contractor GL window → pending-cancel report."""
    assert_authenticated(page)
    started_on_shell_home = _on_fao_shell_home(page)
    ensure_fao_shell_home(page)
    assert_agent_context(page, agent_code)
    report_page = open_businessowner_window(page, on_shell_home=started_on_shell_home)
    open_pending_cancel_report(report_page)
    assert_authenticated(report_page)
    return report_page


def open_policy_documents_tab(page: Any) -> None:
    click_named(page, "Policy", roles=("tab",), error=RowHold)
    locator = page.get_by_role("tab", name="Policy", exact=True)
    if locator.count() != 1 or locator.get_attribute("aria-selected") != "true":
        raise RowHold("Policy documents tab did not become selected")


def _read_html_table(table: Any) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    header_locator = table.locator("thead")
    header_count = header_locator.count()
    if header_count > 1:
        raise IntakeHold("Pending Cancel report table is missing or ambiguous")
    if header_count == 1:
        headers = tuple(_norm(node.inner_text()) for node in header_locator.locator("th").all())
        body = table.locator("tbody")
        if body.count() != 1:
            raise IntakeHold("Pending Cancel report table is missing or ambiguous")
        row_nodes = body.locator("tr").all()
        rows = tuple(tuple(_norm(cell.inner_text()) for cell in row.locator("td").all()) for row in row_nodes)
        return headers, rows
    row_nodes = tuple(table.locator("tr").all())
    if not row_nodes:
        return (), ()
    header_cells = row_nodes[0].locator("th").all() or row_nodes[0].locator("td").all()
    headers = tuple(_norm(cell.inner_text()) for cell in header_cells)
    rows = tuple(
        tuple(_norm(cell.inner_text()) for cell in row.locator("td").all())
        for row in row_nodes[1:]
    )
    return headers, rows


def extract_tables(page: Any) -> list[tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]]:
    tables = page.locator("table")
    found = []
    for index in range(tables.count()):
        found.append(_read_html_table(tables.nth(index)))
    return found


def _matching_exports(page: Any, names: tuple[str, ...]) -> tuple[Any, str] | None:
    matches = []
    for name in names:
        for role in ("link", "button"):
            locator = page.get_by_role(role, name=name, exact=True)
            count = _locator_count(locator)
            if count < 0 or count == 0:
                continue
            matches.append((locator, name, count))
    if not matches:
        return None
    if len(matches) != 1 or matches[0][2] != 1:
        raise IntakeHold("Pending Cancel report export is missing or ambiguous")
    return matches[0][0], matches[0][1]


def _finished_download_bytes(download: Any, *, label: str) -> bytes:
    """Wait until Playwright says the download finished, then read the file."""
    failure = getattr(download, "failure", None)
    if callable(failure):
        try:
            reason = failure()
        except Exception as exc:
            raise _unfinished_export_hold(label) from exc
        if reason:
            raise _unfinished_export_hold(label, str(reason))
    handle = tempfile.NamedTemporaryFile(prefix="bop-export-", suffix=".bin", delete=False)
    handle.close()
    path = Path(handle.name)
    try:
        download.save_as(str(path))
        return path.read_bytes()
    except IntakeHold:
        raise
    except Exception as exc:
        raise _unfinished_export_hold(label) from exc
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def _download_one_export(page: Any, click: Callable[[], None], *, label: str) -> bytes:
    """Click one export and keep the one finished file.

    A 0-byte or truncated file is a hold. It is not an empty policy list.
    A PDF that opens in a new Progressive tab counts when the download itself
    is empty.
    """
    context = getattr(page, "context", None)
    if context is None:
        raise IntakeHold(
            f"Pending Cancel export {label!r} did not download. "
            "A missing download is not a report with no policies."
        )
    opened: list[Any] = []

    def on_page(new_page: Any) -> None:
        opened.append(new_page)

    context.on("page", on_page)
    clicked = False
    timed_out = False
    blob = b""
    try:
        def wrapped() -> None:
            nonlocal clicked
            click()
            clicked = True

        try:
            with page.expect_download(timeout=BOP_EXPORT_TIMEOUT_MS) as download_info:
                wrapped()
            blob = _finished_download_bytes(download_info.value, label=label)
        except IntakeHold:
            raise
        except Exception as exc:
            if not clicked or not _is_download_timeout(exc):
                raise IntakeHold(
                    f"Pending Cancel export {label!r} did not download. "
                    "A missing download is not a report with no policies."
                ) from exc
            timed_out = True
        files: list[bytes] = []
        if blob:
            files.append(blob)
        for item in opened:
            if not _is_own_tab(item):
                continue
            wait = getattr(item, "wait_for_load_state", None)
            if callable(wait):
                try:
                    wait("domcontentloaded", timeout=BOP_EXPORT_TIMEOUT_MS)
                except Exception as exc:
                    if not _is_download_timeout(exc):
                        raise IntakeHold(
                            f"Pending Cancel export {label!r} did not download. "
                            "A missing download is not a report with no policies."
                        ) from exc
            files.extend(read_playwright_pdf_view(item).pdfs)
        current_url = str(getattr(page, "url", "") or "")
        if current_url.startswith("blob:") or _url_looks_like_pdf(current_url):
            files.extend(read_playwright_pdf_view(page).pdfs)
        real = [item for item in files if item]
        if not real:
            if timed_out:
                raise _unfinished_export_hold(label)
            raise _empty_export_hold(label)
        if len(real) != 1:
            raise IntakeHold("Pending Cancel report export is missing or ambiguous")
        return _assert_export_bytes(real[0], label=label)
    finally:
        remover = getattr(context, "remove_listener", None)
        if callable(remover):
            try:
                remover("page", on_page)
            except Exception:
                pass
        for item in opened:
            if not _is_own_tab(item):
                continue
            closer = getattr(item, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    pass


@dataclass(frozen=True)
class ReportDateChoice:
    """Range the reports page accepted before an export."""

    kind: str
    label: str
    start: date
    end: date


_DATE_TOGGLE_NAME = "Select Date Range"
_DATE_TOGGLE_CSS = "#dropdownMenu2"
_DATE_MENU_SIBLING = (
    "xpath=following-sibling::*["
    "contains(concat(' ', normalize-space(@class), ' '), ' dropdown-menu ')"
    " or @role='menu']"
)
_DATE_MENU_PARENT = (
    "xpath=parent::*/*["
    "contains(concat(' ', normalize-space(@class), ' '), ' dropdown-menu ')"
    " or @role='menu']"
)
_REPORT_START_CSS = ".report-start"
_REPORT_END_CSS = ".report-end"
_APPLY_NAME = "Apply"
_REPORT_DATES_MISSING = (
    "Progressive BOP Report Dates control is missing or ambiguous. "
    "The export was not downloaded."
)
_DATES_NOT_ON_PAGE = (
    "Progressive BOP Report Dates is still on Select Date Range, "
    "and the report start and report end fields were not on the page."
)


def _export_not_downloaded(detail: str) -> IntakeHold:
    return IntakeHold(f"{detail} The export was not downloaded.")


def _is_visible(locator: Any) -> bool:
    if _locator_count(locator) != 1:
        return False
    check = getattr(locator, "is_visible", None)
    if not callable(check):
        return True
    try:
        return bool(check())
    except Exception:
        return False


def _wait_visible(locator: Any) -> bool:
    if _is_visible(locator):
        return True
    wait = getattr(locator, "wait_for", None)
    if not callable(wait):
        return False
    try:
        wait(state="visible", timeout=BOP_REPORTS_READY_TIMEOUT_MS)
    except Exception as exc:
        if not _is_download_timeout(exc):
            raise _export_not_downloaded(
                "Progressive BOP Report Dates could not be set."
            ) from exc
        return False
    return _is_visible(locator)


def _same_day(observed: str, day: date) -> bool:
    if _norm(observed) == day.isoformat():
        return True
    try:
        return parse_report_date(observed) == day
    except IntakeHold:
        return False


def _date_toggle(page: Any) -> Any:
    """The Select Date Range dropdown button. The id is only a unique fallback."""
    button = page.get_by_role("button", name=_DATE_TOGGLE_NAME, exact=True)
    count = _locator_count(button)
    if count == 1:
        if not _wait_visible(button):
            raise IntakeHold(_REPORT_DATES_MISSING)
        return button
    if count != 0:
        raise IntakeHold(_REPORT_DATES_MISSING)
    fallback = page.locator(_DATE_TOGGLE_CSS)
    if _locator_count(fallback) != 1 or not _wait_visible(fallback):
        raise IntakeHold(_REPORT_DATES_MISSING)
    return fallback


def _labelled_menu(page: Any, toggle: Any) -> Any | None:
    """Bootstrap marks the open menu with aria-labelledby set to the toggle id."""
    try:
        raw = _norm(str(toggle.get_attribute("id") or ""))
    except Exception:
        return None
    if not re.fullmatch(r"[A-Za-z][\w:-]*", raw):
        return None
    menu = page.locator(f"[aria-labelledby='{raw}']")
    count = _locator_count(menu)
    if count == 1:
        return menu
    if count != 0:
        raise IntakeHold(_REPORT_DATES_MISSING)
    return None


def _unique_menu(page: Any, toggle: Any) -> Any | None:
    for selector in (_DATE_MENU_SIBLING, _DATE_MENU_PARENT):
        menu = toggle.locator(selector)
        count = _locator_count(menu)
        if count == 1:
            return menu
        if count != 0:
            raise IntakeHold(_REPORT_DATES_MISSING)
    return _labelled_menu(page, toggle)


def _open_date_menu(page: Any, toggle: Any) -> Any:
    menu = _unique_menu(page, toggle)
    if menu is not None and _menu_inputs_visible(menu):
        return menu
    try:
        toggle.click()
    except IntakeHold:
        raise
    except Exception as exc:
        raise _export_not_downloaded(
            "Progressive BOP Select Date Range could not be opened."
        ) from exc
    menu = _unique_menu(page, toggle)
    if menu is None:
        raise _export_not_downloaded(_DATES_NOT_ON_PAGE)
    return menu


def _menu_inputs_visible(menu: Any) -> bool:
    start = menu.locator(_REPORT_START_CSS)
    end = menu.locator(_REPORT_END_CSS)
    return _is_visible(start) and _is_visible(end)


def _require_menu_input(menu: Any, selector: str) -> Any:
    locator = menu.locator(selector)
    count = _locator_count(locator)
    if count != 1:
        if count == 0:
            raise _export_not_downloaded(_DATES_NOT_ON_PAGE)
        raise IntakeHold(_REPORT_DATES_MISSING)
    if not _wait_visible(locator):
        raise _export_not_downloaded(_DATES_NOT_ON_PAGE)
    return locator


def _date_text_for_input(locator: Any, day: date) -> str:
    try:
        raw = locator.get_attribute("type")
    except Exception as exc:
        raise _export_not_downloaded(
            "Progressive BOP report date input type could not be read."
        ) from exc
    kind = _norm(str(raw or "text")).casefold()
    if kind == "date":
        return day.isoformat()
    if kind in {"text", ""}:
        return day.strftime("%m/%d/%Y")
    raise _export_not_downloaded(
        f"Progressive BOP report date input type {kind} is not a date or text field."
    )


def _fill_menu_input(locator: Any, label: str, day: date) -> str:
    typed = _date_text_for_input(locator, day)
    try:
        locator.fill(typed)
        observed = _norm(str(locator.input_value() or ""))
    except IntakeHold:
        raise
    except Exception as exc:
        raise _export_not_downloaded(
            f"Progressive BOP {label} did not accept {typed}."
        ) from exc
    if not _same_day(observed, day):
        raise _export_not_downloaded(
            f"Progressive BOP {label} did not accept {typed}."
        )
    return typed


def _click_apply(menu: Any) -> None:
    button = menu.get_by_role("button", name=_APPLY_NAME, exact=True)
    count = _locator_count(button)
    if count != 1 or not _wait_visible(button):
        raise _export_not_downloaded(
            "Progressive BOP Apply button is missing or ambiguous."
        )
    try:
        button.click()
    except IntakeHold:
        raise
    except Exception as exc:
        raise _export_not_downloaded(
            "Progressive BOP Apply did not run."
        ) from exc


def _button_shows_day(text: str, day: date) -> bool:
    found: list[date] = []
    for match in re.finditer(r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4}", text):
        try:
            found.append(parse_report_date(match.group(0)))
        except IntakeHold:
            continue
    if not found:
        return False
    return all(item == day for item in found)


def _range_was_accepted(start: Any, end: Any, toggle: Any, day: date) -> bool:
    try:
        if _same_day(_norm(str(start.input_value() or "")), day) and _same_day(
            _norm(str(end.input_value() or "")), day
        ):
            return True
    except Exception:
        pass
    try:
        text = _norm(str(toggle.inner_text() or ""))
    except Exception as exc:
        raise _export_not_downloaded(
            "Progressive BOP Report Dates could not be read after it was set."
        ) from exc
    return _button_shows_day(text, day)


def _wait_for_report_refresh(page: Any) -> None:
    """Let the report query finish after the date is accepted. Not a sleep."""
    idle = getattr(page, "wait_for_load_state", None)
    if not callable(idle):
        return
    try:
        idle("networkidle", timeout=BOP_REPORTS_READY_TIMEOUT_MS)
    except Exception as exc:
        raise _export_not_downloaded(
            "Progressive BOP reports page did not finish loading after Report Dates was set."
        ) from exc


def apply_bop_report_dates(page: Any, report_date: date) -> ReportDateChoice:
    """Open Select Date Range and set both menu inputs to ``report_date``.

    The live control is a dropdown button, not a Report Dates combobox.
    ``#dropdownMenu2`` is used only when that button name is missing and the
    id matches one element. The open menu is the toggle's next sibling, a
    direct child of its parent, or the one element labelled by the toggle
    id. The date inputs are ``.report-start`` and ``.report-end`` inside
    that menu. A ``type=date`` input is filled
    with ``YYYY-MM-DD``. A text input is filled with ``MM/DD/YYYY``. Apply
    is clicked, then the input values or the button text must show that
    day. The network is idle before the caller exports.
    """
    toggle = _date_toggle(page)
    menu = _open_date_menu(page, toggle)
    start = _require_menu_input(menu, _REPORT_START_CSS)
    end = _require_menu_input(menu, _REPORT_END_CSS)
    _fill_menu_input(start, "report start", report_date)
    _fill_menu_input(end, "report end", report_date)
    _click_apply(menu)
    if not _range_was_accepted(start, end, toggle, report_date):
        raise _export_not_downloaded(
            f"Progressive BOP Report Dates did not accept {report_date.isoformat()}."
        )
    _wait_for_report_refresh(page)
    return ReportDateChoice(
        kind="exact",
        label=f"{report_date.isoformat()} to {report_date.isoformat()}",
        start=report_date,
        end=report_date,
    )


def _page_offers_export(page: Any) -> bool:
    return (
        _matching_exports(page, _PDF_EXPORTS) is not None
        or _matching_exports(page, _EXCEL_EXPORTS) is not None
    )


def download_export(page: Any, names: tuple[str, ...]) -> bytes | None:
    found = _matching_exports(page, names)
    if found is None:
        return None
    locator, label = found

    def click() -> None:
        locator.click()

    return _download_one_export(page, click, label=label)


def _export_failure_can_try_the_other(exc: BaseException) -> bool:
    text = str(exc).casefold()
    return (
        "empty file" in text
        or "truncated" in text
        or "did not finish" in text
        or "did not download" in text
        or "policies are missing" in text
        or "not a pdf" in text
        or "not a list this pull can read" in text
    )


def read_report_from_page(page: Any, report_date: date) -> PendingCancelReport:
    tables = extract_tables(page)
    policy_tables = [table for table in tables if _header_indexes(table[0], kind="report")]
    if len(policy_tables) > 1:
        raise IntakeHold("Pending Cancel report table is missing or ambiguous")
    if len(policy_tables) == 1:
        text = str(page.locator("body").inner_text() or "")
        return report_from_extracted(
            tables=tables,
            page_text=text,
            excel_bytes=None,
            pdf_bytes=None,
            report_date=report_date,
        )
    if _page_offers_export(page):
        apply_bop_report_dates(page, report_date)
    pdf_hold: IntakeHold | None = None
    if _matching_exports(page, _PDF_EXPORTS) is not None:
        try:
            pdf_bytes = download_export(page, _PDF_EXPORTS)
            if pdf_bytes:
                return parse_pdf_report(pdf_bytes, report_date=report_date)
        except IntakeHold as exc:
            pdf_hold = exc
    if _matching_exports(page, _EXCEL_EXPORTS) is not None and (
        pdf_hold is None or _export_failure_can_try_the_other(pdf_hold)
    ):
        try:
            excel_bytes = download_export(page, _EXCEL_EXPORTS)
        except IntakeHold as exc:
            if pdf_hold is not None:
                raise IntakeHold(f"{pdf_hold} {exc}") from exc
            raise
        if excel_bytes:
            return parse_xls_export(excel_bytes, report_date=report_date)
    if pdf_hold is not None:
        raise pdf_hold
    pdf_bytes = _embedded_pdf(page)
    text = str(page.locator("body").inner_text() or "")
    return report_from_extracted(
        tables=tables,
        page_text=text,
        excel_bytes=None,
        pdf_bytes=pdf_bytes,
        report_date=report_date,
    )


def _embedded_pdf(page: Any) -> bytes | None:
    embeds = page.locator("embed[type='application/pdf'], iframe")
    if embeds.count() == 0:
        return None
    if embeds.count() != 1:
        raise IntakeHold("Pending Cancel report export is missing or ambiguous")
    src = str(embeds.get_attribute("src") or "")
    if not src:
        return None
    view = read_playwright_pdf_view(page)
    if len(view.pdfs) == 1:
        return view.pdfs[0]
    if src.startswith("blob:"):
        blob = _read_blob(page, src)
        return blob or None
    if _allowed_pdf_url(src):
        return _http_get(page, src) or None
    raise IntakeHold("Pending Cancel report export is missing or ambiguous")


class PlaywrightFaoBopBrowser:
    """Drive one already-authenticated FAO tab. Does not type credentials."""

    def __init__(self, page: Any, *, agent_code: str = DEFAULT_AGENT_CODE):
        self.shell = page
        self.agent_code = require_agent_code(agent_code)
        self.report_page: Any = None
        self.shell_blocked: str | None = None
        self._doc_rows: tuple[Any, ...] = ()

    def load_report(self, report_date: date) -> ReportCapture:
        self.report_page = navigate_to_pending_cancel(self.shell, self.agent_code)
        png = require_png(self.report_page.screenshot(full_page=True, type="png"))
        try:
            report = read_report_from_page(self.report_page, report_date)
        except IntakeHold as exc:
            return ReportCapture(png=png, report=None, error=str(exc))
        return ReportCapture(png=png, report=report, error=None)

    def download_notice(self, policy: PendingCancelPolicy) -> bytes:
        assert_authenticated(self.shell)
        assert_agent_context(self.shell, self.agent_code)
        home = str(getattr(self.shell, "url", "") or "")
        try:
            fill_policy_search(self.shell, policy.policy_number)
            click_named(self.shell, "Search", roles=("button",), error=RowHold)
            click_named(self.shell, "Documents", roles=("link", "tab", "button"), error=RowHold)
            open_policy_documents_tab(self.shell)
            documents, rows = extract_policy_documents(self.shell)
            self._doc_rows = rows
            selected = select_notice(documents, policy.report_date)
            if selected.row_index >= len(rows):
                raise RowHold("Notice of Non Payment open control is missing or ambiguous")
            target = rows[selected.row_index]

            def open_doc() -> None:
                click_notice_on_row(target)

            content = pdf_bytes_from_observation(collect_pdf(self.shell, open_doc))
        except RowHold:
            self._return_to_shell(home)
            raise
        except IntakeHold:
            self._return_to_shell(home)
            raise
        self._return_to_shell(home)
        return content

    def _return_to_shell(self, home: str) -> None:
        current = str(getattr(self.shell, "url", "") or "")
        if not home or current == home:
            return
        go_back = getattr(self.shell, "go_back", None)
        if callable(go_back):
            try:
                go_back()
            except Exception as exc:
                self.shell_blocked = "FAO search could not continue"
                raise IntakeHold("FAO search could not continue") from exc
        if str(getattr(self.shell, "url", "") or "") != home:
            self.shell_blocked = "FAO search could not continue"
            raise IntakeHold("FAO search could not continue")


def extract_policy_documents(page: Any) -> tuple[tuple[PolicyDocument, ...], tuple[Any, ...]]:
    tables = page.locator("table")
    matched: list[tuple[Any, dict[str, int], tuple[tuple[str, ...], ...]]] = []
    for index in range(tables.count()):
        table = tables.nth(index)
        headers, grid_rows = _read_html_table(table)
        indexes = _header_indexes(headers, kind="documents")
        if indexes is not None:
            matched.append((table, indexes, grid_rows))
    if len(matched) != 1:
        raise RowHold("Policy documents are missing or ambiguous")
    table, indexes, grid_rows = matched[0]
    body = table.locator("tbody")
    if body.count() == 1:
        row_nodes = tuple(body.locator("tr").all())
    else:
        row_nodes = tuple(table.locator("tr").all())[1:]
    if len(row_nodes) != len(grid_rows):
        raise RowHold("Policy documents are missing or ambiguous")
    documents: list[PolicyDocument] = []
    for index, cells in enumerate(grid_rows):
        if not any(cells):
            continue
        if indexes["name"] >= len(cells):
            raise RowHold("Policy documents are missing or ambiguous")
        name = cells[indexes["name"]]
        if not name:
            continue
        document_date = None
        if "document_date" in indexes and indexes["document_date"] < len(cells) and cells[indexes["document_date"]]:
            try:
                document_date = parse_report_date(cells[indexes["document_date"]])
            except IntakeHold as exc:
                raise RowHold("Notice of Non Payment date is ambiguous") from exc
        documents.append(PolicyDocument(name=name, document_date=document_date, row_index=index))
    return tuple(documents), row_nodes


def click_notice_on_row(row: Any) -> None:
    matches = []
    for role in ("link", "button"):
        locator = row.get_by_role(role)
        count = locator.count()
        if count == 0:
            continue
        if count != 1 and not hasattr(locator, "all"):
            raise RowHold("Notice of Non Payment open control is missing or ambiguous")
        nodes = locator.all() if hasattr(locator, "all") else [locator]
        for node in nodes:
            name = _norm(getattr(node, "inner_text", lambda: "")())
            if _NOTICE.search(name):
                matches.append(node)
    if len(matches) != 1:
        raise RowHold("Notice of Non Payment open control is missing or ambiguous")
    matches[0].click()


def pdf_bytes_from_observation(observation: PdfObservation) -> bytes:
    candidates: list[bytes] = []
    for blob in observation.downloads:
        if isinstance(blob, memoryview):
            blob = blob.tobytes()
        if not isinstance(blob, (bytes, bytearray)):
            raise IntakeHold("Notice of Non Payment capture is missing or ambiguous")
        if not blob:
            continue
        if not _is_pdf(blob):
            raise IntakeHold("Notice of Non Payment download is not a PDF")
        candidates.append(bytes(blob))
    for view in observation.pages:
        candidates.extend(view.pdfs)
    digests = {hashlib.sha256(blob).digest() for blob in candidates}
    if len(digests) != 1:
        raise IntakeHold("Notice of Non Payment capture is missing or ambiguous")
    return candidates[0]


def read_playwright_pdf_view(page: Any) -> PagePdfView:
    url = str(getattr(page, "url", "") or "")
    candidates: list[bytes] = []
    seen: set[str] = set()

    def add(target: str) -> None:
        if not target or target in seen:
            return
        seen.add(target)
        if target.startswith("blob:"):
            _push_pdf(candidates, _read_blob(page, target))
            return
        if not _allowed_pdf_url(target):
            return
        _push_pdf(candidates, _http_get(page, target))

    if url.startswith("blob:") or _url_looks_like_pdf(url):
        add(url)
    for found in _REMOTE_PDF.findall(url):
        add(found)
    for src in _embed_sources(page):
        add(src)
        for found in _REMOTE_PDF.findall(src):
            add(found)
    return PagePdfView(url=url, pdfs=tuple(candidates))


_OWN_TAB_HOSTS = ("foragentsonly.com", "progressive.com", "progressivecommercial.com", "americanstrategic.com")


def _is_own_tab(page: Any) -> bool:
    """A tab this pull may read or close: Progressive (or blank/blob) only.

    Another agent can open a tab (for example EZLynx) in the same browser
    while a memo opens. That tab is never read or closed here.
    """
    url = str(getattr(page, "url", "") or "")
    host = (urllib.parse.urlsplit(url).hostname or "").casefold()
    if not host:
        return True
    return any(host == own or host.endswith("." + own) for own in _OWN_TAB_HOSTS)


def collect_pdf(page: Any, open_document: Callable[[], None], *, timeout_ms: int = DOWNLOAD_TIMEOUT_MS) -> PdfObservation:
    context = page.context
    opened: list[Any] = []

    def on_page(new_page: Any) -> None:
        opened.append(new_page)

    context.on("page", on_page)
    downloads: list[bytes] = []
    clicked = False
    try:
        def wrapped() -> None:
            nonlocal clicked
            open_document()
            clicked = True

        try:
            with page.expect_download(timeout=timeout_ms) as download_info:
                wrapped()
            downloads.append(_download_bytes(download_info.value))
        except (IntakeHold, RowHold):
            raise
        except Exception as exc:
            if not clicked or not _is_download_timeout(exc):
                raise IntakeHold("Notice of Non Payment capture is missing or ambiguous") from exc
        for item in opened:
            wait = getattr(item, "wait_for_load_state", None)
            if not callable(wait):
                continue
            try:
                wait("domcontentloaded", timeout=timeout_ms)
            except Exception as exc:
                if not _is_download_timeout(exc):
                    raise IntakeHold("Notice of Non Payment capture is missing or ambiguous") from exc
        views = [read_playwright_pdf_view(item) for item in opened if _is_own_tab(item)]
        current_url = str(getattr(page, "url", "") or "")
        if current_url.startswith("blob:") or _url_looks_like_pdf(current_url):
            views.append(read_playwright_pdf_view(page))
        return PdfObservation(downloads=tuple(downloads), pages=tuple(views))
    finally:
        remover = getattr(context, "remove_listener", None)
        if callable(remover):
            try:
                remover("page", on_page)
            except Exception:
                pass
        for item in opened:
            if not _is_own_tab(item):
                continue
            closer = getattr(item, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    pass


def connect_cdp_browser(cdp_url: str | None, *, agent_code: str) -> tuple[PlaywrightFaoBopBrowser, Callable[[], None]]:
    require_test()
    url = require_loopback_cdp(cdp_url or os.environ.get("ROBIE_BROWSER_CDP_URL") or DEFAULT_CDP_URL)
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(url)
        pages = [page for context in browser.contexts for page in context.pages]
        return PlaywrightFaoBopBrowser(select_fao_page(pages), agent_code=agent_code), playwright.stop
    except Exception:
        playwright.stop()
        raise


def select_fao_page(pages: list[Any]) -> Any:
    matches = [page for page in pages if _is_fao_app_url(str(getattr(page, "url", "") or ""))]
    if len(matches) != 1:
        raise IntakeHold("Expected exactly one Progressive FAO tab")
    return matches[0]


def require_loopback_cdp(url: str) -> str:
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in {"127.0.0.1", "localhost"}:
        raise IntakeHold("Progressive FAO browser attach must use the local Test CDP endpoint")
    return str(url).strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Pull Progressive BOP Pending Cancel nonpayment notices (Test only)",
    )
    parser.add_argument("--report-date", required=True, help="Pending Cancel report date, YYYY-MM-DD")
    parser.add_argument(
        "--output-root",
        default=str(DEFAULT_OUTPUT_ROOT),
        help="QA root. Writes a private YYYY-MM-DD folder "
        "(default: hermes-test carrier-pull-qa/progressive-bop)",
    )
    parser.add_argument(
        "--upload-drive",
        action="store_true",
        help="Upload that day's QA folder under Nicole / Progressive BOP. Not implemented; fails closed.",
    )
    parser.add_argument("--agent-code", default=os.environ.get("PROGRESSIVE_FAO_AGENT_CODE", DEFAULT_AGENT_CODE))
    parser.add_argument("--cdp-url", default=None, help="Loopback CDP URL. Defaults to 127.0.0.1:9222")
    return parser


def main(argv: list[str] | None = None, *, browser_factory: Callable[[argparse.Namespace], Any] | None = None) -> int:
    args = build_parser().parse_args(argv)
    closer: Callable[[], None] | None = None
    portal: BopPendingCancelPortal | None = None
    try:
        require_test()
        try:
            report_date = date.fromisoformat(args.report_date)
        except ValueError as exc:
            raise IntakeHold("Pending Cancel report date is missing or ambiguous") from exc
        agent_code = require_agent_code(args.agent_code)
        output = Path(args.output_root)
        ledger = LocalNocLedger(output)
        ledger.ensure_private()
        archive = SourceArchive(output / "sources")
        if browser_factory is None:
            browser, closer = connect_cdp_browser(args.cdp_url, agent_code=agent_code)
        else:
            browser = browser_factory(args)
        portal = BopPendingCancelPortal(
            browser, ledger, archive, agent_code=agent_code, report_date=report_date,
        )
        verification = portal.pull()
        if args.upload_drive:
            refuse_bop_drive_upload(ledger, report_date=report_date, verification=verification)
        status = str((verification or {}).get("status") or "PULLED")
        _emit({
            "status": status,
            "scope": BOP_SCOPE,
            "process": "progressive",
            "agent_code": agent_code,
            "report_date": report_date.isoformat(),
            "policies_on_report": (verification or {}).get("policies_on_report", 0),
            "successful_noc": (verification or {}).get("successful_noc", 0),
            "pdfs": (verification or {}).get("pdfs", 0),
            "output": (verification or {}).get("pack"),
            "policies": [_outcome_dict(row) for row in portal.outcomes],
            "verification": verification,
            "ezlynx": "not_run",
        })
        return 0
    except IntakeHold as exc:
        payload: dict[str, Any] = {"status": "HELD", "reason": str(exc), "ezlynx": "not_run"}
        if portal is not None and portal.verification is not None:
            payload["verification"] = portal.verification
            payload["output"] = portal.verification.get("pack")
        _emit(payload)
        return 2
    except Exception as exc:
        _emit({
            "status": "UNVERIFIED",
            "reason": f"Progressive BOP pending-cancel pull unavailable ({type(exc).__name__})",
            "ezlynx": "not_run",
        })
        return 1
    finally:
        if closer is not None:
            closer()


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


def _received_at(day: date) -> str:
    return datetime.combine(day, time(12, 0), tzinfo=_EASTERN).isoformat()


def _is_pdf(blob: bytes | bytearray | None) -> bool:
    return isinstance(blob, (bytes, bytearray)) and len(blob) >= 8 and bytes(blob).startswith(b"%PDF")


def _push_pdf(candidates: list[bytes], blob: bytes) -> None:
    if not blob:
        return
    if not _is_pdf(blob):
        raise IntakeHold("Notice of Non Payment download is not a PDF")
    candidates.append(blob)


def _url_looks_like_pdf(url: str) -> bool:
    path = urllib.parse.urlsplit(url).path.lower()
    return path.endswith(".pdf") or "application/pdf" in url.lower()


def _allowed_pdf_url(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https":
        return False
    return (
        host == "foragentsonly.com"
        or host.endswith(".foragentsonly.com")
        or         host == "progressive.com"
        or host.endswith(".progressive.com")
        or host == _BOP_APP_HOST
        or host.endswith("." + _BOP_APP_HOST)
    )


def _is_fao_app_url(url: str) -> bool:
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return False
    if host.endswith("foragentsonlylogin.progressive.com") or parsed.path.rstrip("/").lower().endswith("/login"):
        return False
    return host == "foragentsonly.com" or host.endswith(".foragentsonly.com")


def _embed_sources(page: Any) -> list[str]:
    sources: list[str] = []
    for item in _each(page.locator("embed[type='application/pdf']")):
        src = str(item.get_attribute("src") or "")
        if src:
            sources.append(src)
    for item in _each(page.locator("iframe")):
        src = str(item.get_attribute("src") or "")
        if src.startswith("blob:") or _url_looks_like_pdf(src) or _REMOTE_PDF.search(src):
            sources.append(src)
    return sources


def _each(locator: Any) -> list[Any]:
    all_fn = getattr(locator, "all", None)
    if callable(all_fn):
        try:
            return list(all_fn())
        except Exception:
            return []
    if locator.count() == 0:
        return []
    if locator.count() != 1:
        raise IntakeHold("Notice of Non Payment capture is missing or ambiguous")
    return [locator]


def _read_blob(page: Any, url: str) -> bytes:
    payload = page.evaluate(_BLOB_JS, url)
    if not payload:
        return b""
    if not isinstance(payload, str):
        raise IntakeHold("Notice of Non Payment capture is missing or ambiguous")
    try:
        return base64.b64decode(payload, validate=True)
    except Exception as exc:
        raise IntakeHold("Notice of Non Payment capture is missing or ambiguous") from exc


def _http_get(page: Any, url: str) -> bytes:
    response = page.context.request.get(url, timeout=DOWNLOAD_TIMEOUT_MS)
    if getattr(response, "ok", True) is False:
        raise IntakeHold("Notice of Non Payment capture is missing or ambiguous")
    body = response.body()
    if not isinstance(body, (bytes, bytearray)):
        raise IntakeHold("Notice of Non Payment capture is missing or ambiguous")
    return bytes(body)


def _download_bytes(download: Any) -> bytes:
    handle = tempfile.NamedTemporaryFile(prefix="bop-noc-", suffix=".pdf", delete=False)
    handle.close()
    path = Path(handle.name)
    try:
        download.save_as(str(path))
        return path.read_bytes()
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def _is_download_timeout(exc: BaseException) -> bool:
    return "Timeout" in type(exc).__name__ or "Timeout" in str(exc)


def module_import_names() -> set[str]:
    """Import names in this file. Used to prove the pull does not import EZLynx."""
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


if __name__ == "__main__":
    raise SystemExit(main())
