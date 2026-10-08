"""Test-only Utica First pending cancellation document pull.

The playbook opens an already signed-in UFirst Now tab
(ufirstnow.uticafirst.com), opens the POLICY TRANSACTIONS tab, filters to
cancellation transaction types, and for each row follows the DOCUMENTS
"Documents" link to the POLICY | TRANSACTION | DOCUMENT LIST. Notice PDFs
are fetched from the DocGenServlet endpoint the portal itself uses:

    /oneshield/DocGenServlet?docId={documentId}
        &USER_SESSION_GUID={sessionGuid}
        &DRAGON_TRANSACTION_ID={txnId}

The document grid's ID column is the durable document identity (like
Guard's scribeItemId): identical display names are distinct documents,
so the ledger keys on the document ID, never the name.

A missing or non-unique control raises IntakeHold. The pull itself does
not type credentials; ``ensure_utica_page()`` (used by the carrier dry run)
reuses a signed-in UFirst Now tab or, on hermes-test-01 only, signs in via
``utica_login`` (Okta + email MFA; see docs/CARRIER_DOCUMENT_RETRIEVAL.md for
the Gmail requirement). This module does not upload, note, task, or label in
EZLynx, and does not register a timer.

Portal labels are Title Case live ("Policy Transactions", "Filter List");
controls are matched case-insensitively via ``carrier_locators``.

PROVEN live 2026-10-07 (hermes-test-01, Morsan BOP3001386190, 2 PDFs): the
DocGenServlet fetch with the session GUID, DRAGON_TRANSACTION_ID and the
Referer/Origin headers the portal's CSRF filter demands returns real PDFs;
direct navigation without Referer is rejected. Document IDs regenerate per
session, so they are harvested fresh every run and never cached.

The transaction and document lists are ExtJS grids. ``collect_paged_rows``
reads every page, not only the first 25 rows.
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

from .carrier_locators import unique_control_ci, wait_for_text_ci
from .intake_core import IntakeHold, SourceArchive, SourceItem

PROCESS = "utica"
SCOPE = "pending_cancellation"
UTICA_HOST = "ufirstnow.uticafirst.com"
DEFAULT_CDP_URL = "http://127.0.0.1:9223"
DOWNLOAD_TIMEOUT_MS = 15000
FILTER_RADIO_ATTEMPTS = 6
LEDGER_NAME = "utica-cancellation-ledger.json"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_PDF_MAGIC = b"%PDF"
DEFAULT_OUTPUT_ROOT = Path(
    "/opt/streetsmart-hermes-test/robie-job-engine/data/artifacts/carrier-pull-qa/utica"
)
# Shared Drive "Robie Carrier Pull QA (Nicole)". The Utica First child folder
# id is UNVERIFIED. Folder upload is TODO. --upload-drive fails closed and
# does not call Google.
CARRIER_QA_DRIVE_PARENT_ID = "1cLEpR-0T6KdiVjcdAr0qpGTO447MetI2"
DRIVE_QA_FOLDER_NAME = "Robie Carrier Pull QA (Nicole)/Utica First"
DRIVE_UPLOAD_UNAVAILABLE = (
    "Drive upload of the Utica First QA pack is not available; "
    "refusing to report the pack as uploaded"
)
HERMES_TEST_HOST = "hermes-test-01"
# Live policy numbers look like "ART3000926870" (3 letters + 10 digits).
_POLICY_NUMBER = re.compile(r"^[A-Z0-9]{6,16}$")
# Transaction types that carry cancellation / non-renewal notices.
# "Rescind Pending Cancellation" rescinds a pending cancellation — it is
# policy-status related and in scope. "Reinstatement" and
# "Change Payment Plan" are related types on the grid but are NOT targets
# (billing-adjacent / not a cancellation notice).
_CANCELLATION_TRANSACTION_TYPES = frozenset({
    "pending cancellation(noc)",
    "cancellation",
    "rescind pending cancellation",
    "non-renewal",
    "prerenewal notice",
    # Live 2026-10-08 (JITOW LLC ART3000699220): the non-renewal notice
    # transaction is labelled "Intent to Non-Renew".
    "intent to non-renew",
})
# Document names that mark a notice worth pulling. The DOCUMENT LIST is
# already scoped to a cancellation transaction, so this is a guard against
# pulling unrelated attachments, not the primary filter.
_CANCELLATION_TERMS = frozenset({
    "cancellation", "cancel", "cancelled", "canceled",
    "notice of cancellation", "pending cancellation",
    "nonpay notice", "non-pay notice", "non pay notice",
    "non-renewal", "nonrenewal", "notice of non-renewal",
    "intent to cancel", "pre-cancellation", "prerenewal",
})
_BILLING_TERMS = frozenset({
    "invoice", "billing", "premium due", "payment coupon",
    "installment",
})
_TXN_GRID_HEADERS = {
    "policy_number": {"policy number", "policy #", "policy"},
    "transaction_type": {"transaction type", "type"},
    "insured_name": {"insured name", "insured", "named insured", "name"},
    "effective": {"effective", "effective date"},
    "documents": {"documents", "docs"},
}
_DOC_GRID_HEADERS = {
    "doc_id": {"id", "document id"},
    "name": {"name", "document name"},
    "content_type": {"content type", "type"},
    "description": {"description"},
    "added_date": {"added date", "date added"},
    "source": {"source"},
    "rendering_status": {"rendering status", "status"},
}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _is_pdf(content: bytes) -> bool:
    return bytes(content or b"")[:4] == _PDF_MAGIC


def parse_carrier_date(text: str) -> date:
    cleaned = _norm(text)
    for fmt in ("%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d",
                "%m/%d/%Y %I:%M %p", "%m/%d/%Y %I:%M:%S %p"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    raise IntakeHold(f"Utica First date is missing or ambiguous: {cleaned!r}")


def require_policy_number(text: str) -> str:
    cleaned = _norm(text).upper().replace(" ", "")
    if not _POLICY_NUMBER.fullmatch(cleaned):
        raise IntakeHold(f"Utica First policy number is missing or ambiguous: {text!r}")
    return cleaned


def is_cancellation_transaction(transaction_type: str) -> bool:
    return _norm(transaction_type).casefold() in _CANCELLATION_TRANSACTION_TYPES


def is_cancellation_document(name: str, description: str = "") -> bool:
    key = f"{_norm(name)} {_norm(description)}".casefold()
    if any(term in key for term in _BILLING_TERMS):
        return False
    return any(term in key for term in _CANCELLATION_TERMS)


def session_guid_from_url(url: str) -> str:
    """Extract the session token the DocGenServlet URL needs.

    Prefers the ``USER_SESSION_GUID`` query param; falls back to the SPA's
    ``osst`` token. Whether the two are interchangeable is UNVERIFIED and
    needs a live download to prove.
    """
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url or "").query)
    except Exception:
        query = {}
    for param in ("USER_SESSION_GUID", "osst"):
        values = query.get(param, [])
        candidate = _norm(values[0]) if values else ""
        if candidate:
            return candidate
    raise IntakeHold("Utica First session token is missing or ambiguous")


def docgen_pdf_url(*, doc_id: str, session_guid: str, transaction_id: str) -> str:
    """Build the notice-PDF URL the portal's own viewer uses."""
    doc_id = _norm(doc_id)
    session_guid = _norm(session_guid)
    transaction_id = _norm(transaction_id)
    if not doc_id or not session_guid or not transaction_id:
        raise IntakeHold("Utica First DocGenServlet URL is missing or ambiguous")
    params = urllib.parse.urlencode({
        "docId": doc_id,
        "USER_SESSION_GUID": session_guid,
        "DRAGON_TRANSACTION_ID": transaction_id,
    })
    return f"https://{UTICA_HOST}/oneshield/DocGenServlet?{params}"


def docgen_request_headers(*, referer_url: str) -> dict[str, str]:
    """Headers the portal's CSRF filter demands on the DocGenServlet request.

    Direct navigation to the DocGenServlet URL is rejected ("Try using the
    application menu for navigation"); the same URL works from the in-app
    viewer because that request carries Referer/Origin. The referer must be
    the in-app page the document list was opened from, harvested from the
    live page URL each run, never hardcoded.
    """
    referer = _norm(referer_url)
    if not referer:
        raise IntakeHold("Utica First PDF request is missing or ambiguous")
    parsed = urllib.parse.urlsplit(referer)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != UTICA_HOST:
        raise IntakeHold("Utica First PDF request is missing or ambiguous")
    return {
        "Referer": referer,
        "Origin": f"https://{UTICA_HOST}",
    }


_CSRF_REJECTION_MARKERS = (
    "try using the application menu",
)


def _is_csrf_rejection(content: bytes) -> bool:
    """Detect the portal CSRF filter's rejection page.

    A real PDF is never a rejection, even if it contains similar text.
    """
    if _is_pdf(content):
        return False
    try:
        text = bytes(content or b"").decode("utf-8", errors="replace")
    except Exception:
        return False
    lowered = text.casefold()
    return any(marker in lowered for marker in _CSRF_REJECTION_MARKERS)


_TXN_KEY = r"""["']?DRAGON_TRANSACTION_ID["']?"""
_TXN_VALUE = r"""\s*["']?(\d+)["']?"""
_TRANSACTION_ID_PATTERNS = (
    # DRAGON_TRANSACTION_ID=123, "DRAGON_TRANSACTION_ID": "123", ...ID: 123
    re.compile(_TXN_KEY + r"\s*[:=]" + _TXN_VALUE, re.IGNORECASE),
    # "DRAGON_TRANSACTION_ID","value":"123" and ...ID", "value" : 123
    re.compile(_TXN_KEY + r"""\s*,\s*["']?value["']?\s*[:=]""" + _TXN_VALUE, re.IGNORECASE),
    # {"name":"DRAGON_TRANSACTION_ID", ..., "value":"123"} within one object
    re.compile(_TXN_KEY + r"""\s*,[^{}]{0,200}?["']value["']\s*:""" + _TXN_VALUE, re.IGNORECASE),
    # <input name="DRAGON_TRANSACTION_ID" ... value="123">
    re.compile(r"""name\s*=\s*["']DRAGON_TRANSACTION_ID["'][^>]*?\bvalue\s*=""" + _TXN_VALUE, re.IGNORECASE),
)


def transaction_id_from_page(html: str) -> str:
    """Extract the DRAGON_TRANSACTION_ID from the DOCUMENT LIST page.

    The PDF URL needs it; the grid does not expose it, so it is read from
    the document-list page markup. Zero or multiple distinct values hold.

    Accepted shapes (live UFirst Now uses the ExtJS name/value pair, seen
    2026-10-07 as ``"DRAGON_TRANSACTION_ID","value":"1247842754"``):

    - ``DRAGON_TRANSACTION_ID=123`` / ``DRAGON_TRANSACTION_ID: "123"``
    - ``"DRAGON_TRANSACTION_ID","value":"123"`` (ExtJS name, value pair)
    - ``{"name":"DRAGON_TRANSACTION_ID","value":"123"}``
    - ``<input name="DRAGON_TRANSACTION_ID" value="123">``
    """
    text = html or ""
    matches: list[str] = []
    for pattern in _TRANSACTION_ID_PATTERNS:
        matches.extend(pattern.findall(text))
    unique = sorted(set(matches))
    if len(unique) != 1:
        raise IntakeHold("Utica First DRAGON_TRANSACTION_ID is missing or ambiguous")
    return unique[0]


@dataclass(frozen=True)
class TransactionRow:
    policy_number: str
    transaction_type: str
    insured_name: str
    effective_date: date
    list_url: str

    @property
    def document_id(self) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", self.transaction_type.casefold()).strip("-")
        return f"utica:{self.policy_number}:{self.effective_date.isoformat()}:{slug}"


@dataclass(frozen=True)
class UticaDocument:
    doc_id: str
    name: str
    content_type: str
    description: str
    added_date: date
    source: str
    rendering_status: str
    policy_number: str

    @property
    def document_id(self) -> str:
        # The grid's document ID is the durable identity: two rows with the
        # same visible name (e.g. "NonPay Notice-Insured" re-rendered) are
        # distinct documents, so the ledger never keys on the name.
        return f"utica:{self.policy_number}:{self.doc_id}"

    @property
    def filename(self) -> str:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", self.name).strip("_")
        return f"{self.policy_number} {safe or 'document'} UticaFirst.pdf"


def _match_headers(headers: tuple[str, ...], aliases: dict[str, set[str]], *, what: str) -> dict[str, int]:
    normed = [_norm(h).casefold() for h in headers]
    indexes: dict[str, int] = {}
    for field, names in aliases.items():
        matches = [i for i, h in enumerate(normed) if h in names]
        if len(matches) != 1:
            raise IntakeHold(f"Utica First {what} headers are missing or ambiguous")
        indexes[field] = matches[0]
    return indexes


def parse_transactions_grid(
    headers: tuple[str, ...], rows: tuple[tuple[str, ...], ...], *, list_url: str
) -> tuple[TransactionRow, ...]:
    """Parse the POLICY | TRANSACTION LIST grid into typed rows."""
    indexes = _match_headers(headers, _TXN_GRID_HEADERS, what="transactions")
    parsed: list[TransactionRow] = []
    for cells in rows:
        if len(cells) < len(headers):
            raise IntakeHold("Utica First transactions grid row is missing or ambiguous")
        policy = require_policy_number(cells[indexes["policy_number"]])
        txn_type = _norm(cells[indexes["transaction_type"]])
        if not txn_type:
            raise IntakeHold("Utica First transaction type is missing or ambiguous")
        insured = _norm(cells[indexes["insured_name"]])
        if not insured:
            raise IntakeHold("Utica First insured name is missing or ambiguous")
        effective = parse_carrier_date(cells[indexes["effective"]])
        parsed.append(TransactionRow(policy, txn_type, insured, effective, list_url))
    if not parsed:
        raise IntakeHold("Utica First transactions grid has no rows")
    return tuple(parsed)


def parse_document_grid(
    headers: tuple[str, ...],
    rows: tuple[tuple[str, ...], ...],
    *,
    policy_number: str,
) -> tuple[UticaDocument, ...]:
    """Parse the POLICY | TRANSACTION | DOCUMENT LIST grid.

    Live columns: ID | NAME | CONTENT TYPE | DESCRIPTION | ADDED DATE |
    SOURCE | RENDERING STATUS.
    """
    indexes = _match_headers(headers, _DOC_GRID_HEADERS, what="document")
    docs: list[UticaDocument] = []
    for cells in rows:
        if len(cells) < len(headers):
            raise IntakeHold("Utica First document row is missing or ambiguous")
        doc_id = _norm(cells[indexes["doc_id"]])
        if not doc_id or not re.fullmatch(r"[A-Za-z0-9_-]+", doc_id):
            raise IntakeHold("Utica First document ID is missing or ambiguous")
        name = _norm(cells[indexes["name"]])
        if not name:
            raise IntakeHold("Utica First document name is missing or ambiguous")
        docs.append(
            UticaDocument(
                doc_id=doc_id,
                name=name,
                content_type=_norm(cells[indexes["content_type"]]),
                description=_norm(cells[indexes["description"]]),
                added_date=parse_carrier_date(cells[indexes["added_date"]]),
                source=_norm(cells[indexes["source"]]),
                rendering_status=_norm(cells[indexes["rendering_status"]]),
                policy_number=policy_number,
            )
        )
    if not docs:
        raise IntakeHold("Utica First document list is empty")
    return tuple(docs)


def refuse_production_host() -> None:
    """Refuse a hermes-poc host unless Utica First Production filing is enabled."""
    from .document_retrieval_filing import live_filing_decision

    if live_filing_decision(os.environ, socket.gethostname(), "uticafirst").allowed:
        return
    raw = f"{socket.gethostname()} {socket.getfqdn()}".lower()
    labels = [label for label in re.split(r"[\s.]+", raw) if label]
    if any(label == "hermes-poc-01" or label.startswith("hermes-poc") for label in labels):
        raise IntakeHold("Utica First document pull refuses Production host hermes-poc-01")


def require_hermes_test_host() -> None:
    """Live packs are produced on hermes-test-01. Fixture runs inject a browser."""
    from .document_retrieval_filing import live_filing_decision

    if live_filing_decision(os.environ, socket.gethostname(), "uticafirst").allowed:
        return
    raw = f"{socket.gethostname()} {socket.getfqdn()}".lower()
    labels = [label for label in re.split(r"[\s.]+", raw) if label]
    if HERMES_TEST_HOST not in labels:
        raise IntakeHold("Utica First QA pack must be produced on hermes-test-01")


def require_utica_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise IntakeHold("Utica First UFirst Now URL is missing or ambiguous")
    if host != UTICA_HOST:
        raise IntakeHold("Utica First UFirst Now URL is missing or ambiguous")
    if "login" in parsed.path.lower() or "sso/login" in parsed.path.lower():
        # A login screen means the SSO session expired; the worker does not
        # perform login.
        raise IntakeHold("Utica First session is not authenticated")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def _unique_control(page: Any, role: str, name: str, *, exact: bool = True) -> Any:
    locator = page.get_by_role(role, name=name, exact=exact)
    try:
        count = int(locator.count())
    except Exception:
        raise IntakeHold(f"Utica First control {name!r} is missing or ambiguous")
    if count != 1:
        raise IntakeHold(f"Utica First control {name!r} is missing or ambiguous")
    return locator


def _read_text(node: Any) -> str:
    getter = getattr(node, "inner_text", None)
    if callable(getter):
        try:
            return str(getter())
        except Exception:
            return ""
    return str(getattr(node, "text", "") or "")


# UFirst Now is ExtJS: each grid row is its own <table> (live 2026-10-08:
# 29 tables on the transaction list), headers are .x-column-header divs and
# cells are .x-grid-cell. Read visible grids as {id, headers, rows}.
_EXT_GRIDS_JS = """() => {
  const vis = e => !!(e.offsetParent || e.getClientRects().length);
  return Array.from(document.querySelectorAll('.x-grid')).filter(vis).map(g => ({
    id: g.id || '',
    headers: Array.from(g.querySelectorAll('.x-column-header')).filter(vis)
      .map(h => ((h.querySelector('.x-column-header-text') || h).innerText || '').trim()),
    rows: Array.from(g.querySelectorAll('.x-grid-item')).filter(vis)
      .map(r => Array.from(r.querySelectorAll('.x-grid-cell')).map(c => (c.innerText || '').trim()))
  }));
}"""
_EXT_GRID_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def find_ext_grid(
    grids: Any, aliases: dict[str, set[str]], *, what: str
) -> tuple[str, tuple[str, ...], tuple[tuple[str, ...], ...]]:
    """The one ExtJS grid whose headers carry every field in ``aliases``."""
    found = []
    for grid in grids or ():
        if not isinstance(grid, dict):
            continue
        headers = tuple(_norm(str(h)) for h in grid.get("headers") or ())
        try:
            _match_headers(headers, aliases, what=what)
        except IntakeHold:
            continue
        rows = tuple(tuple(_norm(str(c)) for c in row) for row in grid.get("rows") or ())
        found.append((str(grid.get("id") or ""), headers, rows))
    if len(found) != 1:
        raise IntakeHold(f"Utica First {what} grid is missing or ambiguous (found {len(found)})")
    grid_id = found[0][0]
    if not _EXT_GRID_ID.fullmatch(grid_id):
        raise IntakeHold(f"Utica First {what} grid is missing or ambiguous")
    return found[0]


def _row_key(row: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(row)


def collect_paged_rows(
    first_rows: tuple[tuple[str, ...], ...] | list[tuple[str, ...]],
    *,
    next_state: Callable[[], str],
    click_next: Callable[[], bool],
    read_rows: Callable[[], tuple[tuple[str, ...], ...]],
    what: str,
    max_pages: int = 40,
) -> tuple[tuple[str, ...], ...]:
    """Read an ExtJS grid past the first 25-row page until the pager stops.

    ``next_state`` is ``next``, ``done``, ``none``, or ``missing``. A page
    that does not advance holds. Hitting ``max_pages`` with another page
    still available holds, so a long list is never silently truncated.
    """
    collected = [tuple(row) for row in first_rows]
    seen = {_row_key(row) for row in collected}
    pages_read = 1
    while pages_read < max_pages:
        state = next_state()
        if state in {"done", "none", "missing"}:
            return tuple(collected)
        if state != "next":
            raise IntakeHold(f"Utica First {what} paging control is missing or ambiguous")
        if not click_next():
            raise IntakeHold(f"Utica First {what} grid page {pages_read + 1} did not advance")
        pages_read += 1
        fresh = [tuple(row) for row in read_rows() if _row_key(tuple(row)) not in seen]
        if not fresh:
            raise IntakeHold(
                f"Utica First {what} grid still shows the same rows after paging to page {pages_read}"
            )
        for row in fresh:
            seen.add(_row_key(row))
            collected.append(row)
    if next_state() == "next":
        raise IntakeHold(
            f"Utica First {what} grid has more than {max_pages} pages; not all rows were read"
        )
    return tuple(collected)


_EXT_PAGE_STATE_JS = """(gridId) => {
  const grid = document.getElementById(gridId);
  if (!grid) return {state: 'missing'};
  const panel = grid.closest('.x-panel') || grid.parentElement || grid;
  const next = panel.querySelector('.x-tbar-page-next');
  if (!next) return {state: 'none'};
  const btn = next.closest('.x-btn') || next;
  const cls = btn.className || '';
  const disabled = cls.indexOf('disabled') !== -1 || btn.getAttribute('aria-disabled') === 'true';
  return {state: disabled ? 'done' : 'next'};
}"""

_EXT_PAGE_NEXT_JS = """(gridId) => {
  const grid = document.getElementById(gridId);
  if (!grid) return false;
  const panel = grid.closest('.x-panel') || grid.parentElement || grid;
  const next = panel.querySelector('.x-tbar-page-next');
  if (!next) return false;
  const btn = next.closest('.x-btn') || next;
  const cls = btn.className || '';
  if (cls.indexOf('disabled') !== -1 || btn.getAttribute('aria-disabled') === 'true') return false;
  btn.click();
  return true;
}"""


_EXT_EXIT_JS = """() => {
  const vis = e => !!(e.offsetParent || e.getClientRects().length);
  const hits = Array.from(document.querySelectorAll('.x-btn-inner'))
    .filter(e => vis(e) && (e.textContent || '').trim().toLowerCase() === 'exit');
  if (hits.length !== 1) return false;
  (hits[0].closest('.x-btn') || hits[0]).click();
  return true;
}"""


def ext_click(control: Any) -> None:
    """Click, falling back to a DOM click when an ExtJS mask intercepts.

    Live 2026-10-08: an invisible x-mask (left by the "Personalized Filter
    Names" window) sits over UFirst Now and swallows pointer clicks.
    """
    try:
        control.click(timeout=8000)
        return
    except TypeError:
        control.click()
        return
    except Exception:  # noqa: BLE001 - fall through to the DOM click
        pass
    target = control.first if hasattr(control, "first") else control
    target.evaluate("e => (e.closest('.x-btn') || e).click()")


_EXT_RADIO_LABEL_JS = """(label) => {
  const vis = e => !!(e.offsetParent || e.getClientRects().length);
  const hits = Array.from(document.querySelectorAll('label.x-form-cb-label'))
    .filter(e => vis(e) && (e.textContent || '').trim().toLowerCase() === label.toLowerCase());
  if (hits.length !== 1) return false;
  const field = hits[0].closest('.x-form-type-radio') || hits[0].parentElement;
  const input = field ? field.querySelector('input[type=radio]') : null;
  (input || hits[0]).click();
  return true;
}"""


def click_ext_radio_label(page: Any, label: str) -> bool:
    """Click the one visible ExtJS radio whose label reads ``label``."""
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return False
    try:
        return bool(evaluate(_EXT_RADIO_LABEL_JS, label))
    except Exception:
        return False


def click_ext_exit(page: Any) -> bool:
    """Click the one visible ExtJS "Exit" button (DOCUMENT LIST -> policy page)."""
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return False
    try:
        clicked = bool(evaluate(_EXT_EXIT_JS))
    except Exception:
        return False
    if clicked:
        waiter = getattr(page, "wait_for_timeout", None)
        if callable(waiter):
            waiter(6000)
    return clicked


def transaction_row_index(
    headers: tuple[str, ...], rows: tuple[tuple[str, ...], ...], row: "TransactionRow"
) -> int:
    """Index of ``row`` in the live grid (policy + type + effective date)."""
    idx = _match_headers(headers, _TXN_GRID_HEADERS, what="transactions")
    hits = []
    for i, cells in enumerate(rows):
        if len(cells) < len(headers):
            continue
        try:
            effective = parse_carrier_date(cells[idx["effective"]])
        except IntakeHold:
            continue
        if (
            _norm(cells[idx["policy_number"]]).upper() == row.policy_number
            and _norm(cells[idx["transaction_type"]]).casefold() == row.transaction_type.casefold()
            and effective == row.effective_date
        ):
            hits.append(i)
    if len(hits) != 1:
        raise IntakeHold(
            f"Utica First transaction row {row.policy_number!r} is missing or ambiguous"
        )
    return hits[0]


class PlaywrightUticaCancellationBrowser:
    """Drive one already-authenticated UFirst Now tab. Does not type credentials."""

    def __init__(self, page: Any):
        self.page = page
        self._list_url = ""
        self._doclist_url = ""
        self._transaction_id = ""

    # -- navigation -----------------------------------------------------
    def open_transactions(self) -> None:
        """Open the Policy Transactions tab (label casing varies live).

        Live 2026-10-07 the control reads "Policy Transactions" and is not an
        ARIA tab, so the visible text is the fallback (first visible match:
        it is a navigation click, and the grid checks below catch a miss).
        """
        page = self.page
        require_utica_url(str(getattr(page, "url", "") or ""))
        try:
            control = unique_control_ci(
                page, "tab", "Policy Transactions", carrier="Utica First",
                text_fallback=True, first_visible_text=True,
            )
        except IntakeHold:
            # Live 2026-10-08: a DOCUMENT LIST page has no top navigation;
            # its Exit button leads back to a page that has it.
            if not click_ext_exit(page):
                raise
            control = unique_control_ci(
                page, "tab", "Policy Transactions", carrier="Utica First",
                text_fallback=True, first_visible_text=True,
            )
        ext_click(control)
        if not wait_for_text_ci(page, "Transaction List", timeout_ms=15000):
            # The heading wording is not proven live; the hand patch on Test
            # waited a fixed 5 s. Settle briefly; the grid parse still holds
            # if the list never rendered.
            page.wait_for_timeout(3000)
        self._list_url = require_utica_url(str(getattr(page, "url", "") or ""))

    def select_filter_all(self) -> None:
        """Select the "All" filter radio and apply it ("Filter List" live)."""
        page = self.page
        radio = None
        for attempt in range(FILTER_RADIO_ATTEMPTS):
            # Live 2026-10-08: right after the Policy Transactions click the
            # ExtJS list is re-rendering and the radio is briefly absent.
            try:
                radio = unique_control_ci(page, "radio", "All", carrier="Utica First")
                break
            except IntakeHold:
                if click_ext_radio_label(page, "All"):
                    break
                if attempt == FILTER_RADIO_ATTEMPTS - 1:
                    raise
                waiter = getattr(page, "wait_for_timeout", None)
                if not callable(waiter):
                    raise
                waiter(2000)
        if radio is not None:
            try:
                radio.check(timeout=8000)
            except TypeError:
                radio.check()
            except Exception:  # noqa: BLE001 - ExtJS mask over the page
                ext_click(radio)
        ext_click(unique_control_ci(page, "button", "Filter List", carrier="Utica First"))
        page.wait_for_selector("table", timeout=15000)

    def _ext_grids(self) -> Any:
        evaluate = getattr(self.page, "evaluate", None)
        if not callable(evaluate):
            return []
        try:
            return evaluate(_EXT_GRIDS_JS) or []
        except Exception:
            return []

    def load_transactions(self) -> tuple[TransactionRow, ...]:
        page = self.page
        tables = page.locator("table")
        table_count = int(tables.count())
        if table_count != 1:
            grids = self._ext_grids()
            if not grids:
                raise IntakeHold(
                    "Utica First transactions table is missing or ambiguous "
                    f"(found {table_count} tables and no ExtJS grid)"
                )
            grid_id, headers, rows = find_ext_grid(grids, _TXN_GRID_HEADERS, what="transactions")
            rows = self._all_ext_rows(grid_id, rows, _TXN_GRID_HEADERS, "transactions")
            self._ext_mode = True
            return parse_transactions_grid(headers, rows, list_url=self._list_url)
        table = tables.first if hasattr(tables, "first") else tables
        header_nodes = table.locator("thead th").all()
        if not header_nodes:
            raise IntakeHold("Utica First transactions table headers are missing or ambiguous")
        headers = tuple(_norm(_read_text(node)) for node in header_nodes)
        row_nodes = table.locator("tbody tr").all()
        rows = tuple(
            tuple(_norm(_read_text(cell)) for cell in row.locator("td").all())
            for row in row_nodes
        )
        return parse_transactions_grid(headers, rows, list_url=self._list_url)

    def open_documents(self, row: TransactionRow) -> None:
        """Click the row's DOCUMENTS "Documents" link -> DOCUMENT LIST.

        Captures the DRAGON_TRANSACTION_ID the DocGenServlet PDF URL needs.
        """
        page = self.page
        if getattr(self, "_ext_mode", False):
            self._open_documents_ext(row)
        else:
            self._open_documents_table(row)
        if not wait_for_text_ci(page, "Document List", timeout_ms=15000):
            # Heading wording unproven live; the DRAGON_TRANSACTION_ID read
            # below holds if the document list never rendered.
            page.wait_for_timeout(3000)
        html = page.content() if callable(getattr(page, "content", None)) else ""
        self._transaction_id = transaction_id_from_page(str(html))
        # The DocGenServlet request's Referer must be the in-app page the
        # document list was opened from, harvested fresh each run.
        self._doclist_url = require_utica_url(str(getattr(page, "url", "") or ""))

    def _open_documents_ext(self, row: TransactionRow) -> None:
        """ExtJS grid: click the row's DOCUMENTS cell text.

        Grid ids are regenerated on every visit, so the grid is re-read here.
        The cell is a <span>Documents</span> action link, and an invisible
        ExtJS mask can sit over the page (live 2026-10-08), so the click is
        dispatched on the element itself.
        """
        grid_id, headers, rows = find_ext_grid(self._ext_grids(), _TXN_GRID_HEADERS, what="transactions")
        index = transaction_row_index(headers, rows, row)
        doc_col = _match_headers(headers, _TXN_GRID_HEADERS, what="transactions")["documents"]
        cell = (
            self.page.locator(f"#{grid_id} .x-grid-item").nth(index)
            .locator(".x-grid-cell").nth(doc_col)
        )
        target = cell.get_by_text(re.compile(r"^\s*documents\s*$", re.IGNORECASE))
        try:
            if int(target.count()) != 1:
                raise IntakeHold(
                    f"Utica First Documents link for {row.policy_number!r} is missing or ambiguous"
                )
            target.evaluate("e => e.click()")
        except IntakeHold:
            raise
        except Exception:
            raise IntakeHold(
                f"Utica First Documents link for {row.policy_number!r} is missing or ambiguous"
            )

    def _open_documents_table(self, row: TransactionRow) -> None:
        page = self.page
        grid_row = page.locator("tr", has_text=row.policy_number)
        try:
            if int(grid_row.count()) != 1:
                raise IntakeHold(
                    f"Utica First transaction row {row.policy_number!r} is missing or ambiguous"
                )
        except IntakeHold:
            raise
        except Exception:
            raise IntakeHold(
                f"Utica First transaction row {row.policy_number!r} is missing or ambiguous"
            )
        try:
            link = unique_control_ci(
                grid_row.first, "link", "Documents", carrier="Utica First"
            )
        except IntakeHold:
            raise IntakeHold(
                f"Utica First Documents link for {row.policy_number!r} is missing or ambiguous"
            )
        link.first.click()

    def list_documents(self, policy_number: str) -> tuple[UticaDocument, ...]:
        page = self.page
        tables = page.locator("table")
        table_count = int(tables.count())
        if table_count != 1:
            grids = self._ext_grids()
            if not grids:
                raise IntakeHold(
                    "Utica First document table is missing or ambiguous "
                    f"(found {table_count} tables and no ExtJS grid)"
                )
            grid_id, headers, rows = find_ext_grid(grids, _DOC_GRID_HEADERS, what="document")
            rows = self._all_ext_rows(grid_id, rows, _DOC_GRID_HEADERS, "document")
            return parse_document_grid(headers, rows, policy_number=policy_number)
        table = tables.first if hasattr(tables, "first") else tables
        header_nodes = table.locator("thead th").all()
        if not header_nodes:
            raise IntakeHold("Utica First document table headers are missing or ambiguous")
        headers = tuple(_norm(_read_text(node)) for node in header_nodes)
        row_nodes = table.locator("tbody tr").all()
        rows = tuple(
            tuple(_norm(_read_text(cell)) for cell in row.locator("td").all())
            for row in row_nodes
        )
        return parse_document_grid(headers, rows, policy_number=policy_number)

    def _all_ext_rows(
        self,
        grid_id: str,
        first_rows: tuple[tuple[str, ...], ...],
        aliases: dict[str, set[str]],
        what: str,
    ) -> tuple[tuple[str, ...], ...]:
        page = self.page

        def next_state() -> str:
            evaluate = getattr(page, "evaluate", None)
            if not callable(evaluate):
                return "none"
            try:
                info = evaluate(_EXT_PAGE_STATE_JS, grid_id)
            except TypeError:
                return "none"
            except Exception:
                return "none"
            if not isinstance(info, dict):
                return "none"
            return str(info.get("state") or "none")

        def click_next() -> bool:
            try:
                return bool(page.evaluate(_EXT_PAGE_NEXT_JS, grid_id))
            except Exception:
                return False

        def read_rows() -> tuple[tuple[str, ...], ...]:
            waiter = getattr(page, "wait_for_timeout", None)
            if callable(waiter):
                waiter(1500)
            _, _, rows = find_ext_grid(self._ext_grids(), aliases, what=what)
            return rows

        return collect_paged_rows(
            first_rows, next_state=next_state, click_next=click_next, read_rows=read_rows, what=what
        )

    def download_document(self, doc: UticaDocument) -> bytes:
        """Fetch the notice PDF via the portal's own DocGenServlet URL.

        Uses the browser context's session (cookies) so the SSO token rides
        along, WITH the Referer/Origin headers the portal's CSRF filter
        demands. Direct navigation to the URL is rejected by the filter, and
        the in-app viewer's Download button is unreachable from automation,
        so this authenticated request with headers is the download path.

        Doc IDs, the DRAGON_TRANSACTION_ID, the session GUID, and the
        Referer are all harvested from the live document list each run;
        nothing is hardcoded. A CSRF rejection page or any other non-PDF
        response raises IntakeHold and is never kept.
        """
        page = self.page
        url = docgen_pdf_url(
            doc_id=doc.doc_id,
            session_guid=session_guid_from_url(str(getattr(page, "url", "") or "")),
            transaction_id=self._transaction_id,
        )
        headers = docgen_request_headers(referer_url=self._doclist_url or self._list_url)
        request = getattr(getattr(page, "context", None), "request", None)
        getter = getattr(request, "get", None)
        if not callable(getter):
            raise IntakeHold("Utica First PDF request is missing or ambiguous")
        try:
            response = getter(url, timeout=DOWNLOAD_TIMEOUT_MS, headers=headers)
            body = response.body() if callable(getattr(response, "body", None)) else b""
        except Exception as exc:
            raise IntakeHold(f"Utica First PDF download failed: {type(exc).__name__}")
        content = bytes(body or b"")
        if _is_csrf_rejection(content):
            raise IntakeHold(
                "Utica First PDF request was rejected by the portal CSRF filter "
                f"for document {doc.name!r}; the bytes are not kept"
            )
        if not _is_pdf(content):
            raise IntakeHold(
                f"Utica First document {doc.name!r} download is not a PDF"
            )
        return content

    def return_to_transactions(self) -> None:
        # The SPA keeps one URL; go back through the tab rather than history.
        self._transaction_id = ""
        self._doclist_url = ""
        self.open_transactions()
        self.select_filter_all()

    def screenshot_transactions(self) -> bytes:
        from .carrier_page_capture import capture_png

        data = capture_png(self.page, full_page=True)
        if not bytes(data or b"")[:8] == _PNG_MAGIC:
            raise IntakeHold("Utica First transactions screenshot is missing or not a PNG")
        return bytes(data)


class UticaDeliveryLedger:
    """Private named-PDF ledger keyed by utica:<policy>:<docId>."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def ensure_private(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        if self.root.is_symlink() or not self.root.is_dir() or self.root.stat().st_mode & 0o077:
            raise IntakeHold("Utica First output directory must be private (0700)")

    def _load(self) -> dict[str, Any]:
        self.ensure_private()
        path = self.root / LEDGER_NAME
        if not path.exists():
            return {"items": {}}
        try:
            data = json.loads(path.read_text())
        except Exception:
            raise IntakeHold("Utica First delivery ledger is missing or ambiguous")
        if not isinstance(data, dict) or not isinstance(data.get("items"), dict):
            raise IntakeHold("Utica First delivery ledger is missing or ambiguous")
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
            raise IntakeHold("Utica First filename is missing or ambiguous")
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
            raise IntakeHold("Existing Utica First file conflicts with the pull ledger")
        return True

    def record(self, source: SourceItem, *, issued_on: date, insured_name: str = "") -> Path:
        self.ensure_private()
        if source.filename == LEDGER_NAME:
            raise IntakeHold("Utica First filename is missing or ambiguous")
        path = self.pdf_path(issued_on, source.filename)
        if path.exists():
            raise IntakeHold("Existing Utica First file conflicts with the pull ledger")
        digest = hashlib.sha256(source.content).hexdigest()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(source.content)
        data = self._load()
        if source.source_id in data["items"]:
            raise IntakeHold("Existing Utica First file conflicts with the pull ledger")
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
            raise IntakeHold("Utica First transactions screenshot is missing or not a PNG")
        path = self.pdf_path(day, f"utica-transactions-{day.isoformat()}.png")
        if path.exists():
            return path
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(png)
        return path


def _received_at(as_of: date) -> str:
    return datetime(as_of.year, as_of.month, as_of.day, tzinfo=timezone.utc).isoformat()


def _row_payload(row: TransactionRow, *, outcome: str, reason: str = "", filename: str = "") -> dict[str, Any]:
    payload = {
        "policy_number": row.policy_number,
        "insured_name": row.insured_name,
        "transaction_type": row.transaction_type,
        "effective_date": row.effective_date.isoformat(),
        "document_id": row.document_id,
        "outcome": outcome,
    }
    if reason:
        payload["hold_reason"] = reason
    if filename:
        payload["filename"] = filename
    return payload


def run_pull(
    browser: PlaywrightUticaCancellationBrowser,
    ledger: UticaDeliveryLedger,
    archive: SourceArchive,
    *,
    as_of: date,
) -> dict[str, Any]:
    """Pull Utica First cancellation notice PDFs.

    For each cancellation-type transaction: open its DOCUMENT LIST, target
    notice documents (a transaction can legitimately carry more than one —
    e.g. the Insured and Agent copies of a NonPay notice), download each via
    DocGenServlet, and record it in the durable ledger. A second run skips
    ledger hits.
    """
    from .document_retrieval_filing import require_carrier_pull

    require_carrier_pull("uticafirst")
    refuse_production_host()
    if not isinstance(as_of, date):
        raise IntakeHold("Utica First as-of date is missing or ambiguous")

    downloaded: list[dict[str, Any]] = []
    held: list[dict[str, Any]] = []
    skipped: list[str] = []
    targeted: list[str] = []
    rows_payload: list[dict[str, Any]] = []
    from .carrier_tabs import snapshot_ids

    pages_before = snapshot_ids(getattr(browser, "page", None))

    try:
        return _utica_pull_body(
            browser, ledger, archive, as_of=as_of,
            downloaded=downloaded, held=held, skipped=skipped,
            targeted=targeted, rows_payload=rows_payload,
        )
    finally:
        from .carrier_tabs import close_new_pages

        close_new_pages(getattr(browser, "page", None), pages_before, keep=getattr(browser, "page", None))


def _utica_pull_body(
    browser: Any,
    ledger: Any,
    archive: Any,
    *,
    as_of: date,
    downloaded: list,
    held: list,
    skipped: list,
    targeted: list,
    rows_payload: list,
) -> dict[str, Any]:
    browser.open_transactions()
    browser.select_filter_all()
    png = browser.screenshot_transactions()
    ledger.save_screenshot(as_of, png)
    rows = browser.load_transactions()
    targets = [row for row in rows if is_cancellation_transaction(row.transaction_type)]

    for row in targets:
        try:
            browser.open_documents(row)
            docs = browser.list_documents(row.policy_number)
        except IntakeHold as exc:
            held.append(_row_payload(row, outcome="HELD", reason=str(exc)))
            browser.return_to_transactions()
            continue
        notices = [
            doc for doc in docs
            if is_cancellation_document(doc.name, doc.description)
        ]
        if not notices:
            held.append(_row_payload(
                row, outcome="HELD",
                reason=(
                    f"Utica First policy {row.policy_number} has no notice "
                    "document in its document list"
                    + (": " + "; ".join(d.name for d in docs[:6]) if docs else "")
                ),
            ))
            browser.return_to_transactions()
            continue
        for doc in notices:
            try:
                if ledger.delivery_status(
                    document_id=doc.document_id,
                    filename=doc.filename,
                    issued_on=doc.added_date,
                ):
                    skipped.append(doc.document_id)
                    targeted.append(doc.document_id)
                    rows_payload.append(_row_payload(
                        row, outcome="ALREADY_DELIVERED", filename=doc.filename
                    ))
                    continue
            except IntakeHold as exc:
                held.append(_row_payload(row, outcome="HELD", reason=str(exc)))
                continue
            try:
                content = browser.download_document(doc)
            except IntakeHold as exc:
                held.append(_row_payload(row, outcome="HELD", reason=str(exc)))
                continue
            source = SourceItem(
                system=PROCESS,
                source_account=UTICA_HOST,
                source_id=doc.document_id,
                source_url=(
                    f"https://{UTICA_HOST}/oneshield/DocGenServlet"
                    f"?docId={urllib.parse.quote(doc.doc_id)}"
                ),
                received_at=_received_at(as_of),
                filename=doc.filename,
                content=content,
            )
            source.validate()
            try:
                saved = ledger.record(source, issued_on=doc.added_date, insured_name=row.insured_name)
                archive.preserve(source)
            except IntakeHold as exc:
                held.append(_row_payload(row, outcome="HELD", reason=str(exc)))
                continue
            downloaded.append({
                "document_id": doc.document_id,
                "filename": doc.filename,
                "sha256": source.digest,
                "bytes": len(content),
                "policy_number": row.policy_number,
                "insured_name": row.insured_name,
                "transaction_type": row.transaction_type,
                "effective_date": row.effective_date.isoformat(),
                "added_date": doc.added_date.isoformat(),
                "doc_name": doc.name,
                "path": str(saved),
            })
            targeted.append(doc.document_id)
            rows_payload.append(_row_payload(row, outcome="PULLED", filename=doc.filename))
        browser.return_to_transactions()

    return {
        "status": "PULLED",
        "scope": SCOPE,
        "process": PROCESS,
        "as_of": as_of.isoformat(),
        "transaction_count": len(rows),
        "cancellation_transaction_count": len(targets),
        "targeted": len(targeted),
        "count": len(downloaded),
        "downloaded": downloaded,
        "skipped_already_delivered": skipped,
        "held": held,
        "rows": rows_payload,
        "ezlynx": "not_run",
    }


def select_utica_page(pages: list[Any]) -> Any:
    """Use the single UFirst Now tab."""
    matches = [
        page for page in pages
        if (urllib.parse.urlsplit(str(getattr(page, "url", "") or "")).hostname or "").lower() == UTICA_HOST
    ]
    if len(matches) != 1:
        raise IntakeHold("Expected exactly one Utica First UFirst Now tab")
    return matches[0]


def ensure_utica_page(cdp_browser: Any) -> Any:
    """Return the one signed-in UFirst Now tab, signing in if there is none.

    BUILT 2026-10-07 (Ralph); tightened in the Test-patch reconcile.

    - Exactly one signed-in UFirst Now tab: use it (expired tabs are ignored).
    - Several signed-in tabs: hold (ambiguous, never guess).
    - None: on hermes-test-01 only, open a new tab and run
      ``utica_login.login_utica`` (Okta + email MFA). Production hosts and any
      other host hold before credentials are read.
    """
    from . import utica_login
    from .document_retrieval_filing import require_carrier_pull

    require_carrier_pull("uticafirst")
    refuse_production_host()
    pages = [p for ctx in cdp_browser.contexts for p in ctx.pages]
    matches = [
        page for page in pages
        if (urllib.parse.urlsplit(str(getattr(page, "url", "") or "")).hostname or "").lower() == UTICA_HOST
    ]
    signed_in = [page for page in matches if utica_login.is_logged_in(page)]
    if len(signed_in) == 1:
        return signed_in[0]
    if len(signed_in) > 1:
        raise IntakeHold("Expected exactly one signed-in Utica First UFirst Now tab")

    # No signed-in tab: auto-login is a Test-host action only.
    require_hermes_test_host()
    contexts = list(getattr(cdp_browser, "contexts", None) or [])
    if not contexts:
        raise IntakeHold("Utica First auto-login needs an open browser context")
    from .carrier_tabs import close_new_pages

    before = set()
    for context in contexts:
        existing_pages = getattr(context, "pages", None)
        if not isinstance(existing_pages, list):
            continue
        for existing in existing_pages:
            before.add(id(existing))
    page = contexts[0].new_page()
    try:
        utica_login.login_utica(page)
        close_new_pages(page, before, keep=page)
        return page
    except Exception:
        try:
            page.close()
        except Exception:
            pass
        raise


def connect_cdp_browser(cdp_url: str | None) -> tuple[PlaywrightUticaCancellationBrowser, Callable[[], None]]:
    """Attach to the local carrier Chrome. Exactly one UFirst Now tab."""
    from .document_retrieval_filing import require_carrier_pull

    require_carrier_pull("uticafirst")
    refuse_production_host()
    require_hermes_test_host()
    url = (cdp_url or os.environ.get("ROBIE_BROWSER_CDP_URL") or DEFAULT_CDP_URL).strip()
    parsed = urllib.parse.urlsplit(url)
    if parsed.username or parsed.password or parsed.scheme not in {"http", "https"}:
        raise IntakeHold("Utica First browser attach must use the local Test CDP endpoint")
    if (parsed.hostname or "").lower() not in {"127.0.0.1", "localhost"}:
        raise IntakeHold("Utica First browser attach must use the local Test CDP endpoint")
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(url)
        pages = [page for context in browser.contexts for page in context.pages]
        return PlaywrightUticaCancellationBrowser(select_utica_page(pages)), playwright.stop
    except Exception:
        playwright.stop()
        raise


def qa_pack_dir(output_root: Path, as_of: date) -> Path:
    folder = output_root / "UticaFirst" / as_of.isoformat()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(folder, 0o700)
    return folder


def main(argv: list[str] | None = None, *, browser_factory: Callable[[Any], Any] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Test-only Utica First cancellation document pull")
    parser.add_argument("--as-of", default=date.today().isoformat())
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--cdp-url", default=None)
    parser.add_argument("--upload-drive", action="store_true")
    args = parser.parse_args(argv)
    if args.upload_drive:
        raise IntakeHold(DRIVE_UPLOAD_UNAVAILABLE)
    as_of = parse_carrier_date(args.as_of)
    output_root = qa_pack_dir(Path(args.output_root), as_of)
    ledger = UticaDeliveryLedger(output_root)
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


if __name__ == "__main__":
    sys.exit(main())
