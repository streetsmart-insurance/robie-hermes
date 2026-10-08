"""Test-only NatGen Pending Cancellation NOC list and download.

The playbook walks natgenagency.com: Agent Dashboard, Your Notifications,
Policy To Dos, Pending Cancellations, then the policy, Policy History, and
the most recent Pending Cancellation / NOC Forms View PDF. When the tab is
already the Pending Cancellations report — that URL, or an Agency Activity
report page that shows the heading and the table — Agent Dashboard is not
required. A missing Agent Dashboard does not hold by itself. The pull still
holds when the report is not reached.

The Pending Cancellations list is not day-filtered. Rows are scrubbed by
process date. A Monday in the requested window also keeps the preceding
Saturday and Sunday. Each saved PDF's cancel effective date must match the
list. A mismatch is noted in the README and the pack is HELD. The wrong
document is not filed under the official name.

Accessible names below are that path, not a certified live DOM. A missing or
non-unique control raises IntakeHold. This module does not log in, does not
upload, note, task, or label in EZLynx, and does not register a timer.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.parse
import zlib
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable

from .intake_core import IntakeHold, SourceArchive, SourceItem
from .natgen_retrieval import (
    ADDITIONAL_INFO_SCOPE,
    ADDITIONAL_INFO_TODO,
    NOC_SCOPE,
    NocDateHold,
    NatGenRetrieval,
    require_bounded_scope,
    scrub_window,
)


NATGEN_SOURCE_ACCOUNT = "natgenagency"
DEFAULT_CDP_URL = "http://127.0.0.1:9222"
DOWNLOAD_TIMEOUT_MS = 8000
LEDGER_NAME = "natgen-noc-ledger.json"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
DEFAULT_QA_ROOT = Path(
    "/opt/streetsmart-hermes-test/robie-job-engine/data/artifacts/carrier-pull-qa/natgen"
)
# Shared Drive "Robie Carrier Pull QA (Nicole)". The NatGen child folder id is
# UNVERIFIED. Folder upload is TODO. --upload-drive fails closed and does not
# call Google. The recording uploader writes one video/webm and is not used.
DRIVE_QA_PARENT_ID = "1cLEpR-0T6KdiVjcdAr0qpGTO447MetI2"
DRIVE_QA_FOLDER_NAME = "Robie Carrier Pull QA (Nicole)/NatGen"
DRIVE_UPLOAD_UNAVAILABLE = (
    "Drive upload of the NatGen QA pack is not available; "
    "refusing to report the pack as uploaded"
)
# Live report shows "2035471506 00" (policy and term suffix).
_POLICY_NUMBER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{4,19}(?: [A-Za-z0-9]{1,4})?$")
_REMOTE_PDF = re.compile(r"https?://[^\s\"'<>]+?\.pdf(?:\?[^\s\"'<>]*)?", re.IGNORECASE)
_EASTERN_NAME = "America/New_York"
_NOC_LABELS = frozenset({"pending cancellation", "noc"})
_NON_PAYMENT = frozenset({"non-payment", "non payment", "nonpayment", "non pay"})

_HEADER_FIELDS = (
    ("policy_number", frozenset({"policy number", "policy"})),
    ("insured_name", frozenset({"insured", "insured name", "named insured"})),
    ("reason", frozenset({"reason", "cancel reason", "cancellation reason"})),
    ("notice_type", frozenset({"type", "notice", "transaction"})),
    ("processed_date", frozenset({"process date", "processed date"})),
    ("cancel_effective", frozenset({
        "cancel effective date",
        "cancellation effective date",
        "cancel effective",
        "cancellation date",
        "cancel date",
    })),
)
# Live Pending Cancellations report (AgencyActivityReports.aspx?r=5).
PENDING_TABLE_CSS = "#ctl00_MainContent_gvPendingCancellations"
# Live Policy Summary history grid. Its header is the first row, not a thead.
HISTORY_TABLE_CSS = "#ctl00_MainContent_PolicyHistoryControl2_dgPolicyHistory"
# The live report has no process-date column. It is read as a snapshot: a row
# first seen today takes the pull's end date, and a row already in the ledger
# keeps its first-seen date. The manifest says which.
SNAPSHOT_SOURCE = "report snapshot (no process date on the NatGen report)"
LIST_SOURCE = "NatGen list"
_HISTORY_FIELDS = (
    ("on", frozenset({"date", "transaction date", "processed date", "date processed"})),
    ("label", frozenset({"type", "transaction", "description", "activity"})),
)
_NAV_STEPS = (
    ("Agent Dashboard", ("link", "button")),
    ("Your Notifications", ("link", "button", "tab")),
    ("Policy To Dos", ("link", "button", "tab")),
    ("Pending Cancellations", ("link", "button", "tab")),
)
_CANCEL_LABEL = re.compile(
    r"(?:"
    r"(?:cancellation|cancel)\s+effective(?:\s+date)?"
    r"|effective\s+date\s+of\s+cancellation"
    r"|noc\s+effective(?:\s+date)?"
    r")"
    r"\s*[:\-]?\s*"
    r"(\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
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


@dataclass(frozen=True)
class NocGrid:
    list_url: str
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    policy_controls: tuple[int, ...]
    more_pages: bool | None


@dataclass(frozen=True)
class NocRow:
    document_id: str
    policy_number: str
    insured_name: str
    reason: str
    processed_on: date
    cancel_effective: date
    filename: str
    row_index: int
    source_url: str
    processed_date_source: str = LIST_SOURCE


def snapshot_document_id(policy_number: str, reason: str, cancel_effective: date) -> str:
    policy = require_policy_number(policy_number)
    return f"natgen-noc:{policy}:snapshot:{normalize_reason(reason)}:{cancel_effective.isoformat()}"


@dataclass(frozen=True)
class HistoryEntry:
    label: str
    on: date
    controls: int
    row_index: int


@dataclass(frozen=True)
class PagePdfView:
    url: str
    pdfs: tuple[bytes, ...]


@dataclass(frozen=True)
class NocOpenObservation:
    downloads: tuple[bytes, ...]
    pages: tuple[PagePdfView, ...]


def require_policy_number(value: str) -> str:
    policy = str(value or "").strip()
    if not _POLICY_NUMBER.fullmatch(policy):
        raise IntakeHold("NOC policy number is missing or ambiguous")
    return policy


def normalize_reason(reason: str) -> str:
    original = _norm(reason)
    if any(char in original for char in '\\/:*?"<>|') or ".." in original:
        raise IntakeHold("NOC reason is missing or ambiguous")
    cleaned = original.rstrip(".").strip()
    if not cleaned or cleaned in {".", ".."} or len(cleaned) > 80:
        raise IntakeHold("NOC reason is missing or ambiguous")
    folded = re.sub(r"\s+", " ", cleaned.casefold().replace("_", " "))
    if folded in _NON_PAYMENT:
        return "non-payment"
    # Live reasons read "Pending Cancel for Non Payment" / "Pending cancel for NSF".
    if re.search(r"\bnon[- ]?payment\b", folded):
        return "non-payment"
    if re.search(r"\bnsf\b", folded):
        return "nsf"
    return folded


def noc_filename(policy_number: str, reason: str) -> str:
    return f"{require_policy_number(policy_number)} NatGen NOC {normalize_reason(reason)}.pdf"


def held_filename(policy_number: str, reason: str) -> str:
    official = noc_filename(policy_number, reason)
    return official[:-4] + " HELD.pdf"


def noc_document_id(policy_number: str, processed: date, reason: str, cancel_effective: date) -> str:
    policy = require_policy_number(policy_number)
    return (
        f"natgen-noc:{policy}:{processed.isoformat()}:"
        f"{normalize_reason(reason)}:{cancel_effective.isoformat()}"
    )


def parse_carrier_date(value: str) -> date:
    raw = _norm(value)
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            return date.fromisoformat(raw)
        if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", raw):
            return datetime.strptime(raw, "%m/%d/%Y").date()
    except ValueError as exc:
        raise IntakeHold("NOC date is missing or ambiguous") from exc
    raise IntakeHold("NOC date is missing or ambiguous")


def header_indexes(headers: tuple[str, ...], fields=_HEADER_FIELDS, required: tuple[str, ...] = ()) -> dict[str, int]:
    normalized = tuple(_norm(header).casefold() for header in headers)
    if not normalized or any(not header for header in normalized) or len(normalized) != len(set(normalized)):
        raise IntakeHold("Pending Cancellations table is missing or ambiguous")
    indexes: dict[str, int] = {}
    for index, header in enumerate(normalized):
        matched = [name for name, aliases in fields if header in aliases]
        if len(matched) > 1:
            raise IntakeHold("Pending Cancellations table is missing or ambiguous")
        if not matched:
            continue
        if matched[0] in indexes:
            raise IntakeHold("Pending Cancellations table is missing or ambiguous")
        indexes[matched[0]] = index
    for name in required:
        if name not in indexes:
            raise IntakeHold("Pending Cancellations table is missing or ambiguous")
    return indexes


def classify_noc_row(notice_type: str | None, controls: int) -> str:
    """Return take, skip, or ambiguous. A pending-cancellation row needs one policy control."""
    if controls < 0:
        return "ambiguous"
    if notice_type is None:
        return "take" if controls == 1 else "ambiguous"
    label = _norm(notice_type).casefold()
    if not label:
        return "ambiguous"
    if label in _NOC_LABELS:
        return "take" if controls == 1 else "ambiguous"
    return "skip" if controls == 0 else "ambiguous"


def _eastern_today() -> date:
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(_EASTERN_NAME)).date()


def parse_noc_grid(
    grid: NocGrid,
    *,
    snapshot_on: date | None = None,
    first_seen: Callable[[str], date | None] | None = None,
) -> tuple[NocRow, ...]:
    """Rows to pull. Without a process-date column the report is a snapshot:
    each row's date is its ledger first-seen date, else ``snapshot_on``
    (the pull's end date; today in Eastern when not given)."""
    list_url = require_list_url(grid.list_url)
    if len(grid.rows) != len(grid.policy_controls):
        raise IntakeHold("Pending Cancellations list is ambiguous")
    indexes = header_indexes(
        grid.headers,
        required=("policy_number", "insured_name", "reason", "cancel_effective"),
    )
    snapshot = "processed_date" not in indexes
    found: list[NocRow] = []
    for index, (cells, controls) in enumerate(zip(grid.rows, grid.policy_controls)):
        if len(cells) != len(grid.headers):
            raise IntakeHold("Pending Cancellations list is ambiguous")
        if not any(_norm(cell) for cell in cells):
            continue
        notice = cells[indexes["notice_type"]] if "notice_type" in indexes else None
        decision = classify_noc_row(notice, controls)
        if decision == "skip":
            continue
        if decision != "take":
            raise IntakeHold("Pending Cancellations list is ambiguous")
        policy = require_policy_number(_norm(cells[indexes["policy_number"]]))
        insured = _norm(cells[indexes["insured_name"]])
        if not insured:
            raise IntakeHold("NOC insured name is missing or ambiguous")
        reason = normalize_reason(cells[indexes["reason"]])
        cancel_effective = parse_carrier_date(cells[indexes["cancel_effective"]])
        if snapshot:
            document_id = snapshot_document_id(policy, reason, cancel_effective)
            seen = first_seen(document_id) if first_seen is not None else None
            processed = seen or snapshot_on or _eastern_today()
            source = SNAPSHOT_SOURCE
        else:
            processed = parse_carrier_date(cells[indexes["processed_date"]])
            document_id = noc_document_id(policy, processed, reason, cancel_effective)
            source = LIST_SOURCE
        found.append(NocRow(
            document_id=document_id,
            policy_number=policy,
            insured_name=insured,
            reason=reason,
            processed_on=processed,
            cancel_effective=cancel_effective,
            filename=noc_filename(policy, reason),
            row_index=index,
            source_url=list_url,
            processed_date_source=source,
        ))
    if len({row.document_id for row in found}) != len(found):
        raise IntakeHold("Pending Cancellations list is ambiguous")
    return tuple(found)


def choose_most_recent_noc(entries: tuple[HistoryEntry, ...] | list[HistoryEntry]) -> HistoryEntry:
    """Pick the single latest Pending Cancellation or NOC history row."""
    chosen = [
        entry for entry in entries
        if _norm(entry.label).casefold() in _NOC_LABELS
        or _norm(entry.label).casefold().startswith("pending cancel")
    ]
    if not chosen:
        raise IntakeHold("Pending Cancellation NOC in Policy History is missing or ambiguous")
    latest = max(entry.on for entry in chosen)
    top = [entry for entry in chosen if entry.on == latest]
    if len(top) != 1 or top[0].controls != 1:
        raise IntakeHold("Pending Cancellation NOC in Policy History is missing or ambiguous")
    return top[0]


def cancel_effective_date_in_pdf(content: bytes) -> date:
    """Read the labeled cancel effective date. Unlabeled or disagreeing dates hold."""
    if not _is_pdf(content):
        raise IntakeHold("NOC download is not a PDF")
    found: set[date] = set()
    for match in _CANCEL_LABEL.finditer(pdf_text_for_dates(content)):
        found.add(parse_carrier_date(match.group(1)))
    if len(found) != 1:
        raise IntakeHold("NOC cancel effective date in the PDF is missing or ambiguous")
    return next(iter(found))


def pdf_text_for_dates(content: bytes) -> str:
    parts: list[str] = []
    raw = content.decode("latin-1", errors="ignore")
    if _CANCEL_LABEL.search(raw):
        parts.append(raw)
    parts.extend(_inflate_streams(content))
    extracted = _pypdf_text(content)
    if extracted and _CANCEL_LABEL.search(extracted):
        parts.append(extracted)
    return "\n".join(parts)


def pdf_bytes_from_observation(observation: NocOpenObservation) -> bytes:
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

    HTML is not printed to PDF. Remote fetches stay on NatGen hosts.
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


def collect_noc_observation(
    page: Any,
    open_noc: Callable[[], None],
    *,
    read_page: Callable[[Any], PagePdfView] = read_playwright_pdf_view,
    timeout_ms: int = DOWNLOAD_TIMEOUT_MS,
) -> NocOpenObservation:
    """Open one NOC and keep a unique PDF from the download or a new tab."""
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
            open_noc()
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
        return NocOpenObservation(downloads=tuple(downloads), pages=tuple(views))
    finally:
        remover = getattr(context, "remove_listener", None)
        if callable(remover):
            try:
                remover("page", on_page)
            except Exception:
                pass
        for item in opened:
            _close_natgen_page(item)


def _close_natgen_page(page: Any) -> None:
    """Close a tab this pull opened on NatGen. Never another site's tab."""
    host = (urllib.parse.urlsplit(str(getattr(page, "url", "") or "")).hostname or "").casefold()
    if host and not (
        host == "natgenagency.com" or host.endswith(".natgenagency.com")
        or host == "nationalgeneral.com" or host.endswith(".nationalgeneral.com")
    ):
        return
    closer = getattr(page, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:
            pass


def assert_authenticated(page: Any) -> None:
    url = str(getattr(page, "url", "") or "")
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/").lower()
    if "login" in host or path.endswith("/login"):
        raise IntakeHold("NatGen session is not authenticated")
    if page.locator("input[type='password']").count() != 0:
        raise IntakeHold("NatGen session is not authenticated")


def click_named(page: Any, name: str, *, roles: tuple[str, ...]) -> None:
    matches = []
    for role in roles:
        locator = page.get_by_role(role, name=name, exact=True)
        count = locator.count()
        if count:
            matches.append((count, locator))
    if len(matches) != 1 or matches[0][0] != 1:
        raise IntakeHold(f"NatGen control {name!r} is missing or ambiguous")
    matches[0][1].click()


_PENDING_REPORT_URL = re.compile(r"pending[-_\s]?cancellat", re.IGNORECASE)
_AGENCY_ACTIVITY_URL = re.compile(r"agency[-_\s]?activity", re.IGNORECASE)
_AGENCY_ACTIVITY_REPORTS = re.compile(r"agencyactivityreports\.aspx", re.IGNORECASE)
_PENDING_HEADING = re.compile(r"pending cancellations", re.IGNORECASE)
# 2026-09-28 hermes-test prove: Pending Cancellations is this report id.
_PENDING_ACTIVITY_REPORT_ID = "5"


def _natgen_host(url: str) -> bool:
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return False
    return host == "natgenagency.com" or host.endswith(".natgenagency.com")


def _url_blob(url: str) -> str:
    parsed = urllib.parse.urlsplit(str(url or ""))
    raw = f"{parsed.path}?{parsed.query}"
    return urllib.parse.unquote(raw).replace("+", " ")


def _role_count(page: Any, role: str, name: Any, *, exact: bool) -> int:
    try:
        return int(page.get_by_role(role, name=name, exact=exact).count())
    except Exception:
        return -1


def _named_state(page: Any, name: str, roles: tuple[str, ...]) -> str:
    matches = []
    for role in roles:
        count = _role_count(page, role, name, exact=True)
        if count:
            matches.append(count)
    if any(count < 0 for count in matches):
        return "error"
    if not matches:
        return "missing"
    if len(matches) != 1 or matches[0] != 1:
        return "ambiguous"
    return "one"


def _heading_state(page: Any) -> str:
    count = _role_count(page, "heading", _PENDING_HEADING, exact=False)
    if count < 0:
        return "error"
    if count == 0:
        return "none"
    if count == 1:
        return "one"
    return "many"


def _single_report_table(page: Any, *, require_rows: bool) -> bool:
    try:
        tables = page.locator("table")
        if int(tables.count()) != 1:
            return False
        if not require_rows:
            return True
        return int(tables.locator("tbody tr").count()) >= 1
    except Exception:
        return False


def _safe_title(page: Any) -> str:
    title_fn = getattr(page, "title", None)
    if not callable(title_fn):
        return ""
    try:
        return str(title_fn() or "")
    except Exception:
        return ""


def _safe_body(page: Any) -> str:
    try:
        return str(page.locator("body").inner_text() or "")
    except Exception:
        return ""


def _mentions_pending(page: Any) -> bool:
    """Heading, document title, or visible text. A nav link alone is not enough."""
    if _heading_state(page) == "one":
        return True
    if _PENDING_HEADING.search(_safe_title(page)):
        return True
    return _PENDING_HEADING.search(_safe_body(page)) is not None


def _activity_report_ids(url: str) -> list[str]:
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    if not _AGENCY_ACTIVITY_REPORTS.search(parsed.path or ""):
        return []
    found: list[str] = []
    for name, values in urllib.parse.parse_qs(parsed.query).items():
        if name.casefold() != "r":
            continue
        found.extend(str(value) for value in values)
    return found


def _is_pending_activity_report_url(url: str) -> bool:
    """AgencyActivityReports.aspx?r=5 is the Pending Cancellations list.

    Other ``r`` values are different Agency Activity reports and are not this list.
    """
    if not _natgen_host(url):
        return False
    return _PENDING_ACTIVITY_REPORT_ID in _activity_report_ids(url)


def _already_on_pending_report(page: Any) -> bool:
    """True when this tab is already the Pending Cancellations report.

    ``/Reports/AgencyActivityReports.aspx?r=5`` is that list even when the
    page has no Agent Dashboard control and does not use the words "Pending
    Cancellations" as a heading. A pending-cancellations URL is also enough.
    An Agency Activity URL qualifies when the page names Pending Cancellations
    and has one table. Any other NatGen page qualifies when it names that
    report and has one table with rows, and Agent Dashboard is not on the
    page. A dashboard that still shows Agent Dashboard keeps the playbook clicks.
    """
    url = str(getattr(page, "url", "") or "")
    if not _natgen_host(url):
        return False
    if _is_pending_activity_report_url(url):
        return True
    blob = _url_blob(url)
    if _PENDING_REPORT_URL.search(blob):
        return True
    mentioned = _mentions_pending(page)
    if not mentioned:
        return False
    if _AGENCY_ACTIVITY_URL.search(blob) and _single_report_table(page, require_rows=False):
        return True
    if not _single_report_table(page, require_rows=True):
        return False
    if _heading_state(page) == "one":
        return True
    return _named_state(page, "Agent Dashboard", ("link", "button")) == "missing"


def open_pending_cancellations(page: Any) -> None:
    """Open the Pending Cancellations list. There is no process-date filter to fill.

    Already sitting on that report skips Agent Dashboard. A missing Agent
    Dashboard is skipped. Ambiguous controls still hold. If the report is
    never reached, the pull holds before scrape.
    """
    assert_authenticated(page)
    if _already_on_pending_report(page):
        return
    clicked_pending = False
    for name, roles in _NAV_STEPS:
        if _already_on_pending_report(page):
            return
        state = _named_state(page, name, roles)
        if state in {"ambiguous", "error"}:
            raise IntakeHold(f"NatGen control {name!r} is missing or ambiguous")
        if state != "one":
            continue
        click_named(page, name, roles=roles)
        if name == "Pending Cancellations":
            clicked_pending = True
    if clicked_pending or _already_on_pending_report(page):
        assert_authenticated(page)
        return
    raise IntakeHold("Pending Cancellations report was not found")


def click_forms_view(page: Any) -> None:
    found = []
    for name in ("Forms View", "View PDF"):
        for role in ("link", "button"):
            locator = page.get_by_role(role, name=name, exact=True)
            count = locator.count()
            if count:
                found.append((count, locator))
    if len(found) != 1 or found[0][0] != 1:
        raise IntakeHold("Forms View PDF control is missing or ambiguous")
    found[0][1].click()


def raise_if_natgen_error_page(page: Any, policy_number: str) -> None:
    """NatGen sends some policy links to ErrorPage.aspx (live 2026-10-08:
    "Renewal policy exists: 2031936859-01"). Hold that policy with NatGen's
    own one-line reason instead of a generic missing-control hold."""
    url = str(getattr(page, "url", "") or "")
    if "/errorpage.aspx" not in urllib.parse.urlsplit(url).path.casefold():
        return
    detail = ""
    try:
        text = str(page.locator("body").inner_text())
    except Exception:  # noqa: BLE001
        text = ""
    for line in text.splitlines():
        line = _norm(line)
        if line and line.casefold() not in {"error page"}:
            detail = line[:120]
            break
    raise IntakeHold(
        f"NatGen showed an error page for {policy_number}"
        + (f": {detail}" if detail else "")
    )


def click_history_noc(row: Any, label: str) -> None:
    matches = []
    for role in ("link", "button"):
        locator = row.get_by_role(role, name=label, exact=True)
        count = locator.count()
        if count:
            matches.append((count, locator))
    if len(matches) != 1 or matches[0][0] != 1:
        raise IntakeHold("Pending Cancellation NOC in Policy History is missing or ambiguous")
    matches[0][1].click()


class PlaywrightNatGenNocBrowser:
    """Drive one already-authenticated NatGen tab. Does not type credentials."""

    def __init__(self, page: Any):
        self.page = page
        self._grid: NocGrid | None = None
        self._row_locators: tuple[Any, ...] = ()
        self._list_url = ""

    def load_pending_cancellations(self) -> NocGrid:
        open_pending_cancellations(self.page)
        headers, raw_rows, locators = extract_table(self.page)
        indexes = header_indexes(
            headers,
            required=("policy_number", "insured_name", "reason", "cancel_effective"),
        )
        controls = []
        for cells, locator in zip(raw_rows, locators):
            if len(cells) != len(headers):
                controls.append(-1)
                continue
            policy = _norm(cells[indexes["policy_number"]])
            controls.append(policy_control_count(locator, policy) if policy else 0)
        grid = NocGrid(
            list_url=str(getattr(self.page, "url", "") or ""),
            headers=headers,
            rows=raw_rows,
            policy_controls=tuple(controls),
            more_pages=more_pages(self.page),
        )
        parse_noc_grid(grid)
        self._grid = grid
        self._row_locators = locators
        self._list_url = grid.list_url
        return grid

    def capture_noc(self, document_id: str) -> NocOpenObservation:
        if self._grid is None or str(getattr(self.page, "url", "") or "") != self._list_url:
            raise IntakeHold("Pending Cancellations list is missing or ambiguous")
        matches = [row for row in parse_noc_grid(self._grid) if row.document_id == document_id]
        if len(matches) != 1 or matches[0].row_index >= len(self._row_locators):
            raise IntakeHold("Selected carrier document is missing or ambiguous")
        target = self._row_locators[matches[0].row_index]
        policy_number = matches[0].policy_number

        def open_noc() -> None:
            click_policy_control(target, policy_number)
            if self._live_history_grid():
                self._open_live_history_noc()
                return
            raise_if_natgen_error_page(self.page, policy_number)
            click_named(self.page, "Policy History", roles=("link", "button", "tab"))
            self._open_most_recent_history_noc()
            click_forms_view(self.page)

        try:
            observation = collect_noc_observation(self.page, open_noc)
        except Exception:
            # Never strand the tab on a policy or error page: the next row
            # (or the next run) needs the list back.
            try:
                self._restore_list()
            except Exception:  # noqa: BLE001 - the original hold is the reason
                pass
            raise
        self._restore_list()
        return observation

    def screenshot_pending_cancellations(self) -> bytes:
        """Full-page PNG of the Pending Cancellations list before any policy is opened."""
        if self._grid is None or str(getattr(self.page, "url", "") or "") != self._list_url:
            raise IntakeHold("Pending Cancellations list screenshot is missing or not a PNG")
        if self.page.locator(PENDING_TABLE_CSS).count() != 1 and self.page.locator("table").count() != 1:
            raise IntakeHold("Pending Cancellations list screenshot is missing or not a PNG")
        from .carrier_page_capture import capture_png

        data = capture_png(self.page, full_page=True)
        return require_png(data)

    def _live_history_grid(self) -> bool:
        wait = getattr(self.page, "wait_for_selector", None)
        if callable(wait):
            try:
                wait(HISTORY_TABLE_CSS, timeout=POLICY_SUMMARY_WAIT_MS)
            except Exception:
                return False
        try:
            return int(self.page.locator(HISTORY_TABLE_CSS).count()) == 1
        except Exception:
            return False

    def _open_live_history_noc(self) -> None:
        """Policy Summary history: latest "Pending Cancel..." row, its PDF
        control, then View. View opens DisplayPDF.aspx in a new tab."""
        headers, raw_rows, locators = extract_history_table(self.page)
        indexes = header_indexes(headers, fields=_HISTORY_FIELDS, required=("on", "label"))
        entries: list[HistoryEntry] = []
        for index, cells in enumerate(raw_rows):
            if len(cells) != len(headers) or not any(_norm(cell) for cell in cells):
                continue
            label = _norm(cells[indexes["label"]])
            if not label.casefold().startswith("pending cancel") and label.casefold() not in _NOC_LABELS:
                continue
            entries.append(HistoryEntry(
                label=label,
                on=parse_carrier_date(cells[indexes["on"]]),
                controls=int(locators[index].locator(HISTORY_PDF_TRIGGER).count()),
                row_index=index,
            ))
        chosen = choose_most_recent_noc(entries)
        locators[chosen.row_index].locator(HISTORY_PDF_TRIGGER).click()
        view = self.page.locator(HISTORY_VIEW_PDF)
        wait = getattr(view, "wait_for", None)
        if callable(wait):
            try:
                view.first.wait_for(state="visible", timeout=POLICY_SUMMARY_WAIT_MS)
            except Exception:
                pass
        visible = [item for item in _each(view) if _visible(item)]
        if len(visible) != 1:
            raise IntakeHold("Pending Cancellation NOC in Policy History is missing or ambiguous")
        visible[0].click()

    def _open_most_recent_history_noc(self) -> None:
        headers, raw_rows, locators = extract_table(self.page)
        indexes = header_indexes(headers, fields=_HISTORY_FIELDS, required=("on", "label"))
        entries: list[HistoryEntry] = []
        for index, cells in enumerate(raw_rows):
            if len(cells) != len(headers) or not any(_norm(cell) for cell in cells):
                continue
            label = _norm(cells[indexes["label"]])
            entries.append(HistoryEntry(
                label=label,
                on=parse_carrier_date(cells[indexes["on"]]),
                controls=history_control_count(locators[index], label),
                row_index=index,
            ))
        chosen = choose_most_recent_noc(entries)
        click_history_noc(locators[chosen.row_index], chosen.label)

    def _restore_list(self) -> None:
        # Live: going back to the report leaves a page whose policy links no
        # longer open anything, so the report is loaded again at its own
        # address and must show the same rows as before.
        goto = getattr(self.page, "goto", None)
        if callable(goto) and self._grid is not None and _is_pending_activity_report_url(self._list_url):
            try:
                goto(self._list_url, wait_until="domcontentloaded", timeout=POLICY_SUMMARY_WAIT_MS)
                self.page.wait_for_selector(PENDING_TABLE_CSS, timeout=POLICY_SUMMARY_WAIT_MS)
            except Exception as exc:
                raise IntakeHold("NOC PDF capture left the Pending Cancellations list") from exc
            headers, rows, locators = extract_table(self.page)
            if (headers, rows) != (self._grid.headers, self._grid.rows):
                raise IntakeHold("Pending Cancellations list changed while NOCs were being pulled")
            self._row_locators = locators
            return
        for _ in range(4):
            if str(getattr(self.page, "url", "") or "") == self._list_url:
                return
            go_back = getattr(self.page, "go_back", None)
            if not callable(go_back):
                break
            try:
                # The live report keeps a connection open, so "load" never
                # fires on the way back; the DOM is enough.
                try:
                    go_back(wait_until="domcontentloaded", timeout=POLICY_SUMMARY_WAIT_MS)
                except TypeError:
                    go_back()
            except Exception as exc:
                if str(getattr(self.page, "url", "") or "") != self._list_url:
                    raise IntakeHold("NOC PDF capture left the Pending Cancellations list") from exc
        if str(getattr(self.page, "url", "") or "") != self._list_url:
            raise IntakeHold("NOC PDF capture left the Pending Cancellations list")


class LocalDeliveryLedger:
    """Private named-PDF ledger. A conflicting file is kept and the pull holds."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def ensure_private(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink() or not self.root.is_dir() or self.root.stat().st_mode & 0o077:
            raise IntakeHold("NOC output directory must be private (0700)")

    def delivery_status(self, *, document_id: str, filename: str, processed_on: date) -> bool:
        self.ensure_private()
        path = self.pdf_path(processed_on, filename)
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
            or entry.get("processed_date") != processed_on.isoformat()
            or entry.get("sha256") != hashlib.sha256(path.read_bytes()).hexdigest()
        ):
            raise IntakeHold("Existing NOC file conflicts with the pull ledger")
        return True

    def record(self, source: SourceItem, *, processed_on: date) -> Path:
        self.ensure_private()
        if source.filename == LEDGER_NAME or source.filename.endswith(" HELD.pdf"):
            raise IntakeHold("NOC filename is missing or ambiguous")
        path = self.pdf_path(processed_on, source.filename)
        if path.exists():
            raise IntakeHold("Existing NOC file conflicts with the pull ledger")
        digest = hashlib.sha256(source.content).hexdigest()
        self._write_new(path, source.content)
        data = self._load()
        if source.source_id in data["items"]:
            raise IntakeHold("Existing NOC file conflicts with the pull ledger")
        data["items"][source.source_id] = {
            "filename": source.filename,
            "sha256": digest,
            "bytes": len(source.content),
            "processed_date": processed_on.isoformat(),
        }
        self._write(data)
        return path

    def save_held_copy(self, *, processed_on: date, filename: str, content: bytes) -> Path:
        """Keep the wrong document for QA without filing it as the official NOC."""
        path = self.pdf_path(processed_on, filename)
        if path.exists():
            if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
                raise IntakeHold("Existing NOC file conflicts with the pull ledger")
            return path
        self._write_new(path, content)
        return path

    def first_seen(self, document_id: str) -> date | None:
        """Processed date already recorded for this document, if any."""
        try:
            entry = self._load()["items"].get(document_id)
        except Exception:
            return None
        if not isinstance(entry, dict):
            return None
        try:
            return date.fromisoformat(str(entry.get("processed_date") or ""))
        except ValueError:
            return None

    def pdf_ids_for_date(self, day: date) -> set[str]:
        found: set[str] = set()
        for document_id, entry in self._load()["items"].items():
            if not isinstance(entry, dict) or entry.get("processed_date") != day.isoformat():
                continue
            filename = str(entry.get("filename") or "")
            processed = str(entry.get("processed_date") or "")
            if self.delivery_status(
                document_id=str(document_id),
                filename=filename,
                processed_on=date.fromisoformat(processed),
            ):
                found.add(str(document_id))
        return found

    def pdf_path(self, day: date, filename: str) -> Path:
        return self.date_dir(day) / self._basename(filename)

    def date_dir(self, day: date) -> Path:
        self.ensure_private()
        folder = self.root / day.isoformat()
        if folder.is_symlink():
            raise IntakeHold("NOC output directory must be private (0700)")
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not folder.is_dir() or folder.stat().st_mode & 0o077:
            raise IntakeHold("NOC output directory must be private (0700)")
        return folder

    def save_screenshot(self, start: date, end: date, png: bytes) -> dict[str, str]:
        """Copy the full-list PNG into each process-date folder. Never replace a different shot."""
        blob = require_png(png)
        saved: dict[str, str] = {}
        for day in _each_day(start, end):
            name = pending_cancellations_screenshot_name(day)
            saved[day.isoformat()] = str(self._save_png_named(self.date_dir(day), name, blob))
        return saved

    def _save_png_named(self, folder: Path, name: str, blob: bytes) -> Path:
        primary = folder / self._basename(name)
        if primary.exists():
            if primary.is_symlink() or not primary.is_file():
                raise IntakeHold("Pending Cancellations list screenshot is missing or not a PNG")
            if primary.read_bytes() == blob:
                return primary
            return self._write_new(self._sibling_screenshot(folder, name), blob)
        return self._write_new(primary, blob)

    def _sibling_screenshot(self, folder: Path, name: str) -> Path:
        stem = self._basename(name)[:-4]
        for index in range(2, 100):
            candidate = folder / f"{stem}-{index}.png"
            if not candidate.exists() and not candidate.is_symlink():
                return candidate
        raise IntakeHold("Pending Cancellations list screenshot is missing or not a PNG")

    def _write_new(self, path: Path, blob: bytes) -> Path:
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
        if not filename or filename != Path(filename).name or filename in {".", ".."}:
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
        self._write_new(temporary, payload)
        try:
            os.replace(temporary, path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        os.chmod(path, 0o600)


class NatGenPendingCancellationPortal:
    """NatGenPort for Pending Cancellations NOC. Additional Info does not navigate."""

    def __init__(self, browser: Any, ledger: LocalDeliveryLedger):
        self.browser = browser
        self.ledger = ledger
        self._cache_key: tuple[Any, ...] | None = None
        self._cache_result: Any = None
        self._rows: dict[str, NocRow] = {}
        self._list_png: bytes | None = None
        self.skipped_document_ids: tuple[str, ...] = ()
        self.scrubbed: tuple[NocRow, ...] = ()
        self.date_holds: list[dict[str, Any]] = []
        self.requested_window: tuple[date, date] | None = None
        self.verification: dict[str, Any] | None = None

    def note_requested_window(self, start: date, end: date) -> None:
        self.requested_window = (start, end)

    def list_documents(self, *, scope: str, start: date, end: date):
        from .intake_core import ReadResult

        if scope == ADDITIONAL_INFO_SCOPE:
            raise IntakeHold(ADDITIONAL_INFO_TODO)
        if scope != NOC_SCOPE:
            raise IntakeHold("This portal lists NatGen Pending Cancellations only")
        key = (scope, start, end)
        if self._cache_key == key and self._cache_result is not None:
            return self._cache_result
        grid = self.browser.load_pending_cancellations()
        parsed = parse_noc_grid(grid, snapshot_on=end, first_seen=self.ledger.first_seen)
        if grid.more_pages is not False:
            raise IntakeHold("Pending Cancellations list is incomplete or ambiguous")
        in_window: list[NocRow] = []
        scrubbed: list[NocRow] = []
        for row in parsed:
            if start <= row.processed_on <= end:
                in_window.append(row)
            else:
                scrubbed.append(row)
        skipped: list[str] = []
        rows: list[dict[str, Any]] = []
        found: dict[str, NocRow] = {}
        for row in in_window:
            delivered = self.ledger.delivery_status(
                document_id=row.document_id,
                filename=row.filename,
                processed_on=row.processed_on,
            )
            if delivered:
                skipped.append(row.document_id)
            rows.append(_row_payload(row, delivered=delivered))
            found[row.document_id] = row
        self._list_png = require_png(self.browser.screenshot_pending_cancellations())
        result = ReadResult(tuple(rows), authoritative=True, complete=True)
        self._cache_key = key
        self._cache_result = result
        self._rows = found
        self.skipped_document_ids = tuple(skipped)
        self.scrubbed = tuple(scrubbed)
        return result

    def download_document(self, document_id: str) -> SourceItem:
        row = self._rows.get(document_id)
        if row is None:
            raise IntakeHold("Selected carrier document is missing or ambiguous")
        try:
            content = pdf_bytes_from_observation(self.browser.capture_noc(document_id))
        except NocDateHold:
            raise
        except IntakeHold as exc:
            # One policy that will not open holds alone; the other NOCs are
            # still pulled and the run stays HELD with this reason.
            reason = str(exc) if row.policy_number in str(exc) else f"{exc} for {row.policy_number}"
            self.date_holds.append({
                "policy_number": row.policy_number,
                "processed_date": row.processed_on.isoformat(),
                "listed": row.cancel_effective.isoformat(),
                "observed": None,
                "reason": reason,
                "held_filename": None,
                "document_id": row.document_id,
            })
            raise NocDateHold(reason) from exc
        self._require_listed_cancel_date(row, content)
        return SourceItem(
            system="natgen",
            source_account=NATGEN_SOURCE_ACCOUNT,
            source_id=document_id,
            source_url=f"{row.source_url}#noc={document_id}",
            received_at=_received_at(row.processed_on),
            filename=row.filename,
            content=content,
        )

    def publish_source(self, source: SourceItem) -> str:
        row = self.noc(source.source_id)
        path = self.ledger.record(source, processed_on=row.processed_on)
        self._mark_delivered(source.source_id)
        return str(path)

    def finish_natgen_pull(self, start: date, end: date) -> dict[str, Any]:
        """Write one QA pack per process date. A bad date or count stays HELD."""
        if self._list_png is None or self._cache_result is None:
            raise IntakeHold("Pending Cancellations list screenshot is missing or not a PNG")
        date_note = " ".join(item["reason"] for item in self.date_holds) or None
        try:
            by_date = require_noc_pdf_parity(
                rows=self._cache_result.rows, ledger=self.ledger, start=start, end=end,
            )
            status = "PULLED"
            held = None
        except IntakeHold as exc:
            held = str(exc)
            try:
                by_date = observed_noc_pdf_counts(
                    rows=self._cache_result.rows, ledger=self.ledger, start=start, end=end,
                )
            except IntakeHold:
                raise exc from None
            status = "HELD"
        if date_note:
            status = "HELD"
            held = date_note if not held else f"{date_note} {held}"
        screenshots = self.ledger.save_screenshot(start, end, self._list_png)
        requested = self.requested_window or (start, end)
        packs = write_natgen_qa_packs(
            ledger=self.ledger,
            start=start,
            end=end,
            requested=requested,
            status=status,
            held=held,
            by_date=by_date,
            screenshots=screenshots,
            rows=self._cache_result.rows,
            skipped_document_ids=self.skipped_document_ids,
            scrubbed=self.scrubbed,
            date_holds=self.date_holds,
            screenshot_covers_window=start != end,
        )
        self.verification = {
            "gate": "noc_rows_equal_pdfs_and_cancel_dates_match",
            "by_date": [dict(row) for row in by_date],
            "screenshot": screenshots.get(start.isoformat()),
            "screenshots": screenshots,
            "packs": packs,
            "date_holds": list(self.date_holds),
        }
        if held:
            raise IntakeHold(held)
        return self.verification

    def noc(self, document_id: str) -> NocRow:
        try:
            return self._rows[document_id]
        except KeyError as exc:
            raise IntakeHold("Selected carrier document is missing or ambiguous") from exc

    def _require_listed_cancel_date(self, row: NocRow, content: bytes) -> None:
        try:
            observed = cancel_effective_date_in_pdf(content)
        except IntakeHold as exc:
            reason = f"{exc} for {row.policy_number}"
            self.date_holds.append({
                "policy_number": row.policy_number,
                "processed_date": row.processed_on.isoformat(),
                "listed": row.cancel_effective.isoformat(),
                "observed": None,
                "reason": reason,
                "held_filename": None,
                "document_id": row.document_id,
            })
            raise NocDateHold(reason) from exc
        if observed == row.cancel_effective:
            return
        name = held_filename(row.policy_number, row.reason)
        self.ledger.save_held_copy(processed_on=row.processed_on, filename=name, content=content)
        reason = (
            f"NOC cancel effective date does not match the list for {row.policy_number}: "
            f"list {row.cancel_effective.isoformat()} PDF {observed.isoformat()}"
        )
        self.date_holds.append({
            "policy_number": row.policy_number,
            "processed_date": row.processed_on.isoformat(),
            "listed": row.cancel_effective.isoformat(),
            "observed": observed.isoformat(),
            "reason": reason,
            "held_filename": name,
            "document_id": row.document_id,
        })
        raise NocDateHold(reason)

    def _mark_delivered(self, document_id: str) -> None:
        if self._cache_result is None:
            return
        rows = []
        for row in self._cache_result.rows:
            if row.get("document_id") == document_id:
                rows.append({**dict(row), "already_delivered": True})
            else:
                rows.append(dict(row))
        from .intake_core import ReadResult
        self._cache_result = ReadResult(tuple(rows), authoritative=True, complete=True)


def connect_cdp_browser(cdp_url: str | None) -> tuple[PlaywrightNatGenNocBrowser, Callable[[], None]]:
    """Attach to the local Chrome. Exactly one NatGen application tab."""
    from .document_retrieval_filing import require_carrier_pull

    require_carrier_pull("natgen")
    url = require_loopback_cdp(cdp_url or os.environ.get("ROBIE_BROWSER_CDP_URL") or DEFAULT_CDP_URL)
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(url)
        pages = [page for context in browser.contexts for page in context.pages]
        return PlaywrightNatGenNocBrowser(select_natgen_page(pages)), playwright.stop
    except Exception:
        playwright.stop()
        raise


def select_natgen_page(pages: list[Any]) -> Any:
    """Use the single NatGen application tab. Extra NatGen tabs are ambiguous."""
    matches = [page for page in pages if _is_natgen_app_url(str(getattr(page, "url", "") or ""))]
    if len(matches) != 1:
        raise IntakeHold("Expected exactly one NatGen tab")
    return matches[0]


def require_loopback_cdp(url: str) -> str:
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in {"127.0.0.1", "localhost"}:
        raise IntakeHold("NatGen browser attach must use the local Test CDP endpoint")
    return str(url).strip()


def require_list_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise IntakeHold("Pending Cancellations list URL is missing or ambiguous")
    if "login" in host or parsed.path.rstrip("/").lower().endswith("/login"):
        raise IntakeHold("NatGen session is not authenticated")
    if host != "natgenagency.com" and not host.endswith(".natgenagency.com"):
        raise IntakeHold("Pending Cancellations list URL is missing or ambiguous")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pull NatGen Pending Cancellation NOCs (Test only)")
    parser.add_argument("--start", required=True, help="Requested process-date window start, YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="Requested process-date window end, YYYY-MM-DD")
    parser.add_argument(
        "--output",
        default=str(DEFAULT_QA_ROOT),
        help="NatGen QA root. One private subfolder per scrubbed process date "
        "(default: hermes-test carrier-pull-qa/natgen)",
    )
    parser.add_argument(
        "--upload-drive",
        action="store_true",
        help="Upload that day's QA folder to the Nicole shared Drive. Not implemented; fails closed.",
    )
    parser.add_argument("--cdp-url", default=None, help="Loopback CDP URL. Defaults to 127.0.0.1:9222")
    return parser


def main(argv: list[str] | None = None, *, browser_factory: Callable[[argparse.Namespace], Any] | None = None) -> int:
    args = build_parser().parse_args(argv)
    closer: Callable[[], None] | None = None
    portal: NatGenPendingCancellationPortal | None = None
    try:
        from .document_retrieval_filing import FilingHeld, require_carrier_pull, resolve_pull_output

        require_carrier_pull("natgen")
        try:
            start = date.fromisoformat(args.start)
            end = date.fromisoformat(args.end)
        except ValueError as exc:
            raise IntakeHold("Process date window is missing or ambiguous") from exc
        require_bounded_scope(NOC_SCOPE, start, end)
        scrub_start, scrub_end = scrub_window(start, end)
        try:
            output = resolve_pull_output(args.output, DEFAULT_QA_ROOT)
        except FilingHeld as exc:
            raise IntakeHold(str(exc)) from exc
        ledger = LocalDeliveryLedger(output)
        ledger.ensure_private()
        archive = SourceArchive(output / "sources")
        if browser_factory is None:
            browser, closer = connect_cdp_browser(args.cdp_url)
        else:
            browser = browser_factory(args)
        portal = NatGenPendingCancellationPortal(browser, ledger)
        items = NatGenRetrieval(None, archive).pull_pending_cancellation_noc(portal, start=start, end=end)
        downloaded = []
        for item in items:
            row = portal.noc(item.source_id)
            downloaded.append({
                "document_id": item.source_id,
                "filename": item.filename,
                "sha256": item.digest,
                "bytes": len(item.content),
                "policy_number": row.policy_number,
                "insured_name": row.insured_name,
                "reason": row.reason,
                "processed_date": row.processed_on.isoformat(),
                "cancel_effective_date": row.cancel_effective.isoformat(),
                "path": str(ledger.pdf_path(row.processed_on, item.filename)),
            })
        if args.upload_drive:
            refuse_natgen_drive_upload(ledger, start=scrub_start, end=scrub_end, verification=portal.verification)
        _emit({
            "status": "PULLED",
            "scope": NOC_SCOPE,
            "process": NatGenRetrieval.process,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "scrub_start": scrub_start.isoformat(),
            "scrub_end": scrub_end.isoformat(),
            "count": len(downloaded),
            "downloaded": downloaded,
            "skipped_already_delivered": list(portal.skipped_document_ids),
            "verification": portal.verification,
            "ezlynx": "not_run",
            "additional_info": "TODO",
        })
        return 0
    except IntakeHold as exc:
        payload: dict[str, Any] = {
            "status": "HELD",
            "reason": str(exc),
            "ezlynx": "not_run",
            "additional_info": "TODO",
        }
        if portal is not None and portal.verification is not None:
            payload["verification"] = portal.verification
        _emit(payload)
        return 2
    except Exception as exc:
        _emit({
            "status": "UNVERIFIED",
            "reason": f"NatGen Pending Cancellation NOC pull unavailable ({type(exc).__name__})",
            "ezlynx": "not_run",
            "additional_info": "TODO",
        })
        return 1
    finally:
        if closer is not None:
            closer()


def pending_cancellations_screenshot_name(day: date) -> str:
    name = f"pending-cancellations-noc-{day.isoformat()}.png"
    if name != Path(name).name:
        raise IntakeHold("Pending Cancellations list screenshot is missing or not a PNG")
    return name


def require_png(blob: bytes | bytearray | None) -> bytes:
    if not isinstance(blob, (bytes, bytearray)) or not bytes(blob).startswith(_PNG_MAGIC):
        raise IntakeHold("Pending Cancellations list screenshot is missing or not a PNG")
    return bytes(blob)


def _noc_ids_by_date(rows, start: date, end: date) -> dict[str, set[str]]:
    grouped: dict[str, set[str]] = {day.isoformat(): set() for day in _each_day(start, end)}
    for row in rows:
        processed = str(row.get("processed_or_effective_date") or "")
        document_id = str(row.get("document_id") or "")
        if processed not in grouped or not document_id or document_id in grouped[processed]:
            raise IntakeHold("Pending Cancellations count does not match downloaded PDFs")
        grouped[processed].add(document_id)
    return grouped


def observed_noc_pdf_counts(*, rows, ledger: LocalDeliveryLedger, start: date, end: date) -> tuple[dict[str, Any], ...]:
    evidence: list[dict[str, Any]] = []
    for processed, ids in _noc_ids_by_date(rows, start, end).items():
        pdf_ids = set(ledger.pdf_ids_for_date(date.fromisoformat(processed)))
        evidence.append({
            "processed_date": processed,
            "noc_rows": len(ids),
            "pdfs": len(pdf_ids),
        })
    return tuple(evidence)


def require_noc_pdf_parity(*, rows, ledger: LocalDeliveryLedger, start: date, end: date) -> tuple[dict[str, Any], ...]:
    """Each process date in the scrub window must have one verified PDF per NOC row."""
    evidence: list[dict[str, Any]] = []
    for row in observed_noc_pdf_counts(rows=rows, ledger=ledger, start=start, end=end):
        if row["noc_rows"] != row["pdfs"]:
            raise IntakeHold(
                "Pending Cancellations count does not match downloaded PDFs "
                f"for {row['processed_date']}: {row['noc_rows']} NOC rows and {row['pdfs']} PDFs"
            )
        evidence.append(row)
    return tuple(evidence)


def write_natgen_qa_packs(
    *,
    ledger: LocalDeliveryLedger,
    start: date,
    end: date,
    requested: tuple[date, date],
    status: str,
    held: str | None,
    by_date,
    screenshots: dict[str, str],
    rows,
    skipped_document_ids,
    scrubbed: tuple[NocRow, ...],
    date_holds: list[dict[str, Any]],
    screenshot_covers_window: bool,
) -> dict[str, str]:
    """Write README.md and manifest.json into each process-date folder."""
    counts = {str(row["processed_date"]): row for row in by_date}
    skipped = set(skipped_document_ids)
    stored = ledger._load()["items"]
    holds_by_id = {str(item["document_id"]): item for item in date_holds}
    packs: dict[str, str] = {}
    for day in _each_day(start, end):
        key = day.isoformat()
        folder = ledger.date_dir(day)
        shot = screenshots.get(key)
        if not shot:
            raise IntakeHold("Pending Cancellations list screenshot is missing or not a PNG")
        count = counts.get(key) or {"noc_rows": 0, "pdfs": 0}
        nocs = _manifest_nocs(rows, stored, skipped, holds_by_id, processed=key)
        covers = f"{start.isoformat()} to {end.isoformat()}" if screenshot_covers_window else key
        manifest = {
            "carrier": "natgen",
            "scope": NOC_SCOPE,
            "additional_info": "TODO",
            "processed_date": key,
            "requested_window": {"start": requested[0].isoformat(), "end": requested[1].isoformat()},
            "window": {"start": start.isoformat(), "end": end.isoformat()},
            "list_day_filtered": False,
            "status": status,
            "gate": "noc_rows_equal_pdfs_and_cancel_dates_match",
            "noc_rows": count["noc_rows"],
            "pdfs": count["pdfs"],
            "screenshot": Path(shot).name,
            "screenshot_covers": covers,
            "screenshot_covers_window": screenshot_covers_window,
            "nocs": nocs,
            "scrubbed_out_of_window": [
                {
                    "policy_number": row.policy_number,
                    "processed_date": row.processed_on.isoformat(),
                    "reason": row.reason,
                    "cancel_effective_date": row.cancel_effective.isoformat(),
                }
                for row in scrubbed
            ],
            "date_holds": [item for item in date_holds if item.get("processed_date") == key],
            "held": held,
            "ezlynx": "not_run",
            "drive": _drive_destination(key),
        }
        _write_private_file(folder / "manifest.json", json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n")
        _write_private_file(folder / "README.md", _qa_readme(manifest, nocs).encode("utf-8"))
        packs[key] = str(folder)
    return packs


def refuse_natgen_drive_upload(
    ledger: LocalDeliveryLedger,
    *,
    start: date,
    end: date,
    verification: dict[str, Any] | None = None,
) -> None:
    """TODO: upload the date folder into the Nicole NatGen Drive. Fail closed."""
    for day in _each_day(start, end):
        folder = ledger.date_dir(day)
        manifest_path = folder / "manifest.json"
        readme_path = folder / "README.md"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            readme = readme_path.read_text(encoding="utf-8")
        except (OSError, json.JSONDecodeError) as exc:
            raise IntakeHold(DRIVE_UPLOAD_UNAVAILABLE) from exc
        if not isinstance(manifest, dict) or not isinstance(manifest.get("drive"), dict):
            raise IntakeHold(DRIVE_UPLOAD_UNAVAILABLE)
        if manifest["drive"].get("parent_id") != DRIVE_QA_PARENT_ID:
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


def _drive_destination(processed: str) -> dict[str, Any]:
    return {
        "status": "not_run",
        "parent_id": DRIVE_QA_PARENT_ID,
        "folder_id": None,
        "path": f"{DRIVE_QA_FOLDER_NAME}/{processed}/",
    }


def _row_payload(row: NocRow, *, delivered: bool) -> dict[str, Any]:
    return {
        "document_id": row.document_id,
        "source_account": NATGEN_SOURCE_ACCOUNT,
        "requires_action": True,
        "already_delivered": delivered,
        "processed_or_effective_date": row.processed_on.isoformat(),
        "policy_number": row.policy_number,
        "insured_name": row.insured_name,
        "reason": row.reason,
        "filename": row.filename,
        "cancel_effective_date": row.cancel_effective.isoformat(),
        "processed_date_source": row.processed_date_source,
    }


def _manifest_nocs(rows, stored: dict[str, Any], skipped: set[str], holds_by_id: dict[str, dict[str, Any]], *, processed: str) -> list[dict[str, Any]]:
    nocs: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("processed_or_effective_date") or "") != processed:
            continue
        document_id = str(row.get("document_id") or "")
        entry = stored.get(document_id)
        present = isinstance(entry, dict) and entry.get("processed_date") == processed
        hold = holds_by_id.get(document_id)
        if hold is not None:
            disposition = "date_mismatch"
        elif document_id in skipped and present:
            disposition = "already_present"
        elif present:
            disposition = "pulled"
        else:
            disposition = "missing"
        nocs.append({
            "document_id": document_id,
            "policy_number": row.get("policy_number"),
            "insured_name": row.get("insured_name"),
            "reason": row.get("reason"),
            "filename": hold.get("held_filename") if hold and hold.get("held_filename") else row.get("filename"),
            "cancel_effective_date": row.get("cancel_effective_date"),
            "processed_date_source": row.get("processed_date_source", LIST_SOURCE),
            "sha256": entry.get("sha256") if present else None,
            "bytes": entry.get("bytes") if present else None,
            "disposition": disposition,
        })
    return nocs


def _qa_readme(manifest: dict[str, Any], nocs: list[dict[str, Any]]) -> str:
    lines = [
        f"# NatGen Pending Cancellations QA — {manifest['processed_date']}",
        "",
        "Carrier: natgen",
        f"Requested window: {manifest['requested_window']['start']} through {manifest['requested_window']['end']}",
        f"Scrub window: {manifest['window']['start']} through {manifest['window']['end']}",
        "List day-filtered: no",
        f"Status: {manifest['status']}",
        (
            "Gate: Pending Cancellation rows in the scrub window equal saved NOC PDFs, "
            "and each filed PDF cancel effective date matches the list "
            f"({manifest['noc_rows']} NOC rows, {manifest['pdfs']} PDFs)"
        ),
        "",
        "## Seen",
    ]
    if nocs:
        for noc in nocs:
            lines.append(
                f"- {noc['policy_number']} {noc['insured_name']} — {noc['reason']} "
                f"cancel {noc['cancel_effective_date']} ({noc['filename']}; {noc['disposition']})"
            )
    else:
        lines.append("- No Pending Cancellation rows for this process date.")
    lines.extend(["", "## Pulled this run"])
    pulled = [noc for noc in nocs if noc["disposition"] == "pulled"]
    lines.extend([f"- {noc['filename']}" for noc in pulled] or ["- None"])
    lines.extend(["", "## Already present"])
    present = [noc for noc in nocs if noc["disposition"] == "already_present"]
    lines.extend([f"- {noc['filename']}" for noc in present] or ["- None"])
    lines.extend(["", "## Cancel effective date"])
    holds = manifest.get("date_holds") or []
    if holds:
        for hold in holds:
            observed = hold.get("observed") or "missing or ambiguous"
            filename = hold.get("held_filename") or "not filed"
            lines.append(
                f"- {hold['policy_number']} list {hold['listed']} PDF {observed} — {filename}"
            )
    else:
        lines.append("- Each filed PDF matched the list cancel effective date.")
    lines.extend(["", "## Scrubbed outside the window"])
    scrubbed = manifest.get("scrubbed_out_of_window") or []
    if scrubbed:
        lines.append("The Pending Cancellations list is not day-filtered. These process dates were left unfiled:")
        for row in scrubbed:
            lines.append(
                f"- {row['policy_number']} process {row['processed_date']} cancel {row['cancel_effective_date']}"
            )
    else:
        lines.append("- None")
    lines.extend(["", "## Held", manifest["held"] or "None", ""])
    lines.append(f"Screenshot: {manifest['screenshot']}")
    lines.append(
        "This PNG is the full Pending Cancellations list, not a day-filtered view."
    )
    if manifest["screenshot_covers_window"]:
        lines.append("The same list PNG is copied into each process-date folder in the scrub window.")
    lines.extend([
        "",
        "EZLynx: not_run",
        "Drive: not_run",
        f"Drive destination: {manifest['drive']['path']}",
        f"Drive parent: {manifest['drive']['parent_id']}",
        "Policy To Dos Additional Information: TODO",
        "",
    ])
    return "\n".join(lines)


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


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


def _received_at(day: date) -> str:
    from zoneinfo import ZoneInfo
    return datetime.combine(day, time(12, 0), tzinfo=ZoneInfo(_EASTERN_NAME)).isoformat()


def _each_day(start: date, end: date):
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def _is_pdf(blob: bytes | bytearray) -> bool:
    return len(blob) >= 8 and bytes(blob).startswith(b"%PDF")


def _push_pdf(candidates: list[bytes], blob: bytes) -> None:
    if not blob:
        return
    if not _is_pdf(blob):
        raise IntakeHold("NOC download is not a PDF")
    candidates.append(blob)


# Live Policy History Forms "View" (hermes-test-01, 2026-09-30) opens a new tab
# on /Policy/DisplayPDF.aspx?iid=... in Chrome's PDF viewer. No .pdf suffix.
_NATGEN_DISPLAY_PDF_PATH = re.compile(r"/policy/displaypdf\.aspx$", re.IGNORECASE)


def _url_looks_like_pdf(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    path = parsed.path.lower()
    if _NATGEN_DISPLAY_PDF_PATH.search(path) and _allowed_pdf_url(url):
        return True
    return path.endswith(".pdf") or "application/pdf" in url.lower()


def _allowed_pdf_url(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https":
        return False
    return (
        host == "natgenagency.com"
        or host.endswith(".natgenagency.com")
        or host == "nationalgeneral.com"
        or host.endswith(".nationalgeneral.com")
    )


def _is_natgen_app_url(url: str) -> bool:
    try:
        require_list_url(url)
    except IntakeHold:
        return False
    return True


POLICY_SUMMARY_WAIT_MS = 20000
HISTORY_PDF_TRIGGER = "a.pdfTrigger"
HISTORY_VIEW_PDF = "a[id$='_btnViewPDF']"


def _visible(locator: Any) -> bool:
    probe = getattr(locator, "is_visible", None)
    if not callable(probe):
        return True
    try:
        return bool(probe())
    except Exception:
        return False


def extract_history_table(page: Any) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...], tuple[Any, ...]]:
    table = page.locator(HISTORY_TABLE_CSS)
    if table.count() != 1:
        raise IntakeHold("Pending Cancellation NOC in Policy History is missing or ambiguous")
    rows = tuple(table.locator("tr").all())
    if not rows:
        raise IntakeHold("Pending Cancellation NOC in Policy History is missing or ambiguous")
    header_cells = rows[0].locator("th").all() or rows[0].locator("td").all()
    headers = tuple(_norm(cell.inner_text()) for cell in header_cells)
    body = rows[1:]
    grid = tuple(tuple(_norm(cell.inner_text()) for cell in row.locator("td").all()) for row in body)
    return headers, grid, tuple(body)


def extract_table(page: Any) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...], tuple[Any, ...]]:
    named = page.locator(PENDING_TABLE_CSS)
    try:
        named_count = int(named.count())
    except Exception:
        named_count = 0
    if named_count == 1:
        header_nodes = _unique_child(named, "thead").locator("th").all()
        if not header_nodes:
            raise IntakeHold("Pending Cancellations table is missing or ambiguous")
        headers = tuple(_norm(node.inner_text()) for node in header_nodes)
        row_locators = tuple(named.locator("tbody > tr").all())
        rows = tuple(tuple(_norm(cell.inner_text()) for cell in row.locator("td").all()) for row in row_locators)
        return headers, rows, row_locators
    tables = page.locator("table")
    if tables.count() != 1:
        raise IntakeHold("Pending Cancellations table is missing or ambiguous")
    header_nodes = _unique_child(tables, "thead").locator("th").all()
    if not header_nodes:
        raise IntakeHold("Pending Cancellations table is missing or ambiguous")
    headers = tuple(_norm(node.inner_text()) for node in header_nodes)
    row_locators = tuple(_unique_child(tables, "tbody").locator("tr").all())
    grid_rows = []
    for row in row_locators:
        grid_rows.append(tuple(_norm(cell.inner_text()) for cell in row.locator("td").all()))
    return headers, tuple(grid_rows), row_locators


def policy_control_count(row: Any, policy: str) -> int:
    total = 0
    for role in ("link", "button"):
        total += row.get_by_role(role, name=policy, exact=True).count()
    return total


def history_control_count(row: Any, label: str) -> int:
    total = 0
    for role in ("link", "button"):
        total += row.get_by_role(role, name=label, exact=True).count()
    return total


def click_policy_control(row: Any, policy: str) -> None:
    matches = []
    for role in ("link", "button"):
        locator = row.get_by_role(role, name=policy, exact=True)
        count = locator.count()
        if count:
            matches.append((count, locator))
    if len(matches) != 1 or matches[0][0] != 1:
        raise IntakeHold("NOC policy open control is missing or ambiguous")
    matches[0][1].click()


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


def _unique_child(parent: Any, selector: str) -> Any:
    locator = parent.locator(selector)
    if locator.count() != 1:
        raise IntakeHold("Pending Cancellations table is missing or ambiguous")
    return locator


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
    handle = tempfile.NamedTemporaryFile(prefix="natgen-noc-", suffix=".pdf", delete=False)
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


def _inflate_streams(content: bytes) -> list[str]:
    texts: list[str] = []
    marker = b"stream"
    start = 0
    while True:
        found = content.find(marker, start)
        if found < 0:
            break
        data_start = found + len(marker)
        if data_start < len(content) and content[data_start:data_start + 1] == b"\r":
            data_start += 1
        if data_start < len(content) and content[data_start:data_start + 1] == b"\n":
            data_start += 1
        end = content.find(b"endstream", data_start)
        if end < 0:
            break
        blob = content[data_start:end].rstrip(b"\r\n")
        start = end + len(b"endstream")
        try:
            text = zlib.decompress(blob).decode("latin-1", errors="ignore")
        except zlib.error:
            continue
        if _CANCEL_LABEL.search(text):
            texts.append(text)
    return texts


def _pypdf_text(content: bytes) -> str:
    try:
        from pypdf import PdfReader
        import io
        reader = PdfReader(io.BytesIO(content))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:
        return ""


def _is_download_timeout(exc: BaseException) -> bool:
    return "Timeout" in type(exc).__name__ or "Timeout" in str(exc)


if __name__ == "__main__":
    raise SystemExit(main())
