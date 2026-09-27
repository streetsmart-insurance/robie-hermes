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
click. That step does not ask Gemini. A missing or ambiguous Home control
holds.

Accessible names are the playbook, not a certified live DOM. Zero or multiple
matches hold. Live FAO on hermes-test-01 is UNVERIFIED.
"""
from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.parse
from dataclasses import dataclass
from datetime import date, datetime, time
from io import BytesIO
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
_EXCEL_EXPORTS = ("Excel", "Export to Excel", "Download Excel", "Export Excel")
_PDF_EXPORTS = ("PDF", "Export to PDF", "Download PDF", "Export PDF")
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


def parse_excel_report(blob: bytes, *, report_date: date) -> PendingCancelReport:
    if not blob:
        raise IntakeHold("Pending Cancel report workbook is missing or ambiguous")
    try:
        import openpyxl
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
    if not _is_pdf(blob):
        raise IntakeHold("Pending Cancel report is missing or ambiguous")
    try:
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(blob))
        if not reader.pages:
            raise IntakeHold("Pending Cancel report is missing or ambiguous")
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    except IntakeHold:
        raise
    except Exception as exc:
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


def open_businessowner_window(page: Any) -> Any:
    click_named(page, "Manage Policies", roles=("link", "button"))
    try:
        with page.expect_popup(timeout=DOWNLOAD_TIMEOUT_MS) as popup:
            click_named(page, "Businessowner/Contractor GL", roles=("link", "button"))
        opened = popup.value
    except IntakeHold:
        raise
    except Exception as exc:
        raise IntakeHold("Businessowner/Contractor GL did not open a single new window") from exc
    if opened is None:
        raise IntakeHold("Businessowner/Contractor GL did not open a single new window")
    return opened


def open_pending_cancel_report(report_page: Any) -> None:
    click_named(report_page, "View Reports", roles=("link", "button"))
    click_named(report_page, "Pending Cancel for Nonpayment", roles=("link", "button"))


def navigate_to_pending_cancel(page: Any, agent_code: str) -> Any:
    """Shell FAO tab → Businessowner/Contractor GL window → pending-cancel report."""
    assert_authenticated(page)
    ensure_fao_shell_home(page)
    assert_agent_context(page, agent_code)
    report_page = open_businessowner_window(page)
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


def _matching_exports(page: Any, names: tuple[str, ...]) -> list[Any]:
    matches = []
    for name in names:
        for role in ("link", "button"):
            locator = page.get_by_role(role, name=name, exact=True)
            count = locator.count()
            if count:
                matches.append(locator)
                if count != 1:
                    raise IntakeHold("Pending Cancel report export is missing or ambiguous")
    if len(matches) > 1:
        raise IntakeHold("Pending Cancel report export is missing or ambiguous")
    return matches


def download_export(page: Any, names: tuple[str, ...]) -> bytes | None:
    matches = _matching_exports(page, names)
    if not matches:
        return None
    locator = matches[0]

    def click() -> None:
        locator.click()

    observation = collect_pdf(page, click)
    blobs = list(observation.downloads)
    for view in observation.pages:
        blobs.extend(view.pdfs)
    if len(blobs) != 1:
        raise IntakeHold("Pending Cancel report export is missing or ambiguous")
    return blobs[0]


def read_report_from_page(page: Any, report_date: date) -> PendingCancelReport:
    tables = extract_tables(page)
    policy_tables = [table for table in tables if _header_indexes(table[0], kind="report")]
    excel_bytes = None
    pdf_bytes = None
    if len(policy_tables) == 0:
        excel_bytes = download_export(page, _EXCEL_EXPORTS)
        if excel_bytes is None:
            pdf_bytes = download_export(page, _PDF_EXPORTS)
            if pdf_bytes is None:
                pdf_bytes = _embedded_pdf(page)
    text = str(page.locator("body").inner_text() or "")
    return report_from_extracted(
        tables=tables,
        page_text=text,
        excel_bytes=excel_bytes,
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
        views = [read_playwright_pdf_view(item) for item in opened]
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
        or host == "progressive.com"
        or host.endswith(".progressive.com")
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
