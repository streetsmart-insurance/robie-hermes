"""Test-only Progressive For Agents Only Communications memo list and download.

The manual prove on 2026-09-26 walked foragentsonly.com as agent CA33617:
Manage Policies, Policy Activity, an explicit processed-date window,
Communications, then each Memo row saved as
``[PolicyNumber] Progressive Memo [Reason].pdf``.

Accessible names below are that path, not a certified live DOM. A missing or
non-unique control raises IntakeHold. This module does not log in, does not
submit OTP, and does not upload, note, task, or label in EZLynx.
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
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .intake_core import IntakeHold, SourceArchive, SourceItem, require_test
from .progressive_retrieval import ProgressiveRetrieval, require_bounded_scope


FAO_SCOPE = "fao_communications"
DEFAULT_AGENT_CODE = "CA33617"
DEFAULT_CDP_URL = "http://127.0.0.1:9222"
DOWNLOAD_TIMEOUT_MS = 8000
LEDGER_NAME = "fao-memo-ledger.json"
_AGENT_CODE = re.compile(r"^CA\d{5}$")
_AGENT_CODE_IN_TEXT = re.compile(r"\bCA\d{5}\b")
_POLICY_NUMBER = re.compile(r"^\d{6,12}$")
_REMOTE_PDF = re.compile(r"https?://[^\s\"'<>]+?\.pdf(?:\?[^\s\"'<>]*)?", re.IGNORECASE)
_EASTERN = ZoneInfo("America/New_York")

_HEADER_FIELDS = (
    ("policy_number", frozenset({"policy number", "policy"})),
    ("insured_name", frozenset({"insured", "insured name"})),
    ("reason", frozenset({"reason", "fao reason"})),
    ("memo_type", frozenset({"type", "communication", "communications"})),
    ("processed_date", frozenset({"processed date"})),
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
class MemoGrid:
    list_url: str
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    memo_controls: tuple[int, ...]
    more_pages: bool | None


@dataclass(frozen=True)
class MemoRow:
    document_id: str
    policy_number: str
    insured_name: str
    reason: str
    processed_on: date
    filename: str
    row_index: int
    source_url: str
    agent_code: str


@dataclass(frozen=True)
class PagePdfView:
    url: str
    pdfs: tuple[bytes, ...]


@dataclass(frozen=True)
class MemoOpenObservation:
    downloads: tuple[bytes, ...]
    pages: tuple[PagePdfView, ...]


def require_agent_code(value: str) -> str:
    code = str(value or "").strip().upper()
    if not _AGENT_CODE.fullmatch(code):
        raise IntakeHold("Progressive FAO agent code is missing or ambiguous")
    return code


def normalize_reason(reason: str) -> str:
    original = _norm(reason)
    if any(char in original for char in '\\/:*?"<>|') or ".." in original:
        raise IntakeHold("Memo reason is missing or ambiguous")
    cleaned = original.rstrip(".").strip()
    if not cleaned or cleaned in {".", ".."} or len(cleaned) > 80:
        raise IntakeHold("Memo reason is missing or ambiguous")
    return cleaned


def memo_filename(policy_number: str, reason: str) -> str:
    policy = str(policy_number or "").strip()
    if not _POLICY_NUMBER.fullmatch(policy):
        raise IntakeHold("Memo policy number is missing or ambiguous")
    return f"{policy} Progressive Memo {normalize_reason(reason)}.pdf"


def memo_document_id(agent_code: str, policy_number: str, processed: date, reason: str) -> str:
    policy = str(policy_number or "").strip()
    if not _POLICY_NUMBER.fullmatch(policy):
        raise IntakeHold("Memo policy number is missing or ambiguous")
    reason_key = normalize_reason(reason).casefold()
    return f"fao-memo:{require_agent_code(agent_code)}:{policy}:{processed.isoformat()}:{reason_key}"


def parse_processed_date(value: str) -> date:
    raw = _norm(value)
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            return date.fromisoformat(raw)
        if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", raw):
            return datetime.strptime(raw, "%m/%d/%Y").date()
    except ValueError as exc:
        raise IntakeHold("Memo processed date is missing or ambiguous") from exc
    raise IntakeHold("Memo processed date is missing or ambiguous")


def header_indexes(headers: tuple[str, ...]) -> dict[str, int]:
    normalized = tuple(_norm(header).casefold() for header in headers)
    if not normalized or any(not header for header in normalized) or len(normalized) != len(set(normalized)):
        raise IntakeHold("Communications memo table is missing or ambiguous")
    indexes: dict[str, int] = {}
    for index, header in enumerate(normalized):
        fields = [name for name, aliases in _HEADER_FIELDS if header in aliases]
        if len(fields) > 1:
            raise IntakeHold("Communications memo table is missing or ambiguous")
        if not fields:
            continue
        if fields[0] in indexes:
            raise IntakeHold("Communications memo table is missing or ambiguous")
        indexes[fields[0]] = index
    for required in ("policy_number", "insured_name", "reason", "processed_date"):
        if required not in indexes:
            raise IntakeHold("Communications memo table is missing or ambiguous")
    return indexes


def classify_memo_row(memo_type: str | None, controls: int) -> str:
    """Return take, skip, or ambiguous. Only an explicit Memo row is taken."""
    if controls < 0:
        return "ambiguous"
    if memo_type is None:
        return "take" if controls == 1 else "ambiguous"
    label = _norm(memo_type)
    if not label:
        return "ambiguous"
    if label.casefold() == "memo":
        return "take" if controls == 1 else "ambiguous"
    return "skip" if controls == 0 else "ambiguous"


def parse_memo_grid(grid: MemoGrid, *, agent_code: str) -> tuple[MemoRow, ...]:
    code = require_agent_code(agent_code)
    list_url = require_list_url(grid.list_url)
    if len(grid.rows) != len(grid.memo_controls):
        raise IntakeHold("Communications memo list is ambiguous")
    indexes = header_indexes(grid.headers)
    memos: list[MemoRow] = []
    for index, (cells, controls) in enumerate(zip(grid.rows, grid.memo_controls)):
        if len(cells) != len(grid.headers):
            raise IntakeHold("Communications memo list is ambiguous")
        if not any(_norm(cell) for cell in cells):
            continue
        memo_type = cells[indexes["memo_type"]] if "memo_type" in indexes else None
        decision = classify_memo_row(memo_type, controls)
        if decision == "skip":
            continue
        if decision != "take":
            raise IntakeHold("Communications memo list is ambiguous")
        policy = _norm(cells[indexes["policy_number"]])
        insured = _norm(cells[indexes["insured_name"]])
        if not _POLICY_NUMBER.fullmatch(policy):
            raise IntakeHold("Memo policy number is missing or ambiguous")
        if not insured:
            raise IntakeHold("Memo insured name is missing or ambiguous")
        reason = normalize_reason(cells[indexes["reason"]])
        processed = parse_processed_date(cells[indexes["processed_date"]])
        document_id = memo_document_id(code, policy, processed, reason)
        memos.append(MemoRow(
            document_id=document_id,
            policy_number=policy,
            insured_name=insured,
            reason=reason,
            processed_on=processed,
            filename=memo_filename(policy, reason),
            row_index=index,
            source_url=list_url,
            agent_code=code,
        ))
    if len({memo.document_id for memo in memos}) != len(memos):
        raise IntakeHold("Communications memo list is ambiguous")
    return tuple(memos)


def pdf_bytes_from_observation(observation: MemoOpenObservation) -> bytes:
    candidates: list[bytes] = []
    for blob in observation.downloads:
        if isinstance(blob, memoryview):
            blob = blob.tobytes()
        if not isinstance(blob, (bytes, bytearray)):
            raise IntakeHold("Memo PDF capture is missing or ambiguous")
        if not blob:
            continue
        if not _is_pdf(blob):
            raise IntakeHold("Memo download is not a PDF")
        candidates.append(bytes(blob))
    for view in observation.pages:
        candidates.extend(view.pdfs)
    digests = {hashlib.sha256(blob).digest() for blob in candidates}
    if len(digests) != 1:
        raise IntakeHold("Memo PDF capture is missing or ambiguous")
    return candidates[0]


def read_playwright_pdf_view(page: Any) -> PagePdfView:
    """Collect PDF bytes from a download target, blob URL, or embedded PDF.

    HTML is not printed to PDF. Remote fetches stay on Progressive hosts.
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


def collect_memo_observation(
    page: Any,
    open_memo: Callable[[], None],
    *,
    read_page: Callable[[Any], PagePdfView] = read_playwright_pdf_view,
    timeout_ms: int = DOWNLOAD_TIMEOUT_MS,
) -> MemoOpenObservation:
    """Click one Memo control and keep a unique PDF from the download or a new tab."""
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
            open_memo()
            clicked = True

        try:
            with page.expect_download(timeout=timeout_ms) as download_info:
                wrapped()
            downloads.append(_download_bytes(download_info.value))
        except IntakeHold:
            raise
        except Exception as exc:
            if not clicked or not _is_download_timeout(exc):
                raise IntakeHold("Memo PDF capture is missing or ambiguous") from exc
        for item in opened:
            wait = getattr(item, "wait_for_load_state", None)
            if not callable(wait):
                continue
            try:
                wait("domcontentloaded", timeout=timeout_ms)
            except Exception as exc:
                if not _is_download_timeout(exc):
                    raise IntakeHold("Memo PDF capture is missing or ambiguous") from exc
        views = [read_page(item) for item in opened]
        current_url = str(getattr(page, "url", "") or "")
        if current_url.startswith("blob:") or _url_looks_like_pdf(current_url):
            views.append(read_page(page))
        return MemoOpenObservation(downloads=tuple(downloads), pages=tuple(views))
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
    if host.endswith("foragentsonlylogin.progressive.com") or path.endswith("/login"):
        raise IntakeHold("Progressive FAO session is not authenticated")
    if page.locator("input[type='password']").count() != 0:
        raise IntakeHold("Progressive FAO session is not authenticated")


def assert_agent_context(page: Any, agent_code: str) -> None:
    body = page.locator("body").inner_text()
    found = set(_AGENT_CODE_IN_TEXT.findall(str(body or "")))
    if found != {require_agent_code(agent_code)}:
        raise IntakeHold("Progressive FAO agent context is missing or ambiguous")


def click_named(page: Any, name: str, *, roles: tuple[str, ...]) -> None:
    matches = []
    for role in roles:
        locator = page.get_by_role(role, name=name, exact=True)
        count = locator.count()
        if count:
            matches.append((count, locator))
    if len(matches) != 1 or matches[0][0] != 1:
        raise IntakeHold(f"Progressive control {name!r} is missing or ambiguous")
    matches[0][1].click()


def fill_labeled_date(page: Any, label: str, day: date) -> None:
    locator = page.get_by_label(label, exact=True)
    if locator.count() != 1:
        raise IntakeHold(f"Progressive control {label!r} is missing or ambiguous")
    locator.fill(day.strftime("%m/%d/%Y"))
    try:
        observed = parse_processed_date(str(locator.input_value()).strip())
    except IntakeHold:
        observed = None
    if observed != day:
        raise IntakeHold("Processed date filter did not stick")


def open_communications_tab(page: Any) -> None:
    click_named(page, "Communications", roles=("tab",))
    locator = page.get_by_role("tab", name="Communications", exact=True)
    if locator.count() != 1 or locator.get_attribute("aria-selected") != "true":
        raise IntakeHold("Communications tab did not become selected")


def memo_control_count(row: Any) -> int:
    total = 0
    for role in ("link", "button"):
        total += row.get_by_role(role, name="Memo", exact=True).count()
    return total


def click_memo_control(row: Any) -> None:
    matches = []
    for role in ("link", "button"):
        locator = row.get_by_role(role, name="Memo", exact=True)
        count = locator.count()
        if count:
            matches.append((count, locator))
    if len(matches) != 1 or matches[0][0] != 1:
        raise IntakeHold("Memo open control is missing or ambiguous")
    matches[0][1].click()


def extract_memo_grid(page: Any) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...], tuple[int, ...], tuple[Any, ...]]:
    tables = page.locator("table")
    if tables.count() != 1:
        raise IntakeHold("Communications memo table is missing or ambiguous")
    header_nodes = _unique_child(tables, "thead").locator("th").all()
    if not header_nodes:
        raise IntakeHold("Communications memo table is missing or ambiguous")
    headers = tuple(_norm(node.inner_text()) for node in header_nodes)
    row_locators = tuple(_unique_child(tables, "tbody").locator("tr").all())
    grid_rows = []
    controls = []
    for row in row_locators:
        cells = tuple(_norm(cell.inner_text()) for cell in row.locator("td").all())
        grid_rows.append(cells)
        controls.append(memo_control_count(row))
    return headers, tuple(grid_rows), tuple(controls), row_locators


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


class PlaywrightFaoMemoBrowser:
    """Drive one already-authenticated FAO tab. Does not type credentials."""

    def __init__(self, page: Any, *, agent_code: str = DEFAULT_AGENT_CODE):
        self.page = page
        self.agent_code = require_agent_code(agent_code)
        self._grid: MemoGrid | None = None
        self._row_locators: tuple[Any, ...] = ()
        self._list_url = ""

    def load_communications(self, *, start: date, end: date, agent_code: str) -> MemoGrid:
        if require_agent_code(agent_code) != self.agent_code:
            raise IntakeHold("Progressive FAO agent context is missing or ambiguous")
        assert_authenticated(self.page)
        assert_agent_context(self.page, self.agent_code)
        click_named(self.page, "Manage Policies", roles=("link", "button"))
        click_named(self.page, "Policy Activity", roles=("link", "button"))
        fill_labeled_date(self.page, "Processed date from", start)
        fill_labeled_date(self.page, "Processed date to", end)
        click_named(self.page, "Search", roles=("button",))
        open_communications_tab(self.page)
        assert_authenticated(self.page)
        assert_agent_context(self.page, self.agent_code)
        headers, rows, controls, locators = extract_memo_grid(self.page)
        grid = MemoGrid(
            list_url=str(getattr(self.page, "url", "") or ""),
            headers=headers,
            rows=rows,
            memo_controls=controls,
            more_pages=more_pages(self.page),
        )
        parse_memo_grid(grid, agent_code=self.agent_code)
        self._grid = grid
        self._row_locators = locators
        self._list_url = grid.list_url
        return grid

    def capture_memo(self, document_id: str) -> MemoOpenObservation:
        if self._grid is None or str(getattr(self.page, "url", "") or "") != self._list_url:
            raise IntakeHold("Communications memo list is missing or ambiguous")
        matches = [
            row for row in parse_memo_grid(self._grid, agent_code=self.agent_code)
            if row.document_id == document_id
        ]
        if len(matches) != 1 or matches[0].row_index >= len(self._row_locators):
            raise IntakeHold("Selected carrier document is missing or ambiguous")
        target = self._row_locators[matches[0].row_index]

        def open_memo() -> None:
            click_memo_control(target)

        observation = collect_memo_observation(self.page, open_memo)
        if str(getattr(self.page, "url", "") or "") != self._list_url:
            go_back = getattr(self.page, "go_back", None)
            if callable(go_back):
                try:
                    go_back()
                except Exception as exc:
                    raise IntakeHold("Memo PDF capture left the Communications list") from exc
            if str(getattr(self.page, "url", "") or "") != self._list_url:
                raise IntakeHold("Memo PDF capture left the Communications list")
        return observation


class LocalDeliveryLedger:
    """Private named-PDF ledger. A conflicting file is kept and the pull holds."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def ensure_private(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink() or not self.root.is_dir() or self.root.stat().st_mode & 0o077:
            raise IntakeHold("Memo output directory must be private (0700)")

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
            raise IntakeHold("Existing memo file conflicts with the pull ledger")
        return True

    def record(self, source: SourceItem) -> Path:
        self.ensure_private()
        if source.filename == LEDGER_NAME:
            raise IntakeHold("Memo filename is missing or ambiguous")
        path = self._named_path(source.filename)
        if path.exists():
            raise IntakeHold("Existing memo file conflicts with the pull ledger")
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
            raise IntakeHold("Existing memo file conflicts with the pull ledger")
        data["items"][source.source_id] = {
            "filename": source.filename,
            "sha256": digest,
            "bytes": len(source.content),
        }
        self._write(data)
        return path

    def _named_path(self, filename: str) -> Path:
        if not filename or filename != Path(filename).name or filename in {".", ".."}:
            raise IntakeHold("Memo filename is missing or ambiguous")
        return self.root / filename

    def _ledger_path(self) -> Path:
        return self.root / LEDGER_NAME

    def _load(self) -> dict[str, Any]:
        path = self._ledger_path()
        if not path.exists():
            return {"version": 1, "items": {}}
        if path.is_symlink() or not path.is_file():
            raise IntakeHold("Existing memo file conflicts with the pull ledger")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise IntakeHold("Existing memo file conflicts with the pull ledger") from exc
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("items"), dict):
            raise IntakeHold("Existing memo file conflicts with the pull ledger")
        return data

    def _write(self, data: dict[str, Any]) -> None:
        path = self._ledger_path()
        temporary = path.with_name(LEDGER_NAME + ".tmp")
        if temporary.exists() or temporary.is_symlink():
            raise IntakeHold("Existing memo file conflicts with the pull ledger")
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


class FaoCommunicationsMemoPortal:
    """ProgressivePort for fao_communications memos. Other scopes do not navigate."""

    def __init__(self, browser: Any, ledger: LocalDeliveryLedger, *, agent_code: str = DEFAULT_AGENT_CODE):
        self.browser = browser
        self.ledger = ledger
        self.agent_code = require_agent_code(agent_code)
        self._cache_key: tuple[Any, ...] | None = None
        self._cache_result: Any = None
        self._memos: dict[str, MemoRow] = {}
        self.skipped_document_ids: tuple[str, ...] = ()

    def list_documents(self, *, scope: str, start: date, end: date):
        from .intake_core import ReadResult

        if scope != FAO_SCOPE:
            raise IntakeHold("This portal lists FAO Communications memos only")
        key = (scope, start, end, self.agent_code)
        if self._cache_key == key and self._cache_result is not None:
            return self._cache_result
        grid = self.browser.load_communications(start=start, end=end, agent_code=self.agent_code)
        memos = parse_memo_grid(grid, agent_code=self.agent_code)
        if grid.more_pages is not False:
            raise IntakeHold("Communications memo list is incomplete or ambiguous")
        rows = []
        found: dict[str, MemoRow] = {}
        skipped: list[str] = []
        for memo in memos:
            delivered = self.ledger.delivery_status(document_id=memo.document_id, filename=memo.filename)
            if delivered:
                skipped.append(memo.document_id)
            rows.append({
                "document_id": memo.document_id,
                "source_account": self.agent_code,
                "requires_action": True,
                "already_delivered": delivered,
                "processed_or_effective_date": memo.processed_on.isoformat(),
                "policy_number": memo.policy_number,
                "insured_name": memo.insured_name,
                "reason": memo.reason,
                "filename": memo.filename,
            })
            found[memo.document_id] = memo
        result = ReadResult(tuple(rows), authoritative=True, complete=True)
        self._cache_key = key
        self._cache_result = result
        self._memos = found
        self.skipped_document_ids = tuple(skipped)
        return result

    def download_document(self, document_id: str) -> SourceItem:
        memo = self._memos.get(document_id)
        if memo is None:
            raise IntakeHold("Selected carrier document is missing or ambiguous")
        content = pdf_bytes_from_observation(self.browser.capture_memo(document_id))
        return SourceItem(
            system="progressive",
            source_account=self.agent_code,
            source_id=document_id,
            source_url=f"{memo.source_url}#memo={document_id}",
            received_at=_received_at(memo.processed_on),
            filename=memo.filename,
            content=content,
        )

    def publish_source(self, source: SourceItem) -> str:
        path = self.ledger.record(source)
        self._mark_delivered(source.source_id)
        return str(path)

    def memo(self, document_id: str) -> MemoRow:
        try:
            return self._memos[document_id]
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


def connect_cdp_browser(cdp_url: str | None, *, agent_code: str) -> tuple[PlaywrightFaoMemoBrowser, Callable[[], None]]:
    """Attach to the local Test Chrome. Exactly one FAO application tab."""
    require_test()
    url = require_loopback_cdp(cdp_url or os.environ.get("ROBIE_BROWSER_CDP_URL") or DEFAULT_CDP_URL)
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(url)
        pages = [page for context in browser.contexts for page in context.pages]
        return PlaywrightFaoMemoBrowser(select_fao_page(pages), agent_code=agent_code), playwright.stop
    except Exception:
        playwright.stop()
        raise


def select_fao_page(pages: list[Any]) -> Any:
    """Use the single FAO application tab. Extra FAO tabs are ambiguous."""
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


def require_list_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise IntakeHold("Communications list URL is missing or ambiguous")
    if host.endswith("foragentsonlylogin.progressive.com") or parsed.path.rstrip("/").lower().endswith("/login"):
        raise IntakeHold("Progressive FAO session is not authenticated")
    if host != "foragentsonly.com" and not host.endswith(".foragentsonly.com"):
        raise IntakeHold("Communications list URL is missing or ambiguous")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pull Progressive FAO Communications memos (Test only)")
    parser.add_argument("--start", required=True, help="Processed date window start, YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="Processed date window end, YYYY-MM-DD")
    parser.add_argument("--output", required=True, help="Private directory for named PDFs and the ledger")
    parser.add_argument("--agent-code", default=os.environ.get("PROGRESSIVE_FAO_AGENT_CODE", DEFAULT_AGENT_CODE))
    parser.add_argument("--cdp-url", default=None, help="Loopback CDP URL. Defaults to 127.0.0.1:9222")
    return parser


def main(argv: list[str] | None = None, *, browser_factory: Callable[[argparse.Namespace], Any] | None = None) -> int:
    args = build_parser().parse_args(argv)
    closer: Callable[[], None] | None = None
    try:
        require_test()
        try:
            start = date.fromisoformat(args.start)
            end = date.fromisoformat(args.end)
        except ValueError as exc:
            raise IntakeHold("Processed date window is missing or ambiguous") from exc
        agent_code = require_agent_code(args.agent_code)
        require_bounded_scope(FAO_SCOPE, start, end)
        output = Path(args.output)
        ledger = LocalDeliveryLedger(output)
        ledger.ensure_private()
        archive = SourceArchive(output / "sources")
        if browser_factory is None:
            browser, closer = connect_cdp_browser(args.cdp_url, agent_code=agent_code)
        else:
            browser = browser_factory(args)
        portal = FaoCommunicationsMemoPortal(browser, ledger, agent_code=agent_code)
        items = ProgressiveRetrieval(None, archive).pull_fao_communications(portal, start=start, end=end)
        downloaded = []
        for item in items:
            memo = portal.memo(item.source_id)
            downloaded.append({
                "document_id": item.source_id,
                "filename": item.filename,
                "sha256": item.digest,
                "bytes": len(item.content),
                "policy_number": memo.policy_number,
                "insured_name": memo.insured_name,
                "reason": memo.reason,
                "processed_date": memo.processed_on.isoformat(),
                "path": str(ledger.root / item.filename),
            })
        _emit({
            "status": "PULLED",
            "scope": FAO_SCOPE,
            "process": ProgressiveRetrieval.process,
            "agent_code": agent_code,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "count": len(downloaded),
            "downloaded": downloaded,
            "skipped_already_delivered": list(portal.skipped_document_ids),
            "ezlynx": "not_run",
        })
        return 0
    except IntakeHold as exc:
        _emit({"status": "HELD", "reason": str(exc), "ezlynx": "not_run"})
        return 2
    except Exception as exc:
        _emit({
            "status": "UNVERIFIED",
            "reason": f"Progressive FAO memo pull unavailable ({type(exc).__name__})",
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


def _is_pdf(blob: bytes) -> bool:
    return len(blob) >= 8 and blob.startswith(b"%PDF")


def _push_pdf(candidates: list[bytes], blob: bytes) -> None:
    if not blob:
        return
    if not _is_pdf(blob):
        raise IntakeHold("Memo download is not a PDF")
    candidates.append(blob)


def _url_looks_like_pdf(url: str) -> bool:
    path = urllib.parse.urlsplit(url).path.lower()
    return path.endswith(".pdf") or "application/pdf" in url.lower()


def _allowed_pdf_url(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https":
        return False
    return host == "foragentsonly.com" or host.endswith(".foragentsonly.com") or host == "progressive.com" or host.endswith(".progressive.com")


def _is_fao_app_url(url: str) -> bool:
    try:
        require_list_url(url)
    except IntakeHold:
        return False
    return True


def _unique_child(parent: Any, selector: str) -> Any:
    locator = parent.locator(selector)
    if locator.count() != 1:
        raise IntakeHold("Communications memo table is missing or ambiguous")
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
        raise IntakeHold("Memo PDF capture is missing or ambiguous")
    return [locator]


def _read_blob(page: Any, url: str) -> bytes:
    payload = page.evaluate(_BLOB_JS, url)
    if not payload:
        return b""
    if not isinstance(payload, str):
        raise IntakeHold("Memo PDF capture is missing or ambiguous")
    try:
        return base64.b64decode(payload, validate=True)
    except Exception as exc:
        raise IntakeHold("Memo PDF capture is missing or ambiguous") from exc


def _http_get(page: Any, url: str) -> bytes:
    response = page.context.request.get(url, timeout=DOWNLOAD_TIMEOUT_MS)
    if getattr(response, "ok", True) is False:
        raise IntakeHold("Memo PDF capture is missing or ambiguous")
    body = response.body()
    if not isinstance(body, (bytes, bytearray)):
        raise IntakeHold("Memo PDF capture is missing or ambiguous")
    return bytes(body)


def _download_bytes(download: Any) -> bytes:
    handle = tempfile.NamedTemporaryFile(prefix="fao-memo-", suffix=".pdf", delete=False)
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
