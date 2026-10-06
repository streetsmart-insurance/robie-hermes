"""Test-only Utica First Pending Cancellation NOC list and download.

The playbook opens an already signed-in UFIRST Now tab
(ufirstnow.uticafirst.com, Okta via login.uticafirst.com) and works the
cancellation activity that lives in the POLICY TRANSACTIONS tab
("POLICY | TRANSACTION LIST"). Utica First has no separate pending
cancellation queue: the worker selects the "All" radio, filters TRANSACTION
TYPE to the cancellation family, clicks FILTER LIST, and then for each
policy carrying a cancellation-family transaction it opens the policy
("POLICY | CURRENT SUMMARY"), switches to the DOCUMENTS tab, finds the
cancellation notice document, and captures the PDF.

Document path (verified live 2026-10-02): click the policy number link on
the POLICIES grid -> "POLICY | CURRENT SUMMARY" -> DOCUMENTS tab (green
banner "Following is a list of documents for this policy.") -> document grid
with columns ID, NAME, TYPE, SOURCE, RENDERING STATUS -> double-click the
document name -> document viewer overlay opens with the PDF.

EMBEDDED-VIEWER QUIRK: the PDF renders in Chrome's viewer inside an embedded
frame. Sandbox automation cannot click the viewer's Download button
(frame-origin restriction, same as GEICO). The worker captures the PDF bytes
from the viewer overlay's underlying document response (the viewer loads a
real PDF document URL) and FAILS CLOSED with IntakeHold when only the
framed viewer is reachable with no byte access. A download is never claimed
unless bytes were actually captured and start with the PDF magic.

A missing or non-unique control raises IntakeHold. This module does not log
in, does not upload, note, task, or label in EZLynx, and does not register a
timer.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .intake_core import IntakeHold, SourceArchive, SourceItem


UTICAFIRST_SOURCE_ACCOUNT = "uticafirst"
LEDGER_NAME = "uticafirst-noc-ledger.json"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_PDF_MAGIC = b"%PDF-"
DEFAULT_QA_ROOT = Path(
    "/opt/streetsmart-hermes-test/robie-job-engine/data/artifacts/carrier-pull-qa/uticafirst"
)
# Shared Drive "Robie Carrier Pull QA (Nicole)". The Utica First child folder
# id is UNVERIFIED. Folder upload is TODO. --upload-drive fails closed.
DRIVE_QA_PARENT_ID = "1cLEpR-0T6KdiVjcdAr0qpGTO447MetI2"
DRIVE_QA_FOLDER_NAME = "Robie Carrier Pull QA (Nicole)/UticaFirst"

# Verified live 2026-10-02: policy ART3001958910 (Sunflower Maid Services LLC).
# Format is 3 letters + 10 digits.
_POLICY_NUMBER = re.compile(r"^[A-Z]{3}\d{10}$")
# The viewer overlay loads a real document URL for the PDF.
_REMOTE_PDF = re.compile(r"https?://[^\s\"'<>]+?\.pdf(?:\?[^\s\"'<>]*)?", re.IGNORECASE)

# TRANSACTION TYPE values seen live on the POLICY TRANSACTIONS tab.
# The cancellation family is what Nicole pulls. Reinstatement, Rescind Pending
# Cancellation, and Renewal are reversals/renewals and are NOT pulled.
_CANCELLATION_FAMILY = frozenset({
    "cancellation",
    "cancellation - insured",
    "non-renewal",
    "pending cancellation(noc)",
    "pending cancellation (noc)",
    "intent to non-renew",
    "prerenewal notice",
    "pre-renewal notice",
})
_NON_CANCELLATION_TYPES = frozenset({
    "reinstatement",
    "rescind pending cancellation",
    "renewal",
})

_TRANSACTION_HEADER_FIELDS = (
    ("policy_number", frozenset({"policy number", "policy", "policy #"})),
    ("insured_name", frozenset({"insured", "insured name", "named insured"})),
    ("transaction_type", frozenset({"transaction type", "type", "transaction"})),
    ("transaction_date", frozenset({
        "transaction date", "date", "effective date", "processed date",
    })),
    ("status", frozenset({"status"})),
)

_DOCUMENT_HEADER_FIELDS = (
    ("document_id", frozenset({"id", "document id"})),
    ("name", frozenset({"name", "document name"})),
    ("doc_type", frozenset({"type", "document type"})),
    ("source", frozenset({"source"})),
    ("rendering_status", frozenset({"rendering status"})),
)

# Document names that identify a cancellation notice on the DOCUMENTS tab.
_CANCELLATION_DOC_LABELS = frozenset({
    "cancellation",
    "notice of cancellation",
    "cancellation notice",
    "noc",
    "pending cancellation",
    "non-renewal",
    "nonrenewal",
    "intent to non-renew",
})

_HOME_HOSTS = ("uticafirst.com",)
_APP_HOSTS = ("ufirstnow.uticafirst.com",)
_OKTA_HOSTS = ("login.uticafirst.com",)


def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _is_uticafirst_url(url: str) -> bool:
    host = (url or "").casefold()
    return any(hint in host for hint in ("uticafirst.com",))


def assert_authenticated(page: Any) -> None:
    """The tab must already be signed in to UFIRST Now. No login here."""
    if not _is_uticafirst_url(str(getattr(page, "url", "") or "")):
        raise IntakeHold("Expected a Utica First tab")
    try:
        body = page.locator("body").inner_text()
    except Exception:
        body = ""
    text = _norm(body).casefold()
    if "welcome" not in text and "carlo ferrara" not in text and "agency admin" not in text:
        raise IntakeHold("Utica First session is not signed in")


def refuse_production_host() -> None:
    """Refuse a hermes-poc host unless Utica First Production filing is enabled.

    The kill switch and the carrier allowlist are the only way through.
    Every other Utica First check stays in place.
    """
    from .document_retrieval_filing import live_filing_decision

    if live_filing_decision(os.environ, socket.gethostname(), "uticafirst").allowed:
        return
    raw = f"{socket.gethostname()} {socket.getfqdn()}".lower()
    labels = [label for label in re.split(r"[\s.]+", raw) if label]
    if any(label == "hermes-poc-01" or label.startswith("hermes-poc") for label in labels):
        raise IntakeHold("Utica First NOC pull refuses Production host hermes-poc-01")


def is_cancellation_family(transaction_type: str) -> bool:
    """True for the TRANSACTION TYPE values Nicole pulls."""
    key = _norm(transaction_type).casefold()
    # Normalize "Pending Cancellation(NOC)" vs "Pending Cancellation (NOC)".
    key = re.sub(r"\(\s*", "(", key)
    key = re.sub(r"\s*\)", ")", key)
    return key in _CANCELLATION_FAMILY


def is_reversal_or_renewal(transaction_type: str) -> bool:
    """True for transaction types that must never be pulled as cancellations."""
    return _norm(transaction_type).casefold() in _NON_CANCELLATION_TYPES


def _header_indexes(
    headers: tuple[str, ...],
    fields: tuple[tuple[str, frozenset[str]], ...],
    *,
    required: tuple[str, ...],
) -> dict[str, int]:
    normalized = [_norm(h).casefold() for h in headers]
    indexes: dict[str, int] = {}
    for field, aliases in fields:
        hits = [i for i, h in enumerate(normalized) if h in aliases]
        if len(hits) != 1:
            if field in required:
                raise IntakeHold(f"Transaction grid header {field!r} is missing or ambiguous")
            continue
        indexes[field] = hits[0]
    missing = [f for f in required if f not in indexes]
    if missing:
        raise IntakeHold(f"Transaction grid headers missing: {', '.join(missing)}")
    return indexes


@dataclass(frozen=True)
class TransactionRow:
    policy_number: str
    insured_name: str
    transaction_type: str
    transaction_date: str
    status: str


@dataclass(frozen=True)
class TransactionGrid:
    list_url: str
    headers: tuple[str, ...]
    rows: tuple[TransactionRow, ...]


@dataclass(frozen=True)
class DocumentRow:
    document_id: str
    name: str
    doc_type: str
    source: str
    rendering_status: str


@dataclass(frozen=True)
class DocumentGrid:
    policy_number: str
    headers: tuple[str, ...]
    rows: tuple[DocumentRow, ...]


def parse_transaction_grid(
    headers: tuple[str, ...],
    grid: tuple[tuple[str, ...], ...],
    *,
    list_url: str,
) -> TransactionGrid:
    """Parse the POLICY TRANSACTIONS grid. Only cancellation-family rows kept."""
    indexes = _header_indexes(
        headers,
        _TRANSACTION_HEADER_FIELDS,
        required=("policy_number", "transaction_type"),
    )
    rows: list[TransactionRow] = []
    for cells in grid:
        ttype = cells[indexes["transaction_type"]] if indexes["transaction_type"] < len(cells) else ""
        if not is_cancellation_family(ttype):
            continue
        policy = cells[indexes["policy_number"]] if indexes["policy_number"] < len(cells) else ""
        policy = _norm(policy)
        if not _POLICY_NUMBER.match(policy):
            raise IntakeHold("Transaction policy number is missing or ambiguous")
        def _cell(name: str) -> str:
            i = indexes.get(name)
            if i is None or i >= len(cells):
                return ""
            return _norm(cells[i])
        rows.append(TransactionRow(
            policy_number=policy,
            insured_name=_cell("insured_name"),
            transaction_type=_norm(ttype),
            transaction_date=_cell("transaction_date"),
            status=_cell("status"),
        ))
    return TransactionGrid(list_url=list_url, headers=headers, rows=tuple(rows))


def parse_document_grid(
    headers: tuple[str, ...],
    grid: tuple[tuple[str, ...], ...],
    *,
    policy_number: str,
) -> DocumentGrid:
    """Parse the policy DOCUMENTS tab grid."""
    indexes = _header_indexes(
        headers,
        _DOCUMENT_HEADER_FIELDS,
        required=("document_id", "name"),
    )
    rows: list[DocumentRow] = []
    for cells in grid:
        def _cell(name: str) -> str:
            i = indexes.get(name)
            if i is None or i >= len(cells):
                return ""
            return _norm(cells[i])
        rows.append(DocumentRow(
            document_id=_cell("document_id"),
            name=_cell("name"),
            doc_type=_cell("doc_type"),
            source=_cell("source"),
            rendering_status=_cell("rendering_status"),
        ))
    if not rows:
        raise IntakeHold("Policy document grid is empty")
    return DocumentGrid(policy_number=policy_number, headers=headers, rows=tuple(rows))


def find_cancellation_document(documents: DocumentGrid) -> DocumentRow:
    """Pick the cancellation notice from the DOCUMENTS tab grid."""
    matches = [
        doc for doc in documents.rows
        if any(label in _norm(doc.name).casefold() for label in _CANCELLATION_DOC_LABELS)
    ]
    # Prefer a completed rendering; a non-completed rendering is still a
    # candidate but must not be silently preferred over a completed one.
    completed = [d for d in matches if _norm(d.rendering_status).casefold() == "completed"]
    pool = completed or matches
    if len(pool) != 1:
        raise IntakeHold("Cancellation notice document is missing or ambiguous")
    return pool[0]


def parse_carrier_date(raw: str) -> date:
    text = _norm(raw)
    for fmt in ("%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise IntakeHold("Transaction date is missing or ambiguous")


def noc_document_id(policy_number: str, transaction_type: str, transaction_date: date) -> str:
    policy = _norm(policy_number)
    ttype = _norm(transaction_type).casefold()
    if not _POLICY_NUMBER.match(policy):
        raise IntakeHold("NOC document id policy number is missing or ambiguous")
    return f"uticafirst-noc:{policy}:{ttype}:{transaction_date.isoformat()}"


def noc_filename(policy_number: str, transaction_type: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9]+", " ", transaction_type).strip().title().replace(" ", "")
    return f"{_norm(policy_number)} {safe or 'Notice'} UticaFirst.pdf"


def _require_pdf_bytes(content: bytes, *, what: str) -> bytes:
    if not content or not content.startswith(_PDF_MAGIC):
        raise IntakeHold(f"{what} did not produce PDF bytes")
    return content


class LocalDeliveryLedger:
    """Private named-PDF ledger. A conflicting file is kept and the pull holds."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def ensure_private(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
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

    def record(self, source: SourceItem, *, processed_on: date, insured_name: str = "") -> Path:
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
            "insured_name": insured_name,
            "sha256": digest,
            "bytes": len(source.content),
            "processed_date": processed_on.isoformat(),
        }
        self._write(data)
        return path

    def _load(self) -> dict:
        ledger = self.root / LEDGER_NAME
        if not ledger.is_file():
            return {"items": {}}
        try:
            data = json.loads(ledger.read_text())
        except (OSError, ValueError):
            raise IntakeHold("NOC ledger is unreadable")
        if not isinstance(data, dict) or not isinstance(data.get("items"), dict):
            raise IntakeHold("NOC ledger is unreadable")
        return data

    def _write(self, data: dict) -> None:
        ledger = self.root / LEDGER_NAME
        tmp = ledger.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        os.chmod(tmp, 0o600)
        os.replace(tmp, ledger)

    def _write_new(self, path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)

    def _basename(self, filename: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9._ -]+", "", filename).strip()
        if not safe or safe in {".", ".."}:
            raise IntakeHold("NOC filename is missing or ambiguous")
        return safe

    def pdf_path(self, day: date, filename: str) -> Path:
        return self.date_dir(day) / self._basename(filename)

    def date_dir(self, day: date) -> Path:
        self.ensure_private()
        folder = self.root / day.isoformat()
        if folder.is_symlink():
            raise IntakeHold("NOC output directory must be private (0700)")
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(folder, 0o700)
        return folder


def _click_unique(page: Any, name: str, *, roles: tuple[str, ...]) -> None:
    matches = []
    for role in roles:
        locator = page.get_by_role(role, name=name, exact=True)
        count = locator.count()
        if count:
            matches.append((count, locator))
    if len(matches) != 1 or matches[0][0] != 1:
        raise IntakeHold(f"Utica First control {name!r} is missing or ambiguous")
    matches[0][1].click()


def _wait_for(page: Any, selector: str, *, timeout_ms: int = 15000) -> None:
    try:
        page.wait_for_selector(selector, timeout=timeout_ms)
    except Exception:
        raise IntakeHold(f"Utica First view did not load ({selector})")


class PlaywrightUticaFirstNocBrowser:
    """Playwright page object for the Utica First cancellation pull.

    The page must already be signed in to UFIRST Now. Every navigation step
    reads back the resulting view before continuing.
    """

    def __init__(self, page: Any):
        self.page = page
        self._transactions_url = ""

    def _on_transactions_list(self) -> bool:
        try:
            text = _norm(self.page.locator("body").inner_text()).casefold()
        except Exception:
            return False
        return "policy | transaction list" in text or "transaction list" in text

    def open_policy_transactions(self) -> None:
        assert_authenticated(self.page)
        _click_unique(self.page, "POLICY TRANSACTIONS", roles=("link", "button", "tab"))
        _wait_for(self.page, "text=POLICY | TRANSACTION LIST")
        if not self._on_transactions_list():
            raise IntakeHold("Policy Transactions list did not open")
        self._transactions_url = str(getattr(self.page, "url", "") or "")

    def filter_cancellation_family(self) -> None:
        """Select the All radio, tick cancellation-family types, FILTER LIST."""
        assert_authenticated(self.page)
        # Radio filters: Today / All / Processed / Unprocessed.
        _click_unique(self.page, "All", roles=("radio",))
        # TRANSACTION TYPE multi-select: tick each cancellation-family value.
        for label in (
            "Cancellation",
            "Cancellation - Insured",
            "Non-Renewal",
            "Pending Cancellation(NOC)",
            "Intent to Non-Renew",
            "PreRenewal Notice",
        ):
            try:
                box = self.page.get_by_role("checkbox", name=label, exact=False)
                if box.count() == 1 and not box.is_checked():
                    box.check()
                elif box.count() != 1:
                    # Fall back to an option/combobox entry when the filter is
                    # a dropdown rather than checkboxes.
                    option = self.page.get_by_role("option", name=label, exact=False)
                    if option.count() == 1:
                        option.click()
                    # A missing label is tolerated: the family is matched
                    # again when the grid is parsed.
            except Exception:
                # A missing label is tolerated; grid parsing re-checks.
                continue
        _click_unique(self.page, "FILTER LIST", roles=("button",))
        _wait_for(self.page, "text=TRANSACTION TYPE")
        if not self._on_transactions_list():
            raise IntakeHold("Filtered Transaction list did not load")

    def load_pending_cancellations(self) -> TransactionGrid:
        self.open_policy_transactions()
        self.filter_cancellation_family()
        headers = self._grid_headers()
        rows = self._grid_rows()
        grid = parse_transaction_grid(
            headers, rows, list_url=self._transactions_url
        )
        return grid

    def _grid_headers(self) -> tuple[str, ...]:
        try:
            nodes = self.page.locator("table thead th").all()
            headers = tuple(_norm(n.inner_text()) for n in nodes)
        except Exception:
            headers = ()
        if not headers:
            raise IntakeHold("Transaction grid headers are missing or ambiguous")
        return headers

    def _grid_rows(self) -> tuple[tuple[str, ...], ...]:
        try:
            trs = self.page.locator("table tbody tr").all()
        except Exception:
            raise IntakeHold("Transaction grid rows are missing or ambiguous")
        out = []
        for tr in trs:
            try:
                cells = tuple(_norm(td.inner_text()) for td in tr.locator("td").all())
            except Exception:
                continue
            if any(cells):
                out.append(cells)
        return tuple(out)

    def _on_policy_summary(self, policy_number: str) -> bool:
        try:
            text = _norm(self.page.locator("body").inner_text())
        except Exception:
            return False
        return "POLICY | CURRENT SUMMARY" in text and policy_number in text

    def open_policy(self, policy_number: str) -> None:
        assert_authenticated(self.page)
        if not _POLICY_NUMBER.match(policy_number):
            raise IntakeHold("Policy number is missing or ambiguous")
        _click_unique(self.page, policy_number, roles=("link",))
        _wait_for(self.page, "text=POLICY | CURRENT SUMMARY")
        if not self._on_policy_summary(policy_number):
            raise IntakeHold("Policy summary did not open")

    def open_documents_tab(self) -> None:
        _click_unique(self.page, "DOCUMENTS", roles=("tab", "link", "button"))
        _wait_for(self.page, "text=Following is a list of documents for this policy.")
        try:
            text = _norm(self.page.locator("body").inner_text())
        except Exception:
            text = ""
        if "Following is a list of documents for this policy." not in text:
            raise IntakeHold("Policy DOCUMENTS tab did not open")

    def list_documents(self, policy_number: str) -> DocumentGrid:
        headers = self._grid_headers()
        rows = self._grid_rows()
        return parse_document_grid(headers, rows, policy_number=policy_number)

    def _viewer_open(self) -> bool:
        try:
            text = _norm(self.page.locator("body").inner_text()).casefold()
        except Exception:
            return False
        return "document viewer" in text or "viewer" in text

    def open_document_viewer(self, document: DocumentRow) -> None:
        """Double-click the document name and wait for the viewer overlay."""
        name = _norm(document.name)
        if not name:
            raise IntakeHold("Document name is missing or ambiguous")
        locator = self.page.get_by_role("link", name=name, exact=False)
        if locator.count() != 1:
            locator = self.page.get_by_text(name, exact=False)
        if locator.count() != 1:
            raise IntakeHold("Document open control is missing or ambiguous")
        try:
            locator.dblclick()
        except Exception:
            raise IntakeHold("Document viewer did not open")
        _wait_for(self.page, "text=viewer")
        if not self._viewer_open():
            raise IntakeHold("Document viewer did not open")

    def capture_viewer_pdf_bytes(self, document: DocumentRow) -> bytes:
        """Capture the PDF bytes behind the viewer overlay.

        The viewer loads a real PDF document URL. The response body is
        captured and must start with the PDF magic. When only the framed
        viewer is reachable with no byte access, this fails closed with
        IntakeHold and no download is claimed.
        """
        name = _norm(document.name)

        def _is_pdf_response(response: Any) -> bool:
            try:
                url = str(getattr(response, "url", "") or "")
                headers = getattr(response, "headers", {}) or {}
                ctype = str(headers.get("content-type", "")).casefold()
            except Exception:
                return False
            if "application/pdf" in ctype:
                return True
            return bool(_REMOTE_PDF.search(url))

        content: bytes | None = None
        try:
            with self.page.expect_response(_is_pdf_response, timeout=20000) as resp_info:
                self.open_document_viewer(document)
            response = resp_info.value
            try:
                content = response.body()
            except Exception:
                content = None
        except IntakeHold:
            raise
        except Exception:
            content = None
        if not content:
            raise IntakeHold(
                "Document viewer is framed with no PDF byte access; "
                f"no download claimed for {name!r}"
            )
        return _require_pdf_bytes(content, what=f"Viewer document {name!r}")

    def screenshot_transaction_list(self) -> bytes:
        if not self._on_transactions_list():
            raise IntakeHold("Transaction list screenshot is missing or not a PNG")
        try:
            data = self.page.screenshot(full_page=True, type="png")
        except Exception:
            raise IntakeHold("Transaction list screenshot is missing or not a PNG")
        if not data or not data.startswith(_PNG_MAGIC):
            raise IntakeHold("Transaction list screenshot is missing or not a PNG")
        return data

    def return_to_transactions(self) -> None:
        try:
            self.page.go_back()
        except Exception:
            raise IntakeHold("Could not return to the Transaction list")
        _wait_for(self.page, "text=POLICY | TRANSACTION LIST")
        if not self._on_transactions_list():
            raise IntakeHold("Could not return to the Transaction list")


def _row_payload(row: TransactionRow, outcome: str, reason: str = "") -> dict:
    payload = {
        "policy_number": row.policy_number,
        "insured_name": row.insured_name,
        "transaction_type": row.transaction_type,
        "transaction_date": row.transaction_date,
        "status": row.status,
        "outcome": outcome,
    }
    if reason:
        payload["reason"] = reason
    return payload


def run_pull(
    browser: PlaywrightUticaFirstNocBrowser,
    ledger: LocalDeliveryLedger,
    archive: SourceArchive,
    *,
    as_of: date,
) -> dict[str, Any]:
    """List cancellation-family transactions and pull each notice PDF.

    Returns a receipt dict with status/count/held/downloaded. A missing or
    ambiguous control raises IntakeHold; a download is only recorded when
    PDF bytes were captured.
    """
    from .document_retrieval_filing import require_carrier_pull

    require_carrier_pull("uticafirst")
    refuse_production_host()
    if not isinstance(as_of, date):
        raise IntakeHold("Pending Cancellation as-of date is missing or ambiguous")

    grid = browser.load_pending_cancellations()
    png = browser.screenshot_transaction_list()

    seen: dict[str, dict] = {}
    held: list[dict] = []
    downloaded: list[dict] = []

    for row in grid.rows:
        try:
            txn_date = parse_carrier_date(row.transaction_date) if row.transaction_date else as_of
        except IntakeHold:
            txn_date = as_of
        document_id = noc_document_id(row.policy_number, row.transaction_type, txn_date)
        filename = noc_filename(row.policy_number, row.transaction_type)

        if ledger.delivery_status(
            document_id=document_id, filename=filename, processed_on=txn_date
        ):
            seen[row.policy_number] = _row_payload(row, "DUPLICATE")
            continue

        try:
            browser.open_policy(row.policy_number)
            browser.open_documents_tab()
            documents = browser.list_documents(row.policy_number)
            target = find_cancellation_document(documents)
            pdf_bytes = browser.capture_viewer_pdf_bytes(target)
        except IntakeHold as exc:
            held.append(_row_payload(row, "HELD", str(exc)))
            try:
                browser.return_to_transactions()
            except IntakeHold:
                pass
            continue

        item = SourceItem(
            system="uticafirst",
            source_account=UTICAFIRST_SOURCE_ACCOUNT,
            source_id=document_id,
            source_url=grid.list_url,
            received_at=datetime.now().astimezone().isoformat(),
            filename=filename,
            content=pdf_bytes,
        )
        archive.preserve(item)
        ledger.record(item, processed_on=txn_date, insured_name=row.insured_name)
        downloaded.append({
            **_row_payload(row, "PULLED"),
            "document_id": document_id,
            "filename": filename,
            "bytes": len(pdf_bytes),
        })
        try:
            browser.return_to_transactions()
        except IntakeHold:
            pass

    receipt: dict[str, Any] = {
        "status": "PULLED",
        "carrier": "uticafirst",
        "as_of": as_of.isoformat(),
        "count": len(downloaded),
        "downloaded": downloaded,
        "held": held,
        "transactions": [_row_payload(r, "LISTED") for r in grid.rows],
        "verification": {"screenshot_png_bytes": len(png)},
    }
    return receipt


def main(argv: list[str] | None = None) -> int:
    """Manual Test-only entry point. Wires a CDP page; never used in prod."""
    import argparse

    parser = argparse.ArgumentParser(description="Test-only Utica First NOC pull")
    parser.add_argument("--qa-root", default=str(DEFAULT_QA_ROOT))
    parser.add_argument("--as-of", default=date.today().isoformat())
    args = parser.parse_args(argv)

    as_of = date.fromisoformat(args.as_of)
    qa_root = Path(args.qa_root)
    ledger = LocalDeliveryLedger(qa_root / "deliveries")
    archive = SourceArchive(qa_root / "sources")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser_ctx = pw.chromium.connect_over_cdp("http://127.0.0.1:9223")
        page = browser_ctx.contexts[0].pages[0]
        browser = PlaywrightUticaFirstNocBrowser(page)
        receipt = run_pull(browser, ledger, archive, as_of=as_of)
    print(json.dumps(receipt, indent=2, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
