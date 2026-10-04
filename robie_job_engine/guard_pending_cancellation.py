"""Test-only Guard Pending Cancellation document pull.

The playbook opens an already signed-in Guard Agency Service Center tab
(gigezrate.guard.com), navigates Home -> Book of Business -> the
"Cancellations" tab, opens each pending policy's Policy Center, follows
Related Functions (or Quick Functions) -> "printable documents", and pulls
cancellation documents from the Policy Documents group.

CRITICAL QUIRK: Guard's document download links EMAIL the document instead
of downloading it. A live capture produced a 4KB .eml notification ("Your
GUARD Commercial Auto Policy" from the GUARD Automated Delivery System),
not the PDF. The worker clicks the document action, then observes the
outcome: a real PDF download, an in-browser PDF view (bytes captured from
the viewer response), or nothing at all (the document was emailed). An
emailed document is recorded HELD with reason "Guard emails this document
type; no direct download" — fail closed. A download that did not happen is
never claimed.

A missing or non-unique control raises IntakeHold. This module does not
log in, does not handle MFA (the live login uses 6-digit email MFA), does
not upload, note, task, or label in EZLynx, and does not register a timer.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import urllib.parse
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .intake_core import IntakeHold, SourceArchive, SourceItem

PROCESS = "guard"
SCOPE = "pending_cancellation"
GUARD_HOST = "gigezrate.guard.com"
GUARD_PUBLIC_HOST = "guard.com"
DEFAULT_CDP_URL = "http://127.0.0.1:9223"
DOWNLOAD_TIMEOUT_MS = 8000
LEDGER_NAME = "guard-cancellation-ledger.json"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_PDF_MAGIC = b"%PDF"
DEFAULT_OUTPUT_ROOT = Path(
    "/opt/streetsmart-hermes-test/robie-job-engine/data/artifacts/carrier-pull-qa/guard"
)
# Shared Drive "Robie Carrier Pull QA (Nicole)". The Guard child folder id is
# UNVERIFIED. Folder upload is TODO. --upload-drive fails closed and does not
# call Google.
CARRIER_QA_DRIVE_PARENT_ID = "1cLEpR-0T6KdiVjcdAr0qpGTO447MetI2"
DRIVE_QA_FOLDER_NAME = "Robie Carrier Pull QA (Nicole)/Guard"
DRIVE_UPLOAD_UNAVAILABLE = (
    "Drive upload of the Guard QA pack is not available; "
    "refusing to report the pack as uploaded"
)
HERMES_TEST_HOST = "hermes-test-01"
# Live policy numbers look like "PRAU716089" (4 letters + 6 digits), but the
# live Cancellations tab also shows "R2WC794141" (letter-digit prefix), so
# the lead block is a letter + 3 alphanumerics, then 6 digits.
_POLICY_NUMBER = re.compile(r"^[A-Z][A-Z0-9]{3}\d{6}$")
# Document rows carry Description, Form, Issued Date, Action columns.
_DOC_HEADERS = (
    ("description", frozenset({"description", "document", "document description"})),
    ("form", frozenset({"form", "form number", "form #"})),
    ("issued", frozenset({"issued date", "issue date", "date issued", "effective date"})),
)
_CANCELLATION_TERMS = frozenset({
    "cancellation", "cancel", "cancelled", "canceled",
    "notice of cancellation", "pending cancellation",
    "intent to cancel", "pre-cancellation",
})
# The .eml notification Guard sends instead of a download. A body matching
# these markers is proof the document was emailed, not downloaded.
_EML_MARKERS = (
    "guard automated delivery system",
    "automated delivery system",
)
# Groups on the printable-documents page, in display order.
_DOC_GROUPS = ("Policy Documents", "Miscellaneous Documents")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _is_pdf(content: bytes) -> bool:
    return bytes(content or b"")[:4] == _PDF_MAGIC


def _looks_like_eml_notification(content: bytes) -> bool:
    head = bytes(content or b"")[:4096].decode("utf-8", "replace").casefold()
    return any(marker in head for marker in _EML_MARKERS)


def parse_carrier_date(text: str) -> date:
    cleaned = _norm(text)
    for fmt in ("%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    raise IntakeHold(f"Guard date is missing or ambiguous: {cleaned!r}")


def require_policy_number(text: str) -> str:
    cleaned = _norm(text).upper().replace(" ", "")
    if not _POLICY_NUMBER.fullmatch(cleaned):
        raise IntakeHold(f"Guard policy number is missing or ambiguous: {text!r}")
    return cleaned


@dataclass(frozen=True)
class CancellationRow:
    policy_number: str
    insured_name: str
    cancel_date: date
    reason: str
    list_url: str

    @property
    def document_id(self) -> str:
        return f"guard:{self.policy_number}:{self.cancel_date.isoformat()}:cancellation"


@dataclass(frozen=True)
class GuardDocument:
    group: str
    description: str
    form: str
    issued: date
    policy_number: str

    @property
    def document_id(self) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", self.description.casefold()).strip("-")
        return f"guard:{self.policy_number}:{self.issued.isoformat()}:{slug}"

    @property
    def filename(self) -> str:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", self.description).strip("_")
        return f"{self.policy_number} {safe or 'document'} Guard.pdf"


def is_cancellation_document(description: str) -> bool:
    key = _norm(description).casefold()
    return any(term in key for term in _CANCELLATION_TERMS)


def parse_cancellations_grid(
    headers: tuple[str, ...], rows: tuple[tuple[str, ...], ...], *, list_url: str
) -> tuple[CancellationRow, ...]:
    """Parse the Cancellations tab grid into typed rows.

    Live columns (order may vary): policy link, insured name, cancel/effective
    date, reason/type (Underwriting, Billing, Audit). Header matching is by
    alias so column order is not load-bearing.
    """
    normed = [_norm(h).casefold() for h in headers]
    indexes: dict[str, int] = {}
    aliases = {
        "policy_number": {"policy", "policy number", "policy #", "pol #"},
        "insured_name": {"insured", "insured name", "named insured", "name"},
        "cancel_date": {"cancel date", "cancellation date", "effective date", "cancel effective date"},
        "reason": {"reason", "type", "cancel reason", "cancellation reason"},
    }
    for field, names in aliases.items():
        matches = [i for i, h in enumerate(normed) if h in names]
        if len(matches) != 1:
            raise IntakeHold("Guard Cancellations grid headers are missing or ambiguous")
        indexes[field] = matches[0]
    parsed: list[CancellationRow] = []
    for cells in rows:
        if len(cells) < len(headers):
            raise IntakeHold("Guard Cancellations grid row is missing or ambiguous")
        policy = require_policy_number(cells[indexes["policy_number"]])
        insured = _norm(cells[indexes["insured_name"]])
        if not insured:
            raise IntakeHold("Guard Cancellations grid insured name is missing or ambiguous")
        cancel = parse_carrier_date(cells[indexes["cancel_date"]])
        reason = _norm(cells[indexes["reason"]])
        parsed.append(CancellationRow(policy, insured, cancel, reason, list_url))
    if not parsed:
        raise IntakeHold("Guard Cancellations grid has no rows")
    return tuple(parsed)


def parse_document_list(
    groups: tuple[tuple[str, tuple[str, ...], tuple[tuple[str, ...], ...]], ...],
    *,
    policy_number: str,
) -> tuple[GuardDocument, ...]:
    """Parse the printable-documents page.

    ``groups`` is one entry per document group: (group title, headers, rows).
    Each row is (Description, Form, Issued Date, Action). The Action cell is
    the download/view control and is not parsed here.
    """
    docs: list[GuardDocument] = []
    seen_groups: set[str] = set()
    for title, headers, rows in groups:
        group = _norm(title)
        if not group:
            raise IntakeHold("Guard document group title is missing or ambiguous")
        seen_groups.add(group.casefold())
        normed = [_norm(h).casefold() for h in headers]
        indexes: dict[str, int] = {}
        for field, names in _DOC_HEADERS:
            matches = [i for i, h in enumerate(normed) if h in names]
            if len(matches) != 1:
                raise IntakeHold(
                    f"Guard document list headers are missing or ambiguous in group {group!r}"
                )
            indexes[field] = matches[0]
        for cells in rows:
            if len(cells) < len(headers):
                raise IntakeHold("Guard document row is missing or ambiguous")
            description = _norm(cells[indexes["description"]])
            if not description:
                raise IntakeHold("Guard document description is missing or ambiguous")
            issued = parse_carrier_date(cells[indexes["issued"]])
            docs.append(
                GuardDocument(
                    group=group,
                    description=description,
                    form=_norm(cells[indexes["form"]]),
                    issued=issued,
                    policy_number=policy_number,
                )
            )
    if not docs:
        raise IntakeHold("Guard printable documents list is empty")
    return tuple(docs)


@dataclass(frozen=True)
class DocumentOpenObservation:
    """What happened after clicking a document's download/view action."""

    downloads: tuple[bytes, ...]
    viewer_pdfs: tuple[bytes, ...]
    emailed: bool


def refuse_production_host() -> None:
    """Refuse a hermes-poc host unless Guard Production filing is enabled.

    The kill switch and the carrier allowlist are the only way through.
    Every other Guard check stays in place.
    """
    from .document_retrieval_filing import live_filing_decision

    if live_filing_decision(os.environ, socket.gethostname(), "guard").allowed:
        return
    raw = f"{socket.gethostname()} {socket.getfqdn()}".lower()
    labels = [label for label in re.split(r"[\s.]+", raw) if label]
    if any(label == "hermes-poc-01" or label.startswith("hermes-poc") for label in labels):
        raise IntakeHold("Guard document pull refuses Production host hermes-poc-01")


def require_hermes_test_host() -> None:
    """Live packs are produced on hermes-test-01. Fixture runs inject a browser.

    hermes-poc-01 is accepted only when Guard Production filing is enabled.
    """
    from .document_retrieval_filing import live_filing_decision

    if live_filing_decision(os.environ, socket.gethostname(), "guard").allowed:
        return
    raw = f"{socket.gethostname()} {socket.getfqdn()}".lower()
    labels = [label for label in re.split(r"[\s.]+", raw) if label]
    if HERMES_TEST_HOST not in labels:
        raise IntakeHold("Guard QA pack must be produced on hermes-test-01")


def require_guard_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise IntakeHold("Guard Agency Service Center URL is missing or ambiguous")
    if host != GUARD_HOST:
        raise IntakeHold("Guard Agency Service Center URL is missing or ambiguous")
    if "login" in parsed.path.lower() or "auth" in parsed.path.lower():
        # The /auth login screen means the session expired; the worker does
        # not perform login or MFA.
        raise IntakeHold("Guard session is not authenticated")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def _unique_control(page: Any, role: str, name: str, *, exact: bool = True) -> Any:
    locator = page.get_by_role(role, name=name, exact=exact)
    try:
        count = int(locator.count())
    except Exception:
        raise IntakeHold(f"Guard control {name!r} is missing or ambiguous")
    if count != 1:
        raise IntakeHold(f"Guard control {name!r} is missing or ambiguous")
    return locator


def _read_text(node: Any) -> str:
    getter = getattr(node, "inner_text", None)
    if callable(getter):
        try:
            return str(getter())
        except Exception:
            return ""
    return str(getattr(node, "text", "") or "")


class PlaywrightGuardBrowser:
    """Page object for the Guard Agency Service Center cancellation flow."""

    def __init__(self, page: Any):
        self.page = page
        self._list_url = ""

    # -- navigation -----------------------------------------------------
    def open_cancellations(self) -> None:
        """Home -> Book of Business -> Cancellations tab."""
        page = self.page
        require_guard_url(str(getattr(page, "url", "") or ""))
        _unique_control(page, "link", "Book of Business").click()
        _unique_control(page, "tab", "Cancellations").click()
        page.wait_for_selector("table", timeout=15000)
        self._list_url = require_guard_url(str(getattr(page, "url", "") or ""))

    def load_cancellations(self) -> tuple[CancellationRow, ...]:
        page = self.page
        tables = page.locator("table")
        if int(tables.count()) != 1:
            raise IntakeHold("Guard Cancellations table is missing or ambiguous")
        table = tables.first if hasattr(tables, "first") else tables
        header_nodes = table.locator("thead th").all()
        if not header_nodes:
            raise IntakeHold("Guard Cancellations table headers are missing or ambiguous")
        headers = tuple(_norm(_read_text(node)) for node in header_nodes)
        row_nodes = table.locator("tbody tr").all()
        rows = tuple(
            tuple(_norm(_read_text(cell)) for cell in row.locator("td").all())
            for row in row_nodes
        )
        return parse_cancellations_grid(headers, rows, list_url=self._list_url)

    def open_policy(self, policy_number: str) -> None:
        """Click the policy link on the Cancellations grid -> Policy Center."""
        require_policy_number(policy_number)
        link = _unique_control(self.page, "link", policy_number, exact=True)
        link.click()
        self.page.wait_for_selector("text=Policy Center", timeout=15000)

    def open_printable_documents(self) -> None:
        """Related Functions (or Quick Functions) -> "printable documents"."""
        page = self.page
        for role in ("link", "button"):
            try:
                control = _unique_control(page, role, "printable documents", exact=False)
            except IntakeHold:
                continue
            control.click()
            page.wait_for_selector("text=Policy Documents", timeout=15000)
            return
        raise IntakeHold("Guard printable documents control is missing or ambiguous")

    def list_documents(self, policy_number: str) -> tuple[GuardDocument, ...]:
        """Parse the Policy Documents / Miscellaneous Documents groups."""
        page = self.page
        groups: list[tuple[str, tuple[str, ...], tuple[tuple[str, ...], ...]]] = []
        for title in _DOC_GROUPS:
            heading = page.get_by_role("heading", name=title, exact=True)
            try:
                if int(heading.count()) != 1:
                    continue
            except Exception:
                raise IntakeHold(f"Guard document group {title!r} is missing or ambiguous")
            table = heading.locator("xpath=following::table[1]")
            try:
                if int(table.count()) != 1:
                    raise IntakeHold(
                        f"Guard document table for group {title!r} is missing or ambiguous"
                    )
            except IntakeHold:
                raise
            except Exception:
                raise IntakeHold(
                    f"Guard document table for group {title!r} is missing or ambiguous"
                )
            headers = tuple(
                _norm(_read_text(node)) for node in table.locator("thead th").all()
            )
            rows = tuple(
                tuple(_norm(_read_text(cell)) for cell in row.locator("td").all())
                for row in table.locator("tbody tr").all()
            )
            groups.append((title, headers, rows))
        if not groups:
            raise IntakeHold("Guard printable documents groups are missing or ambiguous")
        return parse_document_list(tuple(groups), policy_number=policy_number)

    # -- document open observation --------------------------------------
    def open_document(self, doc: GuardDocument) -> DocumentOpenObservation:
        """Click the document's Action control and observe the outcome.

        Returns downloads (real file bytes), viewer_pdfs (in-browser render),
        or emailed=True when Guard sends the document by email instead of
        downloading it. Never guesses.
        """
        page = self.page
        row = page.locator("tr", has_text=doc.description)
        try:
            if int(row.count()) != 1:
                raise IntakeHold(
                    f"Guard document row {doc.description!r} is missing or ambiguous"
                )
        except IntakeHold:
            raise
        except Exception:
            raise IntakeHold(
                f"Guard document row {doc.description!r} is missing or ambiguous"
            )
        action = row.get_by_role("link", name="Download", exact=False)
        try:
            if int(action.count()) < 1:
                action = row.get_by_role("button", name="Download", exact=False)
            if int(action.count()) != 1:
                raise IntakeHold(
                    f"Guard document action for {doc.description!r} is missing or ambiguous"
                )
        except IntakeHold:
            raise
        except Exception:
            raise IntakeHold(
                f"Guard document action for {doc.description!r} is missing or ambiguous"
            )
        return collect_document_observation(page, action.first.click)

    def return_to_cancellations(self) -> None:
        self.page.goto(self._list_url, wait_until="domcontentloaded")
        self.page.wait_for_selector("table", timeout=15000)

    def screenshot_cancellations(self) -> bytes:
        data = self.page.screenshot(full_page=True, type="png")
        if not bytes(data or b"")[:8] == _PNG_MAGIC:
            raise IntakeHold("Guard Cancellations screenshot is missing or not a PNG")
        return bytes(data)


def collect_document_observation(page: Any, click_action: Callable[[], None]) -> DocumentOpenObservation:
    """Click a document action and classify what actually happened.

    - A download event with PDF bytes -> downloads.
    - A new viewer tab rendering a PDF -> viewer_pdfs (bytes read back).
    - Neither (Guard emailed the document) -> emailed=True.
    A non-PDF download raises IntakeHold: the wrong bytes are never kept.
    """
    downloads: list[bytes] = []
    viewer_pdfs: list[bytes] = []
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
        except Exception as exc:
            if type(exc).__name__ == "TimeoutError" or "timeout" in type(exc).__name__.lower():
                content = b""
            else:
                raise
        if content:
            if _looks_like_eml_notification(content):
                return DocumentOpenObservation((), (), True)
            if not _is_pdf(content):
                raise IntakeHold("Guard document download is not a PDF")
            downloads.append(content)
    finally:
        if context is not None and hasattr(context, "remove_listener"):
            try:
                context.remove_listener("page", _on_page)
            except Exception:
                pass
    for new_page in opened:
        pdfs = read_playwright_pdf_view(new_page)
        viewer_pdfs.extend(pdfs)
    if not downloads and not viewer_pdfs:
        # No download and no viewer: Guard emailed the document (the live
        # .eml quirk). Fail closed — do not claim a download.
        return DocumentOpenObservation((), (), True)
    return DocumentOpenObservation(tuple(downloads), tuple(viewer_pdfs), False)


def read_playwright_pdf_view(page: Any) -> tuple[bytes, ...]:
    """Read PDF bytes rendered in a viewer tab, without navigating away."""
    url = str(getattr(page, "url", "") or "")
    if url.startswith("blob:"):
        try:
            encoded = page.evaluate(
                "(url) => fetch(url).then(r => r.arrayBuffer()).then(b => btoa(String.fromCharCode(...new Uint8Array(b))))",
                url,
            )
        except Exception:
            return ()
        import base64 as _b64

        try:
            raw = _b64.b64decode(str(encoded or ""))
        except Exception:
            return ()
        return (raw,) if _is_pdf(raw) else ()
    request = getattr(getattr(page, "context", None), "request", None)
    getter = getattr(request, "get", None)
    if callable(getter) and url.lower().endswith(".pdf"):
        try:
            response = getter(url, timeout=15000)
            body = response.body() if callable(getattr(response, "body", None)) else b""
        except Exception:
            return ()
        return (bytes(body),) if _is_pdf(bytes(body)) else ()
    return ()


class GuardDeliveryLedger:
    """Private named-PDF ledger. A conflicting file is kept and the pull holds."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def ensure_private(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        if self.root.is_symlink() or not self.root.is_dir() or self.root.stat().st_mode & 0o077:
            raise IntakeHold("Guard output directory must be private (0700)")

    def _load(self) -> dict[str, Any]:
        self.ensure_private()
        path = self.root / LEDGER_NAME
        if not path.exists():
            return {"items": {}}
        try:
            data = json.loads(path.read_text())
        except Exception:
            raise IntakeHold("Guard delivery ledger is missing or ambiguous")
        if not isinstance(data, dict) or not isinstance(data.get("items"), dict):
            raise IntakeHold("Guard delivery ledger is missing or ambiguous")
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
            raise IntakeHold("Guard filename is missing or ambiguous")
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
            raise IntakeHold("Existing Guard file conflicts with the pull ledger")
        return True

    def record(self, source: SourceItem, *, issued_on: date) -> Path:
        self.ensure_private()
        if source.filename == LEDGER_NAME:
            raise IntakeHold("Guard filename is missing or ambiguous")
        path = self.pdf_path(issued_on, source.filename)
        if path.exists():
            raise IntakeHold("Existing Guard file conflicts with the pull ledger")
        digest = hashlib.sha256(source.content).hexdigest()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(source.content)
        data = self._load()
        if source.source_id in data["items"]:
            raise IntakeHold("Existing Guard file conflicts with the pull ledger")
        data["items"][source.source_id] = {
            "filename": source.filename,
            "sha256": digest,
            "bytes": len(source.content),
            "issued_date": issued_on.isoformat(),
        }
        self._write(data)
        return path

    def save_screenshot(self, day: date, png: bytes) -> Path:
        if bytes(png or b"")[:8] != _PNG_MAGIC:
            raise IntakeHold("Guard Cancellations screenshot is missing or not a PNG")
        path = self.pdf_path(day, f"guard-cancellations-{day.isoformat()}.png")
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
        "reason": row.reason,
        "document_id": row.document_id,
        "outcome": outcome,
    }
    if reason:
        payload["hold_reason"] = reason
    if filename:
        payload["filename"] = filename
    return payload


def run_pull(
    browser: PlaywrightGuardBrowser,
    ledger: GuardDeliveryLedger,
    archive: SourceArchive,
    *,
    as_of: date,
) -> dict[str, Any]:
    """Pull Guard cancellation documents for every policy on the Cancellations tab.

    For each pending policy: open Policy Center -> printable documents ->
    target cancellation documents in the Policy Documents group. A real PDF
    download or in-browser PDF view is saved; a document Guard emails
    instead is recorded HELD ("Guard emails this document type; no direct
    download") and never claimed as downloaded.
    """
    from .document_retrieval_filing import require_carrier_pull

    require_carrier_pull("guard")
    refuse_production_host()
    if not isinstance(as_of, date):
        raise IntakeHold("Guard as-of date is missing or ambiguous")

    downloaded: list[dict[str, Any]] = []
    held: list[dict[str, Any]] = []
    skipped: list[str] = []
    targeted: list[str] = []
    rows_payload: list[dict[str, Any]] = []

    browser.open_cancellations()
    png = browser.screenshot_cancellations()
    ledger.save_screenshot(as_of, png)
    rows = browser.load_cancellations()

    for row in rows:
        browser.open_policy(row.policy_number)
        try:
            browser.open_printable_documents()
            docs = browser.list_documents(row.policy_number)
        except IntakeHold as exc:
            held.append(_row_payload(row, outcome="HELD", reason=str(exc)))
            browser.return_to_cancellations()
            continue
        targets = [
            doc for doc in docs
            if doc.group.casefold() == "policy documents" and is_cancellation_document(doc.description)
        ]
        if len(targets) != 1:
            held.append(_row_payload(
                row, outcome="HELD",
                reason=(
                    f"Guard policy {row.policy_number} cancellation document "
                    f"is missing or ambiguous (found {len(targets)})"
                ),
            ))
            browser.return_to_cancellations()
            continue
        doc = targets[0]
        try:
            if ledger.delivery_status(
                document_id=doc.document_id, filename=doc.filename, issued_on=doc.issued
            ):
                skipped.append(doc.document_id)
                targeted.append(doc.document_id)
                rows_payload.append(_row_payload(
                    row, outcome="ALREADY_DELIVERED", filename=doc.filename
                ))
                browser.return_to_cancellations()
                continue
        except IntakeHold as exc:
            held.append(_row_payload(row, outcome="HELD", reason=str(exc)))
            browser.return_to_cancellations()
            continue
        observation = browser.open_document(doc)
        if observation.emailed and not observation.downloads and not observation.viewer_pdfs:
            held.append(_row_payload(
                row, outcome="HELD",
                reason=(
                    f"Guard emails this document type ({doc.description}); "
                    "no direct download — not claimed as downloaded"
                ),
                filename=doc.filename,
            ))
            browser.return_to_cancellations()
            continue
        content = (
            observation.downloads[0] if observation.downloads else observation.viewer_pdfs[0]
        )
        if len(observation.downloads) + len(observation.viewer_pdfs) != 1:
            held.append(_row_payload(
                row, outcome="HELD",
                reason=f"Guard document {doc.description!r} open is missing or ambiguous",
            ))
            browser.return_to_cancellations()
            continue
        source = SourceItem(
            system=PROCESS,
            source_account=GUARD_HOST,
            source_id=doc.document_id,
            source_url=f"{row.list_url}#policy={row.policy_number}",
            received_at=_received_at(as_of),
            filename=doc.filename,
            content=content,
        )
        source.validate()
        try:
            saved = ledger.record(source, issued_on=doc.issued)
            archive.preserve(source)
        except IntakeHold as exc:
            held.append(_row_payload(row, outcome="HELD", reason=str(exc)))
            browser.return_to_cancellations()
            continue
        downloaded.append({
            "document_id": doc.document_id,
            "filename": doc.filename,
            "sha256": source.digest,
            "bytes": len(content),
            "policy_number": row.policy_number,
            "insured_name": row.insured_name,
            "cancel_date": row.cancel_date.isoformat(),
            "issued_date": doc.issued.isoformat(),
            "description": doc.description,
            "path": str(saved),
        })
        targeted.append(doc.document_id)
        rows_payload.append(_row_payload(row, outcome="PULLED", filename=doc.filename))
        browser.return_to_cancellations()

    return {
        "status": "PULLED",
        "scope": SCOPE,
        "process": PROCESS,
        "as_of": as_of.isoformat(),
        "cancellation_count": len(rows),
        "targeted": len(targeted),
        "count": len(downloaded),
        "downloaded": downloaded,
        "skipped_already_delivered": skipped,
        "held": held,
        "rows": rows_payload,
        "ezlynx": "not_run",
    }


def select_guard_page(pages: list[Any]) -> Any:
    """Use the single Guard Agency Service Center tab."""
    matches = [
        page for page in pages
        if (urllib.parse.urlsplit(str(getattr(page, "url", "") or "")).hostname or "").lower() == GUARD_HOST
    ]
    if len(matches) != 1:
        raise IntakeHold("Expected exactly one Guard Agency Service Center tab")
    return matches[0]


def connect_cdp_browser(cdp_url: str | None) -> tuple[PlaywrightGuardBrowser, Callable[[], None]]:
    """Attach to the local carrier Chrome. Exactly one Guard tab."""
    from .document_retrieval_filing import require_carrier_pull

    require_carrier_pull("guard")
    refuse_production_host()
    require_hermes_test_host()
    url = (cdp_url or os.environ.get("ROBIE_BROWSER_CDP_URL") or DEFAULT_CDP_URL).strip()
    parsed = urllib.parse.urlsplit(url)
    if parsed.username or parsed.password or parsed.scheme not in {"http", "https"}:
        raise IntakeHold("Guard browser attach must use the local Test CDP endpoint")
    if (parsed.hostname or "").lower() not in {"127.0.0.1", "localhost"}:
        raise IntakeHold("Guard browser attach must use the local Test CDP endpoint")
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(url)
        pages = [page for context in browser.contexts for page in context.pages]
        return PlaywrightGuardBrowser(select_guard_page(pages)), playwright.stop
    except Exception:
        playwright.stop()
        raise


def qa_pack_dir(output_root: Path, as_of: date) -> Path:
    folder = output_root / "Guard" / as_of.isoformat()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(folder, 0o700)
    return folder


def main(argv: list[str] | None = None, *, browser_factory: Callable[[Any], Any] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Test-only Guard cancellation document pull")
    parser.add_argument("--as-of", default=date.today().isoformat())
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--cdp-url", default=None)
    parser.add_argument("--upload-drive", action="store_true")
    args = parser.parse_args(argv)
    if args.upload_drive:
        raise IntakeHold(DRIVE_UPLOAD_UNAVAILABLE)
    as_of = parse_carrier_date(args.as_of)
    output_root = qa_pack_dir(Path(args.output_root), as_of)
    ledger = GuardDeliveryLedger(output_root)
    archive = SourceArchive(output_root / "sources")
    if browser_factory is not None:
        browser = browser_factory(args)
        receipt = run_pull(browser, ledger, archive, as_of=as_of)
    else:
        browser, close = connect_cdp_browser(args.cdp_url)
        try:
            receipt = run_pull(browser, ledger, archive, as_of=as_of)
        finally:
            close()
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0
