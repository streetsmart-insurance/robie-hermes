"""Test-only Geico Agent Gateway Pending Cancellation NOC list and download.

The manual prove on 2026-09-26 walked gateway2.geico.com/client-alerts:
Client Alerts, Pending Cancellations, then each personal-lines notice saved
as ``[PolicyNumber] NOC Geico.pdf``. The 2026-09-28 hermes-test Gateway
instead shows filter chips on that same URL. ``Pending Cancellations`` may
include a count, for example ``Pending Cancellations (3)``. When that chip
is on the page, Client Alerts is not required. Commercial Auto with Billing
only and no Documents route was held, not downloaded.

Accessible names below are that path, not a certified live DOM. A missing or
non-unique control raises IntakeHold. This module does not log in, does not
submit a one-time passcode, and does not upload, note, task, or label in
EZLynx. There is no earlier geico process; this is the pull sibling.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import socket
import sys
import tempfile
import urllib.parse
import weakref
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .intake_core import IntakeHold, SourceArchive, SourceItem


PROCESS = "geico"
SCOPE = "pending_cancellation_noc"
DEFAULT_CDP_URL = "http://127.0.0.1:9223"
DOWNLOAD_TIMEOUT_MS = 8000
LEDGER_NAME = "geico-noc-ledger.json"
GATEWAY_HOST = "gateway2.geico.com"
HERMES_TEST_HOST = "hermes-test-01"
DEFAULT_OUTPUT_ROOT = Path(
    "/opt/streetsmart-hermes-test/robie-job-engine/data/artifacts/carrier-pull-qa/geico"
)
# Shared Drive "Robie Carrier Pull QA (Nicole)". Folder upload is TODO.
# --upload-drive fails closed and does not call Google.
CARRIER_QA_DRIVE_PARENT_ID = "1cLEpR-0T6KdiVjcdAr0qpGTO447MetI2"
GEICO_QA_DRIVE_FOLDER_ID = "1mMy9nrYjN8PRRwihRLgjjDdb213WqBLt"
DRIVE_UPLOAD_UNAVAILABLE = (
    "Drive upload of the Geico QA pack is not available; "
    "refusing to report the pack as uploaded"
)
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_POLICY_NUMBER = re.compile(r"^\d{10}$")
_REMOTE_PDF = re.compile(r"https?://[^\s\"'<>]+?\.pdf(?:\?[^\s\"'<>]*)?", re.IGNORECASE)
_EASTERN = ZoneInfo("America/New_York")
NOTICE_NAMES = ("Pending Cancellation Notice", "CANCELLATION NOTICE", "Cancellation Notice")
PERSONAL_PRODUCT = "private passenger auto"
COMMERCIAL_PRODUCT = "commercial auto"
# Live Gateway 2026-09-30 shows the product as "Personal Auto" or "Commercial".
PERSONAL_PRODUCTS = frozenset({PERSONAL_PRODUCT, "personal auto"})
COMMERCIAL_PRODUCTS = frozenset({COMMERCIAL_PRODUCT, "commercial"})
# A Billing notice issued this long before the alert's due date belongs to an
# earlier cancellation (live: notice issued 6/24/2026 for a 7/6/2026 cancel,
# alert due 10/6/2026). It is not pulled as this alert's notice.
NOTICE_MAX_AGE_DAYS = 35

_HEADER_FIELDS = (
    ("status", frozenset({"severity", "status", "priority"})),
    ("policy_number", frozenset({"policy number", "policy"})),
    ("insured_name", frozenset({"insured name", "insured"})),
    ("product", frozenset({"product", "product/description"})),
    ("due_date", frozenset({"due date"})),
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


class PullHeld(IntakeHold):
    """Whole-job hold that still reports commercial rows and any PDFs already saved."""

    def __init__(
        self,
        reason: str,
        *,
        held: list[dict[str, Any]] | None = None,
        downloaded: list[dict[str, Any]] | None = None,
        rows: list[dict[str, Any]] | None = None,
        targeted: int | None = None,
        pdfs: int | None = None,
    ):
        super().__init__(reason)
        self.details: dict[str, Any] = {
            "held": list(held or []),
            "downloaded": list(downloaded or []),
            "rows": list(rows or []),
        }
        if targeted is not None:
            self.details["targeted"] = targeted
        if pdfs is not None:
            self.details["pdfs"] = pdfs


@dataclass(frozen=True)
class AlertGrid:
    list_url: str
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    more_pages: bool | None


@dataclass(frozen=True)
class AlertRow:
    document_id: str
    policy_number: str
    insured_name: str
    due_on: date
    status: str
    product: str
    line: str
    filename: str
    row_index: int
    source_url: str


@dataclass(frozen=True)
class NoticePath:
    kind: str
    notice_name: str = ""
    issued_on: date | None = None
    document_id: str = ""


# GEICO serves cancellation notice PDFs from edgeextended.geico.com with a
# documentId query param (observed live 2026-10-05:
# eaf9b197-7471-3c36-2f23-3ed54027b6d6). The UUID is the durable document
# identity for the ledger.
#
# Live 2026-10-05 correction: the notice link's href is literally "#" — the
# Angular app (edgeextended.geico.com) fetches the document ID via XHR into
# JavaScript runtime state, so there is NO UUID in the markup. Clicking the
# link opens the consolidated document viewer:
#   /documents/consolidated-document-viewer?documentId={uuid}&token={session}&visitAppId=E01&convToken=
# The viewer URL itself returns an HTML shell (Angular + PDF.js), NOT raw PDF
# bytes. The PDF loads client-side via XHR; the PDF.js iframe has
# src="about:blank". The worker clicks the link, reads the viewer URL for the
# documentId, and captures the PDF bytes from the viewer's XHR via Playwright
# network interception. Fallback: the viewer's Download button via frames.
CONSOLIDATED_VIEWER_PATH = "/documents/consolidated-document-viewer"
_DOCUMENT_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_DOWNLOAD_BUTTON_NAME = re.compile(r"download", re.IGNORECASE)


def is_consolidated_viewer_url(url: str) -> bool:
    """True if the URL is GEICO's consolidated document viewer."""
    try:
        parsed = urllib.parse.urlsplit(str(url or ""))
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host == "geico.com" or host.endswith(".geico.com")):
        return False
    return parsed.path.rstrip("/").lower().endswith(CONSOLIDATED_VIEWER_PATH)


def extract_viewer_document_id(url: str) -> str:
    """Extract the document UUID from a consolidated-viewer URL.

    Fail-closed: not a viewer URL, or missing/ambiguous documentId, raises
    IntakeHold.
    """
    if not is_consolidated_viewer_url(url):
        raise IntakeHold("NOC viewer URL is missing or ambiguous")
    return extract_document_id(url)


def extract_document_id(url: str) -> str:
    """Extract the document UUID from a GEICO document URL.

    Fail-closed: missing or ambiguous documentId raises IntakeHold.
    """
    raw = str(url or "")
    try:
        query = urllib.parse.urlsplit(raw).query
    except Exception as exc:
        raise IntakeHold("NOC document URL is missing or ambiguous") from exc
    values = urllib.parse.parse_qs(query).get("documentId", [])
    ids = [v for v in values if _DOCUMENT_ID_RE.fullmatch(v.strip())]
    if len(ids) != 1:
        raise IntakeHold("NOC document ID is missing or ambiguous")
    return ids[0].lower()


def _pdf_response_handler(captured: list[bytes]) -> Callable[[Any], None]:
    """Build a Playwright response listener that captures PDF XHR bodies.

    Only responses with a PDF content-type or a PDF-looking URL are kept,
    and only if the body starts with %PDF. Everything else (including the
    viewer's HTML shell) is ignored. The caller decides what to do with zero
    or multiple captures.
    """
    def on_response(response: Any) -> None:
        try:
            url = str(getattr(response, "url", "") or "")
            headers = getattr(response, "headers", None)
            content_type = ""
            if isinstance(headers, dict):
                content_type = str(headers.get("content-type", "") or "").lower()
            if "application/pdf" not in content_type and not _url_looks_like_pdf(url):
                return
            body_fn = getattr(response, "body", None)
            if not callable(body_fn):
                return
            body = body_fn()
            if isinstance(body, (bytes, bytearray)) and _is_pdf(body):
                captured.append(bytes(body))
        except Exception:
            pass
    return on_response


def _find_viewer_page(page: Any, opened: list[Any]) -> Any:
    """Return the consolidated-viewer page: a new tab or the current tab.

    Fail-closed if no viewer URL is found.
    """
    for item in opened:
        if is_consolidated_viewer_url(str(getattr(item, "url", "") or "")):
            return item
    if is_consolidated_viewer_url(str(getattr(page, "url", "") or "")):
        return page
    raise IntakeHold("NOC viewer did not open")


def _settle_viewer_pages(page: Any, opened: list[Any], timeout_ms: int) -> None:
    """Wait for viewer pages to load so their PDF XHR can complete."""
    for target in (page, *opened):
        wait = getattr(target, "wait_for_load_state", None)
        if callable(wait):
            try:
                wait("domcontentloaded", timeout=timeout_ms)
            except Exception:
                pass
    # Give the PDF.js XHR a moment to complete after DOM load.
    sleeper = getattr(page, "wait_for_timeout", None)
    if callable(sleeper):
        try:
            sleeper(min(3000, timeout_ms))
        except Exception:
            pass


def _click_viewer_download(viewer: Any, timeout_ms: int) -> bytes | None:
    """Click the viewer's Download button via frame traversal.

    Live 2026-10-05: the button was inside an iframe that browser automation
    could not operate. Playwright's frame API may reach it. Returns PDF bytes,
    or None if the button is not found/operable (the caller holds on None).
    """
    frames: list[Any] = [viewer]
    get_frames = getattr(viewer, "frames", None)
    if callable(get_frames):
        try:
            frames.extend(get_frames())
        except Exception:
            pass
    elif isinstance(get_frames, (list, tuple)):
        # Real Playwright exposes page.frames as a property (list).
        frames.extend(get_frames)
    for frame in frames:
        get_by_role = getattr(frame, "get_by_role", None)
        if not callable(get_by_role):
            continue
        try:
            button = get_by_role("button", name=_DOWNLOAD_BUTTON_NAME)
        except Exception:
            continue
        if _locator_count(button) != 1:
            continue
        expect = getattr(viewer, "expect_download", None)
        if not callable(expect):
            return None
        try:
            with expect(timeout=timeout_ms) as download_info:
                button.click(timeout=timeout_ms)
            blob = _download_bytes(download_info.value)
        except Exception:
            continue
        if _is_pdf(blob):
            return bytes(blob)
        # A non-PDF download is not the notice; keep looking.
    return None


def fetch_notice_pdf_via_viewer(
    page: Any,
    *,
    timeout_ms: int = DOWNLOAD_TIMEOUT_MS,
) -> tuple[str, bytes]:
    """Click the notice link, capture the viewer URL's documentId, intercept the PDF XHR.

    Live 2026-10-05: the notice link href is "#" (Angular fetches the document
    ID via XHR). Clicking opens the consolidated document viewer with
    documentId in the URL. The viewer URL itself returns HTML (Angular +
    PDF.js), not PDF bytes; the PDF loads client-side via XHR. This function
    captures those XHR bytes via Playwright network interception. If
    interception yields nothing, it falls back to the viewer's Download button
    via frame traversal. Fail-closed at every step.

    Returns (document_id, pdf_bytes).
    """
    notice = _notice_match(page)
    if not isinstance(notice, tuple):
        raise IntakeHold("NOC PDF capture is missing or ambiguous")
    _, locator = notice

    context = page.context
    captured: list[bytes] = []
    opened: list[Any] = []
    on_response = _pdf_response_handler(captured)

    def on_page(new_page: Any) -> None:
        opened.append(new_page)

    context.on("response", on_response)
    context.on("page", on_page)
    try:
        click = getattr(locator, "click", None)
        if not callable(click):
            raise IntakeHold("NOC PDF capture is missing or ambiguous")
        try:
            click(timeout=timeout_ms)
        except Exception as exc:
            raise IntakeHold("NOC notice link did not open") from exc

        _settle_viewer_pages(page, opened, timeout_ms)

        viewer = _find_viewer_page(page, opened)
        document_id = extract_viewer_document_id(str(getattr(viewer, "url", "") or ""))

        if len(captured) == 1:
            return document_id, captured[0]
        if len(captured) > 1:
            raise IntakeHold("NOC PDF capture is missing or ambiguous")

        downloaded = _click_viewer_download(viewer, timeout_ms)
        if downloaded is not None:
            return document_id, downloaded
        raise IntakeHold("NOC PDF capture is missing or ambiguous")
    finally:
        remover = getattr(context, "remove_listener", None)
        if callable(remover):
            for event, handler in (("response", on_response), ("page", on_page)):
                try:
                    remover(event, handler)
                except Exception:
                    pass
        for item in opened:
            _close_gateway_page(item)


@dataclass(frozen=True)
class PagePdfView:
    url: str
    pdfs: tuple[bytes, ...]


@dataclass(frozen=True)
class NoticeOpenObservation:
    downloads: tuple[bytes, ...]
    pages: tuple[PagePdfView, ...]


def refuse_production_host() -> None:
    """Refuse a hermes-poc host unless Geico Production filing is enabled.

    The kill switch and the FAO/NatGen/Geico allowlist are the only way
    through. Every other Geico check stays in place.
    """

    from .document_retrieval_filing import live_filing_decision

    if live_filing_decision(os.environ, socket.gethostname(), "geico").allowed:
        return
    raw = f"{socket.gethostname()} {socket.getfqdn()}".lower()
    labels = [label for label in re.split(r"[\s.]+", raw) if label]
    if any(label == "hermes-poc-01" or label.startswith("hermes-poc") for label in labels):
        raise IntakeHold("Geico NOC pull refuses Production host hermes-poc-01")


def classify_line(product: str) -> str:
    key = _norm(product).casefold()
    if key in PERSONAL_PRODUCTS:
        return "personal"
    if key in COMMERCIAL_PRODUCTS:
        return "commercial"
    raise IntakeHold("Pending Cancellation product line is missing or ambiguous")


def noc_filename(policy_number: str) -> str:
    policy = str(policy_number or "").strip()
    if not _POLICY_NUMBER.fullmatch(policy):
        raise IntakeHold("NOC policy number is missing or ambiguous")
    return f"{policy} NOC Geico.pdf"


def noc_document_id(policy_number: str, due_on: date, document_id: str | None = None) -> str:
    policy = str(policy_number or "").strip()
    if not _POLICY_NUMBER.fullmatch(policy):
        raise IntakeHold("NOC policy number is missing or ambiguous")
    base = f"geico-noc:{policy}:{due_on.isoformat()}"
    doc = str(document_id or "").strip().lower()
    if doc:
        if not _DOCUMENT_ID_RE.fullmatch(doc):
            raise IntakeHold("NOC document ID is missing or ambiguous")
        return f"{base}:doc-{doc}"
    return base


def parse_due_date(value: str) -> date:
    raw = _norm(value)
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            return date.fromisoformat(raw)
        if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", raw):
            return datetime.strptime(raw, "%m/%d/%Y").date()
    except ValueError as exc:
        raise IntakeHold("NOC due date is missing or ambiguous") from exc
    raise IntakeHold("NOC due date is missing or ambiguous")


def header_indexes(headers: tuple[str, ...]) -> dict[str, int]:
    normalized = tuple(_norm(header).casefold() for header in headers)
    named = [header for header in normalized if header]
    if not named or len(named) != len(set(named)):
        raise IntakeHold("Pending Cancellations table is missing or ambiguous")
    indexes: dict[str, int] = {}
    for index, header in enumerate(normalized):
        fields = [name for name, aliases in _HEADER_FIELDS if header in aliases]
        if len(fields) > 1:
            raise IntakeHold("Pending Cancellations table is missing or ambiguous")
        if not fields:
            continue
        if fields[0] in indexes:
            raise IntakeHold("Pending Cancellations table is missing or ambiguous")
        indexes[fields[0]] = index
    for required in ("status", "policy_number", "insured_name", "product", "due_date"):
        if required not in indexes:
            raise IntakeHold("Pending Cancellations table is missing or ambiguous")
    return indexes


def parse_alert_grid(grid: AlertGrid) -> tuple[AlertRow, ...]:
    list_url = require_list_url(grid.list_url)
    indexes = header_indexes(grid.headers)
    alerts: list[AlertRow] = []
    for index, cells in enumerate(grid.rows):
        if len(cells) != len(grid.headers):
            raise IntakeHold("Pending Cancellations list is ambiguous")
        if not any(_norm(cell) for cell in cells):
            continue
        status = _norm(cells[indexes["status"]])
        if status.casefold() != "high":
            raise IntakeHold("Pending Cancellations list includes a non-High alert")
        policy = _norm(cells[indexes["policy_number"]])
        insured = _norm(cells[indexes["insured_name"]])
        if not _POLICY_NUMBER.fullmatch(policy):
            raise IntakeHold("NOC policy number is missing or ambiguous")
        if not insured:
            raise IntakeHold("NOC insured name is missing or ambiguous")
        product = _norm(cells[indexes["product"]])
        line = classify_line(product)
        due_on = parse_due_date(cells[indexes["due_date"]])
        alerts.append(AlertRow(
            document_id=noc_document_id(policy, due_on),
            policy_number=policy,
            insured_name=insured,
            due_on=due_on,
            status=status,
            product=product,
            line=line,
            filename=noc_filename(policy),
            row_index=index,
            source_url=list_url,
        ))
    policies = [alert.policy_number for alert in alerts]
    if len(policies) != len(set(policies)) or len({alert.document_id for alert in alerts}) != len(alerts):
        raise IntakeHold("Pending Cancellations list is ambiguous")
    return tuple(alerts)


def require_noc_pdf_parity(*, targeted_ids: set[str], verified_ids: set[str]) -> dict[str, Any]:
    """Targeted personal-lines alerts must each have one verified PDF.

    A short download or an extra id in the verified set holds the pull.
    Commercial holds are not part of either set.
    """
    if verified_ids != set(targeted_ids):
        raise IntakeHold(
            "Pending Cancellation NOC count does not match downloaded PDFs: "
            f"{len(targeted_ids)} targeted and {len(verified_ids)} PDFs"
        )
    return {
        "gate": "targeted_personal_lines_equal_pdfs",
        "targeted": len(targeted_ids),
        "pdfs": len(verified_ids),
    }


def pdf_bytes_from_observation(observation: NoticeOpenObservation) -> bytes:
    candidates: list[bytes] = []
    for blob in observation.downloads:
        if isinstance(blob, memoryview):
            blob = blob.tobytes()
        if not isinstance(blob, (bytes, bytearray)):
            raise IntakeHold("NOC PDF capture is missing or ambiguous")
        if not blob:
            continue
        if not _is_pdf(blob):
            raise IntakeHold("NOC download is not a PDF")
        candidates.append(bytes(blob))
    for view in observation.pages:
        candidates.extend(view.pdfs)
    digests = {hashlib.sha256(blob).digest() for blob in candidates}
    if len(digests) != 1:
        raise IntakeHold("NOC PDF capture is missing or ambiguous")
    return candidates[0]


def read_playwright_pdf_view(page: Any) -> PagePdfView:
    """Collect PDF bytes from a download target, blob URL, or embedded PDF.

    HTML is not printed to PDF. Remote fetches stay on Geico hosts.
    """
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


def collect_notice_observation(
    page: Any,
    open_notice: Callable[[], None],
    *,
    read_page: Callable[[Any], PagePdfView] = read_playwright_pdf_view,
    timeout_ms: int = DOWNLOAD_TIMEOUT_MS,
) -> NoticeOpenObservation:
    """Click one notice control and keep a unique PDF from the download or a new tab."""
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
            open_notice()
            clicked = True

        try:
            with page.expect_download(timeout=timeout_ms) as download_info:
                wrapped()
            downloads.append(_download_bytes(download_info.value))
        except IntakeHold:
            raise
        except Exception as exc:
            if not clicked or not _is_download_timeout(exc):
                raise IntakeHold("NOC PDF capture is missing or ambiguous") from exc
        for item in opened:
            wait = getattr(item, "wait_for_load_state", None)
            if not callable(wait):
                continue
            try:
                wait("domcontentloaded", timeout=timeout_ms)
            except Exception as exc:
                if not _is_download_timeout(exc):
                    raise IntakeHold("NOC PDF capture is missing or ambiguous") from exc
        views = [read_page(item) for item in opened]
        current_url = str(getattr(page, "url", "") or "")
        if current_url.startswith("blob:") or _url_looks_like_pdf(current_url):
            views.append(read_page(page))
        return NoticeOpenObservation(downloads=tuple(downloads), pages=tuple(views))
    finally:
        remover = getattr(context, "remove_listener", None)
        if callable(remover):
            try:
                remover("page", on_page)
            except Exception:
                pass
        for item in opened:
            _close_gateway_page(item)


def assert_authenticated(page: Any) -> None:
    url = str(getattr(page, "url", "") or "")
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/").lower()
    if "b2clogin" in host or path.endswith("/login"):
        raise IntakeHold("Geico Gateway session is not authenticated")
    if page.locator("input[type='password']").count() != 0:
        raise IntakeHold("Geico Gateway session is not authenticated")


# Modern Gateway filter chip. The count badge is part of the accessible name.
_PENDING_CHIP_NAME = re.compile(
    r"^pending cancellations(?:\s*\(\s*\d+\s*\))?$",
    re.IGNORECASE,
)
_UNDERWRITING_CHIP_NAME = re.compile(
    r"^underwriting(?:\s*\(\s*\d+\s*\))?$",
    re.IGNORECASE,
)
_ALL_ALERTS_CHIP_NAME = re.compile(
    r"^all alerts(?:\s*\(\s*\d+\s*\))?$",
    re.IGNORECASE,
)
_CHIP_ROLES = ("button", "radio", "link")
_TOGGLE_TRUE = frozenset({"true", "page", "step"})
# Pages whose Pending Cancellations chip was clicked in this process.
# Live Gateway leaves both "Pending Cancellations (3)" and "All Alerts (50)"
# on screen, and the pending button may not expose aria-pressed.
_PENDING_CHIP_CLICKED: dict[int, weakref.ReferenceType] = {}


def _remember_pending_chip(page: Any) -> None:
    key = id(page)
    def forget(reference: weakref.ReferenceType) -> None:
        if _PENDING_CHIP_CLICKED.get(key) is reference:
            _PENDING_CHIP_CLICKED.pop(key, None)
    try:
        _PENDING_CHIP_CLICKED[key] = weakref.ref(page, forget)
    except TypeError:
        # A non-weak-referenceable page must prove selection through its UI.
        _PENDING_CHIP_CLICKED.pop(key, None)


def _pending_chip_was_clicked(page: Any) -> bool:
    reference = _PENDING_CHIP_CLICKED.get(id(page))
    return reference is not None and reference() is page


def _locator_count(locator: Any) -> int:
    try:
        return int(locator.count())
    except Exception:
        return -1


def _attr(locator: Any, name: str) -> str | None:
    getter = getattr(locator, "get_attribute", None)
    if not callable(getter):
        return None
    try:
        value = getter(name)
    except Exception:
        return None
    if value is None:
        return None
    return str(value).strip().casefold()


def _chip_toggle(locator: Any) -> str:
    """selected, unselected, or bare (no toggle attribute)."""
    values = [
        _attr(locator, "aria-pressed"),
        _attr(locator, "aria-selected"),
        _attr(locator, "aria-checked"),
    ]
    current = _attr(locator, "aria-current")
    present = [value for value in values if value is not None]
    if any(value == "true" for value in present) or current in _TOGGLE_TRUE:
        return "selected"
    if any(value == "false" for value in present) or current == "false":
        return "unselected"
    return "bare"


_PENDING_TOGGLE_TEXT = re.compile(r"^\s*Pending Cancellations", re.IGNORECASE)
_UNDERWRITING_TOGGLE_TEXT = re.compile(r"^\s*Underwriting", re.IGNORECASE)


def _pending_chip_matches(page: Any) -> list[tuple[int, Any]]:
    # Live Gateway chip is <gds-toggle-button aria-pressed> with a shadow
    # button inside, so role queries see two buttons. The host element is the
    # one control and carries aria-pressed.
    try:
        toggle = page.locator("gds-toggle-button", has_text=_PENDING_TOGGLE_TEXT)
        toggles = _locator_count(toggle)
    except Exception:
        toggles = 0
    if toggles == 1:
        return [(1, toggle)]
    if toggles > 1:
        return [(toggles, toggle)]
    found = []
    for role in _CHIP_ROLES:
        locator = page.get_by_role(role, name=_PENDING_CHIP_NAME, exact=False)
        count = _locator_count(locator)
        if count:
            found.append((count, locator))
    return found


def _pending_chip_view(page: Any) -> str:
    """selected, unselected, bare, absent, ambiguous, or error."""
    matches = _pending_chip_matches(page)
    if any(count < 0 for count, _locator in matches):
        return "error"
    if not matches:
        return "absent"
    if len(matches) != 1 or matches[0][0] != 1:
        return "ambiguous"
    return _chip_toggle(matches[0][1])


def _alerts_table_count(page: Any) -> int:
    try:
        return int(page.locator("table").count()) + int(page.locator("gds-table").count())
    except Exception:
        return -1


def _filter_chip_count(page: Any, name: re.Pattern) -> int:
    total = 0
    for role in _CHIP_ROLES:
        count = _locator_count(page.get_by_role(role, name=name, exact=False))
        if count < 0:
            return -1
        total += count
    return total


def _click_pending_chip(page: Any) -> None:
    matches = _pending_chip_matches(page)
    if len(matches) != 1 or matches[0][0] != 1:
        raise IntakeHold("Pending Cancellations view is missing or ambiguous")
    # GDS web components don't respond to Playwright click; use JS
    clicked = False
    if hasattr(page, "evaluate") and callable(page.evaluate):
        clicked = bool(page.evaluate("""(() => {
            const btn = document.querySelector('gds-toggle-button');
            if (btn) {
                // Try clicking the shadow button first, then the host
                const shadowBtn = btn.shadowRoot ? btn.shadowRoot.querySelector('button') : null;
                if (shadowBtn) { shadowBtn.click(); return true; }
                btn.click();
                return true;
            }
            return false;
        })()"""))
    if not clicked:
        matches[0][1].click()
    _remember_pending_chip(page)


def pending_view_selected(page: Any) -> bool:
    combos = page.get_by_role("combobox")
    count = combos.count()
    if count > 1:
        raise IntakeHold("Pending Cancellations view is missing or ambiguous")
    if count == 1:
        return _control_text(combos).casefold() == "pending cancellations"
    tab = page.get_by_role("tab", name="Pending Cancellations", exact=True)
    if tab.count() > 1:
        raise IntakeHold("Pending Cancellations view is missing or ambiguous")
    if tab.count() == 1 and tab.get_attribute("aria-selected") == "true":
        return True
    chip = _pending_chip_view(page)
    if chip in {"ambiguous", "error"}:
        raise IntakeHold("Pending Cancellations view is missing or ambiguous")
    if chip == "selected":
        return True
    # The prove page shows button "Pending Cancellations (3)" beside
    # "All Alerts (50)". That pending button is not selected just because
    # a table is visible. A click of the one pending button is the selection
    # when the control does not expose aria-pressed.
    if _pending_chip_was_clicked(page) and chip in {"bare", "unselected", "selected"}:
        return True
    if (
        chip == "bare"
        and _alerts_table_count(page) == 1
        and _filter_chip_count(page, _ALL_ALERTS_CHIP_NAME) == 0
    ):
        return True
    return False


def click_named(page: Any, name: str, *, roles: tuple[str, ...]) -> None:
    matches = []
    for role in roles:
        locator = page.get_by_role(role, name=name, exact=True)
        count = locator.count()
        if count:
            matches.append((count, locator))
    if len(matches) != 1 or matches[0][0] != 1:
        raise IntakeHold(f"Geico control {name!r} is missing or ambiguous")
    matches[0][1].click()


def _named_control_state(page: Any, name: str, roles: tuple[str, ...]) -> str:
    matches = []
    for role in roles:
        locator = page.get_by_role(role, name=name, exact=True)
        count = _locator_count(locator)
        if count:
            matches.append(count)
    if any(count < 0 for count in matches):
        return "error"
    if not matches:
        return "absent"
    if len(matches) != 1 or matches[0] != 1:
        return "ambiguous"
    return "one"


def ensure_pending_view(page: Any) -> None:
    """Select Pending Cancellations. Filter chips do not need Client Alerts.

    The live /client-alerts page exposes the Pending Cancellations filter as
    a toggle button whose accessible name carries a count suffix, e.g.
    "Pending Cancellations (2)". _pending_chip_view/_click_pending_chip match
    that suffix via regex. There is no exact "Client Alerts" link/button and
    no exact "Pending Cancellations" control, so the old Client Alerts
    fallback (which could only raise IntakeHold) is removed.
    """
    assert_authenticated(page)
    if not _is_gateway_app_url(str(getattr(page, "url", "") or "")):
        raise IntakeHold("Expected exactly one Geico Gateway tab")
    if pending_view_selected(page):
        return
    # The filter chips render via JavaScript after DOM ready. Wait for the
    # Pending Cancellations chip before concluding it is absent.
    try:
        page.get_by_role("button", name=_PENDING_TOGGLE_TEXT).wait_for(timeout=20000)
    except Exception:
        pass
    chip = _pending_chip_view(page)
    if chip in {"ambiguous", "error", "absent"}:
        raise IntakeHold("Pending Cancellations view is missing or ambiguous")
    _click_pending_chip(page)
    if not pending_view_selected(page):
        raise IntakeHold("Pending Cancellations view did not become selected")


def more_pages(page: Any) -> bool | None:
    found = []
    for role in ("link", "button"):
        locator = page.get_by_role(role, name="Next", exact=True)
        if locator.count():
            found.append(locator)
    if not found:
        return False
    if len(found) != 1 or found[0].count() != 1:
        return None
    locator = found[0]
    try:
        aria = locator.get_attribute("aria-disabled")
        disabled = locator.is_disabled()
    except Exception:
        return None
    if aria not in {None, "true", "false"}:
        return None
    if aria == "true" or disabled is True:
        return False
    return True


def classify_policy_documents(page: Any) -> NoticePath:
    """Personal-lines notices are reached through Documents then Billing.

    Commercial prove shape is Billing only: no Documents control and no notice.
    """
    documents = _named_locator(page, "Documents", ("link", "tab", "button"))
    billing = _named_locator(page, "Billing", ("link", "tab", "button"))
    notices = _notice_match(page)
    if documents == "ambiguous" or billing == "ambiguous" or notices == "ambiguous":
        return NoticePath("ambiguous")
    if documents is None and billing is not None and notices is None:
        return NoticePath("billing_only")
    if documents is None:
        return NoticePath("absent")
    documents.click()
    billing_after = _named_locator(page, "Billing", ("link", "tab", "button"))
    if billing_after == "ambiguous" or billing_after is None:
        return NoticePath("ambiguous")
    billing_after.click()
    notices_after = _notice_match(page)
    if notices_after == "ambiguous":
        return NoticePath("ambiguous")
    if notices_after is None:
        return NoticePath("absent")
    return NoticePath("noc", notices_after[0])


POLICY_VIEW_HOST = "edgeextended.geico.com"
POLICY_TAB_TIMEOUT_MS = 30000
_DOCUMENTS_BOX = "gds-navigational-box#documents-consolidated-documents"
_ISSUED = re.compile(r"issued\s+(\d{1,2}/\d{1,2}/\d{4})", re.IGNORECASE)


def _settle(page: Any, ms: int = 3000) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=POLICY_TAB_TIMEOUT_MS)
    except Exception:
        pass
    try:
        page.wait_for_timeout(ms)
    except Exception:
        pass


def _close_gateway_page(page: Any) -> None:
    """Close a Geico tab this pull opened. Never anything else."""
    host = (urllib.parse.urlsplit(str(getattr(page, "url", "") or "")).hostname or "").casefold()
    if host and not (host == "geico.com" or host.endswith(".geico.com")) and host != "about:blank":
        return
    try:
        page.close()
    except Exception:
        pass


def _click_or_dispatch(locator: Any) -> None:
    # The Documents box sits under a summary overlay that swallows a real
    # click on the live policy page; the element's own click event still works.
    try:
        locator.click(timeout=6000)
    except Exception:
        locator.dispatch_event("click")


def notice_issued_on(text: str) -> date | None:
    found = {datetime.strptime(item, "%m/%d/%Y").date() for item in _ISSUED.findall(str(text or ""))}
    return found.pop() if len(found) == 1 else None


def open_billing_notices(page: Any) -> NoticePath:
    """Policy tab -> Documents -> Billing tab -> the one Cancellation Notice.

    Live 2026-10-05: the notice link href is "#" (Angular). The document UUID
    is not in the markup; it is captured from the viewer URL after the link
    is clicked during download_notice(). The durable ledger key is resolved
    after the fetch.
    """
    _settle(page, 6000)
    assert_authenticated(page)
    documents = page.locator(_DOCUMENTS_BOX)
    if _locator_count(documents) != 1:
        return NoticePath("absent")
    _click_or_dispatch(documents)
    _settle(page, 4000)
    billing = page.get_by_role("tab", name="Billing", exact=True)
    if _locator_count(billing) != 1:
        return NoticePath("ambiguous")
    _click_or_dispatch(billing)
    _settle(page, 4000)
    notice = _notice_match(page)
    if notice is None:
        return NoticePath("absent")
    if notice == "ambiguous":
        return NoticePath("ambiguous")
    try:
        item = page.locator("li", has=notice[1])
        text = str(item.first.inner_text() or "") if _locator_count(item) >= 1 else ""
    except Exception:
        text = ""
    # Live 2026-10-05: the notice link href is "#" (Angular). The document UUID
    # is not in the markup; it is captured from the viewer URL after the link
    # is clicked during download_notice(). Leave document_id empty here — the
    # durable key is resolved after the fetch.
    return NoticePath("noc", notice[0], issued_on=notice_issued_on(text))


def notice_is_stale(issued_on: date | None, due_on: date) -> bool:
    if issued_on is None:
        return False
    return (due_on - issued_on).days > NOTICE_MAX_AGE_DAYS


ALERT_LIST_WAIT_MS = 20000


def wait_for_alert_list(page: Any, *, timeout_ms: int = ALERT_LIST_WAIT_MS) -> None:
    """The live list renders a moment after the chip click; wait for one list
    whose row count holds steady across two looks. Pages without a wait hook
    (tests) return at once."""
    wait = getattr(page, "wait_for_timeout", None)
    if not callable(wait):
        return
    last = None
    waited = 0
    while waited < timeout_ms:
        if _alerts_table_count(page) == 1:
            try:
                rows = int(page.locator("gds-table gds-table-tbody gds-table-tr").count()) + int(
                    page.locator("table tbody tr").count()
                )
            except Exception:
                rows = -1
            if rows > 0 and rows == last:
                return
            last = rows
        wait(500)
        waited += 500


class PlaywrightGeicoNocBrowser:
    """Drive one already-authenticated Gateway tab. Does not type credentials."""

    def __init__(self, page: Any):
        self.page = page
        self._list_url = ""
        self.policy_page: Any = None

    def load_pending_cancellations(self) -> AlertGrid:
        ensure_pending_view(self.page)
        assert_authenticated(self.page)
        wait_for_alert_list(self.page)
        headers, rows = extract_alert_grid(self.page)
        grid = AlertGrid(
            list_url=str(getattr(self.page, "url", "") or ""),
            headers=headers,
            rows=rows,
            more_pages=more_pages(self.page),
        )
        parse_alert_grid(grid)
        self._list_url = require_list_url(grid.list_url)
        return grid

    def screenshot_pending_cancellations(self) -> bytes:
        """Full-page PNG of Pending Cancellations while that view is selected."""
        if not self._on_list():
            raise IntakeHold("Pending Cancellations screenshot is missing or not a PNG")
        data = self.page.screenshot(full_page=True, type="png")
        return require_png(data)

    def inspect_notice_path(self, policy_number: str) -> NoticePath:
        if self._gds_list():
            return self._inspect_gds_policy(policy_number)
        self._open_policy(policy_number)
        return classify_policy_documents(self.page)

    def _gds_list(self) -> bool:
        try:
            return self.page.locator("table").count() == 0 and self.page.locator("gds-table").count() == 1
        except Exception:
            return False

    def _inspect_gds_policy(self, policy_number: str) -> NoticePath:
        """Live Gateway: the row's View Policy link opens the policy in a new tab.

        No link on the row, or a link to Geico's separate commercial site, is
        reported as such without leaving the list.
        """
        policy = str(policy_number or "").strip()
        if not self._on_list() or not _POLICY_NUMBER.fullmatch(policy):
            raise IntakeHold("Policy open control is missing or ambiguous")
        rows = self.page.locator("gds-table gds-table-tbody gds-table-tr", has_text=policy)
        if _locator_count(rows) != 1:
            raise IntakeHold("Policy open control is missing or ambiguous")
        link = rows.get_by_role("link", name="View Policy", exact=True)
        count = _locator_count(link)
        if count == 0:
            return NoticePath("no_policy_link")
        if count != 1:
            return NoticePath("ambiguous")
        href = str(link.get_attribute("href") or "")
        host = (urllib.parse.urlsplit(href).hostname or "").casefold()
        if host.startswith("commercialservicing"):
            return NoticePath("commercial_site")
        if host != POLICY_VIEW_HOST:
            return NoticePath("ambiguous")
        with self.page.context.expect_page(timeout=POLICY_TAB_TIMEOUT_MS) as info:
            link.click()
        popup = info.value
        self.policy_page = popup
        return open_billing_notices(popup)

    def download_notice(self, policy_number: str) -> tuple[str, bytes]:
        """Click the notice link, capture the viewer URL's documentId, intercept the PDF XHR.

        Returns (document_id, pdf_bytes). The viewer URL returns HTML, not
        PDF; bytes come from network interception of the viewer's XHR, with
        the viewer's Download button as fallback. Fail-closed on any
        ambiguity.
        """
        if not _POLICY_NUMBER.fullmatch(str(policy_number or "").strip()):
            raise IntakeHold("NOC policy number is missing or ambiguous")
        page = self.policy_page if self.policy_page is not None else self.page
        return fetch_notice_pdf_via_viewer(page)

    def return_to_pending_list(self) -> None:
        popup, self.policy_page = self.policy_page, None
        if popup is not None:
            _close_gateway_page(popup)
        if self._on_list():
            return
        go_back = getattr(self.page, "go_back", None)
        if callable(go_back):
            try:
                go_back()
            except Exception as exc:
                raise IntakeHold("NOC capture left the Pending Cancellations list") from exc
        if not self._on_list():
            raise IntakeHold("NOC capture left the Pending Cancellations list")

    def _open_policy(self, policy_number: str) -> None:
        policy = str(policy_number or "").strip()
        if not self._on_list() or not _POLICY_NUMBER.fullmatch(policy):
            raise IntakeHold("Policy open control is missing or ambiguous")
        click_named(self.page, policy, roles=("link", "button"))

    def _on_list(self) -> bool:
        try:
            if not self._list_url or not _same_gateway_path(str(getattr(self.page, "url", "") or ""), self._list_url):
                return False
            if not pending_view_selected(self.page):
                return False
            return _alerts_table_count(self.page) == 1
        except IntakeHold:
            return False


_GDS_CLIENT_HEADER = "client/policy#"


def split_client_cell(text: str) -> tuple[str, str]:
    """Gateway "Client/Policy#" cell: name line(s) then the 10-digit policy."""
    lines = [_norm(line) for line in str(text or "").splitlines() if _norm(line)]
    if len(lines) < 2 or not _POLICY_NUMBER.fullmatch(lines[-1]):
        raise IntakeHold("NOC policy number is missing or ambiguous")
    return " ".join(lines[:-1]), lines[-1]


def normalize_gds_grid(
    headers: tuple[str, ...], rows: tuple[tuple[str, ...], ...]
) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    """Turn the live gds-table into the Insured / Policy / Product shape.

    Cells keep their line breaks here. The Client/Policy# cell splits into
    insured and policy; Product/Description keeps its first line (the product).
    """
    keys = [_norm(header).casefold() for header in headers]
    if keys.count(_GDS_CLIENT_HEADER) != 1:
        raise IntakeHold("Pending Cancellations table is missing or ambiguous")
    out_headers: list[str] = []
    for key, header in zip(keys, headers):
        if key == _GDS_CLIENT_HEADER:
            out_headers.extend(["Insured", "Policy"])
        else:
            out_headers.append(_norm(header))
    out_rows = []
    for cells in rows:
        if len(cells) != len(headers):
            raise IntakeHold("Pending Cancellations list is ambiguous")
        row: list[str] = []
        for key, cell in zip(keys, cells):
            if key == _GDS_CLIENT_HEADER:
                row.extend(split_client_cell(cell))
            elif key == "product/description":
                first = next((_norm(line) for line in str(cell or "").splitlines() if _norm(line)), "")
                row.append(first)
            else:
                row.append(_norm(cell))
        out_rows.append(tuple(row))
    return tuple(out_headers), tuple(out_rows)


def _gds_row_locators(page: Any, grid: Any = None) -> tuple[Any, ...]:
    if grid is not None:
        return tuple(grid.locator("gds-table-tbody gds-table-tr").all())
    return tuple(page.locator("gds-table gds-table-tbody gds-table-tr").all())


def extract_alert_grid(page: Any) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    tables = page.locator("table")
    if tables.count() == 0 and page.locator("gds-table").count() >= 1:
        # Gateway has 3 gds-tables: Client Alerts, Recent Policies, Recent Quotes.
        # Pending Cancellations is a filter on the Client Alerts table, identified
        # by its "Client/Policy#" header.
        grid = None
        for candidate in page.locator("gds-table").all():
            try:
                htexts = [str(n.inner_text() or "").strip().lower() for n in candidate.locator("gds-table-th").all()[:4]]
                if any("client/policy" in h for h in htexts):
                    grid = candidate
                    break
            except Exception:
                continue
        if grid is None:
            raise IntakeHold("Pending Cancellations table is missing or ambiguous")
        header_nodes = grid.locator("gds-table-thead gds-table-th").all()
        if not header_nodes:
            raise IntakeHold("Pending Cancellations table is missing or ambiguous")
        headers = tuple(str(node.inner_text() or "") for node in header_nodes)
        rows = tuple(
            tuple(str(cell.inner_text() or "") for cell in row.locator("gds-table-td").all())
            for row in _gds_row_locators(page, grid)
        )
        return normalize_gds_grid(headers, rows)
    if tables.count() != 1:
        raise IntakeHold("Pending Cancellations table is missing or ambiguous")
    header_nodes = _unique_child(tables, "thead").locator("th").all()
    if not header_nodes:
        raise IntakeHold("Pending Cancellations table is missing or ambiguous")
    headers = tuple(_norm(node.inner_text()) for node in header_nodes)
    row_locators = tuple(_unique_child(tables, "tbody").locator("tr").all())
    grid_rows = []
    for row in row_locators:
        cells = tuple(_norm(cell.inner_text()) for cell in row.locator("td").all())
        grid_rows.append(cells)
    return headers, tuple(grid_rows)


class LocalDeliveryLedger:
    """Private named-PDF ledger. A conflicting file is kept and the pull holds."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def ensure_private(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        if self.root.is_symlink() or not self.root.is_dir() or self.root.stat().st_mode & 0o077:
            raise IntakeHold("NOC output directory must be private (0700)")

    def has_named_file_or_record(self, *, document_id: str, filename: str) -> bool:
        self.ensure_private()
        path = self._named_path(filename)
        if path.exists() or path.is_symlink() or document_id in self._load()["items"]:
            return True
        return False

    def delivery_status(self, *, document_id: str, filename: str) -> bool:
        self.ensure_private()
        path = self._named_path(filename)
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
            or entry.get("sha256") != hashlib.sha256(path.read_bytes()).hexdigest()
        ):
            raise IntakeHold("Existing NOC file conflicts with the pull ledger")
        return True

    def record(self, source: SourceItem, *, due_on: date, policy_number: str, insured_name: str = "") -> Path:
        self.ensure_private()
        if source.filename == LEDGER_NAME:
            raise IntakeHold("NOC filename is missing or ambiguous")
        path = self._named_path(source.filename)
        if path.exists():
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
            "insured_name": insured_name,
            "sha256": digest,
            "bytes": len(source.content),
            "due_date": due_on.isoformat(),
            "policy_number": policy_number,
            "line": "personal",
        }
        self._write(data)
        return path

    def verified_ids(self, document_ids: set[str]) -> set[str]:
        found: set[str] = set()
        for document_id in document_ids:
            entry = self._load()["items"].get(document_id)
            if not isinstance(entry, dict):
                continue
            filename = str(entry.get("filename") or "")
            if self.delivery_status(document_id=document_id, filename=filename):
                found.add(document_id)
        return found

    def save_screenshot(self, day: date, png: bytes) -> Path:
        """Write the list PNG. A different existing shot is kept and a sibling is added."""
        self.ensure_private()
        blob = require_png(png)
        primary = self._named_path(pending_screenshot_name(day))
        if primary.exists():
            if primary.is_symlink() or not primary.is_file():
                raise IntakeHold("Pending Cancellations screenshot is missing or not a PNG")
            if primary.read_bytes() == blob:
                return primary
            return self._write_png(self._sibling_screenshot(day), blob)
        return self._write_png(primary, blob)

    def _sibling_screenshot(self, day: date) -> Path:
        stem = pending_screenshot_name(day)[:-4]
        for index in range(2, 100):
            candidate = self.root / f"{stem}-{index}.png"
            if not candidate.exists() and not candidate.is_symlink():
                return candidate
        raise IntakeHold("Pending Cancellations screenshot is missing or not a PNG")

    def _write_png(self, path: Path, blob: bytes) -> Path:
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

    def _named_path(self, filename: str) -> Path:
        if not filename or filename != Path(filename).name or filename in {".", ".."}:
            raise IntakeHold("NOC filename is missing or ambiguous")
        return self.root / filename

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


def _ledger_id_for_filename(ledger: LocalDeliveryLedger, filename: str) -> str:
    """Find the ledger document ID for a filename (any key format)."""
    try:
        items = ledger._load()["items"]
    except Exception:
        return ""
    target = str(filename or "")
    for doc_id, entry in items.items():
        if isinstance(entry, dict) and str(entry.get("filename") or "") == target:
            return str(doc_id)
    return ""


def _ledger_filename_verified(ledger: LocalDeliveryLedger, filename: str) -> bool:
    """Check if the ledger has a verified entry for a filename (any document ID).

    Used during the policy-based pre-download check to handle the key
    migration to UUID-based ledger keys. Returns True if a ledger entry
    exists for the filename AND the file content matches the recorded hash.
    """
    try:
        items = ledger._load()["items"]
    except Exception:
        return False
    target = str(filename or "")
    path = ledger.root / target
    if not path.is_file() or path.is_symlink():
        return False
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return False
    for entry in items.values():
        if not isinstance(entry, dict):
            continue
        if str(entry.get("filename") or "") == target and entry.get("sha256") == digest:
            return True
    return False


def _ledger_has_filename(ledger: LocalDeliveryLedger, filename: str) -> bool:
    """Check if the ledger has any entry for a filename (any document ID).

    Used to distinguish key-migration (UUID-based entries exist) from true
    conflicts (no entry at all) during the policy-based pre-download check.
    """
    try:
        items = ledger._load()["items"]
    except Exception:
        return False
    target = str(filename or "")
    for entry in items.values():
        if isinstance(entry, dict) and str(entry.get("filename") or "") == target:
            return True
    return False


def run_pull(
    browser: Any,
    ledger: LocalDeliveryLedger,
    archive: SourceArchive,
    *,
    as_of: date,
) -> dict[str, Any]:
    """List Pending Cancellations, download personal-lines NOCs, hold commercial gaps.

    Once the Pending Cancellations view is on screen, the list PNG is kept for
    the dated QA pack on both a matched pull and a later hold. A hold before
    that capture does not invent a screenshot.
    """
    from .document_retrieval_filing import require_carrier_pull

    require_carrier_pull("geico")
    refuse_production_host()
    if not isinstance(as_of, date):
        raise IntakeHold("Pending Cancellations as-of date is missing or ambiguous")
    grid = browser.load_pending_cancellations()
    alerts = parse_alert_grid(grid)
    png = require_png(browser.screenshot_pending_cancellations())
    seen = {alert.policy_number: _row_payload(alert, outcome="LISTED") for alert in alerts}
    held: list[dict[str, Any]] = []
    downloaded: list[dict[str, Any]] = []
    targeted: list[AlertRow] = []
    skipped: list[str] = []

    def current_rows() -> list[dict[str, Any]]:
        return [seen[alert.policy_number] for alert in alerts]

    def fail(reason: str, *, pdfs: int | None = None) -> None:
        try:
            shot = str(ledger.save_screenshot(as_of, png))
        except IntakeHold as exc:
            raise IntakeHold(f"{reason}; screenshot was not saved ({exc})") from exc
        held_exc = _held_now(reason, held, downloaded, current_rows(), targeted, pdfs=pdfs)
        held_exc.details["screenshot"] = shot
        held_exc.details["alerts"] = current_rows()
        held_exc.details["as_of"] = as_of.isoformat()
        raise held_exc

    def remember(alert: AlertRow, row: dict[str, Any]) -> None:
        seen[alert.policy_number] = row

    def back() -> None:
        try:
            browser.return_to_pending_list()
        except IntakeHold as exc:
            fail(str(exc))

    if grid.more_pages is not False:
        fail("Pending Cancellations list is incomplete or ambiguous")
    for alert in alerts:
        if alert.line == "commercial":
            if ledger.has_named_file_or_record(document_id=alert.document_id, filename=alert.filename):
                row = _row_payload(
                    alert, outcome="HELD",
                    reason=f"commercial policy {alert.policy_number} has a local NOC file; not a personal-lines pull",
                )
                held.append(row)
                remember(alert, row)
                fail(row["reason"])
            path = browser.inspect_notice_path(alert.policy_number)
            back()
            if path.kind in {"no_policy_link", "commercial_site"}:
                reason = (
                    "Commercial policy. Geico has no policy page link on this alert, so no notice was pulled."
                    if path.kind == "no_policy_link"
                    else "Commercial policy. Geico sends this one to its separate commercial site, so no notice was pulled."
                )
                row = _row_payload(alert, outcome="HELD", reason=reason)
                held.append(row)
                remember(alert, row)
                continue
            if path.kind == "billing_only":
                row = _row_payload(
                    alert,
                    outcome="HELD",
                    reason="commercial policy: Documents path missing; Billing only",
                )
                held.append(row)
                remember(alert, row)
                continue
            if path.kind == "noc":
                row = _row_payload(
                    alert,
                    outcome="HELD",
                    reason="commercial policy exposed a Pending Cancellation Notice; not downloaded as a personal-lines NOC",
                )
                held.append(row)
                remember(alert, row)
                continue
            row = _row_payload(
                alert, outcome="HELD",
                reason=f"commercial policy {alert.policy_number} document path is missing or ambiguous",
            )
            held.append(row)
            remember(alert, row)
            fail(row["reason"])
        if alert.line != "personal":
            raise IntakeHold("Pending Cancellation alert line is missing or ambiguous")
        try:
            already = ledger.delivery_status(document_id=alert.document_id, filename=alert.filename)
        except IntakeHold as exc:
            # Policy-based key may not match UUID-based ledger entries from
            # the direct-fetch path. If the ledger has an entry for this
            # filename (any document ID) and the file content matches, treat
            # as already delivered without inspecting.
            already = _ledger_filename_verified(ledger, alert.filename)
            if not already:
                # No verifiable entry: true conflict, fail fast.
                remember(alert, _row_payload(alert, outcome="HELD", reason=str(exc)))
                fail(str(exc))
        if already:
            targeted.append(alert)
            # For filename-verified skips, find the actual ledger document ID.
            skip_id = alert.document_id
            if skip_id not in ledger._load().get("items", {}):
                found = _ledger_id_for_filename(ledger, alert.filename)
                if found:
                    skip_id = found
            skipped.append(skip_id)
            remember(alert, _row_payload(alert, outcome="ALREADY_DELIVERED", filename=alert.filename))
            continue
        path = browser.inspect_notice_path(alert.policy_number)
        soft_reason = ""
        if path.kind == "no_policy_link":
            soft_reason = "Geico has no policy page link on this alert, so no notice was pulled."
        elif path.kind == "noc" and notice_is_stale(path.issued_on, alert.due_on):
            issued = path.issued_on
            soft_reason = (
                f"The only cancellation notice Geico shows was issued {issued.month}/{issued.day}/{issued.year}, "
                f"well before this cancellation due {alert.due_on.month}/{alert.due_on.day}/{alert.due_on.year}, "
                "so it belongs to an earlier cancellation and was not pulled."
            )
        if soft_reason:
            try:
                browser.return_to_pending_list()
            except IntakeHold as exc:
                fail(str(exc))
            row = _row_payload(alert, outcome="HELD", reason=soft_reason)
            held.append(row)
            remember(alert, row)
            continue
        if path.kind != "noc":
            try:
                browser.return_to_pending_list()
            except IntakeHold:
                pass
            reason = (
                f"personal-lines policy {alert.policy_number} is missing "
                "Documents → Billing → Pending Cancellation Notice"
            )
            row = _row_payload(alert, outcome="HELD", reason=reason)
            held.append(row)
            remember(alert, row)
            fail(reason)
        # Durable ledger key uses the GEICO document UUID when available.
        # The pre-download check above used the policy-based key; re-check
        # with the durable key to avoid re-downloading a UUID-keyed entry.
        durable_id = alert.document_id
        if path.document_id:
            try:
                durable_id = noc_document_id(alert.policy_number, alert.due_on, path.document_id)
            except IntakeHold:
                durable_id = alert.document_id
            try:
                if ledger.delivery_status(document_id=durable_id, filename=alert.filename):
                    targeted.append(alert)
                    skipped.append(durable_id)
                    remember(alert, _row_payload(alert, outcome="ALREADY_DELIVERED", filename=alert.filename))
                    back()
                    continue
            except IntakeHold as exc:
                remember(alert, _row_payload(alert, outcome="HELD", reason=str(exc)))
                fail(str(exc))
        document_uuid, content = browser.download_notice(alert.policy_number)
        if not _is_pdf(content):
            remember(alert, _row_payload(alert, outcome="HELD", reason="NOC download is not a PDF"))
            fail("NOC download is not a PDF")
        # Prefer the UUID from the direct fetch; fall back to the inspected one.
        fetch_durable_id = durable_id
        if document_uuid:
            try:
                fetch_durable_id = noc_document_id(alert.policy_number, alert.due_on, document_uuid)
            except IntakeHold:
                pass
        source = SourceItem(
            system=PROCESS,
            source_account=GATEWAY_HOST,
            source_id=fetch_durable_id,
            source_url=f"{alert.source_url}#policy={alert.policy_number}",
            received_at=_received_at(as_of),
            filename=alert.filename,
            content=content,
        )
        source.validate()
        try:
            saved = ledger.record(source, due_on=alert.due_on, policy_number=alert.policy_number, insured_name=alert.insured_name)
            archive.preserve(source)
        except IntakeHold as exc:
            row = _row_payload(alert, outcome="HELD", reason=str(exc))
            held.append(row)
            remember(alert, row)
            fail(str(exc))
        item = {
            "document_id": fetch_durable_id,
            "filename": alert.filename,
            "sha256": source.digest,
            "bytes": len(content),
            "policy_number": alert.policy_number,
            "insured_name": alert.insured_name,
            "due_date": alert.due_on.isoformat(),
            "status": alert.status,
            "path": str(saved),
        }
        downloaded.append(item)
        remember(alert, _row_payload(alert, outcome="PULLED", filename=alert.filename))
        back()
        targeted.append(alert)
    # Parity uses durable ledger keys: UUID-based for downloads, and the
    # skipped IDs (verified to exist in the ledger during the pull).
    targeted_ids = {item["document_id"] for item in downloaded} | set(skipped)
    verified = ledger.verified_ids(targeted_ids)
    try:
        evidence = require_noc_pdf_parity(targeted_ids=targeted_ids, verified_ids=verified)
    except IntakeHold as exc:
        fail(str(exc), pdfs=len(verified))
    shot = ledger.save_screenshot(as_of, png)
    evidence["screenshot"] = str(shot)
    pack_pdfs = downloaded + [
        {
            "filename": alert.filename,
            "policy_number": alert.policy_number,
            "insured_name": alert.insured_name,
            "due_date": alert.due_on.isoformat(),
            "path": str(ledger.root / alert.filename),
            "already_present": True,
        }
        for alert in targeted
        if alert.document_id in skipped
    ]
    return {
        "status": "PULLED",
        "scope": SCOPE,
        "process": PROCESS,
        "as_of": as_of.isoformat(),
        "alert_count": len(alerts),
        "targeted": len(targeted),
        "count": len(downloaded),
        "downloaded": downloaded,
        "pack_pdfs": pack_pdfs,
        "skipped_already_delivered": skipped,
        "held": held,
        "alerts": current_rows(),
        "rows": current_rows(),
        "verification": evidence,
        "ezlynx": "not_run",
    }


def connect_cdp_browser(cdp_url: str | None) -> tuple[PlaywrightGeicoNocBrowser, Callable[[], None]]:
    """Attach to the local Chrome. Exactly one Gateway application tab."""
    from .document_retrieval_filing import require_carrier_pull

    require_carrier_pull("geico")
    refuse_production_host()
    require_hermes_test_host()
    url = require_loopback_cdp(cdp_url or os.environ.get("ROBIE_BROWSER_CDP_URL") or DEFAULT_CDP_URL)
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(url)
        pages = [page for context in browser.contexts for page in context.pages]
        return PlaywrightGeicoNocBrowser(select_gateway_page(pages)), playwright.stop
    except Exception:
        playwright.stop()
        raise


def select_gateway_page(pages: list[Any]) -> Any:
    """Use the single Gateway application tab. Extra Gateway tabs are ambiguous."""
    matches = [page for page in pages if _is_gateway_app_url(str(getattr(page, "url", "") or ""))]
    if len(matches) != 1:
        raise IntakeHold("Expected exactly one Geico Gateway tab")
    return matches[0]


def require_loopback_cdp(url: str) -> str:
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.username or parsed.password or parsed.scheme not in {"http", "https"} or host not in {"127.0.0.1", "localhost"}:
        raise IntakeHold("Geico Gateway browser attach must use the local Test CDP endpoint")
    return str(url).strip()


def require_gateway_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise IntakeHold("Geico Gateway URL is missing or ambiguous")
    if "b2clogin" in host or parsed.path.rstrip("/").lower().endswith("/login"):
        raise IntakeHold("Geico Gateway session is not authenticated")
    if host != GATEWAY_HOST:
        raise IntakeHold("Geico Gateway URL is missing or ambiguous")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def require_list_url(url: str) -> str:
    cleaned = require_gateway_url(url)
    # Pending Cancellations is a filter on the Gateway home page (/) or /client-alerts
    return cleaned


def require_hermes_test_host() -> None:
    """Live packs are produced on hermes-test-01. Fixture runs inject a browser.

    hermes-poc-01 is accepted only when Geico Production filing is enabled.
    """

    from .document_retrieval_filing import live_filing_decision

    if live_filing_decision(os.environ, socket.gethostname(), "geico").allowed:
        return
    raw = f"{socket.gethostname()} {socket.getfqdn()}".lower()
    labels = [label for label in re.split(r"[\s.]+", raw) if label]
    if HERMES_TEST_HOST not in labels:
        raise IntakeHold("Geico QA pack must be produced on hermes-test-01")


def _replace_private(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(path.name + ".tmp")
    if temporary.is_symlink():
        raise IntakeHold("QA pack file is missing or ambiguous")
    if temporary.exists():
        temporary.unlink()
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


def qa_pack_dir(output_root: Path, day: date) -> Path:
    """Return ``{output_root}/{YYYY-MM-DD}``.

    The default root is ``.../carrier-pull-qa/geico``, so the dated pack is
    ``.../carrier-pull-qa/geico/{YYYY-MM-DD}/``.
    """
    root = Path(output_root).expanduser()
    if not root.is_absolute():
        raise IntakeHold("QA pack output root must be an absolute path")
    if not isinstance(day, date):
        raise IntakeHold("Pending Cancellations as-of date is missing or ambiguous")
    pack = root / day.isoformat()
    resolved_root = root.resolve()
    resolved_pack = pack.resolve()
    if resolved_root != resolved_pack and resolved_root not in resolved_pack.parents:
        raise IntakeHold("QA pack path is missing or ambiguous")
    if pack.name != day.isoformat():
        raise IntakeHold("QA pack path is missing or ambiguous")
    return pack


def drive_record(day: date) -> dict[str, Any]:
    return {
        "status": "not_run",
        "parent_id": CARRIER_QA_DRIVE_PARENT_ID,
        "folder_id": GEICO_QA_DRIVE_FOLDER_ID,
        "path": f"Robie Carrier Pull QA (Nicole)/Geico/{day.isoformat()}/",
    }


def render_qa_readme(
    *,
    as_of: date,
    run_ts: str,
    status: str,
    alerts: list[dict[str, Any]],
    downloaded: list[dict[str, Any]],
    held: list[dict[str, Any]],
    reason: str = "",
    drive: dict[str, Any] | None = None,
) -> str:
    lines = [
        f"# Geico Pending Cancellation QA — {as_of.isoformat()}",
        "",
        f"Run: {run_ts}",
        f"Processed date: {as_of.isoformat()}",
        f"Status: {status}",
        "",
        "Robie listed Pending Cancellations on Geico Agent Gateway "
        "(gateway2.geico.com/client-alerts). This pack was written under the "
        "Test artifacts root on hermes-test-01. It does not file anything in EZLynx.",
        "",
        "## What Robie saw",
        "",
        "| Policy | Insured | Due | Status | Product | Line | Outcome |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in alerts:
        lines.append(
            "| {policy_number} | {insured_name} | {due_date} | {status} | {product} | {line} | {outcome} |".format(
                policy_number=row.get("policy_number", ""),
                insured_name=row.get("insured_name", ""),
                due_date=row.get("due_date", ""),
                status=row.get("status", ""),
                product=row.get("product", ""),
                line=row.get("line", ""),
                outcome=row.get("outcome", ""),
            )
        )
    lines.extend(["", "## Downloaded", ""])
    if downloaded:
        for item in downloaded:
            lines.append(f"- `{item.get('filename', '')}`")
    else:
        lines.append("No PDFs saved on this run.")
    lines.extend(["", "## HELD", ""])
    if held:
        for row in held:
            lines.append(
                f"- `{row.get('policy_number', '')}` {row.get('insured_name', '')}: {row.get('reason', '')}"
            )
    else:
        lines.append("No held rows.")
    if reason:
        lines.extend(["", "## Job hold", "", reason])
    info = drive or {}
    lines.extend([
        "",
        f"Drive: {info.get('status', 'not_run')}",
        f"Drive parent: {info.get('parent_id', '')}",
        f"Drive Geico folder: {info.get('folder_id', '')}",
        f"Drive path: {info.get('path', '')}",
        "",
    ])
    lines.extend([
        "",
        "Nicole: open this day's folder, compare the Pending Cancellations screenshot "
        "with the PDFs, then complete the EZLynx filing and the status sheet. "
        "Add further carriers from the shared `_Carriers to add` note. "
        "This pack does not do that filing.",
        "",
    ])
    return "\n".join(lines)


def publish_qa_pack(
    pack: Path,
    *,
    as_of: date,
    run_ts: str,
    status: str,
    alerts: list[dict[str, Any]],
    downloaded: list[dict[str, Any]],
    held: list[dict[str, Any]],
    screenshot: str,
    reason: str = "",
    drive: dict[str, Any] | None = None,
    portal_gate: str = "",
) -> dict[str, Any]:
    """Write README.md and manifest.json next to the screenshot and PDFs."""
    shot = Path(screenshot)
    if shot.parent.resolve() != pack.resolve() or shot.is_symlink() or not shot.is_file():
        raise IntakeHold("Pending Cancellations screenshot is missing or not a PNG")
    require_png(shot.read_bytes())
    drive_info = dict(drive or drive_record(as_of))
    manifest: dict[str, Any] = {
        "carrier": "geico",
        "run_ts": run_ts,
        "rows": alerts,
        "alerts": alerts,
        "downloaded": downloaded,
        "held": held,
        "screenshot": str(shot),
        "status": status,
        "as_of": as_of.isoformat(),
        "ezlynx": "not_run",
        "drive": drive_info,
    }
    if portal_gate:
        manifest["portal_gate"] = portal_gate
    if reason:
        manifest["reason"] = reason
    readme = render_qa_readme(
        as_of=as_of, run_ts=run_ts, status=status, alerts=alerts,
        downloaded=downloaded, held=held, reason=reason, drive=drive_info,
    )
    _replace_private(pack / "README.md", readme.encode("utf-8"))
    _replace_private(pack / "manifest.json", json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n")
    return manifest


def refuse_geico_drive_upload(pack: Path) -> None:
    """TODO: upload the date folder into the Nicole Geico Drive. Fail closed.

    The local pack is left in place. This function does not import or call Google.
    """
    manifest_path = pack / "manifest.json"
    readme_path = pack / "README.md"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        readme = readme_path.read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError) as exc:
        raise IntakeHold(DRIVE_UPLOAD_UNAVAILABLE) from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("drive"), dict):
        raise IntakeHold(DRIVE_UPLOAD_UNAVAILABLE)
    drive = manifest["drive"]
    if drive.get("parent_id") != CARRIER_QA_DRIVE_PARENT_ID:
        raise IntakeHold(DRIVE_UPLOAD_UNAVAILABLE)
    if drive.get("folder_id") != GEICO_QA_DRIVE_FOLDER_ID:
        raise IntakeHold(DRIVE_UPLOAD_UNAVAILABLE)
    if "Drive: not_run\n" not in readme:
        raise IntakeHold(DRIVE_UPLOAD_UNAVAILABLE)
    drive["status"] = "HELD"
    drive["reason"] = DRIVE_UPLOAD_UNAVAILABLE
    _replace_private(
        manifest_path,
        json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n",
    )
    _replace_private(
        readme_path,
        readme.replace(
            "Drive: not_run\n",
            f"Drive: HELD — {DRIVE_UPLOAD_UNAVAILABLE}\n",
            1,
        ).encode("utf-8"),
    )
    raise IntakeHold(DRIVE_UPLOAD_UNAVAILABLE)


def _publish_held(pack: Path, exc: PullHeld, *, as_of: date, run_ts: str) -> dict[str, Any] | None:
    shot = str(exc.details.get("screenshot") or "")
    if not shot:
        return None
    return publish_qa_pack(
        pack,
        as_of=as_of,
        run_ts=run_ts,
        status="HELD",
        alerts=list(exc.details.get("alerts") or exc.details.get("rows") or []),
        downloaded=list(exc.details.get("downloaded") or []),
        held=list(exc.details.get("held") or []),
        screenshot=shot,
        reason=str(exc),
        drive=drive_record(as_of),
        portal_gate="HELD",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pull Geico Pending Cancellation NOCs (Test only)")
    parser.add_argument("--as-of", required=True, help="List screenshot date, YYYY-MM-DD")
    parser.add_argument(
        "--output-root",
        default=str(DEFAULT_OUTPUT_ROOT),
        help=(
            "Geico QA root on hermes-test-01. The pack is written to "
            "{output-root}/{YYYY-MM-DD}/ "
            "(default: .../carrier-pull-qa/geico)."
        ),
    )
    parser.add_argument(
        "--upload-drive",
        action="store_true",
        help=(
            "Upload that day's QA folder to Nicole's Geico Drive folder "
            f"({GEICO_QA_DRIVE_FOLDER_ID}) under parent {CARRIER_QA_DRIVE_PARENT_ID}. "
            "Not implemented; fails closed and does not call Google."
        ),
    )
    parser.add_argument("--cdp-url", default=None, help="Loopback CDP URL. Defaults to 127.0.0.1:9222")
    return parser


def _emit_drive_refusal(pack: Path, *, portal_gate: str, extra: dict[str, Any] | None = None) -> int:
    """Local pack stays. The command is HELD because Drive was requested and refused."""
    payload: dict[str, Any] = {
        "status": "HELD",
        "portal_gate": portal_gate,
        "reason": DRIVE_UPLOAD_UNAVAILABLE,
        "ezlynx": "not_run",
        "pack": str(pack),
        "drive": {
            "status": "HELD",
            "reason": DRIVE_UPLOAD_UNAVAILABLE,
            "parent_id": CARRIER_QA_DRIVE_PARENT_ID,
            "folder_id": GEICO_QA_DRIVE_FOLDER_ID,
        },
    }
    if (pack / "manifest.json").is_file():
        payload["manifest"] = str(pack / "manifest.json")
        payload["readme"] = str(pack / "README.md")
    if extra:
        payload.update(extra)
    _emit(payload)
    return 2


def main(
    argv: list[str] | None = None,
    *,
    browser_factory: Callable[[argparse.Namespace], Any] | None = None,
    run_ts: str | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    closer: Callable[[], None] | None = None
    pack: Path | None = None
    try:
        from .document_retrieval_filing import FilingHeld, require_carrier_pull, resolve_pull_output

        require_carrier_pull("geico")
        refuse_production_host()
        try:
            as_of = date.fromisoformat(args.as_of)
        except ValueError as exc:
            raise IntakeHold("Pending Cancellations as-of date is missing or ambiguous") from exc
        try:
            output_root = resolve_pull_output(args.output_root, DEFAULT_OUTPUT_ROOT)
        except FilingHeld as exc:
            raise IntakeHold(str(exc)) from exc
        pack = qa_pack_dir(output_root, as_of)
        stamped = run_ts or datetime.now(_EASTERN).isoformat()
        if browser_factory is None:
            require_hermes_test_host()
            browser, closer = connect_cdp_browser(args.cdp_url)
        else:
            browser = browser_factory(args)
        ledger = LocalDeliveryLedger(pack)
        archive = SourceArchive(pack / "sources")
        try:
            receipt = run_pull(browser, ledger, archive, as_of=as_of)
        except PullHeld as exc:
            published = _publish_held(pack, exc, as_of=as_of, run_ts=stamped)
            if args.upload_drive:
                if published is None:
                    return _emit_drive_refusal(pack, portal_gate="HELD", extra={"pull_reason": str(exc)})
                try:
                    refuse_geico_drive_upload(pack)
                except IntakeHold:
                    return _emit_drive_refusal(
                        pack,
                        portal_gate="HELD",
                        extra={"pull_reason": str(exc), "held": exc.details.get("held") or []},
                    )
            payload = {"status": "HELD", "reason": str(exc), "ezlynx": "not_run", "pack": str(pack)}
            payload.update(exc.details)
            payload["drive"] = drive_record(as_of)
            if (pack / "manifest.json").is_file():
                payload["manifest"] = str(pack / "manifest.json")
                payload["readme"] = str(pack / "README.md")
            _emit(payload)
            return 2
        publish_qa_pack(
            pack,
            as_of=as_of,
            run_ts=stamped,
            status="PULLED",
            alerts=list(receipt.get("alerts") or []),
            downloaded=list(receipt.get("pack_pdfs") or receipt.get("downloaded") or []),
            held=list(receipt.get("held") or []),
            screenshot=str(receipt["verification"]["screenshot"]),
            drive=drive_record(as_of),
        )
        if args.upload_drive:
            try:
                refuse_geico_drive_upload(pack)
            except IntakeHold:
                return _emit_drive_refusal(
                    pack,
                    portal_gate="PULLED",
                    extra={
                        "downloaded": receipt.get("downloaded") or [],
                        "held": receipt.get("held") or [],
                    },
                )
        receipt["drive"] = drive_record(as_of)
        receipt["pack"] = str(pack)
        receipt["manifest"] = str(pack / "manifest.json")
        receipt["readme"] = str(pack / "README.md")
        _emit(receipt)
        return 0
    except PullHeld as exc:
        payload = {"status": "HELD", "reason": str(exc), "ezlynx": "not_run"}
        payload.update(exc.details)
        _emit(payload)
        return 2
    except IntakeHold as exc:
        _emit({"status": "HELD", "reason": str(exc), "ezlynx": "not_run"})
        return 2
    except Exception as exc:
        _emit({
            "status": "UNVERIFIED",
            "reason": f"Geico NOC pull unavailable ({type(exc).__name__})",
            "ezlynx": "not_run",
        })
        return 1
    finally:
        if closer is not None:
            closer()


def pending_screenshot_name(day: date) -> str:
    name = f"geico-pending-cancellations-{day.isoformat()}.png"
    if name != Path(name).name:
        raise IntakeHold("Pending Cancellations screenshot is missing or not a PNG")
    return name


def require_png(blob: bytes | bytearray | None) -> bytes:
    if not isinstance(blob, (bytes, bytearray)) or not bytes(blob).startswith(_PNG_MAGIC):
        raise IntakeHold("Pending Cancellations screenshot is missing or not a PNG")
    return bytes(blob)


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


def _received_at(day: date) -> str:
    return datetime.combine(day, time(12, 0), tzinfo=_EASTERN).isoformat()


def _is_pdf(blob: bytes | bytearray) -> bool:
    data = bytes(blob)
    return len(data) >= 8 and data.startswith(b"%PDF")


def _push_pdf(candidates: list[bytes], blob: bytes) -> None:
    if not blob:
        return
    if not _is_pdf(blob):
        raise IntakeHold("NOC download is not a PDF")
    candidates.append(blob)


def _url_looks_like_pdf(url: str) -> bool:
    path = urllib.parse.urlsplit(url).path.lower()
    return path.endswith(".pdf") or "application/pdf" in url.lower()


def _allowed_pdf_url(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or "b2clogin" in host:
        return False
    return host == "geico.com" or host.endswith(".geico.com")


def _is_gateway_app_url(url: str) -> bool:
    try:
        require_gateway_url(url)
    except IntakeHold:
        return False
    return True


def _same_gateway_path(url: str, list_url: str) -> bool:
    current = urllib.parse.urlsplit(url)
    expected = urllib.parse.urlsplit(list_url)
    return (
        current.scheme == expected.scheme
        and (current.hostname or "").lower() == (expected.hostname or "").lower()
        and current.path.rstrip("/") == expected.path.rstrip("/")
    )


def _unique_child(parent: Any, selector: str) -> Any:
    locator = parent.locator(selector)
    if locator.count() != 1:
        raise IntakeHold("Pending Cancellations table is missing or ambiguous")
    return locator


def _control_text(locator: Any) -> str:
    inner = getattr(locator, "inner_text", None)
    if callable(inner):
        try:
            text = _norm(inner())
        except Exception:
            text = ""
        if text:
            return text
    value = getattr(locator, "input_value", None)
    if callable(value):
        try:
            return _norm(value())
        except Exception:
            return ""
    return ""


def _named_locator(page: Any, name: str, roles: tuple[str, ...]) -> Any:
    matches = []
    for role in roles:
        locator = page.get_by_role(role, name=name, exact=True)
        count = locator.count()
        if count:
            matches.append((count, locator))
    if not matches:
        return None
    if len(matches) != 1 or matches[0][0] != 1:
        return "ambiguous"
    return matches[0][1]


def _notice_match(page: Any) -> Any:
    matches = []
    for name in NOTICE_NAMES:
        for role in ("link", "button"):
            locator = page.get_by_role(role, name=name, exact=True)
            count = locator.count()
            if count:
                matches.append((count, name, locator))
    if not matches:
        return None
    if len(matches) != 1 or matches[0][0] != 1:
        return "ambiguous"
    return matches[0][1], matches[0][2]


def _row_payload(alert: AlertRow, *, outcome: str, reason: str = "", filename: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {
        "policy_number": alert.policy_number,
        "insured_name": alert.insured_name,
        "due_date": alert.due_on.isoformat(),
        "status": alert.status,
        "product": alert.product,
        "line": alert.line,
        "outcome": outcome,
    }
    if reason:
        payload["reason"] = reason
    if filename:
        payload["filename"] = filename
    return payload


def _held_now(reason, held, downloaded, rows, targeted, pdfs: int | None = None) -> PullHeld:
    return PullHeld(
        reason,
        held=held,
        downloaded=downloaded,
        rows=rows,
        targeted=len(targeted),
        pdfs=pdfs if pdfs is not None else len(downloaded),
    )


def _require_return(browser, held, downloaded, rows, targeted) -> None:
    try:
        browser.return_to_pending_list()
    except IntakeHold as exc:
        raise _held_now(str(exc), held, downloaded, rows, targeted) from exc


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
    for item in _each(page.locator("a")):
        href = str(item.get_attribute("href") or "")
        if href.startswith("blob:") or _url_looks_like_pdf(href):
            sources.append(href)
    return sources


def _each(locator: Any) -> list[Any]:
    all_fn = getattr(locator, "all", None)
    if callable(all_fn):
        return list(all_fn())
    if locator.count() == 0:
        return []
    if locator.count() != 1:
        raise IntakeHold("NOC PDF capture is missing or ambiguous")
    return [locator]


def _read_blob(page: Any, url: str) -> bytes:
    payload = page.evaluate(_BLOB_JS, url)
    if not payload:
        return b""
    if not isinstance(payload, str):
        raise IntakeHold("NOC PDF capture is missing or ambiguous")
    try:
        return base64.b64decode(payload, validate=True)
    except Exception as exc:
        raise IntakeHold("NOC PDF capture is missing or ambiguous") from exc


def _http_get(page: Any, url: str) -> bytes:
    response = page.context.request.get(url, timeout=DOWNLOAD_TIMEOUT_MS)
    if getattr(response, "ok", True) is False:
        raise IntakeHold("NOC PDF capture is missing or ambiguous")
    body = response.body()
    if not isinstance(body, (bytes, bytearray)):
        raise IntakeHold("NOC PDF capture is missing or ambiguous")
    return bytes(body)


def _download_bytes(download: Any) -> bytes:
    handle = tempfile.NamedTemporaryFile(prefix="geico-noc-", suffix=".pdf", delete=False)
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


if __name__ == "__main__":
    raise SystemExit(main())
