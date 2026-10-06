"""Test-only Progressive FAO Pending Cancellation document pull.

The playbook opens an already signed-in FAO tab (www.foragentsonly.com),
loads the "Policies pending cancel or renewal" report directly, extracts rows
from all three tabs (Non-Payment, Underwriting, Renewals), and for each policy
follows Route A: click the policy-number link ("View Policy Summary") on
clpolicy.foragentsonly.com, open the DOCUMENTS tab, and pull cancellation
notice documents from the Policy Documents table. For UNDERWRITING tab rows
it also pulls standalone underwriting memos (additional-information requests,
agent review notices) into a separate ``uw_memos`` receipt section.

PDFs open inline in Chrome's PDF viewer via /Express/PDFHandler.ashx on
clpolicy.foragentsonly.com. Bytes are read back from the viewer tab (blob or
an HTTP GET on an allowed Progressive host). A document that is not a PDF is
never kept; a document that cannot be captured is recorded HELD, never
claimed as downloaded. Billing and renewal paperwork are never targeted.

This module does not log in, does not handle MFA, does not upload, note,
task, or label in EZLynx, and does not register a timer.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import sys
import urllib.parse
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .intake_core import IntakeHold, SourceArchive, SourceItem
from .progressive_agent_context import (
    DEFAULT_AGENT_CODE,
    assert_agent_context,
    require_agent_code,
)
from .progressive_fao_memo import read_playwright_pdf_view

PROCESS = "fao"
SCOPE = "pending_cancellation"
FAO_HOST = "www.foragentsonly.com"
CL_POLICY_HOST = "clpolicy.foragentsonly.com"
REPORT_PATH = "/managepolicies/reports/policiesneedservice/policiespendingcancellation/"
REPORT_URL = f"https://{FAO_HOST}{REPORT_PATH}"
DEFAULT_CDP_URL = "http://127.0.0.1:9222"
DOWNLOAD_TIMEOUT_MS = 8000
LEDGER_NAME = "fao-cancellation-ledger.json"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_PDF_MAGIC = b"%PDF"
DEFAULT_OUTPUT_ROOT = Path(
    "/opt/streetsmart-hermes-test/robie-job-engine/data/artifacts/carrier-pull-qa/progressive"
)
# Shared Drive "Robie Carrier Pull QA (Nicole)". The Progressive child folder
# id is UNVERIFIED (inherited from the FAO memo pull). Folder upload is TODO.
# --upload-drive fails closed and does not call Google.
CARRIER_QA_DRIVE_PARENT_ID = "1cLEpR-0T6KdiVjcdAr0qpGTO447MetI2"
DRIVE_QA_FOLDER_NAME = "Robie Carrier Pull QA (Nicole)/Progressive"
DRIVE_UPLOAD_UNAVAILABLE = (
    "Drive upload of the Progressive QA pack is not available; "
    "refusing to report the pack as uploaded"
)
HERMES_TEST_HOST = "hermes-test-01"
# Live policy numbers are numeric, 6-12 digits (e.g. 970498127, 876263535).
_POLICY_NUMBER = re.compile(r"^\d{6,12}$")
# Report grid headers (column order may vary; matching is by alias).
_REPORT_HEADERS = (
    ("insured_name", frozenset({"primary named insured", "insured", "insured name", "named insured"})),
    ("policy_number", frozenset({"policy number", "policy", "policy #", "pol #"})),
    ("product", frozenset({"product"})),
    ("state", frozenset({"state"})),
    ("agent_code", frozenset({"agent code", "agency", "agt"})),
    ("producer", frozenset({"producer"})),
    ("cancel_date", frozenset({"cancel effective date", "cancellation date", "effective date", "cancel date"})),
    ("amount_due", frozenset({"amount due", "amount"})),
)
# DOCUMENTS tab table headers.
_DOC_HEADERS = (
    ("date", frozenset({"date", "document date", "issued date"})),
    ("delivery", frozenset({"delivery", "delivery method", "delivered via"})),
    ("name", frozenset({"document name", "document", "description", "name"})),
)
# The three report tabs, in display order, with the reason recorded per row.
_REPORT_TABS = (
    ("Pending Cancellation Due to Non-Payment", "NON-PAYMENT"),
    ("Pending Cancellation Due to Underwriting Reasons", "UNDERWRITING"),
    ("Pending Renewals", "RENEWAL"),
)
# Document scope: cancellation, pending-cancellation, pre-cancellation /
# intent notices. Billing and renewal paperwork are out of scope.
_CANCELLATION_TERMS = frozenset({
    "cancellation", "cancel", "cancelled", "canceled",
    "notice of cancellation", "pending cancellation",
    "intent to cancel", "pre-cancellation",
})
_CANCELLATION_DOC_TYPES = frozenset({"CANCNTC"})
# Underwriting memo scope (UNDERWRITING tab rows only): standalone
# underwriting memos, additional-information requests, agent review notices.
# Billing and renewal paperwork are explicitly out even if memo-shaped.
_MEMO_TERMS = frozenset({
    "memo", "underwriting", "additional information", "info requested",
    "information requested", "information needed", "agent review",
    "documentation required", "proof of",
})
_BILLING_TERMS = frozenset({
    "billing", "invoice", "payment", "premium", "statement",
})
_RENEWAL_TERMS = frozenset({
    "renewal", "renew ", " renew", "renewal offer", "renewal reminder",
})
_LOGIN_PATHS = ("/login", "/logon", "/signin", "/auth")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _is_pdf(content: bytes) -> bool:
    return bytes(content or b"")[:4] == _PDF_MAGIC


def parse_carrier_date(text: str) -> date:
    """Parse FAO date text: MM/DD/YYYY, ISO, or ISO-8601 with time."""
    cleaned = _norm(text)
    for fmt in ("%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(cleaned.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    raise IntakeHold(f"Progressive FAO date is missing or ambiguous: {cleaned!r}")


def require_policy_number(text: str) -> str:
    cleaned = _norm(text).replace(" ", "")
    if not _POLICY_NUMBER.fullmatch(cleaned):
        raise IntakeHold(f"Progressive FAO policy number is missing or ambiguous: {text!r}")
    return cleaned


@dataclass(frozen=True)
class CancellationRow:
    policy_number: str
    insured_name: str
    cancel_date: date
    amount_due: str
    reason: str
    list_url: str

    @property
    def document_id(self) -> str:
        return (
            f"fao:{self.policy_number}:{self.cancel_date.isoformat()}:"
            f"{self.reason.lower().replace('-', '')}"
        )


@dataclass(frozen=True)
class FaoDocument:
    policy_number: str
    document_name: str
    document_date: date
    delivery: str
    row_index: int

    @property
    def document_id(self) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", self.document_name.casefold()).strip("-")
        return f"fao:{self.policy_number}:{self.document_date.isoformat()}:{slug}"

    @property
    def filename(self) -> str:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", self.document_name).strip("_")
        return f"{self.policy_number} {safe or 'document'} Progressive.pdf"

    @property
    def memo_document_id(self) -> str:
        """Durable ledger identity for an underwriting memo."""
        slug = re.sub(r"[^a-z0-9]+", "-", self.document_name.casefold()).strip("-")
        return f"progressive:{self.policy_number}:memo:{slug}"

    @property
    def memo_filename(self) -> str:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", self.document_name).strip("_")
        return f"{self.policy_number} {safe or 'document'} UW Memo Progressive.pdf"


def is_cancellation_document(document_name: str) -> bool:
    key = _norm(document_name).casefold()
    return any(term in key for term in _CANCELLATION_TERMS)


def is_billing_document(document_name: str) -> bool:
    key = _norm(document_name).casefold()
    return any(term in key for term in _BILLING_TERMS)


def is_renewal_document(document_name: str) -> bool:
    key = _norm(document_name).casefold()
    return any(term in key for term in _RENEWAL_TERMS)


def is_underwriting_memo(document_name: str) -> bool:
    """A standalone UW memo: memo-shaped but not a cancellation, billing, or renewal doc.

    Cancellation documents are handled by the cancellation flow and are never
    double-counted as memos. Billing and renewal paperwork are out of scope
    even when memo-shaped (e.g. "Billing Memo").
    """
    if is_cancellation_document(document_name):
        return False
    if is_billing_document(document_name):
        return False
    if is_renewal_document(document_name):
        return False
    key = _norm(document_name).casefold()
    return any(term in key for term in _MEMO_TERMS)


def parse_cancellations_report(
    tab_label: str,
    headers: tuple[str, ...],
    rows: tuple[tuple[str, ...], ...],
    *,
    list_url: str,
) -> tuple[CancellationRow, ...]:
    """Parse one tab of the Policies pending cancel or renewal report.

    Live columns (order may vary): Primary Named Insured, Policy Number,
    Product, State, Agent Code, Producer, Cancel Effective Date, Amount Due
    (+ a trailing reason cell). Header matching is by alias so column order
    is not load-bearing. ``tab_label`` selects the recorded reason.
    """
    reasons = {label.casefold(): reason for label, reason in _REPORT_TABS}
    reason = reasons.get(_norm(tab_label).casefold())
    if reason is None:
        raise IntakeHold(f"Progressive FAO report tab is missing or ambiguous: {tab_label!r}")
    normed = [_norm(h).casefold() for h in headers]
    indexes: dict[str, int] = {}
    for field, names in _REPORT_HEADERS:
        matches = [i for i, h in enumerate(normed) if h in names]
        if len(matches) != 1:
            raise IntakeHold("Progressive FAO report headers are missing or ambiguous")
        indexes[field] = matches[0]
    parsed: list[CancellationRow] = []
    for cells in rows:
        if len(cells) < len(headers):
            raise IntakeHold("Progressive FAO report row is missing or ambiguous")
        policy = require_policy_number(cells[indexes["policy_number"]])
        insured = _norm(cells[indexes["insured_name"]])
        if not insured:
            raise IntakeHold("Progressive FAO report insured name is missing or ambiguous")
        cancel = parse_carrier_date(cells[indexes["cancel_date"]])
        parsed.append(
            CancellationRow(
                policy_number=policy,
                insured_name=insured,
                cancel_date=cancel,
                amount_due=_norm(cells[indexes["amount_due"]]),
                reason=reason,
                list_url=list_url,
            )
        )
    if not parsed:
        raise IntakeHold("Progressive FAO report tab has no rows")
    return tuple(parsed)


def parse_policy_documents(
    headers: tuple[str, ...],
    rows: tuple[tuple[str, ...], ...],
    *,
    policy_number: str,
) -> tuple[FaoDocument, ...]:
    """Parse the Policy Documents table on the CL Express DOCUMENTS tab.

    Live columns: (icon) | Date | Delivery | Document name. The icon column
    carries no text; ``rows`` keep the same cell order with the icon cell
    first. Row order is preserved so the browser can click by row index.
    """
    normed = [_norm(h).casefold() for h in headers]
    indexes: dict[str, int] = {}
    for field, names in _DOC_HEADERS:
        matches = [i for i, h in enumerate(normed) if h in names]
        if len(matches) != 1:
            raise IntakeHold("Progressive FAO document list headers are missing or ambiguous")
        indexes[field] = matches[0]
    docs: list[FaoDocument] = []
    for row_index, cells in enumerate(rows):
        if len(cells) < len(headers):
            raise IntakeHold("Progressive FAO document row is missing or ambiguous")
        name = _norm(cells[indexes["name"]])
        if not name:
            raise IntakeHold("Progressive FAO document name is missing or ambiguous")
        docs.append(
            FaoDocument(
                policy_number=policy_number,
                document_name=name,
                document_date=parse_carrier_date(cells[indexes["date"]]),
                delivery=_norm(cells[indexes["delivery"]]),
                row_index=row_index,
            )
        )
    if not docs:
        raise IntakeHold("Progressive FAO document list is empty")
    return tuple(docs)


@dataclass(frozen=True)
class DocumentCapture:
    """Bytes observed after opening a document: a download or viewer PDFs."""

    downloads: tuple[bytes, ...]
    viewer_pdfs: tuple[bytes, ...]


def refuse_production_host() -> None:
    """Refuse a hermes-poc host unless FAO Production filing is enabled.

    The kill switch and the carrier allowlist are the only way through.
    Every other FAO check stays in place.
    """
    from .document_retrieval_filing import live_filing_decision

    if live_filing_decision(os.environ, socket.gethostname(), "fao").allowed:
        return
    raw = f"{socket.gethostname()} {socket.getfqdn()}".lower()
    labels = [label for label in re.split(r"[\s.]+", raw) if label]
    if any(label == "hermes-poc-01" or label.startswith("hermes-poc") for label in labels):
        raise IntakeHold("Progressive FAO document pull refuses Production host hermes-poc-01")


def require_hermes_test_host() -> None:
    """Live packs are produced on hermes-test-01. Fixture runs inject a browser.

    hermes-poc-01 is accepted only when FAO Production filing is enabled.
    """
    from .document_retrieval_filing import live_filing_decision

    if live_filing_decision(os.environ, socket.gethostname(), "fao").allowed:
        return
    raw = f"{socket.gethostname()} {socket.getfqdn()}".lower()
    labels = [label for label in re.split(r"[\s.]+", raw) if label]
    if HERMES_TEST_HOST not in labels:
        raise IntakeHold("Progressive FAO QA pack must be produced on hermes-test-01")


def require_fao_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise IntakeHold("Progressive FAO URL is missing or ambiguous")
    if host != FAO_HOST and host != CL_POLICY_HOST:
        raise IntakeHold("Progressive FAO URL is missing or ambiguous")
    path = parsed.path.lower()
    if any(login in path for login in _LOGIN_PATHS):
        # A login screen means the session expired; the worker does not
        # perform login or MFA.
        raise IntakeHold("Progressive FAO session is not authenticated")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def _unique_control(page: Any, role: str, name: str, *, exact: bool = True) -> Any:
    locator = page.get_by_role(role, name=name, exact=exact)
    try:
        count = int(locator.count())
    except Exception:
        raise IntakeHold(f"Progressive FAO control {name!r} is missing or ambiguous")
    if count < 1:
        raise IntakeHold(f"Progressive FAO control {name!r} is missing or ambiguous")
    if count > 1:
        try:
            return locator.first
        except Exception:
            raise IntakeHold(f"Progressive FAO control {name!r} is missing or ambiguous")
    return locator


def _read_text(node: Any) -> str:
    getter = getattr(node, "inner_text", None)
    if callable(getter):
        try:
            return str(getter())
        except Exception:
            return ""
    return str(getattr(node, "text", "") or "")


def collect_document_capture(page: Any, click_action: Callable[[], None]) -> DocumentCapture:
    """Click a document control and keep a unique PDF from a download or a new tab.

    The PDF opens inline in Chrome's PDF viewer (no download event). Viewer
    tabs are read back via the FAO PDF view reader, which only fetches from
    allowed Progressive hosts. A non-PDF capture raises IntakeHold: the wrong
    bytes are never kept.
    """
    downloads: list[bytes] = []
    opened: list[Any] = []

    def _on_page(new_page: Any) -> None:
        opened.append(new_page)

    context = getattr(page, "context", None)
    if context is not None and hasattr(context, "on"):
        context.on("page", _on_page)
    try:
        try:
            with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as download_info:
                click_action()
            download = download_info.value
            path = download.path()
            content = Path(str(path)).read_bytes()
            if not _is_pdf(content):
                raise IntakeHold("Progressive FAO document capture is not a PDF")
            downloads.append(content)
        except IntakeHold:
            raise
        except Exception as exc:
            if "timeout" not in type(exc).__name__.lower() and "Timeout" not in str(exc):
                raise IntakeHold("Progressive FAO document capture is missing or ambiguous") from exc
        viewer_pdfs: list[bytes] = []
        for new_page in opened:
            wait = getattr(new_page, "wait_for_load_state", None)
            if callable(wait):
                try:
                    wait("domcontentloaded", timeout=DOWNLOAD_TIMEOUT_MS)
                except Exception:
                    pass
            url = str(getattr(new_page, "url", "") or "")
            host = (urllib.parse.urlsplit(url).hostname or "").lower()
            if host and not (host == FAO_HOST or host.endswith(".foragentsonly.com")):
                continue
            view = read_playwright_pdf_view(new_page)
            viewer_pdfs.extend(view.pdfs)
            closer = getattr(new_page, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    pass
        return DocumentCapture(tuple(downloads), tuple(viewer_pdfs))
    finally:
        if context is not None and hasattr(context, "remove_listener"):
            try:
                context.remove_listener("page", _on_page)
            except Exception:
                pass


class PlaywrightFaoCancellationBrowser:
    """Drive one already-authenticated FAO tab. Does not type credentials."""

    def __init__(self, page: Any, *, agent_code: str = DEFAULT_AGENT_CODE):
        self.page = page
        self.agent_code = require_agent_code(agent_code)
        self._list_url = ""

    # -- navigation -----------------------------------------------------
    def load_report(self) -> None:
        """Open the Policies pending cancel or renewal report directly."""
        page = self.page
        require_fao_url(str(getattr(page, "url", "") or ""))
        assert_agent_context(page, self.agent_code)
        page.goto(REPORT_URL, wait_until="domcontentloaded")
        require_fao_url(str(getattr(page, "url", "") or ""))
        page.wait_for_selector("table", timeout=15000)
        self._list_url = require_fao_url(str(getattr(page, "url", "") or ""))

    def select_tab(self, tab_label: str) -> None:
        """Activate one of the three report tabs."""
        labels = {label for label, _ in _REPORT_TABS}
        if _norm(tab_label) not in labels:
            raise IntakeHold(f"Progressive FAO report tab is missing or ambiguous: {tab_label!r}")
        _unique_control(self.page, "tab", tab_label, exact=True).click()
        self.page.wait_for_selector("table", timeout=15000)

    def load_current_tab(self, tab_label: str) -> tuple[CancellationRow, ...]:
        """Parse the currently displayed tab's rows."""
        page = self.page
        tables = page.locator("table")
        if int(tables.count()) != 1:
            raise IntakeHold("Progressive FAO report table is missing or ambiguous")
        table = tables.first if hasattr(tables, "first") else tables
        header_nodes = table.locator("thead th").all()
        if not header_nodes:
            raise IntakeHold("Progressive FAO report headers are missing or ambiguous")
        headers = tuple(_norm(_read_text(node)) for node in header_nodes)
        row_nodes = table.locator("tbody tr").all()
        rows = tuple(
            tuple(_norm(_read_text(cell)) for cell in row.locator("td").all())
            for row in row_nodes
        )
        return parse_cancellations_report(tab_label, headers, rows, list_url=self._list_url)

    def open_policy_summary(self, policy_number: str) -> None:
        """Click the policy-number link ("View Policy Summary") -> CL Express."""
        require_policy_number(policy_number)
        link = _unique_control(self.page, "link", policy_number, exact=True)
        link.click()
        self.page.wait_for_url(f"**://{CL_POLICY_HOST}/**", timeout=15000)
        require_fao_url(str(getattr(self.page, "url", "") or ""))

    def open_documents_tab(self) -> None:
        """On the CL Express policy page, open the DOCUMENTS tab."""
        _unique_control(self.page, "tab", "DOCUMENTS", exact=True).click()
        self.page.wait_for_selector("text=Policy Documents", timeout=15000)

    def list_documents(self, policy_number: str) -> tuple[FaoDocument, ...]:
        """Parse the Policy Documents table on the DOCUMENTS tab.

        Live columns: (icon) | Date | Delivery | Document name. The document
        name is a button that opens the PDF in the viewer.
        """
        page = self.page
        tables = page.locator("table")
        if int(tables.count()) < 1:
            raise IntakeHold("Progressive FAO document table is missing or ambiguous")
        table = tables.first if hasattr(tables, "first") else tables
        header_nodes = table.locator("thead th").all()
        if not header_nodes:
            raise IntakeHold("Progressive FAO document headers are missing or ambiguous")
        headers = tuple(_norm(_read_text(node)) for node in header_nodes)
        row_nodes = table.locator("tbody tr").all()
        rows = tuple(
            tuple(_norm(_read_text(cell)) for cell in row.locator("td").all())
            for row in row_nodes
        )
        return parse_policy_documents(headers, rows, policy_number=policy_number)

    def document_button(self, doc: FaoDocument) -> Any:
        """The clickable control for one document row, located by row index.

        Rows are selected by index (not by name) so duplicate document names
        on the same policy stay unambiguous.
        """
        table = self.page.locator("table").first
        row = table.locator("tbody tr").nth(doc.row_index)
        try:
            if int(row.count()) != 1:
                raise IntakeHold(
                    f"Progressive FAO document row {doc.row_index} is missing or ambiguous"
                )
        except IntakeHold:
            raise
        except Exception:
            raise IntakeHold(
                f"Progressive FAO document row {doc.row_index} is missing or ambiguous"
            )
        button = row.get_by_role("button", name=doc.document_name, exact=False)
        try:
            if int(button.count()) != 1:
                raise IntakeHold(
                    f"Progressive FAO document control {doc.document_name!r} is missing or ambiguous"
                )
        except IntakeHold:
            raise
        except Exception:
            raise IntakeHold(
                f"Progressive FAO document control {doc.document_name!r} is missing or ambiguous"
            )
        return button.first

    def capture_document(self, doc: FaoDocument) -> DocumentCapture:
        """Click the document's button and observe the outcome."""
        button = self.document_button(doc)
        return collect_document_capture(self.page, button.click)

    def return_to_report(self) -> None:
        self.page.goto(self._list_url, wait_until="domcontentloaded")
        self.page.wait_for_selector("table", timeout=15000)

    def screenshot_report(self) -> bytes:
        data = self.page.screenshot(full_page=True, type="png")
        if not bytes(data or b"")[:8] == _PNG_MAGIC:
            raise IntakeHold("Progressive FAO report screenshot is missing or not a PNG")
        return bytes(data)


class FaoCancellationLedger:
    """Private named-PDF ledger. A conflicting file is kept and the pull holds."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def ensure_private(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        if self.root.is_symlink() or not self.root.is_dir() or self.root.stat().st_mode & 0o077:
            raise IntakeHold("Progressive FAO output directory must be private (0700)")

    def _load(self) -> dict[str, Any]:
        self.ensure_private()
        path = self.root / LEDGER_NAME
        if not path.exists():
            return {"items": {}}
        try:
            data = json.loads(path.read_text())
        except Exception:
            raise IntakeHold("Progressive FAO delivery ledger is missing or ambiguous")
        if not isinstance(data, dict) or not isinstance(data.get("items"), dict):
            raise IntakeHold("Progressive FAO delivery ledger is missing or ambiguous")
        return data

    def _write(self, data: dict[str, Any]) -> None:
        path = self.root / LEDGER_NAME
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def pdf_path(self, day: date, filename: str) -> Path:
        folder = self.root / day.isoformat()
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(folder, 0o700)
        safe = re.sub(r"[^\w.\- ]+", "_", filename).strip()
        if not safe:
            raise IntakeHold("Progressive FAO filename is missing or ambiguous")
        return folder / safe

    def delivery_status(self, *, document_id: str, filename: str, issued_on: date) -> bool:
        """True when this exact document was already delivered."""
        self.ensure_private()
        path = self.pdf_path(issued_on, filename)
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
            or entry.get("issued_date") != issued_on.isoformat()
            or entry.get("sha256") != hashlib.sha256(path.read_bytes()).hexdigest()
        ):
            raise IntakeHold("Existing Progressive FAO file conflicts with the pull ledger")
        return True

    def record(self, source: SourceItem, *, issued_on: date, insured_name: str = "") -> Path:
        self.ensure_private()
        if source.filename == LEDGER_NAME:
            raise IntakeHold("Progressive FAO filename is missing or ambiguous")
        path = self.pdf_path(issued_on, source.filename)
        if path.exists():
            raise IntakeHold("Existing Progressive FAO file conflicts with the pull ledger")
        digest = hashlib.sha256(source.content).hexdigest()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(source.content)
        data = self._load()
        if source.source_id in data["items"]:
            raise IntakeHold("Existing Progressive FAO file conflicts with the pull ledger")
        data["items"][source.source_id] = {
            "filename": source.filename,
            "insured_name": insured_name,
            "sha256": digest,
            "bytes": len(source.content),
            "issued_date": issued_on.isoformat(),
        }
        self._write(data)
        return path

    def save_screenshot(self, day: date, png: bytes) -> Path:
        if bytes(png or b"")[:8] != _PNG_MAGIC:
            raise IntakeHold("Progressive FAO report screenshot is missing or not a PNG")
        path = self.pdf_path(day, f"fao-cancellations-{day.isoformat()}.png")
        if path.exists():
            return path
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(png)
        return path


def _received_at(as_of: date) -> str:
    return datetime(as_of.year, as_of.month, as_of.day, tzinfo=timezone.utc).isoformat()


def _row_payload(row: CancellationRow, *, outcome: str, reason: str = "", filename: str = "") -> dict[str, Any]:
    payload = {
        "policy_number": row.policy_number,
        "insured_name": row.insured_name,
        "cancel_date": row.cancel_date.isoformat(),
        "amount_due": row.amount_due,
        "tab_reason": row.reason,
        "document_id": row.document_id,
        "outcome": outcome,
    }
    if reason:
        payload["hold_reason"] = reason
    if filename:
        payload["filename"] = filename
    return payload


def _pull_underwriting_memos(
    row: CancellationRow,
    docs: tuple[FaoDocument, ...],
    browser: Any,
    ledger: FaoCancellationLedger,
    archive: SourceArchive,
    *,
    as_of: date,
    held: list[dict[str, Any]],
    uw_memos: list[dict[str, Any]],
) -> None:
    """Pull standalone UW memos for one UNDERWRITING-tab row.

    Runs against the same DOCUMENTS list the cancellation flow read, so a
    failed DOCUMENTS read already recorded a hold before this is called.
    Each memo failure is recorded HELD with the policy; nothing is skipped
    silently. Cancellation documents are excluded (handled separately);
    billing and renewal paperwork are never targeted.
    """
    for doc in docs:
        if not is_underwriting_memo(doc.document_name):
            continue
        memo_id = doc.memo_document_id
        try:
            if ledger.delivery_status(
                document_id=memo_id, filename=doc.memo_filename, issued_on=doc.document_date
            ):
                uw_memos.append({
                    "document_id": memo_id,
                    "filename": doc.memo_filename,
                    "policy_number": row.policy_number,
                    "insured_name": row.insured_name,
                    "document_name": doc.document_name,
                    "document_date": doc.document_date.isoformat(),
                    "outcome": "ALREADY_DELIVERED",
                })
                continue
        except IntakeHold as exc:
            held.append(_row_payload(row, outcome="HELD", reason=str(exc)))
            continue
        capture = browser.capture_document(doc)
        pdfs = list(capture.downloads) + list(capture.viewer_pdfs)
        if len(pdfs) != 1:
            held.append(_row_payload(
                row, outcome="HELD",
                reason=f"Progressive FAO memo {doc.document_name!r} capture is missing or ambiguous",
            ))
            continue
        content = pdfs[0]
        source = SourceItem(
            system=PROCESS,
            source_account=FAO_HOST,
            source_id=memo_id,
            source_url=f"{row.list_url}#policy={row.policy_number}",
            received_at=_received_at(as_of),
            filename=doc.memo_filename,
            content=content,
        )
        source.validate()
        try:
            saved = ledger.record(source, issued_on=doc.document_date, insured_name=row.insured_name)
            archive.preserve(source)
        except IntakeHold as exc:
            held.append(_row_payload(row, outcome="HELD", reason=str(exc)))
            continue
        uw_memos.append({
            "document_id": memo_id,
            "filename": doc.memo_filename,
            "sha256": source.digest,
            "bytes": len(content),
            "policy_number": row.policy_number,
            "insured_name": row.insured_name,
            "document_name": doc.document_name,
            "document_date": doc.document_date.isoformat(),
            "delivery": doc.delivery,
            "path": str(saved),
            "outcome": "PULLED",
        })


def run_pull(
    browser: PlaywrightFaoCancellationBrowser,
    ledger: FaoCancellationLedger,
    archive: SourceArchive,
    *,
    as_of: date,
) -> dict[str, Any]:
    """Pull Progressive FAO cancellation documents from the pending-cancel report.

    For each policy on every report tab: open Policy Summary on CL Express,
    open the DOCUMENTS tab, and target cancellation notice documents. For
    UNDERWRITING tab rows, standalone underwriting memos (additional-info
    requests, agent review notices) are pulled into the ``uw_memos`` receipt
    section. A real PDF download or viewer PDF is saved; anything else is
    recorded HELD and never claimed as downloaded. Billing documents are out
    of scope and are never targeted.
    """
    from .document_retrieval_filing import require_carrier_pull

    require_carrier_pull("fao")
    refuse_production_host()
    if not isinstance(as_of, date):
        raise IntakeHold("Progressive FAO as-of date is missing or ambiguous")

    downloaded: list[dict[str, Any]] = []
    held: list[dict[str, Any]] = []
    skipped: list[str] = []
    targeted: list[str] = []
    rows_payload: list[dict[str, Any]] = []
    uw_memos: list[dict[str, Any]] = []

    browser.load_report()
    png = browser.screenshot_report()
    ledger.save_screenshot(as_of, png)

    all_rows: list[CancellationRow] = []
    for tab_label, _ in _REPORT_TABS:
        browser.select_tab(tab_label)
        all_rows.extend(browser.load_current_tab(tab_label))

    for row in all_rows:
        browser.open_policy_summary(row.policy_number)
        try:
            browser.open_documents_tab()
            docs = browser.list_documents(row.policy_number)
        except IntakeHold as exc:
            held.append(_row_payload(row, outcome="HELD", reason=str(exc)))
            browser.return_to_report()
            continue
        if row.reason == "UNDERWRITING":
            _pull_underwriting_memos(
                row, docs, browser, ledger, archive,
                as_of=as_of, held=held, uw_memos=uw_memos,
            )
        targets = [doc for doc in docs if is_cancellation_document(doc.document_name)]
        if len(targets) != 1:
            held.append(_row_payload(
                row, outcome="HELD",
                reason=(
                    f"Progressive FAO policy {row.policy_number} cancellation document "
                    f"is missing or ambiguous (found {len(targets)})"
                ),
            ))
            browser.return_to_report()
            continue
        doc = targets[0]
        try:
            if ledger.delivery_status(
                document_id=doc.document_id, filename=doc.filename, issued_on=doc.document_date
            ):
                skipped.append(doc.document_id)
                targeted.append(doc.document_id)
                rows_payload.append(_row_payload(row, outcome="ALREADY_DELIVERED", filename=doc.filename))
                browser.return_to_report()
                continue
        except IntakeHold as exc:
            held.append(_row_payload(row, outcome="HELD", reason=str(exc)))
            browser.return_to_report()
            continue
        capture = browser.capture_document(doc)
        pdfs = list(capture.downloads) + list(capture.viewer_pdfs)
        if len(pdfs) != 1:
            held.append(_row_payload(
                row, outcome="HELD",
                reason=f"Progressive FAO document {doc.document_name!r} capture is missing or ambiguous",
            ))
            browser.return_to_report()
            continue
        content = pdfs[0]
        source = SourceItem(
            system=PROCESS,
            source_account=FAO_HOST,
            source_id=doc.document_id,
            source_url=f"{row.list_url}#policy={row.policy_number}",
            received_at=_received_at(as_of),
            filename=doc.filename,
            content=content,
        )
        source.validate()
        try:
            saved = ledger.record(source, issued_on=doc.document_date, insured_name=row.insured_name)
            archive.preserve(source)
        except IntakeHold as exc:
            held.append(_row_payload(row, outcome="HELD", reason=str(exc)))
            browser.return_to_report()
            continue
        downloaded.append({
            "document_id": doc.document_id,
            "filename": doc.filename,
            "sha256": source.digest,
            "bytes": len(content),
            "policy_number": row.policy_number,
            "insured_name": row.insured_name,
            "cancel_date": row.cancel_date.isoformat(),
            "document_date": doc.document_date.isoformat(),
            "document_name": doc.document_name,
            "delivery": doc.delivery,
            "path": str(saved),
        })
        targeted.append(doc.document_id)
        rows_payload.append(_row_payload(row, outcome="PULLED", filename=doc.filename))
        browser.return_to_report()

    return {
        "status": "PULLED",
        "scope": SCOPE,
        "process": PROCESS,
        "as_of": as_of.isoformat(),
        "cancellation_count": len(all_rows),
        "targeted": len(targeted),
        "count": len(downloaded),
        "downloaded": downloaded,
        "uw_memos": uw_memos,
        "uw_memo_count": len([m for m in uw_memos if m.get("outcome") == "PULLED"]),
        "skipped_already_delivered": skipped,
        "held": held,
        "rows": rows_payload,
        "ezlynx": "not_run",
    }


def select_fao_page(pages: list[Any]) -> Any:
    """Use the single FAO tab."""
    matches = [
        page for page in pages
        if (urllib.parse.urlsplit(str(getattr(page, "url", "") or "")).hostname or "").lower() == FAO_HOST
    ]
    if len(matches) != 1:
        raise IntakeHold("Expected exactly one Progressive FAO tab")
    return matches[0]


def connect_cdp_browser(
    cdp_url: str | None, agent_code: str = DEFAULT_AGENT_CODE
) -> tuple[PlaywrightFaoCancellationBrowser, Callable[[], None]]:
    """Attach to the local carrier Chrome. Exactly one FAO tab."""
    from .document_retrieval_filing import require_carrier_pull

    require_carrier_pull("fao")
    refuse_production_host()
    require_hermes_test_host()
    url = (cdp_url or os.environ.get("ROBIE_BROWSER_CDP_URL") or DEFAULT_CDP_URL).strip()
    parsed = urllib.parse.urlsplit(url)
    if parsed.username or parsed.password or parsed.scheme not in {"http", "https"}:
        raise IntakeHold("Progressive FAO browser attach must use the local Test CDP endpoint")
    if (parsed.hostname or "").lower() not in {"127.0.0.1", "localhost"}:
        raise IntakeHold("Progressive FAO browser attach must use the local Test CDP endpoint")
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(url)
        pages = [page for context in browser.contexts for page in context.pages]
        return PlaywrightFaoCancellationBrowser(select_fao_page(pages), agent_code=agent_code), playwright.stop
    except Exception:
        playwright.stop()
        raise


def qa_pack_dir(output_root: Path, as_of: date) -> Path:
    folder = output_root / "Progressive" / as_of.isoformat()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(folder, 0o700)
    return folder


def main(argv: list[str] | None = None, *, browser_factory: Callable[[Any], Any] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Test-only Progressive FAO cancellation document pull")
    parser.add_argument("--as-of", default=date.today().isoformat())
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--cdp-url", default=None)
    parser.add_argument("--agent-code", default=os.environ.get("PROGRESSIVE_FAO_AGENT_CODE", DEFAULT_AGENT_CODE))
    parser.add_argument("--upload-drive", action="store_true")
    args = parser.parse_args(argv)
    if args.upload_drive:
        raise IntakeHold(DRIVE_UPLOAD_UNAVAILABLE)
    as_of = parse_carrier_date(args.as_of)
    output_root = qa_pack_dir(Path(args.output_root), as_of)
    ledger = FaoCancellationLedger(output_root)
    archive = SourceArchive(output_root / "sources")
    if browser_factory is not None:
        browser = browser_factory(args)
        receipt = run_pull(browser, ledger, archive, as_of=as_of)
    else:
        browser, close = connect_cdp_browser(args.cdp_url, agent_code=args.agent_code)
        try:
            receipt = run_pull(browser, ledger, archive, as_of=as_of)
        finally:
            close()
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
