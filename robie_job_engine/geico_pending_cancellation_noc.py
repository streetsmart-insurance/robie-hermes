"""Test-only Geico Agent Gateway Pending Cancellation NOC list and download.

The manual prove on 2026-09-26 walked gateway2.geico.com/client-alerts:
Client Alerts, Pending Cancellations, then each personal-lines notice saved
as ``[PolicyNumber] NOC Geico.pdf``. Commercial Auto with Billing only and
no Documents route was held, not downloaded.

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
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .intake_core import IntakeHold, SourceArchive, SourceItem, require_test


PROCESS = "geico"
SCOPE = "pending_cancellation_noc"
DEFAULT_CDP_URL = "http://127.0.0.1:9222"
DOWNLOAD_TIMEOUT_MS = 8000
LEDGER_NAME = "geico-noc-ledger.json"
GATEWAY_HOST = "gateway2.geico.com"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_POLICY_NUMBER = re.compile(r"^\d{10}$")
_REMOTE_PDF = re.compile(r"https?://[^\s\"'<>]+?\.pdf(?:\?[^\s\"'<>]*)?", re.IGNORECASE)
_EASTERN = ZoneInfo("America/New_York")
NOTICE_NAMES = ("Pending Cancellation Notice", "CANCELLATION NOTICE")
PERSONAL_PRODUCT = "private passenger auto"
COMMERCIAL_PRODUCT = "commercial auto"

_HEADER_FIELDS = (
    ("status", frozenset({"severity", "status"})),
    ("policy_number", frozenset({"policy number", "policy"})),
    ("insured_name", frozenset({"insured name", "insured"})),
    ("product", frozenset({"product"})),
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


@dataclass(frozen=True)
class PagePdfView:
    url: str
    pdfs: tuple[bytes, ...]


@dataclass(frozen=True)
class NoticeOpenObservation:
    downloads: tuple[bytes, ...]
    pages: tuple[PagePdfView, ...]


def refuse_production_host() -> None:
    raw = f"{socket.gethostname()} {socket.getfqdn()}".lower()
    labels = [label for label in re.split(r"[\s.]+", raw) if label]
    if any(label == "hermes-poc-01" or label.startswith("hermes-poc") for label in labels):
        raise IntakeHold("Geico NOC pull refuses Production host hermes-poc-01")


def classify_line(product: str) -> str:
    key = _norm(product).casefold()
    if key == PERSONAL_PRODUCT:
        return "personal"
    if key == COMMERCIAL_PRODUCT:
        return "commercial"
    raise IntakeHold("Pending Cancellation product line is missing or ambiguous")


def noc_filename(policy_number: str) -> str:
    policy = str(policy_number or "").strip()
    if not _POLICY_NUMBER.fullmatch(policy):
        raise IntakeHold("NOC policy number is missing or ambiguous")
    return f"{policy} NOC Geico.pdf"


def noc_document_id(policy_number: str, due_on: date) -> str:
    policy = str(policy_number or "").strip()
    if not _POLICY_NUMBER.fullmatch(policy):
        raise IntakeHold("NOC policy number is missing or ambiguous")
    return f"geico-noc:{policy}:{due_on.isoformat()}"


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
    if not normalized or any(not header for header in normalized) or len(normalized) != len(set(normalized)):
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
    if "b2clogin" in host or path.endswith("/login"):
        raise IntakeHold("Geico Gateway session is not authenticated")
    if page.locator("input[type='password']").count() != 0:
        raise IntakeHold("Geico Gateway session is not authenticated")


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
    if tab.count() == 1:
        return tab.get_attribute("aria-selected") == "true"
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


def ensure_pending_view(page: Any) -> None:
    assert_authenticated(page)
    if not _is_gateway_app_url(str(getattr(page, "url", "") or "")):
        raise IntakeHold("Expected exactly one Geico Gateway tab")
    if pending_view_selected(page):
        return
    click_named(page, "Client Alerts", roles=("link", "button"))
    if pending_view_selected(page):
        return
    click_named(page, "Pending Cancellations", roles=("option", "button", "link", "tab"))
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


class PlaywrightGeicoNocBrowser:
    """Drive one already-authenticated Gateway tab. Does not type credentials."""

    def __init__(self, page: Any):
        self.page = page
        self._list_url = ""

    def load_pending_cancellations(self) -> AlertGrid:
        ensure_pending_view(self.page)
        assert_authenticated(self.page)
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
        self._open_policy(policy_number)
        return classify_policy_documents(self.page)

    def download_notice(self, policy_number: str) -> bytes:
        if not _POLICY_NUMBER.fullmatch(str(policy_number or "").strip()):
            raise IntakeHold("NOC policy number is missing or ambiguous")
        notice = _notice_match(self.page)
        if not isinstance(notice, tuple):
            raise IntakeHold("NOC PDF capture is missing or ambiguous")

        def open_notice() -> None:
            notice[1].click()

        return pdf_bytes_from_observation(collect_notice_observation(self.page, open_notice))

    def return_to_pending_list(self) -> None:
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
            return self.page.locator("table").count() == 1
        except IntakeHold:
            return False


def extract_alert_grid(page: Any) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
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

    def record(self, source: SourceItem, *, due_on: date, policy_number: str) -> Path:
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


def run_pull(
    browser: Any,
    ledger: LocalDeliveryLedger,
    archive: SourceArchive,
    *,
    as_of: date,
) -> dict[str, Any]:
    """List Pending Cancellations, download personal-lines NOCs, hold commercial gaps.

    Screenshot bytes are kept in memory and written only after the personal-lines
    PDF count matches the targeted set.
    """
    require_test()
    refuse_production_host()
    if not isinstance(as_of, date):
        raise IntakeHold("Pending Cancellations as-of date is missing or ambiguous")
    grid = browser.load_pending_cancellations()
    alerts = parse_alert_grid(grid)
    if grid.more_pages is not False:
        raise IntakeHold("Pending Cancellations list is incomplete or ambiguous")
    png = require_png(browser.screenshot_pending_cancellations())
    held: list[dict[str, Any]] = []
    downloaded: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    targeted: list[AlertRow] = []
    skipped: list[str] = []
    for alert in alerts:
        if alert.line == "commercial":
            if ledger.has_named_file_or_record(document_id=alert.document_id, filename=alert.filename):
                raise _held_now(
                    f"commercial policy {alert.policy_number} has a local NOC file; not a personal-lines pull",
                    held, downloaded, rows, targeted,
                )
            path = browser.inspect_notice_path(alert.policy_number)
            _require_return(browser, held, downloaded, rows, targeted)
            if path.kind == "billing_only":
                row = _row_payload(
                    alert,
                    outcome="HELD",
                    reason="commercial policy: Documents path missing; Billing only",
                )
                held.append(row)
                rows.append(row)
                continue
            if path.kind == "noc":
                row = _row_payload(
                    alert,
                    outcome="HELD",
                    reason="commercial policy exposed a Pending Cancellation Notice; not downloaded as a personal-lines NOC",
                )
                held.append(row)
                rows.append(row)
                continue
            raise _held_now(
                f"commercial policy {alert.policy_number} document path is missing or ambiguous",
                held, downloaded, rows, targeted,
            )
        if alert.line != "personal":
            raise IntakeHold("Pending Cancellation alert line is missing or ambiguous")
        if ledger.delivery_status(document_id=alert.document_id, filename=alert.filename):
            targeted.append(alert)
            skipped.append(alert.document_id)
            rows.append(_row_payload(alert, outcome="ALREADY_DELIVERED", filename=alert.filename))
            continue
        path = browser.inspect_notice_path(alert.policy_number)
        if path.kind != "noc":
            try:
                browser.return_to_pending_list()
            except IntakeHold:
                pass
            raise _held_now(
                f"personal-lines policy {alert.policy_number} is missing Documents → Billing → Pending Cancellation Notice",
                held, downloaded, rows, targeted,
            )
        content = browser.download_notice(alert.policy_number)
        if not _is_pdf(content):
            raise _held_now("NOC download is not a PDF", held, downloaded, rows, targeted)
        source = SourceItem(
            system=PROCESS,
            source_account=GATEWAY_HOST,
            source_id=alert.document_id,
            source_url=f"{alert.source_url}#policy={alert.policy_number}",
            received_at=_received_at(as_of),
            filename=alert.filename,
            content=content,
        )
        source.validate()
        saved = ledger.record(source, due_on=alert.due_on, policy_number=alert.policy_number)
        try:
            archive.preserve(source)
        except IntakeHold as exc:
            raise _held_now(str(exc), held, downloaded, rows, targeted) from exc
        _require_return(browser, held, downloaded, rows, targeted)
        targeted.append(alert)
        item = {
            "document_id": alert.document_id,
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
        rows.append(_row_payload(alert, outcome="PULLED", filename=alert.filename))
    targeted_ids = {alert.document_id for alert in targeted}
    verified = ledger.verified_ids(targeted_ids)
    try:
        evidence = require_noc_pdf_parity(targeted_ids=targeted_ids, verified_ids=verified)
    except IntakeHold as exc:
        raise _held_now(str(exc), held, downloaded, rows, targeted, pdfs=len(verified)) from exc
    shot = ledger.save_screenshot(as_of, png)
    evidence["screenshot"] = str(shot)
    return {
        "status": "PULLED",
        "scope": SCOPE,
        "process": PROCESS,
        "as_of": as_of.isoformat(),
        "alerts": len(alerts),
        "targeted": len(targeted),
        "count": len(downloaded),
        "downloaded": downloaded,
        "skipped_already_delivered": skipped,
        "held": held,
        "rows": rows,
        "verification": evidence,
        "ezlynx": "not_run",
    }


def connect_cdp_browser(cdp_url: str | None) -> tuple[PlaywrightGeicoNocBrowser, Callable[[], None]]:
    """Attach to the local Test Chrome. Exactly one Gateway application tab."""
    require_test()
    refuse_production_host()
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
    path = urllib.parse.urlsplit(cleaned).path.rstrip("/").lower()
    if not path.startswith("/client-alerts"):
        raise IntakeHold("Pending Cancellations list URL is missing or ambiguous")
    return cleaned


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pull Geico Pending Cancellation NOCs (Test only)")
    parser.add_argument("--as-of", required=True, help="List screenshot date, YYYY-MM-DD")
    parser.add_argument("--output", required=True, help="Private directory for named PDFs and the ledger")
    parser.add_argument("--cdp-url", default=None, help="Loopback CDP URL. Defaults to 127.0.0.1:9222")
    return parser


def main(argv: list[str] | None = None, *, browser_factory: Callable[[argparse.Namespace], Any] | None = None) -> int:
    args = build_parser().parse_args(argv)
    closer: Callable[[], None] | None = None
    try:
        require_test()
        refuse_production_host()
        try:
            as_of = date.fromisoformat(args.as_of)
        except ValueError as exc:
            raise IntakeHold("Pending Cancellations as-of date is missing or ambiguous") from exc
        output = Path(args.output)
        ledger = LocalDeliveryLedger(output)
        ledger.ensure_private()
        archive = SourceArchive(output / "sources")
        if browser_factory is None:
            browser, closer = connect_cdp_browser(args.cdp_url)
        else:
            browser = browser_factory(args)
        receipt = run_pull(browser, ledger, archive, as_of=as_of)
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
