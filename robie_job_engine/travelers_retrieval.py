"""Test-only Travelers Policy Activity Report pull for Personal and Commercial lines.

One module, one CLI. ``--lob pl`` walks Personal Insurance → Featured Reports →
Policy Activity Report. ``--lob cl`` walks Business Insurance → Agency Reports →
Policy Activity Report. Accessible names are the playbook path, not a certified
live DOM. A missing or non-unique control raises IntakeHold.

This module does not log in, does not upload, note, task, or label in EZLynx,
and does not enable a timer. A dashboard alert with no PDF is a held note.
It is never written out as a PDF.
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
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from .intake_core import IntakeHold, IntakeWorker, SourceArchive, SourceItem, require_test


LOBS = frozenset({"pl", "cl"})
LOB_LABELS = {"pl": "Travelers PL", "cl": "Travelers CL"}
LOB_NAV = {
    "pl": ("Personal Insurance", "Featured Reports"),
    "cl": ("Business Insurance", "Agency Reports"),
}
PDF_CONTROL = {"insured": "Insured PDF", "agent": "Agent PDF"}
DEFAULT_CDP_URL = "http://127.0.0.1:9222"
DOWNLOAD_TIMEOUT_MS = 8000
LEDGER_NAME = "travelers-activity-ledger.json"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_QA_PARENT = Path(
    "/opt/streetsmart-hermes-test/robie-job-engine/data/artifacts/carrier-pull-qa"
)
# Shared Drive "Robie Carrier Pull QA (Nicole)". Child folder ids are UNVERIFIED.
# Folder upload is TODO. --upload-drive fails closed and does not call Google.
DRIVE_QA_PARENT_ID = "1cLEpR-0T6KdiVjcdAr0qpGTO447MetI2"
DRIVE_QA_FOLDER_NAME = "Robie Carrier Pull QA (Nicole)"
DRIVE_UPLOAD_UNAVAILABLE = (
    "Drive upload of the Travelers QA pack is not available; "
    "refusing to report the pack as uploaded"
)
_POLICY_NUMBER = re.compile(r"^[A-Z0-9][A-Z0-9-]{3,39}$")
_REMOTE_PDF = re.compile(r"https?://[^\s\"'<>]+?\.pdf(?:\?[^\s\"'<>]*)?", re.IGNORECASE)
_EASTERN = ZoneInfo("America/New_York")
_DASHBOARD_PHRASES = ("dashboard alert", "new business action item")
_SKIP_PHRASES = ("proposal", "non action", "nonaction", "informational")
_ACTIONABLE_PHRASES = (
    "notice of cancellation",
    "non renewal",
    "nonrenewal",
    "reinstatement",
    "reinstate",
    "cancellation",
    "cancel",
    "renewal",
    "additional info",
    "additional interest",
    "additional insured",
    "premium adjustment",
    "audit",
)

_HEADER_FIELDS = (
    ("policy_number", frozenset({"policy number", "policy"})),
    ("document_title", frozenset({
        "document title", "transaction", "activity", "description", "document",
    })),
    ("processed_date", frozenset({"process date", "processed date", "processed"})),
    ("row_kind", frozenset({"type", "alert", "category", "report section"})),
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
class ActivityGrid:
    list_url: str
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    insured_pdf_controls: tuple[int, ...]
    agent_pdf_controls: tuple[int, ...]
    more_pages: bool | None


@dataclass(frozen=True)
class ActivityRow:
    document_id: str
    policy_number: str
    document_title: str
    processed_on: date
    filename: str
    row_index: int
    source_url: str
    lob: str
    copy: str
    decision: str


@dataclass(frozen=True)
class ActivityParse:
    downloads: tuple[ActivityRow, ...]
    held_notes: tuple[ActivityRow, ...]
    skipped: tuple[ActivityRow, ...]


@dataclass(frozen=True)
class PagePdfView:
    url: str
    pdfs: tuple[bytes, ...]


@dataclass(frozen=True)
class PdfOpenObservation:
    downloads: tuple[bytes, ...]
    pages: tuple[PagePdfView, ...]


def require_lob(value: str) -> str:
    lob = str(value or "").strip().lower()
    if lob not in LOBS:
        raise IntakeHold("Travelers line of business must be pl or cl")
    return lob


def qa_root(lob: str) -> Path:
    return _QA_PARENT / f"travelers-{require_lob(lob)}"


def monday_weekend_window(as_of: date) -> tuple[date, date]:
    """Saturday through Sunday immediately before a Monday run date."""
    if as_of.weekday() != 0:
        raise IntakeHold("--include-weekends applies only when the run date is a Monday")
    return as_of - timedelta(days=2), as_of - timedelta(days=1)


def resolve_processed_window(
    *,
    as_of: date,
    include_weekends: bool,
    start: date | None,
    end: date | None,
) -> tuple[date, date]:
    """Last Day is the previous calendar day. Monday expands only with the flag.

    Omitted dates on a Monday hold, so the portal Last Day dropdown is not
    used to drop Saturday. Explicit dates are a historical window and stay
    bounded by ``require_bounded_window``. The flag itself is Monday-only and
    must be Saturday through Sunday.
    """
    if (start is None) != (end is None):
        raise IntakeHold("Travelers processed window is missing or ambiguous")
    if include_weekends:
        expected = monday_weekend_window(as_of)
        if start is None:
            return expected
        if (start, end) != expected:
            raise IntakeHold("Monday --include-weekends window must be Saturday through Sunday")
        return expected
    if as_of.weekday() == 0 and start is None:
        raise IntakeHold(
            "Monday Travelers pull requires --include-weekends so the weekend is in the window"
        )
    if start is None:
        yesterday = as_of - timedelta(days=1)
        return yesterday, yesterday
    return start, end


def require_bounded_window(start: date, end: date) -> None:
    require_test()
    if end < start or (end - start).days > 31:
        raise IntakeHold("An approved Travelers window is at most 32 inclusive days")


def processed_within_mode(
    *,
    start: date,
    end: date,
    as_of: date,
    include_weekends: bool,
) -> str:
    """``last_day`` only for the single calendar day before ``as_of``.

    A Monday weekend window always uses an explicit custom range. The live
    Last Day control is not trusted to include Saturday.
    """
    if include_weekends:
        return "custom_range"
    yesterday = as_of - timedelta(days=1)
    if (start, end) == (yesterday, yesterday):
        return "last_day"
    return "custom_range"


def _fold(value: str) -> str:
    return _norm(value).casefold().replace("-", " ").replace("/", " ")


def _is_dashboard(blob: str) -> bool:
    return any(phrase in blob for phrase in _DASHBOARD_PHRASES)


def _is_skip(title: str) -> bool:
    return any(phrase in title for phrase in _SKIP_PHRASES)


def _is_actionable(title: str) -> bool:
    if re.search(r"\bai\b", title) or re.search(r"\bnoc\b", title):
        return True
    return any(phrase in title for phrase in _ACTIONABLE_PHRASES)


def classify_activity_row(
    lob: str,
    title: str,
    kind: str | None,
    insured_controls: int,
    agent_controls: int,
) -> str:
    """Return download_insured, download_agent, skip, held_no_pdf, or ambiguous.

    Personal Lines downloads the Insured PDF column only. Commercial Lines
    prefers that copy and accepts an agent-only PDF. A dashboard alert with
    no PDF is a note. An actionable row with no PDF is ambiguous so this
    module does not invent a file.
    """
    line = require_lob(lob)
    if insured_controls < 0 or agent_controls < 0 or insured_controls > 1 or agent_controls > 1:
        return "ambiguous"
    label = _fold(title)
    if not label:
        return "ambiguous"
    blob = f"{label} {_fold(kind or '')}".strip()
    if _is_dashboard(blob):
        if insured_controls == 1:
            return "download_insured"
        if insured_controls == 0 and agent_controls == 0:
            return "held_no_pdf"
        if line == "cl" and agent_controls == 1:
            return "download_agent"
        return "ambiguous"
    if _is_skip(label):
        if insured_controls == 1:
            return "download_insured"
        if insured_controls == 0 and agent_controls == 0:
            return "skip"
        return "ambiguous"
    if _is_actionable(label):
        if insured_controls == 1:
            return "download_insured"
        if line == "cl" and insured_controls == 0 and agent_controls == 1:
            return "download_agent"
        return "ambiguous"
    return "ambiguous"


def normalize_title(title: str) -> str:
    original = _norm(title)
    if any(char in original for char in '\\/:*?"<>|') or ".." in original:
        raise IntakeHold("Travelers document title is missing or ambiguous")
    cleaned = original.rstrip(".").strip()
    if not cleaned or cleaned in {".", ".."} or len(cleaned) > 120:
        raise IntakeHold("Travelers document title is missing or ambiguous")
    return cleaned


def require_policy_number(policy_number: str) -> str:
    policy = _norm(policy_number).upper()
    if not _POLICY_NUMBER.fullmatch(policy) or not any(char.isdigit() for char in policy):
        raise IntakeHold("Travelers policy number is missing or ambiguous")
    return policy


def activity_filename(policy_number: str, document_title: str) -> str:
    policy = require_policy_number(policy_number)
    return f"{policy} {normalize_title(document_title)} Travelers.pdf"


def activity_document_id(
    lob: str,
    policy_number: str,
    processed: date,
    document_title: str,
    *,
    copy: str,
) -> str:
    line = require_lob(lob)
    policy = require_policy_number(policy_number)
    title_key = normalize_title(document_title).casefold()
    if copy not in {"insured", "agent", "note"}:
        raise IntakeHold("Travelers document title is missing or ambiguous")
    prefix = "travelers-note" if copy == "note" else "travelers-activity"
    return f"{prefix}:{line}:{policy}:{processed.isoformat()}:{title_key}:{copy}"


def parse_processed_date(value: str) -> date:
    raw = _norm(value)
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            return date.fromisoformat(raw)
        if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", raw):
            return datetime.strptime(raw, "%m/%d/%Y").date()
    except ValueError as exc:
        raise IntakeHold("Travelers processed date is missing or ambiguous") from exc
    raise IntakeHold("Travelers processed date is missing or ambiguous")


def header_indexes(headers: tuple[str, ...]) -> dict[str, int]:
    normalized = tuple(_fold(header) for header in headers)
    if not normalized or any(not header for header in normalized) or len(normalized) != len(set(normalized)):
        raise IntakeHold("Policy Activity Report table is missing or ambiguous")
    indexes: dict[str, int] = {}
    for index, header in enumerate(normalized):
        fields = [name for name, aliases in _HEADER_FIELDS if header in aliases]
        if len(fields) != 1:
            if fields:
                raise IntakeHold("Policy Activity Report table is missing or ambiguous")
            continue
        if fields[0] in indexes:
            raise IntakeHold("Policy Activity Report table is missing or ambiguous")
        indexes[fields[0]] = index
    for required in ("policy_number", "document_title", "processed_date"):
        if required not in indexes:
            raise IntakeHold("Policy Activity Report table is missing or ambiguous")
    return indexes


def parse_activity_grid(grid: ActivityGrid, *, lob: str) -> ActivityParse:
    line = require_lob(lob)
    list_url = require_list_url(grid.list_url)
    if len(grid.rows) != len(grid.insured_pdf_controls) or len(grid.rows) != len(grid.agent_pdf_controls):
        raise IntakeHold("Policy Activity Report list is ambiguous")
    indexes = header_indexes(grid.headers)
    downloads: list[ActivityRow] = []
    held_notes: list[ActivityRow] = []
    skipped: list[ActivityRow] = []
    identities: list[tuple[str, str, str]] = []
    for index, (cells, insured_controls, agent_controls) in enumerate(
        zip(grid.rows, grid.insured_pdf_controls, grid.agent_pdf_controls)
    ):
        if len(cells) != len(grid.headers):
            raise IntakeHold("Policy Activity Report list is ambiguous")
        if not any(_norm(cell) for cell in cells):
            continue
        title_cell = cells[indexes["document_title"]]
        kind = cells[indexes["row_kind"]] if "row_kind" in indexes else None
        decision = classify_activity_row(line, title_cell, kind, insured_controls, agent_controls)
        if decision == "ambiguous":
            raise IntakeHold("Policy Activity Report list is ambiguous")
        policy = require_policy_number(cells[indexes["policy_number"]])
        title = normalize_title(title_cell)
        processed = parse_processed_date(cells[indexes["processed_date"]])
        identity = (policy, title.casefold(), processed.isoformat())
        identities.append(identity)
        if decision == "skip":
            skipped.append(_row(line, policy, title, processed, index, list_url, "", decision))
            continue
        if decision == "held_no_pdf":
            held_notes.append(_row(line, policy, title, processed, index, list_url, "note", decision))
            continue
        copy = "insured" if decision == "download_insured" else "agent"
        downloads.append(_row(line, policy, title, processed, index, list_url, copy, decision))
    if len(identities) != len(set(identities)):
        raise IntakeHold("Policy Activity Report list is ambiguous")
    return ActivityParse(tuple(downloads), tuple(held_notes), tuple(skipped))


def _row(
    lob: str,
    policy: str,
    title: str,
    processed: date,
    index: int,
    list_url: str,
    copy: str,
    decision: str,
) -> ActivityRow:
    filename = activity_filename(policy, title) if decision.startswith("download_") else ""
    return ActivityRow(
        document_id=activity_document_id(lob, policy, processed, title, copy=copy or "note"),
        policy_number=policy,
        document_title=title,
        processed_on=processed,
        filename=filename,
        row_index=index,
        source_url=list_url,
        lob=lob,
        copy=copy,
        decision=decision,
    )


def pdf_bytes_from_observation(observation: PdfOpenObservation) -> bytes:
    candidates: list[bytes] = []
    for blob in observation.downloads:
        if isinstance(blob, memoryview):
            blob = blob.tobytes()
        if not isinstance(blob, (bytes, bytearray)):
            raise IntakeHold("Travelers PDF capture is missing or ambiguous")
        if not blob:
            continue
        if not _is_pdf(blob):
            raise IntakeHold("Travelers download is not a PDF")
        candidates.append(bytes(blob))
    for view in observation.pages:
        candidates.extend(view.pdfs)
    digests = {hashlib.sha256(blob).digest() for blob in candidates}
    if len(digests) != 1:
        raise IntakeHold("Travelers PDF capture is missing or ambiguous")
    return candidates[0]


def read_playwright_pdf_view(page: Any) -> PagePdfView:
    """Collect PDF bytes from a download target, blob URL, or embedded PDF.

    HTML is not printed to PDF. Remote fetches stay on Travelers hosts.
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


def collect_pdf_observation(
    page: Any,
    open_pdf: Callable[[], None],
    *,
    read_page: Callable[[Any], PagePdfView] = read_playwright_pdf_view,
    timeout_ms: int = DOWNLOAD_TIMEOUT_MS,
) -> PdfOpenObservation:
    """Open one PDF control and keep a unique PDF from the download or a new tab."""
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
            open_pdf()
            clicked = True

        try:
            with page.expect_download(timeout=timeout_ms) as download_info:
                wrapped()
            downloads.append(_download_bytes(download_info.value))
        except IntakeHold:
            raise
        except Exception as exc:
            if not clicked or not _is_download_timeout(exc):
                raise IntakeHold("Travelers PDF capture is missing or ambiguous") from exc
        for item in opened:
            wait = getattr(item, "wait_for_load_state", None)
            if not callable(wait):
                continue
            try:
                wait("domcontentloaded", timeout=timeout_ms)
            except Exception as exc:
                if not _is_download_timeout(exc):
                    raise IntakeHold("Travelers PDF capture is missing or ambiguous") from exc
        views = [read_page(item) for item in opened]
        current_url = str(getattr(page, "url", "") or "")
        if current_url.startswith("blob:") or _url_looks_like_pdf(current_url):
            views.append(read_page(page))
        return PdfOpenObservation(downloads=tuple(downloads), pages=tuple(views))
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


def assert_authenticated(page: Any) -> None:
    url = str(getattr(page, "url", "") or "")
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/").lower()
    if parsed.scheme != "https" or not _is_travelers_host(host) or path.endswith("/login"):
        raise IntakeHold("Travelers session is not authenticated")
    if page.locator("input[type='password']").count() != 0:
        raise IntakeHold("Travelers session is not authenticated")


def click_named(page: Any, name: str, *, roles: tuple[str, ...]) -> None:
    matches = []
    for role in roles:
        locator = page.get_by_role(role, name=name, exact=True)
        count = locator.count()
        if count:
            matches.append((count, locator))
    if len(matches) != 1 or matches[0][0] != 1:
        raise IntakeHold(f"Travelers control {name!r} is missing or ambiguous")
    matches[0][1].click()


def fill_labeled_date(page: Any, label: str, day: date) -> None:
    locator = page.get_by_label(label, exact=True)
    if locator.count() != 1:
        raise IntakeHold(f"Travelers control {label!r} is missing or ambiguous")
    locator.fill(day.strftime("%m/%d/%Y"))
    try:
        observed = parse_processed_date(str(locator.input_value()).strip())
    except IntakeHold:
        observed = None
    if observed != day:
        raise IntakeHold("Processed date filter did not stick")


def select_labeled_option(page: Any, label: str, option: str) -> None:
    locator = page.get_by_label(label, exact=True)
    if locator.count() != 1:
        raise IntakeHold(f"Travelers control {label!r} is missing or ambiguous")
    select = getattr(locator, "select_option", None)
    if not callable(select):
        raise IntakeHold(f"Travelers control {label!r} is missing or ambiguous")
    select(label=option)
    observed = _norm(str(locator.input_value() or ""))
    if observed.casefold() != option.casefold():
        raise IntakeHold("Processed Within filter did not stick")


def apply_processed_within(
    page: Any,
    *,
    start: date,
    end: date,
    as_of: date,
    include_weekends: bool,
) -> str:
    mode = processed_within_mode(start=start, end=end, as_of=as_of, include_weekends=include_weekends)
    if mode == "last_day":
        select_labeled_option(page, "Processed Within", "Last Day")
        return mode
    fill_labeled_date(page, "Processed date from", start)
    fill_labeled_date(page, "Processed date to", end)
    return mode


def pdf_control_count(row: Any, name: str) -> int:
    total = 0
    for role in ("link", "button"):
        total += row.get_by_role(role, name=name, exact=True).count()
    return total


def click_pdf_control(row: Any, copy: str) -> None:
    name = PDF_CONTROL.get(copy)
    if name is None:
        raise IntakeHold("Travelers PDF control is missing or ambiguous")
    matches = []
    for role in ("link", "button"):
        locator = row.get_by_role(role, name=name, exact=True)
        count = locator.count()
        if count:
            matches.append((count, locator))
    if len(matches) != 1 or matches[0][0] != 1:
        raise IntakeHold("Travelers PDF control is missing or ambiguous")
    matches[0][1].click()


def extract_activity_grid(
    page: Any,
) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...], tuple[int, ...], tuple[int, ...], tuple[Any, ...]]:
    tables = page.locator("table")
    if tables.count() != 1:
        raise IntakeHold("Policy Activity Report table is missing or ambiguous")
    header_nodes = _unique_child(tables, "thead").locator("th").all()
    if not header_nodes:
        raise IntakeHold("Policy Activity Report table is missing or ambiguous")
    headers = tuple(_norm(node.inner_text()) for node in header_nodes)
    row_locators = tuple(_unique_child(tables, "tbody").locator("tr").all())
    grid_rows = []
    insured = []
    agent = []
    for row in row_locators:
        cells = tuple(_norm(cell.inner_text()) for cell in row.locator("td").all())
        grid_rows.append(cells)
        insured.append(pdf_control_count(row, "Insured PDF"))
        agent.append(pdf_control_count(row, "Agent PDF"))
    return headers, tuple(grid_rows), tuple(insured), tuple(agent), row_locators


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


class PlaywrightTravelersActivityBrowser:
    """Drive one already-authenticated Travelers tab. Does not type credentials."""

    def __init__(self, page: Any):
        self.page = page
        self.trail: list[str] = []
        self.applied: tuple[str, date, date] | None = None
        self._grid: ActivityGrid | None = None
        self._row_locators: tuple[Any, ...] = ()
        self._list_url = ""
        self._rows: dict[str, ActivityRow] = {}
        self._lob = ""

    def open_policy_activity(
        self,
        *,
        lob: str,
        start: date,
        end: date,
        as_of: date,
        include_weekends: bool,
    ) -> str:
        line = require_lob(lob)
        assert_authenticated(self.page)
        insurance, reports = LOB_NAV[line]
        self.trail = []
        click_named(self.page, insurance, roles=("tab", "link"))
        self.trail.append(insurance)
        click_named(self.page, reports, roles=("link", "button"))
        self.trail.append(reports)
        click_named(self.page, "Policy Activity Report", roles=("link", "button"))
        self.trail.append("Policy Activity Report")
        mode = apply_processed_within(
            self.page, start=start, end=end, as_of=as_of, include_weekends=include_weekends,
        )
        self.applied = (mode, start, end)
        self.trail.append(mode)
        click_named(self.page, "Search", roles=("button",))
        self.trail.append("Search")
        assert_authenticated(self.page)
        return mode

    def load_policy_activity(
        self,
        *,
        lob: str,
        start: date,
        end: date,
        as_of: date,
        include_weekends: bool,
    ) -> ActivityGrid:
        line = require_lob(lob)
        self.open_policy_activity(
            lob=line, start=start, end=end, as_of=as_of, include_weekends=include_weekends,
        )
        headers, rows, insured, agent, locators = extract_activity_grid(self.page)
        grid = ActivityGrid(
            list_url=str(getattr(self.page, "url", "") or ""),
            headers=headers,
            rows=rows,
            insured_pdf_controls=insured,
            agent_pdf_controls=agent,
            more_pages=more_pages(self.page),
        )
        parsed = parse_activity_grid(grid, lob=line)
        self._grid = grid
        self._row_locators = locators
        self._list_url = grid.list_url
        self._lob = line
        self._rows = {row.document_id: row for row in (*parsed.downloads, *parsed.held_notes, *parsed.skipped)}
        return grid

    def capture_pdf(self, document_id: str) -> PdfOpenObservation:
        if self._grid is None or str(getattr(self.page, "url", "") or "") != self._list_url:
            raise IntakeHold("Policy Activity Report list is missing or ambiguous")
        row = self._rows.get(document_id)
        if row is None or not row.decision.startswith("download_") or row.row_index >= len(self._row_locators):
            raise IntakeHold("Selected carrier document is missing or ambiguous")
        target = self._row_locators[row.row_index]

        def open_pdf() -> None:
            click_pdf_control(target, row.copy)

        observation = collect_pdf_observation(self.page, open_pdf)
        if str(getattr(self.page, "url", "") or "") != self._list_url:
            go_back = getattr(self.page, "go_back", None)
            if callable(go_back):
                try:
                    go_back()
                except Exception as exc:
                    raise IntakeHold("Travelers PDF capture left the Policy Activity Report") from exc
            if str(getattr(self.page, "url", "") or "") != self._list_url:
                raise IntakeHold("Travelers PDF capture left the Policy Activity Report")
        return observation

    def screenshot_policy_activity(self) -> bytes:
        """Full-page PNG of the report results before any PDF is opened."""
        if self._grid is None or str(getattr(self.page, "url", "") or "") != self._list_url:
            raise IntakeHold("Policy Activity Report screenshot is missing or not a PNG")
        if self.page.locator("table").count() != 1:
            raise IntakeHold("Policy Activity Report screenshot is missing or not a PNG")
        data = self.page.screenshot(full_page=True, type="png")
        return require_png(data)


class LocalDeliveryLedger:
    """Private named-PDF ledger. A conflicting file is kept and the pull holds."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def ensure_private(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink() or not self.root.is_dir() or self.root.stat().st_mode & 0o077:
            raise IntakeHold("Travelers output directory must be private (0700)")

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
            raise IntakeHold("Existing Travelers file conflicts with the pull ledger")
        return True

    def record(self, source: SourceItem, *, processed_on: date) -> Path:
        self.ensure_private()
        if source.filename == LEDGER_NAME or not source.filename.endswith(".pdf"):
            raise IntakeHold("Travelers filename is missing or ambiguous")
        path = self.pdf_path(processed_on, source.filename)
        if path.exists():
            raise IntakeHold("Existing Travelers file conflicts with the pull ledger")
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
            raise IntakeHold("Existing Travelers file conflicts with the pull ledger")
        data["items"][source.source_id] = {
            "filename": source.filename,
            "sha256": digest,
            "bytes": len(source.content),
            "processed_date": processed_on.isoformat(),
        }
        self._write(data)
        return path

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
            raise IntakeHold("Travelers output directory must be private (0700)")
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not folder.is_dir() or folder.stat().st_mode & 0o077:
            raise IntakeHold("Travelers output directory must be private (0700)")
        return folder

    def save_screenshot(self, start: date, end: date, png: bytes) -> dict[str, str]:
        blob = require_png(png)
        saved: dict[str, str] = {}
        day = start
        while day <= end:
            name = activity_screenshot_name(day)
            saved[day.isoformat()] = str(self._save_png_named(self.date_dir(day), name, blob))
            day += timedelta(days=1)
        return saved

    def _save_png_named(self, folder: Path, name: str, blob: bytes) -> Path:
        primary = folder / self._basename(name)
        if primary.exists():
            if primary.is_symlink() or not primary.is_file():
                raise IntakeHold("Policy Activity Report screenshot is missing or not a PNG")
            if primary.read_bytes() == blob:
                return primary
            return self._write_png(self._sibling_screenshot(folder, name), blob)
        return self._write_png(primary, blob)

    def _sibling_screenshot(self, folder: Path, name: str) -> Path:
        stem = self._basename(name)[:-4]
        for index in range(2, 100):
            candidate = folder / f"{stem}-{index}.png"
            if not candidate.exists() and not candidate.is_symlink():
                return candidate
        raise IntakeHold("Policy Activity Report screenshot is missing or not a PNG")

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

    def _basename(self, filename: str) -> str:
        if not filename or filename != Path(filename).name or filename in {".", ".."}:
            raise IntakeHold("Travelers filename is missing or ambiguous")
        return filename

    def _ledger_path(self) -> Path:
        return self.root / LEDGER_NAME

    def _load(self) -> dict[str, Any]:
        path = self._ledger_path()
        if not path.exists():
            return {"version": 1, "items": {}}
        if path.is_symlink() or not path.is_file():
            raise IntakeHold("Existing Travelers file conflicts with the pull ledger")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise IntakeHold("Existing Travelers file conflicts with the pull ledger") from exc
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("items"), dict):
            raise IntakeHold("Existing Travelers file conflicts with the pull ledger")
        return data

    def _write(self, data: dict[str, Any]) -> None:
        path = self._ledger_path()
        temporary = path.with_name(LEDGER_NAME + ".tmp")
        if temporary.exists() or temporary.is_symlink():
            raise IntakeHold("Existing Travelers file conflicts with the pull ledger")
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


class TravelersActivityPortal:
    """Policy Activity list and download for one line. The other line is not opened."""

    def __init__(
        self,
        browser: Any,
        ledger: LocalDeliveryLedger,
        *,
        lob: str,
        as_of: date,
        include_weekends: bool,
    ):
        self.browser = browser
        self.ledger = ledger
        self.lob = require_lob(lob)
        self.as_of = as_of
        self.include_weekends = include_weekends
        self._cache_key: tuple[Any, ...] | None = None
        self._cache_result: Any = None
        self._downloads: dict[str, ActivityRow] = {}
        self._held: tuple[ActivityRow, ...] = ()
        self._skipped: tuple[ActivityRow, ...] = ()
        self._list_png: bytes | None = None
        self.skipped_document_ids: tuple[str, ...] = ()
        self.verification: dict[str, Any] | None = None

    def list_documents(self, *, lob: str, start: date, end: date):
        from .intake_core import ReadResult

        require_test()
        if require_lob(lob) != self.lob:
            raise IntakeHold("This portal lists one Travelers line only")
        key = (self.lob, start, end, self.as_of, self.include_weekends)
        if self._cache_key == key and self._cache_result is not None:
            return self._cache_result
        grid = self.browser.load_policy_activity(
            lob=self.lob,
            start=start,
            end=end,
            as_of=self.as_of,
            include_weekends=self.include_weekends,
        )
        parsed = parse_activity_grid(grid, lob=self.lob)
        if grid.more_pages is not False:
            raise IntakeHold("Policy Activity Report list is incomplete or ambiguous")
        self._list_png = require_png(self.browser.screenshot_policy_activity())
        rows = []
        found: dict[str, ActivityRow] = {}
        skipped_ids: list[str] = []
        for item in parsed.downloads:
            delivered = self.ledger.delivery_status(
                document_id=item.document_id,
                filename=item.filename,
                processed_on=item.processed_on,
            )
            if delivered:
                skipped_ids.append(item.document_id)
            rows.append({
                "document_id": item.document_id,
                "source_account": self.lob,
                "requires_action": True,
                "already_delivered": delivered,
                "processed_or_effective_date": item.processed_on.isoformat(),
                "policy_number": item.policy_number,
                "document_title": item.document_title,
                "filename": item.filename,
                "copy": item.copy,
                "lob": self.lob,
            })
            found[item.document_id] = item
        result = ReadResult(tuple(rows), authoritative=True, complete=True)
        self._cache_key = key
        self._cache_result = result
        self._downloads = found
        self._held = parsed.held_notes
        self._skipped = parsed.skipped
        self.skipped_document_ids = tuple(skipped_ids)
        return result

    def download_document(self, document_id: str) -> SourceItem:
        row = self._downloads.get(document_id)
        if row is None:
            raise IntakeHold("Selected carrier document is missing or ambiguous")
        content = pdf_bytes_from_observation(self.browser.capture_pdf(document_id))
        return SourceItem(
            system="travelers",
            source_account=self.lob,
            source_id=document_id,
            source_url=f"{row.source_url}#activity={document_id}",
            received_at=_received_at(row.processed_on),
            filename=row.filename,
            content=content,
        )

    def publish_source(self, source: SourceItem) -> str:
        row = self._require_download(source.source_id)
        path = self.ledger.record(source, processed_on=row.processed_on)
        self._mark_delivered(source.source_id)
        return str(path)

    def finish_pull(self, start: date, end: date) -> dict[str, Any]:
        """Write one QA pack per processed date. A bad count or a no-PDF alert stays HELD."""
        if self._list_png is None or self._cache_result is None:
            raise IntakeHold("Policy Activity Report screenshot is missing or not a PNG")
        by_date = observed_activity_pdf_counts(
            rows=self._cache_result.rows, ledger=self.ledger, start=start, end=end,
        )
        notes = _group_rows(self._held)
        statuses: dict[str, str] = {}
        held_by_date: dict[str, str | None] = {}
        reasons: list[str] = []
        evidence: list[dict[str, Any]] = []
        for row in by_date:
            day = str(row["processed_date"])
            day_notes = notes.get(day, ())
            status, reason = date_pack_status(row, day_notes)
            statuses[day] = status
            held_by_date[day] = reason
            if reason:
                reasons.append(reason)
            evidence.append({**row, "held_notes": len(day_notes), "status": status})
        screenshots = self.ledger.save_screenshot(start, end, self._list_png)
        packs = write_travelers_qa_packs(
            ledger=self.ledger,
            lob=self.lob,
            start=start,
            end=end,
            as_of=self.as_of,
            include_weekends=self.include_weekends,
            statuses=statuses,
            held_by_date=held_by_date,
            by_date=tuple(evidence),
            screenshots=screenshots,
            rows=self._cache_result.rows,
            held_notes=self._held,
            skipped_rows=self._skipped,
            skipped_document_ids=self.skipped_document_ids,
            screenshot_covers_window=start != end,
        )
        self.verification = {
            "gate": "activity_rows_equal_pdfs",
            "by_date": evidence,
            "screenshot": screenshots.get(start.isoformat()),
            "screenshots": screenshots,
            "packs": packs,
        }
        if reasons:
            raise IntakeHold("; ".join(reasons))
        return self.verification

    def _require_download(self, document_id: str) -> ActivityRow:
        try:
            return self._downloads[document_id]
        except KeyError as exc:
            raise IntakeHold("Selected carrier document is missing or ambiguous") from exc

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


class TravelersRetrieval(IntakeWorker):
    process = "travelers"
    source_system = "travelers"
    title = "Travelers policy activity intake: review carrier item locally"

    def pull_policy_activity(
        self,
        portal: Any,
        *,
        lob: str,
        start: date,
        end: date,
    ) -> tuple[SourceItem, ...]:
        """Download each in-window actionable PDF once. Local archive only."""
        line = require_lob(lob)
        require_bounded_window(start, end)
        if getattr(portal, "lob", None) != line:
            raise IntakeHold("This portal lists one Travelers line only")
        rows = portal.list_documents(lob=line, start=start, end=end).checked()
        _require_unique_download_rows(rows)
        pending: list[str] = []
        for row in rows:
            if row.get("already_delivered") is True and row.get("requires_action") is True:
                _require_carrier_date(start, end, row)
                continue
            _require_download_row(start, end, row)
            pending.append(str(row["document_id"]))
        downloaded: list[SourceItem] = []
        for document_id in pending:
            source = portal.download_document(document_id)
            source.validate()
            if source.system != "travelers" or source.source_id != document_id or source.source_account != line:
                raise IntakeHold("Downloaded source does not match the selected carrier document")
            self.archive.preserve(source)
            publish = getattr(portal, "publish_source", None)
            if callable(publish):
                publish(source)
            downloaded.append(source)
        finish = getattr(portal, "finish_pull", None)
        if callable(finish):
            finish(start, end)
        return tuple(downloaded)


def _require_carrier_date(start: date, end: date, row: Mapping[str, object]) -> None:
    if not start <= date.fromisoformat(str(row.get("processed_or_effective_date") or "")) <= end:
        raise IntakeHold("Carrier date is outside the requested retrieval window")


def _require_download_row(start: date, end: date, row: Mapping[str, object]) -> None:
    if row.get("requires_action") is not True or row.get("already_delivered") is not False:
        raise IntakeHold("Actionability and prior delivery must be explicitly checked")
    _require_carrier_date(start, end, row)


def _require_unique_download_rows(rows: tuple[Mapping[str, object], ...] | list[Mapping[str, object]]) -> None:
    ids: list[str] = []
    identities: list[tuple[str, str, str]] = []
    for row in rows:
        document_id = str(row.get("document_id") or "")
        if not document_id:
            raise IntakeHold("Selected carrier document is missing or ambiguous")
        ids.append(document_id)
        identities.append((
            str(row.get("policy_number") or ""),
            str(row.get("document_title") or "").strip().casefold().rstrip("."),
            str(row.get("processed_or_effective_date") or ""),
        ))
    if len(ids) != len(set(ids)) or len(identities) != len(set(identities)):
        raise IntakeHold("Selected carrier document is missing or ambiguous")


def connect_cdp_browser(cdp_url: str | None) -> tuple[PlaywrightTravelersActivityBrowser, Callable[[], None]]:
    """Attach to the local Test Chrome. Exactly one Travelers application tab."""
    require_test()
    url = require_loopback_cdp(cdp_url or os.environ.get("ROBIE_BROWSER_CDP_URL") or DEFAULT_CDP_URL)
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(url)
        pages = [page for context in browser.contexts for page in context.pages]
        return PlaywrightTravelersActivityBrowser(select_travelers_page(pages)), playwright.stop
    except Exception:
        playwright.stop()
        raise


def select_travelers_page(pages: list[Any]) -> Any:
    matches = [page for page in pages if _is_travelers_app_url(str(getattr(page, "url", "") or ""))]
    if len(matches) != 1:
        raise IntakeHold("Expected exactly one Travelers tab")
    return matches[0]


def require_loopback_cdp(url: str) -> str:
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in {"127.0.0.1", "localhost"}:
        raise IntakeHold("Travelers browser attach must use the local Test CDP endpoint")
    return str(url).strip()


def require_list_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise IntakeHold("Policy Activity Report URL is missing or ambiguous")
    if not _is_travelers_host(host) or parsed.path.rstrip("/").lower().endswith("/login"):
        raise IntakeHold("Travelers session is not authenticated")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pull Travelers Policy Activity Report PDFs (Test only)")
    parser.add_argument("--lob", required=True, choices=sorted(LOBS), help="pl (Personal) or cl (Commercial)")
    parser.add_argument("--start", default=None, help="Processed window start, YYYY-MM-DD. Omit with --end for Last Day.")
    parser.add_argument("--end", default=None, help="Processed window end, YYYY-MM-DD.")
    parser.add_argument("--as-of", default=None, help="Run date, YYYY-MM-DD. Defaults to today in America/New_York.")
    parser.add_argument(
        "--include-weekends",
        action="store_true",
        help="Monday only. Use Saturday through Sunday instead of Last Day.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="QA root. One private subfolder per processed date "
        "(default: hermes-test carrier-pull-qa/travelers-pl or travelers-cl)",
    )
    parser.add_argument(
        "--upload-drive",
        action="store_true",
        help="Upload that day's QA folder under the Nicole shared Drive. Not implemented; fails closed.",
    )
    parser.add_argument("--cdp-url", default=None, help="Loopback CDP URL. Defaults to 127.0.0.1:9222")
    return parser


def main(argv: list[str] | None = None, *, browser_factory: Callable[[argparse.Namespace], Any] | None = None) -> int:
    args = build_parser().parse_args(argv)
    closer: Callable[[], None] | None = None
    portal: TravelersActivityPortal | None = None
    try:
        require_test()
        lob = require_lob(args.lob)
        try:
            as_of = date.fromisoformat(args.as_of) if args.as_of else datetime.now(_EASTERN).date()
            start_arg = date.fromisoformat(args.start) if args.start else None
            end_arg = date.fromisoformat(args.end) if args.end else None
        except ValueError as exc:
            raise IntakeHold("Travelers processed window is missing or ambiguous") from exc
        start, end = resolve_processed_window(
            as_of=as_of,
            include_weekends=bool(args.include_weekends),
            start=start_arg,
            end=end_arg,
        )
        require_bounded_window(start, end)
        output = Path(args.output) if args.output else qa_root(lob)
        ledger = LocalDeliveryLedger(output)
        ledger.ensure_private()
        archive = SourceArchive(output / "sources")
        if browser_factory is None:
            browser, closer = connect_cdp_browser(args.cdp_url)
        else:
            browser = browser_factory(args)
        portal = TravelersActivityPortal(
            browser, ledger, lob=lob, as_of=as_of, include_weekends=bool(args.include_weekends),
        )
        items = TravelersRetrieval(None, archive).pull_policy_activity(portal, lob=lob, start=start, end=end)
        downloaded = []
        for item in items:
            row = portal._require_download(item.source_id)
            downloaded.append({
                "document_id": item.source_id,
                "filename": item.filename,
                "sha256": item.digest,
                "bytes": len(item.content),
                "policy_number": row.policy_number,
                "document_title": row.document_title,
                "processed_date": row.processed_on.isoformat(),
                "copy": row.copy,
                "path": str(ledger.pdf_path(row.processed_on, item.filename)),
            })
        if args.upload_drive:
            refuse_travelers_drive_upload(ledger, lob=lob, start=start, end=end, verification=portal.verification)
        _emit({
            "status": "PULLED",
            "lob": lob,
            "process": TravelersRetrieval.process,
            "as_of": as_of.isoformat(),
            "include_weekends": bool(args.include_weekends),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "count": len(downloaded),
            "downloaded": downloaded,
            "skipped_already_delivered": list(portal.skipped_document_ids),
            "verification": portal.verification,
            "ezlynx": "not_run",
        })
        return 0
    except IntakeHold as exc:
        payload: dict[str, Any] = {"status": "HELD", "reason": str(exc), "ezlynx": "not_run"}
        if portal is not None and portal.verification is not None:
            payload["verification"] = portal.verification
        _emit(payload)
        return 2
    except Exception as exc:
        _emit({
            "status": "UNVERIFIED",
            "reason": f"Travelers policy activity pull unavailable ({type(exc).__name__})",
            "ezlynx": "not_run",
        })
        return 1
    finally:
        if closer is not None:
            closer()


def activity_screenshot_name(day: date) -> str:
    name = f"policy-activity-report-{day.isoformat()}.png"
    if name != Path(name).name:
        raise IntakeHold("Policy Activity Report screenshot is missing or not a PNG")
    return name


def require_png(blob: bytes | bytearray | None) -> bytes:
    if not isinstance(blob, (bytes, bytearray)) or not bytes(blob).startswith(_PNG_MAGIC):
        raise IntakeHold("Policy Activity Report screenshot is missing or not a PNG")
    return bytes(blob)


def _download_ids_by_date(rows, start: date, end: date) -> dict[str, set[str]]:
    grouped: dict[str, set[str]] = {}
    day = start
    while day <= end:
        grouped[day.isoformat()] = set()
        day += timedelta(days=1)
    for row in rows:
        processed = str(row.get("processed_or_effective_date") or "")
        document_id = str(row.get("document_id") or "")
        if processed not in grouped or not document_id or document_id in grouped[processed]:
            raise IntakeHold("Policy Activity Report count does not match downloaded PDFs")
        grouped[processed].add(document_id)
    return grouped


def observed_activity_pdf_counts(*, rows, ledger: LocalDeliveryLedger, start: date, end: date) -> tuple[dict[str, Any], ...]:
    evidence: list[dict[str, Any]] = []
    for processed, ids in _download_ids_by_date(rows, start, end).items():
        pdf_ids = set(ledger.pdf_ids_for_date(date.fromisoformat(processed)))
        evidence.append({
            "processed_date": processed,
            "activity_rows": len(ids),
            "pdfs": len(pdf_ids),
        })
    return tuple(evidence)


def date_pack_status(row: Mapping[str, Any], notes: tuple[ActivityRow, ...] | list[ActivityRow]) -> tuple[str, str | None]:
    day = str(row["processed_date"])
    if row["activity_rows"] != row["pdfs"]:
        return "HELD", (
            "Policy Activity Report count does not match downloaded PDFs "
            f"for {day}: {row['activity_rows']} activity rows and {row['pdfs']} PDFs"
        )
    if notes:
        return "HELD", (
            f"Policy Activity Report for {day} has {len(notes)} dashboard alert(s) with no PDF; "
            "recorded as notes and not saved as PDFs"
        )
    return "PULLED", None


def _group_rows(rows: tuple[ActivityRow, ...]) -> dict[str, tuple[ActivityRow, ...]]:
    grouped: dict[str, list[ActivityRow]] = {}
    for row in rows:
        grouped.setdefault(row.processed_on.isoformat(), []).append(row)
    return {day: tuple(items) for day, items in grouped.items()}


def write_travelers_qa_packs(
    *,
    ledger: LocalDeliveryLedger,
    lob: str,
    start: date,
    end: date,
    as_of: date,
    include_weekends: bool,
    statuses: Mapping[str, str],
    held_by_date: Mapping[str, str | None],
    by_date,
    screenshots: dict[str, str],
    rows,
    held_notes: tuple[ActivityRow, ...],
    skipped_rows: tuple[ActivityRow, ...],
    skipped_document_ids,
    screenshot_covers_window: bool,
) -> dict[str, str]:
    counts = {str(row["processed_date"]): row for row in by_date}
    skipped_ids = set(skipped_document_ids)
    stored = ledger._load()["items"]
    packs: dict[str, str] = {}
    day = start
    while day <= end:
        key = day.isoformat()
        folder = ledger.date_dir(day)
        shot = screenshots.get(key)
        if not shot:
            raise IntakeHold("Policy Activity Report screenshot is missing or not a PNG")
        count = counts.get(key) or {"activity_rows": 0, "pdfs": 0, "held_notes": 0, "status": "PULLED"}
        documents = _manifest_documents(rows, stored, skipped_ids, held_notes, processed=key)
        skipped = [
            {"policy_number": item.policy_number, "document_title": item.document_title, "disposition": "skipped"}
            for item in skipped_rows if item.processed_on.isoformat() == key
        ]
        covers = f"{start.isoformat()} to {end.isoformat()}" if screenshot_covers_window else key
        manifest = {
            "carrier": "travelers",
            "lob": lob,
            "processed_date": key,
            "as_of": as_of.isoformat(),
            "include_weekends": include_weekends,
            "window": {"start": start.isoformat(), "end": end.isoformat()},
            "status": statuses.get(key, count.get("status", "HELD")),
            "gate": "activity_rows_equal_pdfs",
            "gate_passed": count["activity_rows"] == count["pdfs"],
            "activity_rows": count["activity_rows"],
            "pdfs": count["pdfs"],
            "held_notes": count.get("held_notes", 0),
            "screenshot": Path(shot).name,
            "screenshot_covers": covers,
            "screenshot_covers_window": screenshot_covers_window,
            "documents": documents,
            "skipped": skipped,
            "held": held_by_date.get(key),
            "ezlynx": "not_run",
            "drive": _drive_destination(lob, key),
        }
        _write_private_file(folder / "manifest.json", json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n")
        _write_private_file(folder / "README.md", _qa_readme(manifest).encode("utf-8"))
        packs[key] = str(folder)
        day += timedelta(days=1)
    return packs


def refuse_travelers_drive_upload(
    ledger: LocalDeliveryLedger,
    *,
    lob: str,
    start: date,
    end: date,
    verification: dict[str, Any] | None = None,
) -> None:
    """TODO: upload the date folder under the Nicole parent. Fail closed. No Google call."""
    require_lob(lob)
    day = start
    while day <= end:
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
        if manifest["drive"].get("parent_id") != DRIVE_QA_PARENT_ID or "Drive: not_run\n" not in readme:
            raise IntakeHold(DRIVE_UPLOAD_UNAVAILABLE)
        manifest["drive"]["status"] = "HELD"
        manifest["drive"]["reason"] = DRIVE_UPLOAD_UNAVAILABLE
        _write_private_file(
            manifest_path,
            json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n",
        )
        _write_private_file(
            readme_path,
            readme.replace("Drive: not_run\n", f"Drive: HELD — {DRIVE_UPLOAD_UNAVAILABLE}\n", 1).encode("utf-8"),
        )
        day += timedelta(days=1)
    if verification is not None:
        verification["drive_upload"] = "HELD"
    raise IntakeHold(DRIVE_UPLOAD_UNAVAILABLE)


def _drive_destination(lob: str, processed: str) -> dict[str, Any]:
    return {
        "status": "not_run",
        "parent_id": DRIVE_QA_PARENT_ID,
        "folder_id": None,
        "path": f"{DRIVE_QA_FOLDER_NAME}/{LOB_LABELS[require_lob(lob)]}/{processed}/",
    }


def _manifest_documents(rows, stored: dict[str, Any], skipped_ids: set[str], held_notes, *, processed: str) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("processed_or_effective_date") or "") != processed:
            continue
        document_id = str(row.get("document_id") or "")
        entry = stored.get(document_id)
        present = isinstance(entry, dict) and entry.get("processed_date") == processed
        if document_id in skipped_ids and present:
            disposition = "already_present"
        elif present:
            disposition = "pulled"
        else:
            disposition = "missing"
        documents.append({
            "document_id": document_id,
            "policy_number": row.get("policy_number"),
            "document_title": row.get("document_title"),
            "filename": row.get("filename"),
            "copy": row.get("copy"),
            "sha256": entry.get("sha256") if present else None,
            "bytes": entry.get("bytes") if present else None,
            "disposition": disposition,
        })
    for note in held_notes:
        if note.processed_on.isoformat() != processed:
            continue
        documents.append({
            "document_id": note.document_id,
            "policy_number": note.policy_number,
            "document_title": note.document_title,
            "filename": None,
            "copy": "note",
            "sha256": None,
            "bytes": None,
            "disposition": "held_no_pdf",
        })
    return documents


def _qa_readme(manifest: dict[str, Any]) -> str:
    documents = list(manifest["documents"])
    lines = [
        f"# Travelers Policy Activity QA — {LOB_LABELS[manifest['lob']]} — {manifest['processed_date']}",
        "",
        "Carrier: travelers",
        f"Line: {manifest['lob']}",
        f"Run date: {manifest['as_of']}",
        f"Include weekends: {str(manifest['include_weekends']).lower()}",
        f"Window: {manifest['window']['start']} through {manifest['window']['end']}",
        f"Status: {manifest['status']}",
        (
            "Gate: actionable Policy Activity rows with a PDF equal saved PDFs "
            f"({manifest['activity_rows']} activity rows, {manifest['pdfs']} PDFs, "
            f"gate {'passed' if manifest['gate_passed'] else 'failed'})"
        ),
        "",
        "## Seen",
    ]
    if documents:
        for item in documents:
            lines.append(
                f"- {item['policy_number']} — {item['document_title']} "
                f"({item['disposition']})"
            )
    else:
        lines.append("- No Policy Activity rows for this processed date.")
    lines.extend(["", "## Pulled this run"])
    pulled = [item for item in documents if item["disposition"] == "pulled"]
    lines.extend([f"- {item['filename']}" for item in pulled] or ["- None"])
    lines.extend(["", "## Already present"])
    present = [item for item in documents if item["disposition"] == "already_present"]
    lines.extend([f"- {item['filename']}" for item in present] or ["- None"])
    lines.extend(["", "## Held notes (no PDF)"])
    notes = [item for item in documents if item["disposition"] == "held_no_pdf"]
    lines.extend(
        [f"- {item['policy_number']} — {item['document_title']} (note, no PDF)" for item in notes] or ["- None"]
    )
    lines.extend(["", "## Skipped"])
    skipped = list(manifest["skipped"])
    lines.extend(
        [f"- {item['policy_number']} — {item['document_title']}" for item in skipped] or ["- None"]
    )
    lines.extend(["", "## Held", manifest["held"] or "None", ""])
    lines.append(f"Screenshot: {manifest['screenshot']}")
    if manifest["screenshot_covers_window"]:
        lines.append(
            "This PNG is the Policy Activity Report for the whole window, copied into this date folder. "
            "It is not a separate single-day capture."
        )
    lines.extend([
        "",
        "EZLynx: not_run",
        "Drive: not_run",
        f"Drive destination: {manifest['drive']['path']}",
        "",
    ])
    return "\n".join(lines)


def _write_private_file(path: Path, payload: bytes) -> None:
    temporary = path.with_name(path.name + ".tmp")
    if temporary.exists() or temporary.is_symlink():
        raise IntakeHold("Existing Travelers file conflicts with the pull ledger")
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
    return datetime.combine(day, time(12, 0), tzinfo=_EASTERN).isoformat()


def _is_pdf(blob: bytes) -> bool:
    return len(blob) >= 8 and blob.startswith(b"%PDF")


def _push_pdf(candidates: list[bytes], blob: bytes) -> None:
    if not blob:
        return
    if not _is_pdf(blob):
        raise IntakeHold("Travelers download is not a PDF")
    candidates.append(blob)


def _url_looks_like_pdf(url: str) -> bool:
    path = urllib.parse.urlsplit(url).path.lower()
    return path.endswith(".pdf") or "application/pdf" in url.lower()


def _is_travelers_host(host: str) -> bool:
    return host == "travelers.com" or host.endswith(".travelers.com")


def _allowed_pdf_url(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https":
        return False
    return _is_travelers_host(host)


def _is_travelers_app_url(url: str) -> bool:
    try:
        require_list_url(url)
    except IntakeHold:
        return False
    return True


def _unique_child(parent: Any, selector: str) -> Any:
    locator = parent.locator(selector)
    if locator.count() != 1:
        raise IntakeHold("Policy Activity Report table is missing or ambiguous")
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
        raise IntakeHold("Travelers PDF capture is missing or ambiguous")
    return [locator]


def _read_blob(page: Any, url: str) -> bytes:
    payload = page.evaluate(_BLOB_JS, url)
    if not payload:
        return b""
    if not isinstance(payload, str):
        raise IntakeHold("Travelers PDF capture is missing or ambiguous")
    try:
        return base64.b64decode(payload, validate=True)
    except Exception as exc:
        raise IntakeHold("Travelers PDF capture is missing or ambiguous") from exc


def _http_get(page: Any, url: str) -> bytes:
    response = page.context.request.get(url, timeout=DOWNLOAD_TIMEOUT_MS)
    if getattr(response, "ok", True) is False:
        raise IntakeHold("Travelers PDF capture is missing or ambiguous")
    body = response.body()
    if not isinstance(body, (bytes, bytearray)):
        raise IntakeHold("Travelers PDF capture is missing or ambiguous")
    return bytes(body)


def _download_bytes(download: Any) -> bytes:
    handle = tempfile.NamedTemporaryFile(prefix="travelers-activity-", suffix=".pdf", delete=False)
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
