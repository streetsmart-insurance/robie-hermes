"""Test-only Farmers of Salem pending-cancellation notice pull.

Playbook (verified live 2026-10-02 from Nicole's training):

* farmersofsalem.com/agent_login.aspx signs in to "Farmers Of Salem ::
  Agent Home" (already signed in; this module never logs in).
* Click the "FOS PORTAL" link (agent_portal.aspx). It opens a new tab with
  the Finys policy admin system at https://fos.finys.com/ ("THE FINYS
  SUITE").
* The Finys landing page shows "My Open Tasks - pending items" (sidebar:
  "My Pending Cancellation Items") with policy number, insured, product,
  and due date.
* For each policy: the top "Policy" menu or the policy-number Search box ->
  Policy Summary -> left nav "Document Summary" -> the document list.
  Click the target row's "View" link; the PDF opens in the Chrome viewer;
  the worker saves the original PDF bytes.

Target documents, one per policy (most recent first): Intent to Cancel
Notice, cancellation notices, underwriting memos, billing memos.

A missing or non-unique control raises IntakeHold. This module does not log
in, does not upload, note, task, or label in EZLynx, and does not register
a timer. The worker takes an already-signed-in Finys tab; ``open_finys_from_portal``
takes the farmersofsalem.com tab and expects exactly one new fos.finys.com tab.
"""
from __future__ import annotations

import argparse
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
from time import monotonic, sleep
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .document_retrieval_filing import require_carrier_pull
from .intake_core import IntakeHold, SourceArchive, SourceItem


CARRIER = "farmersofsalem"
PROCESS = "farmersofsalem-pending-cancellation"
PORTAL_HOST = "farmersofsalem.com"
FINYS_HOST = "fos.finys.com"
FOS_PORTAL_LINK = "FOS PORTAL"
LEDGER_NAME = "farmersofsalem-noc-ledger.json"
DEFAULT_CDP_URL = "http://127.0.0.1:9223"
DOWNLOAD_TIMEOUT_MS = 8000
_NAV_TIMEOUT_MS = 8000
_EASTERN = ZoneInfo("America/New_York")
_PDF_MAGIC = b"%PDF-"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
DEFAULT_OUTPUT_ROOT = Path(
    "/opt/streetsmart-hermes-test/robie-job-engine/data/artifacts/carrier-pull-qa/farmersofsalem"
)
# Shared Drive "Robie Carrier Pull QA (Nicole)". Upload is not wired: the CLI
# --upload-drive flag fails closed and does not call Google.
DRIVE_QA_PARENT_ID = "1cLEpR-0T6KdiVjcdAr0qpGTO447MetI2"
DRIVE_QA_FOLDER_NAME = "Robie Carrier Pull QA (Nicole)/FarmersOfSalem"
DRIVE_UPLOAD_UNAVAILABLE = (
    "Drive upload of the Farmers of Salem QA pack is not available; "
    "refusing to report the pack as uploaded"
)

# Live policy numbers look like HONJ038633: four letters, six digits.
_POLICY_NUMBER = re.compile(r"^[A-Za-z]{4}\d{6}$")
_DATE_MDY = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$")

# (notice key, filename label, keywords in priority order)
_NOTICE_TYPES = (
    ("intent-to-cancel", "Intent to Cancel Notice", ("intent to cancel",)),
    (
        "cancellation-notice",
        "Cancellation Notice",
        ("cancellation notice", "notice of cancellation", "cancel notice", "cancellation letter"),
    ),
    ("underwriting-memo", "Underwriting Memo", ("underwriting memo", "underwriting",)),
    ("billing-memo", "Billing Memo", ("billing memo", "billing",)),
)
_NOTICE_LABEL = {key: label for key, label, _ in _NOTICE_TYPES}
_NOTICE_PRIORITY = {key: index for index, (key, _, _) in enumerate(_NOTICE_TYPES)}

# Header aliases for the "My Open Tasks - pending items" table.
_PENDING_FIELDS = (
    ("policy_number", frozenset({"policy number", "policy #", "policy", "policynumber"})),
    ("insured_name", frozenset({"insured", "insured name", "named insured", "name"})),
    ("product", frozenset({"product", "line", "lob", "policy type"})),
    ("due_date", frozenset({"due date", "due", "cancel date", "cancellation date", "effective date"})),
)
# Header aliases for the Document Summary table.
_DOC_FIELDS = (
    ("description", frozenset({"description", "document", "document description", "type", "form", "title"})),
    ("doc_date", frozenset({"date", "document date", "created", "issued", "effective date"})),
    ("action", frozenset({"action", "view", ""})),
)


class PullHeld(RuntimeError):
    """A pull that stopped early. details carries held/downloaded rows."""

    def __init__(self, reason: str, *, details: dict[str, Any] | None = None):
        super().__init__(reason)
        self.details = details or {}


def refuse_production_host() -> None:
    """Refuse a hermes-poc host unless Farmers of Salem Production filing is enabled.

    The kill switch and the carrier allowlist are the only way through.
    Every other Farmers of Salem check stays in place.
    """

    from .document_retrieval_filing import live_filing_decision

    if live_filing_decision(os.environ, socket.gethostname(), CARRIER).allowed:
        return
    raw = f"{socket.gethostname()} {socket.getfqdn()}".lower()
    labels = [label for label in re.split(r"[\s.]+", raw) if label]
    if any(label == "hermes-poc-01" or label.startswith("hermes-poc") for label in labels):
        raise IntakeHold("Farmers of Salem pull refuses Production host hermes-poc-01")


def require_loopback_cdp(url: str) -> str:
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.username or parsed.password or parsed.scheme not in {"http", "https"} or host not in {"127.0.0.1", "localhost"}:
        raise IntakeHold("Finys browser attach must use the local Test CDP endpoint")
    return str(url).strip()


def require_hermes_test_host() -> None:
    """Live packs are produced on hermes-test-01. Fixture runs inject a browser."""

    from .document_retrieval_filing import live_filing_decision

    if live_filing_decision(os.environ, socket.gethostname(), CARRIER).allowed:
        return
    raw = f"{socket.gethostname()} {socket.getfqdn()}".lower()
    labels = [label for label in re.split(r"[\s.]+", raw) if label]
    if "hermes-test-01" not in labels:
        raise IntakeHold("Farmers of Salem QA pack must be produced on hermes-test-01")


def require_finys_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise IntakeHold("Finys URL is missing or ambiguous")
    if "login" in parsed.path.lower() or "signin" in parsed.path.lower():
        raise IntakeHold("Finys session is not authenticated")
    if host != FINYS_HOST:
        raise IntakeHold("Finys URL is missing or ambiguous")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _received_at(day: date) -> str:
    return datetime.combine(day, time(12, 0), tzinfo=_EASTERN).isoformat()


def parse_carrier_date(value: Any) -> date:
    text = _norm(value)
    match = _DATE_MDY.match(text)
    if match:
        month, day_num, year = (int(part) for part in match.groups())
        return date(year, month, day_num)
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise IntakeHold(f"Carrier date is missing or ambiguous: {text!r}") from None


def parse_policy_number(value: Any) -> str:
    text = _norm(value).replace(" ", "").upper()
    if not _POLICY_NUMBER.fullmatch(text):
        raise IntakeHold(f"Farmers of Salem policy number is missing or ambiguous: {_norm(value)!r}")
    return text


def classify_notice(description: Any) -> str | None:
    """Notice key for a Document Summary description, or None when out of scope."""
    text = _norm(description).casefold()
    if not text:
        return None
    for key, _, keywords in _NOTICE_TYPES:
        if any(keyword in text for keyword in keywords):
            return key
    return None


def notice_document_id(policy_number: str, doc_date: date, notice_key: str) -> str:
    policy = parse_policy_number(policy_number)
    if notice_key not in _NOTICE_LABEL:
        raise IntakeHold(f"Notice type is missing or ambiguous: {notice_key!r}")
    return f"{CARRIER}:{policy}:{doc_date.isoformat()}:{notice_key}"


def _safe_filename_part(value: str) -> str:
    cleaned = re.sub(r"[^\w\-. ]+", "", _norm(value)).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        raise IntakeHold("Notice filename is missing or ambiguous")
    return cleaned


def notice_filename(policy_number: str, notice_key: str) -> str:
    policy = parse_policy_number(policy_number)
    label = _NOTICE_LABEL.get(notice_key)
    if label is None:
        raise IntakeHold(f"Notice type is missing or ambiguous: {notice_key!r}")
    return f"{_safe_filename_part(policy)} {_safe_filename_part(label)} FarmersofSalem.pdf"


def _is_pdf(blob: bytes | bytearray) -> bool:
    return bytes(blob)[:5] == _PDF_MAGIC

# --- Page-structure helpers -------------------------------------------------


def _control(page: Any, role: str, name: str, *, exact: bool = False) -> Any:
    locator = page.get_by_role(role, name=name, exact=exact)
    try:
        count = int(locator.count())
    except Exception:
        count = -1
    if count != 1:
        raise IntakeHold(f"Finys {name!r} {role} is missing or ambiguous")
    return locator


def _header_indexes(headers: tuple[str, ...], fields: tuple[tuple[str, frozenset[str]], ...]) -> dict[str, int]:
    normalized = [_norm(header).casefold() for header in headers]
    indexes: dict[str, int] = {}
    for key, aliases in fields:
        for position, header in enumerate(normalized):
            if header in aliases:
                indexes[key] = position
                break
    return indexes


def _table_headers(table: Any) -> tuple[str, ...]:
    try:
        head_cells = table.locator("thead th").all()
    except Exception:
        head_cells = []
    if not head_cells:
        try:
            first_row = table.locator("tr").first
            head_cells = first_row.locator("th").all() or first_row.locator("td").all()
        except Exception:
            head_cells = []
    return tuple(_norm(cell.inner_text()) for cell in head_cells)


def _table_body_rows(table: Any) -> list[Any]:
    try:
        rows = table.locator("tbody tr").all()
    except Exception:
        rows = []
    if not rows:
        try:
            rows = table.locator("tr").all()[1:]
        except Exception:
            rows = []
    return list(rows)


def _find_table_by_headers(page: Any, fields: tuple[tuple[str, frozenset[str]], ...], *, required: tuple[str, ...]) -> tuple[Any, dict[str, int]]:
    try:
        tables = page.locator("table").all()
    except Exception:
        tables = []
    candidates = []
    for table in tables:
        headers = _table_headers(table)
        indexes = _header_indexes(headers, fields)
        if all(key in indexes for key in required):
            candidates.append((table, indexes))
    if len(candidates) != 1:
        raise IntakeHold("Finys table is missing or ambiguous")
    return candidates[0]


def _cell_text(row: Any, index: int) -> str:
    try:
        cells = row.locator("td").all()
    except Exception:
        cells = []
    if index >= len(cells):
        return ""
    try:
        return _norm(cells[index].inner_text())
    except Exception:
        return ""


@dataclass(frozen=True)
class PendingItem:
    policy_number: str
    insured_name: str
    product: str
    due_on: date


@dataclass(frozen=True)
class FoSDocument:
    description: str
    doc_date: date | None
    notice_key: str | None
    view: Any | None


def extract_pending_items(page: Any) -> tuple[PendingItem, ...]:
    """Parse "My Open Tasks - pending items" on the Finys landing page."""
    require_finys_url(str(getattr(page, "url", "") or ""))
    table, indexes = _find_table_by_headers(
        page, _PENDING_FIELDS, required=("policy_number", "due_date")
    )
    items: list[PendingItem] = []
    for row in _table_body_rows(table):
        policy = parse_policy_number(_cell_text(row, indexes["policy_number"]))
        due_on = parse_carrier_date(_cell_text(row, indexes["due_date"]))
        insured = _cell_text(row, indexes["insured_name"]) if "insured_name" in indexes else ""
        product = _cell_text(row, indexes["product"]) if "product" in indexes else ""
        items.append(PendingItem(policy_number=policy, insured_name=insured, product=product, due_on=due_on))
    return tuple(items)


def _find_search_box(page: Any) -> Any:
    for name in ("Policy Search", "Search Policy", "Search"):
        locator = page.get_by_role("textbox", name=name, exact=False)
        try:
            count = int(locator.count())
        except Exception:
            count = -1
        if count == 1:
            return locator
        if count > 1:
            raise IntakeHold("Finys policy search box is missing or ambiguous")
    raise IntakeHold("Finys policy search box is missing or ambiguous")


def _wait_for(condition: Callable[[], bool]) -> bool:
    deadline = monotonic() + _NAV_TIMEOUT_MS / 1000
    while monotonic() < deadline:
        try:
            if condition():
                return True
        except Exception:
            pass
        sleep(0.25)
    return False


def search_policy(page: Any, policy_number: str) -> None:
    """Enter the policy number in the Finys search box and open Policy Summary.

    Raises IntakeHold unless the resulting Policy Summary names the policy.
    """
    policy = parse_policy_number(policy_number)
    search = _find_search_box(page)
    try:
        search.fill(policy)
    except Exception as exc:
        raise IntakeHold("Finys policy search box is missing or ambiguous") from exc
    try:
        button = page.get_by_role("button", name="Search", exact=False)
        if int(button.count()) == 1:
            button.click()
        else:
            search.press("Enter")
    except IntakeHold:
        raise
    except Exception as exc:
        raise IntakeHold("Finys policy search did not submit") from exc

    def summary_shows_policy() -> bool:
        try:
            headings = page.get_by_role("heading", name="Policy Summary", exact=False).all()
        except Exception:
            return False
        for heading in headings:
            try:
                text = _norm(heading.inner_text())
            except Exception:
                continue
            if "policy summary" in text.casefold() and re.search(rf"\b{re.escape(policy)}\b", text):
                return True
        return False

    if not _wait_for(summary_shows_policy):
        raise IntakeHold(f"Policy Summary for {policy} is missing or ambiguous")


def open_document_summary(page: Any) -> None:
    """Click the left-nav "Document Summary" link from Policy Summary."""
    _control(page, "link", "Document Summary").click()

    def docs_visible() -> bool:
        try:
            _find_table_by_headers(page, _DOC_FIELDS, required=("description",))
            return True
        except IntakeHold:
            return False

    if not _wait_for(docs_visible):
        raise IntakeHold("Document Summary table is missing or ambiguous")


def extract_documents(page: Any) -> tuple[FoSDocument, ...]:
    """Parse the Document Summary table into candidate documents."""
    table, indexes = _find_table_by_headers(page, _DOC_FIELDS, required=("description",))
    documents: list[FoSDocument] = []
    for row in _table_body_rows(table):
        description = _cell_text(row, indexes["description"])
        if not description:
            continue
        raw_date = _cell_text(row, indexes["doc_date"]) if "doc_date" in indexes else ""
        try:
            doc_date = parse_carrier_date(raw_date) if raw_date else None
        except IntakeHold:
            doc_date = None
        notice_key = classify_notice(description)
        view: Any | None = None
        if notice_key is not None:
            try:
                links = row.get_by_role("link", name="View", exact=False).all()
            except Exception:
                links = []
            exact = [link for link in links if _norm(link.inner_text()).casefold() == "view"]
            candidates = exact or links
            if len(candidates) == 1:
                view = candidates[0]
            elif candidates:
                raise IntakeHold(f"Document View link for {description!r} is missing or ambiguous")
        documents.append(FoSDocument(description=description, doc_date=doc_date, notice_key=notice_key, view=view))
    return tuple(documents)


def select_target_document(documents: tuple[FoSDocument, ...]) -> FoSDocument | None:
    """Most recent in-scope document with a usable date and View link."""
    candidates = [
        doc
        for doc in documents
        if doc.notice_key is not None and doc.doc_date is not None and doc.view is not None
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda doc: (doc.doc_date, -_NOTICE_PRIORITY[doc.notice_key]), reverse=True)  # type: ignore[arg-type]
    return candidates[0]


def _read_viewer_tab_pdf(page: Any) -> bytes | None:
    """Read PDF bytes when View opened the Chrome viewer in a new tab."""
    try:
        context = page.context
        pages = list(context.pages)
    except Exception:
        return None
    me = page
    for tab in pages:
        if tab is me:
            continue
        url = str(getattr(tab, "url", "") or "")
        if not url:
            continue
        lowered = url.lower()
        if lowered.endswith(".pdf") or lowered.startswith("blob:"):
            try:
                response = context.request.get(url, timeout=DOWNLOAD_TIMEOUT_MS)
            except Exception:
                continue
            try:
                body = bytes(response.body())
            except Exception:
                continue
            if _is_pdf(body):
                try:
                    tab.close()
                except Exception:
                    pass
                return body
    return None


def download_view_pdf(page: Any, view: Any) -> bytes:
    """Click a Document Summary View link and return the original PDF bytes."""
    try:
        with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as download_info:
            view.click()
    except TimeoutError:
        pdf = _read_viewer_tab_pdf(page)
        if pdf is None:
            raise IntakeHold("Document View did not produce a PDF") from None
        return pdf
    except Exception as exc:
        raise IntakeHold("Document View did not produce a PDF") from exc
    download = download_info.value
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "view.pdf"
        try:
            download.save_as(str(path))
            content = path.read_bytes()
        except Exception as exc:
            raise IntakeHold("Document View download is missing or ambiguous") from exc
    return content


def open_finys_from_portal(portal_page: Any) -> Any:
    """Click FOS PORTAL on the farmersofsalem.com tab; return the new Finys tab.

    Raises IntakeHold unless exactly one new tab opens on fos.finys.com.
    """
    host = (urllib.parse.urlsplit(str(getattr(portal_page, "url", "") or "")).hostname or "").lower()
    if PORTAL_HOST not in host:
        raise IntakeHold("Farmers of Salem portal tab is missing or ambiguous")
    link = _control(portal_page, "link", FOS_PORTAL_LINK)
    try:
        with portal_page.expect_popup(timeout=DOWNLOAD_TIMEOUT_MS) as popup_info:
            link.click()
        finys_page = popup_info.value
    except Exception as exc:
        raise IntakeHold("FOS PORTAL did not open the Finys tab") from exc
    if finys_page is None:
        raise IntakeHold("FOS PORTAL did not open the Finys tab")
    require_finys_url(str(getattr(finys_page, "url", "") or ""))
    return finys_page

# --- Browser page object ----------------------------------------------------


class FinysFoSBrowser:
    """Playwright page object for an already-signed-in Finys tab."""

    def __init__(self, page: Any):
        self.page = page
        self._tasks_url: str | None = None

    def load_pending_items(self) -> tuple[PendingItem, ...]:
        require_finys_url(str(getattr(self.page, "url", "") or ""))
        items = extract_pending_items(self.page)
        try:
            self._tasks_url = str(getattr(self.page, "url", "") or "") or None
        except Exception:
            self._tasks_url = None
        return items

    def screenshot_pending_items(self) -> bytes:
        try:
            data = bytes(self.page.screenshot(full_page=True, type="png"))
        except Exception as exc:
            raise IntakeHold("Pending items screenshot is missing or not a PNG") from exc
        if data[:8] != _PNG_MAGIC:
            raise IntakeHold("Pending items screenshot is missing or not a PNG")
        return data

    def open_policy(self, policy_number: str) -> None:
        require_finys_url(str(getattr(self.page, "url", "") or ""))
        search_policy(self.page, policy_number)

    def open_document_summary(self) -> None:
        open_document_summary(self.page)

    def list_documents(self) -> tuple[FoSDocument, ...]:
        return extract_documents(self.page)

    def download_target(self, document: FoSDocument) -> bytes:
        if document.view is None:
            raise IntakeHold("Document View link is missing or ambiguous")
        return download_view_pdf(self.page, document.view)

    def return_to_pending_items(self) -> None:
        if self._tasks_url:
            try:
                self.page.goto(self._tasks_url)
            except Exception as exc:
                raise IntakeHold("Could not return to the pending items list") from exc
        else:
            _control(self.page, "link", "My Open Tasks").click()
        require_finys_url(str(getattr(self.page, "url", "") or ""))
        # The list must still be readable after navigation.
        extract_pending_items(self.page)


# --- Local delivery ledger --------------------------------------------------


class LocalDeliveryLedger:
    """Private named-PDF ledger. A conflicting file is kept and the pull holds."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def ensure_private(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        if self.root.is_symlink() or not self.root.is_dir() or self.root.stat().st_mode & 0o077:
            raise IntakeHold("Farmers of Salem output directory must be private (0700)")

    def _load(self) -> dict[str, Any]:
        self.ensure_private()
        path = self.root / LEDGER_NAME
        if not path.exists():
            return {"items": {}}
        try:
            data = json.loads(path.read_text())
        except Exception as exc:
            raise IntakeHold("Farmers of Salem pull ledger is missing or ambiguous") from exc
        if not isinstance(data, dict) or not isinstance(data.get("items"), dict):
            raise IntakeHold("Farmers of Salem pull ledger is missing or ambiguous")
        return data

    def _write(self, data: dict[str, Any]) -> None:
        path = self.root / LEDGER_NAME
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def _write_new(self, path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)

    def _basename(self, filename: str) -> str:
        cleaned = _safe_filename_part(filename)
        if cleaned in {LEDGER_NAME} or cleaned.endswith(" HELD.pdf"):
            raise IntakeHold("Notice filename is missing or ambiguous")
        return cleaned

    def date_dir(self, day: date) -> Path:
        self.ensure_private()
        folder = self.root / day.isoformat()
        if folder.is_symlink():
            raise IntakeHold("Farmers of Salem output directory must be private (0700)")
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(folder, 0o700)
        return folder

    def pdf_path(self, day: date, filename: str) -> Path:
        return self.date_dir(day) / self._basename(filename)

    def delivery_status(self, *, document_id: str, filename: str, processed_on: date) -> bool:
        """True when this exact document was already delivered (dedup)."""
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
            raise IntakeHold("Existing Farmers of Salem file conflicts with the pull ledger")
        return True

    def record(self, source: SourceItem, *, processed_on: date) -> Path:
        self.ensure_private()
        path = self.pdf_path(processed_on, source.filename)
        if path.exists():
            raise IntakeHold("Existing Farmers of Salem file conflicts with the pull ledger")
        digest = hashlib.sha256(source.content).hexdigest()
        self._write_new(path, source.content)
        data = self._load()
        if source.source_id in data["items"]:
            raise IntakeHold("Existing Farmers of Salem file conflicts with the pull ledger")
        data["items"][source.source_id] = {
            "filename": source.filename,
            "sha256": digest,
            "bytes": len(source.content),
            "processed_date": processed_on.isoformat(),
        }
        self._write(data)
        return path


# --- Pull orchestration -----------------------------------------------------


def _held_row(item: PendingItem, reason: str) -> dict[str, Any]:
    return {
        "policy_number": item.policy_number,
        "insured_name": item.insured_name,
        "product": item.product,
        "due_date": item.due_on.isoformat(),
        "reason": reason,
    }


def run_pull(
    browser: FinysFoSBrowser,
    ledger: LocalDeliveryLedger,
    archive: SourceArchive,
    *,
    as_of: date,
) -> dict[str, Any]:
    """Pull the most recent target notice for each Finys pending item.

    Returns a receipt dict with status/count/downloaded/held/skipped. Raises
    PullHeld on a hard failure (bad download, ledger conflict); per-policy
    gaps are recorded in "held" and the pull continues.
    """
    require_carrier_pull(CARRIER)
    refuse_production_host()
    if not isinstance(as_of, date):
        raise IntakeHold("Pending items as-of date is missing or ambiguous")

    items = browser.load_pending_items()
    png = browser.screenshot_pending_items()
    shot_name = f"pending-items-{as_of.isoformat()}.png"
    shot_path = ledger.date_dir(as_of) / shot_name
    if not shot_path.exists():
        ledger.ensure_private()
        shot_path.write_bytes(png)
        os.chmod(shot_path, 0o600)

    downloaded: list[dict[str, Any]] = []
    held: list[dict[str, Any]] = []
    skipped: list[str] = []

    def fail(reason: str) -> None:
        raise PullHeld(
            reason,
            details={
                "status": "HELD",
                "reason": reason,
                "carrier": CARRIER,
                "as_of": as_of.isoformat(),
                "downloaded": downloaded,
                "held": held,
                "skipped": skipped,
                "screenshot": shot_name,
            },
        )

    for item in items:
        browser.return_to_pending_items()
        browser.open_policy(item.policy_number)
        browser.open_document_summary()
        documents = browser.list_documents()
        target = select_target_document(documents)
        if target is None or target.doc_date is None or target.notice_key is None:
            held.append(_held_row(item, "no dated Intent to Cancel / cancellation / underwriting / billing document on Document Summary"))
            continue
        document_id = notice_document_id(item.policy_number, target.doc_date, target.notice_key)
        filename = notice_filename(item.policy_number, target.notice_key)
        try:
            already = ledger.delivery_status(
                document_id=document_id, filename=filename, processed_on=target.doc_date
            )
        except IntakeHold as exc:
            held.append(_held_row(item, str(exc)))
            fail(str(exc))
        if already:
            skipped.append(document_id)
            continue
        try:
            content = browser.download_target(target)
        except IntakeHold as exc:
            held.append(_held_row(item, str(exc)))
            continue
        if not _is_pdf(content):
            held.append(_held_row(item, "Document View download is not a PDF"))
            fail("Document View download is not a PDF")
        source = SourceItem(
            system=PROCESS,
            source_account=FINYS_HOST,
            source_id=document_id,
            source_url=f"https://{FINYS_HOST}/#policy={item.policy_number}",
            received_at=_received_at(as_of),
            filename=filename,
            content=content,
        )
        source.validate()
        try:
            saved = ledger.record(source, processed_on=target.doc_date)
            archive.preserve(source)
        except IntakeHold as exc:
            held.append(_held_row(item, str(exc)))
            fail(str(exc))
        downloaded.append(
            {
                "document_id": document_id,
                "filename": filename,
                "sha256": source.digest,
                "bytes": len(content),
                "policy_number": item.policy_number,
                "insured_name": item.insured_name,
                "product": item.product,
                "due_date": item.due_on.isoformat(),
                "doc_date": target.doc_date.isoformat(),
                "notice_type": target.notice_key,
                "description": target.description,
                "path": str(saved),
            }
        )

    return {
        "status": "PULLED",
        "carrier": CARRIER,
        "as_of": as_of.isoformat(),
        "count": len(downloaded),
        "downloaded": downloaded,
        "held": held,
        "skipped": skipped,
        "screenshot": shot_name,
    }


# --- Test-only CLI ----------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Test-only Farmers of Salem pending-cancellation pull.")
    parser.add_argument("--as-of", required=True, help="Pull date YYYY-MM-DD")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--cdp-url", default=DEFAULT_CDP_URL)
    parser.add_argument("--upload-drive", action="store_true", help="Refused: Drive upload is not wired.")
    return parser


def _select_finys_page(browser: Any) -> Any:
    found = []
    for context in browser.contexts:
        for page in context.pages:
            try:
                host = (urllib.parse.urlsplit(str(page.url or "")).hostname or "").lower()
            except Exception:
                continue
            if host == FINYS_HOST:
                found.append(page)
    if len(found) != 1:
        raise IntakeHold("Finys tab is missing or ambiguous")
    return found[0]


def main(
    argv: list[str] | None = None,
    *,
    browser_factory: Callable[[argparse.Namespace], Any] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    if args.upload_drive:
        print(json.dumps({"status": "REFUSED", "reason": DRIVE_UPLOAD_UNAVAILABLE}))
        return 2
    try:
        require_carrier_pull(CARRIER)
        refuse_production_host()
        try:
            as_of = date.fromisoformat(args.as_of)
        except ValueError as exc:
            raise IntakeHold("Pending items as-of date is missing or ambiguous") from exc
        output_root = Path(args.output_root)
        pack = output_root / as_of.isoformat()
        closer: Callable[[], None] | None = None
        if browser_factory is None:
            require_hermes_test_host()
            cdp_url = require_loopback_cdp(args.cdp_url)
            from playwright.sync_api import sync_playwright

            playwright = sync_playwright().start()

            def _close() -> None:
                playwright.stop()

            closer = _close
            playwright_browser = playwright.chromium.connect_over_cdp(cdp_url)
            browser = FinysFoSBrowser(_select_finys_page(playwright_browser))
        else:
            browser = browser_factory(args)
        ledger = LocalDeliveryLedger(pack)
        archive = SourceArchive(pack / "sources")
        receipt = run_pull(browser, ledger, archive, as_of=as_of)
        receipt["pack"] = str(pack)
        receipt["drive"] = {"status": "not_uploaded", "reason": DRIVE_UPLOAD_UNAVAILABLE}
        print(json.dumps(receipt, indent=2))
        return 0
    except PullHeld as exc:
        payload = {"ezlynx": "not_run", "pack": str(pack) if "pack" in dir() else None}
        payload.update(exc.details)
        print(json.dumps(payload, indent=2))
        return 3
    except IntakeHold as exc:
        print(json.dumps({"status": "HELD", "reason": str(exc), "ezlynx": "not_run"}))
        return 3
    finally:
        if closer is not None:
            closer()


if __name__ == "__main__":
    sys.exit(main())
