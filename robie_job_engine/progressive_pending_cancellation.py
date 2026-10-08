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

Sign-in, when the tab is missing or still on the login host, is
``progressive_login`` (one attempt, ForAgentsOnly, ``progressive-robie-*``
secrets). This module does not upload, note, task, or label in EZLynx, and
does not register a timer.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import socket
import sys
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any, Callable, Iterator

from .carrier_locators import unique_control_ci
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
# Live 2026-10-08: personal-lines policy links now open the new policy
# servicing app (policy-hub/<policy>/policy-and-coverages), not CL Express.
POLICY_SERVICING_HOST = "policyservicing.apps.foragentsonly.com"
POLICY_PAGE_HOSTS = (CL_POLICY_HOST, POLICY_SERVICING_HOST)
POLICY_PAGE_WAIT_MS = 15000
RETURN_TO_REPORT_MS = 15000
STUCK_TAB_GOTO_MS = 20000
# Hard caps. A Playwright timeout that is ignored still trips the alarm,
# holds that policy, and the pull continues. The worker deadline is under
# the 15-minute outer `timeout` so the summary is written before exit 124.
PAGE_BUDGET_S = int(os.environ.get("ROBIE_FAO_PAGE_BUDGET_S", "45"))
POLICY_BUDGET_S = int(os.environ.get("ROBIE_FAO_POLICY_BUDGET_S", "120"))
WORKER_DEADLINE_S = int(os.environ.get("ROBIE_FAO_DEADLINE_S", "840"))
REPORT_PATH = "/managepolicies/reports/policiesneedservice/policiespendingcancellation/"
REPORT_URL = f"https://{FAO_HOST}{REPORT_PATH}"
DEFAULT_CDP_URL = "http://127.0.0.1:9223"
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
# Live policy numbers: 6-12 digits, optional 2-3 letter prefix (e.g. 970498127, NJA129565).
_POLICY_NUMBER = re.compile(r"^(?:[A-Z]{2,3})?\d{6,12}(?:-\d+)?$")
# Report grid headers (column order may vary; matching is by alias).
_REPORT_HEADERS = (
    ("insured_name", frozenset({"primary named insured", "insured", "insured name", "named insured"})),
    ("policy_number", frozenset({"policy number", "policy", "policy #", "pol #"})),
    ("product", frozenset({"product"})),
    ("state", frozenset({"state"})),
    ("agent_code", frozenset({"agent code", "agency", "agt"})),
    ("producer", frozenset({"producer"})),
    ("cancel_date", frozenset({"cancel effective date", "cancel effective d", "cancellation date", "effective date", "cancel date", "renewal effective date", "renewal date"})),
    ("amount_due", frozenset({"amount due", "amount", "premium", "renewal premium"})),
    ("cancel_reason", frozenset({"cancel reason", "reason"})),
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
_STEP_LOG: Path | None = None
_LATEST: dict[str, Any] = {}
_BUDGETS: list[tuple[float, type[BaseException], str]] = []


class FaoBudget(BaseException):
    """A per-page or per-policy alarm. Not an Exception, so Playwright cannot swallow it."""


class FaoDeadline(BaseException):
    """The worker's own deadline. The summary is written and the pull stops."""


def step_log(message: str) -> None:
    """One line on stderr, flushed, and appended to the progress file.

    Block-buffered stdout is empty after a kill. This line is flushed and
    fsynced so a SIGKILL still leaves the last step on disk.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{stamp} FAO {message}\n"
    sys.stderr.write(line)
    sys.stderr.flush()
    path = _STEP_LOG
    if path is None:
        return
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        pass


def _arm_budget() -> None:
    if not _BUDGETS:
        signal.setitimer(signal.ITIMER_REAL, 0)
        return
    soonest = min(_BUDGETS, key=lambda item: item[0])
    signal.setitimer(signal.ITIMER_REAL, max(0.01, soonest[0] - monotonic()))


def _budget_handler(signum: int, frame: Any) -> None:
    now = monotonic()
    due = [item for item in _BUDGETS if item[0] <= now + 0.05]
    moment, exc_type, message = min(due or _BUDGETS, key=lambda item: item[0])
    raise exc_type(message)


@contextmanager
def time_budget(seconds: float, message: str, exc_type: type[BaseException] = FaoBudget) -> Iterator[None]:
    """Arm the worker deadline only.

    A page alarm used to replace this timer. The page call then blocked inside
    Playwright cleanup, and the worker deadline was not armed again until that
    cleanup returned. Page limits are Playwright timeouts. Only the worker
    deadline uses SIGALRM, and nothing else moves it.
    """
    if exc_type is not FaoDeadline:
        yield
        return
    if seconds <= 0:
        raise exc_type(message)
    deadline = monotonic() + seconds
    _BUDGETS.append((deadline, exc_type, message))
    previous = signal.signal(signal.SIGALRM, _budget_handler)
    _arm_budget()
    try:
        yield
    finally:
        try:
            _BUDGETS.remove((deadline, exc_type, message))
        except ValueError:
            pass
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _click(locator: Any, timeout: int = POLICY_PAGE_WAIT_MS) -> None:
    try:
        locator.click(timeout=timeout)
    except TypeError:
        locator.click()


def _needs_fresh_tab(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(
        token in text
        for token in ("timeout", "time budget", "has been closed", "target closed", "target page")
    )


def _cap_page_timeouts(page: Any) -> None:
    ms = PAGE_BUDGET_S * 1000
    for name in ("set_default_timeout", "set_default_navigation_timeout"):
        setter = getattr(page, name, None)
        if not callable(setter):
            continue
        try:
            setter(ms)
        except Exception:
            pass


def _node_visible(node: Any) -> bool:
    visible = getattr(node, "is_visible", None)
    if not callable(visible):
        return True
    try:
        return bool(visible(timeout=500))
    except TypeError:
        try:
            return bool(visible())
        except Exception:
            return False
    except Exception:
        return False


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
    cleaned = _norm(text).replace(" ", "").upper()
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
    cancel_reason: str = ""
    tab_label: str = ""

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
        """Durable ledger identity for an underwriting memo.

        The memo date is part of the key: a policy can carry two memos with
        the same name on different dates (live 2026-10-08, 879176249 and
        879177962), and without the date the second one collided with the
        first in the ledger.
        """
        slug = re.sub(r"[^a-z0-9]+", "-", self.document_name.casefold()).strip("-")
        return f"progressive:{self.policy_number}:memo:{self.document_date.isoformat()}:{slug}"

    @property
    def legacy_memo_document_id(self) -> str:
        """Pre-2026-10-08 memo key (no date); read only, for old ledgers."""
        slug = re.sub(r"[^a-z0-9]+", "-", self.document_name.casefold()).strip("-")
        return f"progressive:{self.policy_number}:memo:{slug}"

    @property
    def memo_filename(self) -> str:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", self.document_name).strip("_")
        return f"{self.policy_number} {safe or 'document'} UW Memo Progressive.pdf"


def is_cancellation_document(document_name: str) -> bool:
    key = _norm(document_name).casefold()
    return any(term in key for term in _CANCELLATION_TERMS)


def newest_cancellation_documents(docs: tuple["FaoDocument", ...] | list["FaoDocument"]) -> list["FaoDocument"]:
    """Cancellation documents on the newest date that has one.

    CL Express lists the whole policy history (live 2026-10-08: Cancel Notice
    9/28, 8/24, 7/20, ...). The pending notice is the newest one; two
    cancellation documents on that same newest date stay ambiguous.
    """
    cancels = [doc for doc in docs if is_cancellation_document(doc.document_name)]
    if not cancels:
        return []
    newest = max(doc.document_date for doc in cancels)
    return [doc for doc in cancels if doc.document_date == newest]


def _is_policy_page_url(url: Any) -> bool:
    host = (urllib.parse.urlsplit(str(url or "")).hostname or "").lower()
    return host in POLICY_PAGE_HOSTS


_DOCUMENTS_NAME = re.compile(r"^\s*documents\s*$", re.IGNORECASE)
_POLICY_HUB_PATH = re.compile(r"^/app/policy-hub/([A-Za-z0-9-]+)/[^/]+/?$")
DOCUMENTS_WAIT_MS = 20000


def _with_one_retry(action: Callable[[], Any], *, what: str) -> Any:
    """Run ``action`` once, and once more if that attempt timed out.

    A second timeout, or any other error, holds with ``what`` instead of
    surfacing a raw timeout.
    """
    error: Exception | None = None
    for attempt in (1, 2):
        try:
            return action()
        except IntakeHold:
            raise
        except Exception as exc:
            error = exc
            timed_out = "timeout" in type(exc).__name__.lower()
            if attempt == 2 or not timed_out:
                break
    name = type(error).__name__ if error is not None else "Error"
    raise IntakeHold(f"Progressive page {what} did not finish ({name}); one retry was used")


def documents_control(page: Any) -> Any | None:
    """The one visible Documents tab or link, any casing. None when absent."""
    for role in ("tab", "link", "button"):
        try:
            locator = page.get_by_role(role, name=_DOCUMENTS_NAME)
            count = int(locator.count())
        except Exception:
            count = 0
        visible = []
        # Cap the scan. A personal-lines page can list dozens of "Documents"
        # nodes; an unbounded is_visible() wait on each one is a multi-minute hang.
        for index in range(min(count, 8)):
            node = locator.nth(index) if hasattr(locator, "nth") else locator
            if _node_visible(node):
                visible.append(node)
        if len(visible) > 1:
            raise IntakeHold("Progressive control 'DOCUMENTS' is missing or ambiguous")
        if visible:
            return visible[0]
    return None


def policy_hub_documents_url(url: str) -> str:
    """``.../policy-hub/<policy>/documents`` for a policy-hub page, else ''."""
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    if (parsed.hostname or "").lower() != POLICY_SERVICING_HOST or parsed.scheme != "https":
        return ""
    match = _POLICY_HUB_PATH.match(parsed.path or "")
    if match is None:
        return ""
    return f"https://{POLICY_SERVICING_HOST}/app/policy-hub/{match.group(1)}/documents"


def _wait_for_policy_documents(page: Any) -> None:
    page.wait_for_selector("text=Policy Documents", timeout=DOCUMENTS_WAIT_MS)


def open_cl_documents(page: Any) -> None:
    """CL Express: the DOCUMENTS header item is an <a>, not a tab (live 2026-10-08).

    Its accessible name is "Documents" (CSS upper-cases it), so match any
    casing, tab role first, then link role; exactly one visible control.
    """
    step_log("CL Express documents control")
    with time_budget(PAGE_BUDGET_S, "Progressive CL Express documents page exceeded its time budget"):
        control = documents_control(page)
        if control is None:
            raise IntakeHold("Progressive control 'DOCUMENTS' is missing or ambiguous")
        control.click()
        _with_one_retry(lambda: _wait_for_policy_documents(page), what="CL Express documents list")


def open_policy_servicing_documents(page: Any) -> None:
    """Personal-lines policy servicing: the same DOCUMENTS control as CL Express.

    The policy-hub coverages page may not show it. One navigation to the
    policy's documents route is tried, then the control again. The documents
    list wait is bounded and retried once. Anything else holds this policy
    with a specific reason.
    """
    step_log("policyservicing documents")
    with time_budget(PAGE_BUDGET_S, "Progressive policyservicing documents page exceeded its time budget"):
        control = documents_control(page)
        if control is None:
            target = policy_hub_documents_url(str(getattr(page, "url", "") or ""))
            if not target:
                raise IntakeHold(
                    "Progressive personal policy on policyservicing has no Documents control"
                )
            step_log(f"policyservicing documents route {target}")

            def go() -> None:
                page.goto(target, wait_until="domcontentloaded", timeout=POLICY_PAGE_WAIT_MS)

            _with_one_retry(go, what="policyservicing documents route")
            control = documents_control(page)
        if control is not None:
            control.click()
        elif not str(getattr(page, "url", "") or "").rstrip("/").endswith("/documents"):
            raise IntakeHold(
                "Progressive personal policy on policyservicing has no Documents control"
            )
        _with_one_retry(
            lambda: _wait_for_policy_documents(page),
            what="policyservicing documents list",
        )


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
    optional_fields = {"amount_due", "cancel_reason"}
    for field, names in _REPORT_HEADERS:
        matches = [i for i, h in enumerate(normed) if h in names]
        if len(matches) == 1:
            indexes[field] = matches[0]
        elif field in optional_fields:
            continue
        else:
            raise IntakeHold("Progressive FAO report headers are missing or ambiguous")
    parsed: list[CancellationRow] = []
    for cells in rows:
        if len(cells) < len(headers):
            continue
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
                amount_due=_norm(cells[indexes["amount_due"]]) if "amount_due" in indexes else "",
                cancel_reason=_norm(cells[indexes["cancel_reason"]]) if "cancel_reason" in indexes else "",
                reason=reason,
                list_url=list_url,
                tab_label=tab_label,
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
        if not matches and field == "name":
            # Live CL Express 2026-10-08: the name column has no header text
            # ("" | Date | Delivery) and holds the document button.
            matches = [i for i, h in enumerate(normed) if not h]
        if len(matches) != 1:
            raise IntakeHold("Progressive FAO document list headers are missing or ambiguous")
        indexes[field] = matches[0]
    docs: list[FaoDocument] = []
    for row_index, cells in enumerate(rows):
        if not any(_norm(cell) for cell in cells):
            continue  # spacer rows between documents
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
    if host != FAO_HOST and host not in POLICY_PAGE_HOSTS:
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
        count = 0
    if count < 1:
        # Fallback: try has_text for links (accessible name may differ from visible text)
        if role == "link":
            locator = page.locator("a", has_text=name)
            try:
                count = int(locator.count())
            except Exception:
                count = 0
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
        step_log("document opened a new tab")

    def _close_opened() -> None:
        for new_page in opened:
            from .carrier_tabs import close_page

            close_page(new_page)

    context = getattr(page, "context", None)
    if context is not None and hasattr(context, "on"):
        context.on("page", _on_page)
    try:
        with time_budget(PAGE_BUDGET_S, "Progressive FAO document capture exceeded its time budget"):
            step_log("document capture")
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
            for new_page in list(opened):
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
                try:
                    view = read_playwright_pdf_view(new_page)
                    viewer_pdfs.extend(view.pdfs)
                except IntakeHold:
                    raise
                except Exception as exc:
                    # Live 2026-10-08 (4a22f443): the PDFHandler GET timed out and
                    # crashed the whole carrier, leaving the viewer tab open.
                    raise IntakeHold(
                        f"Progressive FAO PDF viewer did not return the PDF ({type(exc).__name__})"
                    ) from exc
            return DocumentCapture(tuple(downloads), tuple(viewer_pdfs))
    finally:
        _close_opened()
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
        self._retire: list[Any] = []
        self._pages_before: set[int] | None = None

    # -- navigation -----------------------------------------------------
    def load_report(self) -> None:
        """Open the Policies pending cancel or renewal report directly."""
        step_log("load report")
        _cap_page_timeouts(self.page)
        page = self.page
        current = require_fao_url(str(getattr(page, "url", "") or ""))
        if _is_policy_page_url(current):
            # A tab left on a policy page (CL Express / policy servicing)
            # goes back to the FAO report before the agent check.
            page.goto(REPORT_URL, wait_until="domcontentloaded", timeout=POLICY_PAGE_WAIT_MS)
            require_fao_url(str(getattr(page, "url", "") or ""))
        assert_agent_context(page, self.agent_code)
        page.goto(REPORT_URL, wait_until="domcontentloaded", timeout=POLICY_PAGE_WAIT_MS)
        require_fao_url(str(getattr(page, "url", "") or ""))
        # Wait for report tabs (tables may be hidden until a tab is selected)
        try:
            page.get_by_role("tab", name="Pending Cancellation Due to Non-Payment", exact=True).wait_for(timeout=15000)
        except Exception:
            # FakePage in tests doesn't have wait_for; tables are checked by caller
            pass
        self._list_url = require_fao_url(str(getattr(page, "url", "") or ""))

    def select_tab(self, tab_label: str) -> None:
        """Activate one of the three report tabs."""
        labels = {label for label, _ in _REPORT_TABS}
        if _norm(tab_label) not in labels:
            raise IntakeHold(f"Progressive FAO report tab is missing or ambiguous: {tab_label!r}")
        _click(_unique_control(self.page, "tab", tab_label, exact=True))
        # Wait for the tab's content to load (tables may be hidden initially)
        wait = getattr(self.page, "wait_for_timeout", None)
        if callable(wait):
            wait(8000)

    def load_current_tab(self, tab_label: str) -> tuple[CancellationRow, ...]:
        """Parse the currently displayed tab's rows."""
        page = self.page
        tables = page.locator("table")
        # Find the visible table with the most data rows (page has filter/header tables)
        best = None
        best_rows = -1
        if hasattr(tables, "nth"):
            for i in range(int(tables.count())):
                t = tables.nth(i)
                try:
                    if hasattr(t, "is_visible") and not t.is_visible(timeout=2000):
                        continue
                    rows = t.locator("tbody tr").count()
                    if rows > best_rows:
                        best_rows = rows
                        best = t
                except Exception:
                    continue
        else:
            best = tables.first if hasattr(tables, "first") else tables
        if best is None:
            raise IntakeHold("Progressive FAO report table is missing or ambiguous")
        table = best
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
        step_log(f"policy {policy_number} open summary")
        require_policy_number(policy_number)
        link = _unique_control(self.page, "link", policy_number, exact=True)
        _click(link)

        def landed() -> None:
            self.page.wait_for_url(_is_policy_page_url, timeout=POLICY_PAGE_WAIT_MS)
            require_fao_url(str(getattr(self.page, "url", "") or ""))

        try:
            landed()
        except Exception as exc:
            if not _needs_fresh_tab(exc):
                raise
            step_log(f"policy {policy_number} summary timed out; not retrying on this page")
            raise IntakeHold(
                f"Progressive FAO policy {policy_number} summary page timed out"
            ) from exc

    def open_documents_tab(self) -> None:
        """Open the policy's documents list (CL Express or policy servicing)."""
        host = (urllib.parse.urlsplit(str(getattr(self.page, "url", "") or "")).hostname or "").lower()
        if host == POLICY_SERVICING_HOST:
            open_policy_servicing_documents(self.page)
            return
        open_cl_documents(self.page)

    def list_documents(self, policy_number: str) -> tuple[FaoDocument, ...]:
        """Parse the Policy Documents table on the DOCUMENTS tab.

        Live columns: (icon) | Date | Delivery | Document name. The document
        name is a button that opens the PDF in the viewer.
        """
        table, headers, row_nodes = self._document_table()
        rows = tuple(
            tuple(_norm(_read_text(cell)) for cell in row.locator("td").all())
            for row in row_nodes
        )
        return parse_policy_documents(headers, rows, policy_number=policy_number)

    def _document_table(self) -> tuple[Any, tuple[str, ...], list[Any]]:
        """The documents table, its headers, and its data rows.

        Older markup: <thead><th>...</th></thead><tbody>rows</tbody>.
        Live CL Express 2026-10-08: no thead; the first <tr> holds <th>
        cells ("" | Date | Delivery) and the data rows follow it.
        """
        page = self.page
        tables = page.locator("table")
        if int(tables.count()) < 1:
            raise IntakeHold("Progressive FAO document table is missing or ambiguous")
        table = tables.first if hasattr(tables, "first") else tables
        if hasattr(tables, "nth") and int(tables.count()) > 1:
            found = []
            for i in range(int(tables.count())):
                candidate = tables.nth(i)
                try:
                    if hasattr(candidate, "is_visible") and not candidate.is_visible():
                        continue
                    texts = [_norm(_read_text(n)).casefold() for n in candidate.locator("th").all()]
                except Exception:
                    continue
                if "date" in texts and "delivery" in texts:
                    found.append(candidate)
            if len(found) != 1:
                raise IntakeHold("Progressive FAO document table is missing or ambiguous")
            table = found[0]
        header_nodes = table.locator("thead th").all()
        if header_nodes:
            self._doc_row_selector = "tbody tr"
            self._doc_table = table
            headers = tuple(_norm(_read_text(node)) for node in header_nodes)
            return table, headers, table.locator("tbody tr").all()
        all_rows = table.locator("tr").all()
        if not all_rows:
            raise IntakeHold("Progressive FAO document headers are missing or ambiguous")
        header_cells = all_rows[0].locator("th").all()
        if not header_cells:
            raise IntakeHold("Progressive FAO document headers are missing or ambiguous")
        headers = tuple(_norm(_read_text(node)) for node in header_cells)
        self._doc_row_selector = "tr"
        self._doc_row_offset = 1
        self._doc_table = table
        return table, headers, all_rows[1:]

    def document_button(self, doc: FaoDocument) -> Any:
        """The clickable control for one document row, located by row index.

        Rows are selected by index (not by name) so duplicate document names
        on the same policy stay unambiguous.
        """
        table = getattr(self, "_doc_table", None) or self.page.locator("table").first
        selector = getattr(self, "_doc_row_selector", "tbody tr")
        offset = getattr(self, "_doc_row_offset", 0) if selector == "tr" else 0
        row = table.locator(selector).nth(doc.row_index + offset)
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

    def replace_stuck_tab(self) -> None:
        """Swap a hung FAO tab for a fresh one on the report, same signed-in context.

        Live 2026-10-08 (f53568f6): the policygateway link for some policies
        (e.g. 984419689) never commits a navigation. The tab stays stuck, so
        the return to the report and every later policy timed out (11 held).
        Open the report in a new tab of the same browser context (cookies,
        no credentials typed), then close the stuck tab.
        """
        old = self.page
        context = getattr(old, "context", None)
        if callable(context):
            context = context()
        new_page = getattr(context, "new_page", None)
        if not callable(new_page):
            raise IntakeHold("Progressive FAO tab is stuck and no browser context is available")
        step_log("fresh report tab")
        fresh = new_page()
        if not hasattr(self, "_retire"):
            self._retire = []
        self._retire.append(old)
        try:
            fresh.goto(self._list_url or REPORT_URL, wait_until="domcontentloaded", timeout=STUCK_TAB_GOTO_MS)
            require_fao_url(str(getattr(fresh, "url", "") or ""))
        except Exception:
            from .carrier_tabs import close_page

            close_page(fresh)
            raise
        self.page = fresh
        from .carrier_tabs import close_page

        close_page(old)

    def finish_tabs(self) -> None:
        """Close tabs this run opened, and any tab a stuck-policy swap retired.

        The page the pull is still using stays open.
        """
        from .carrier_tabs import close_listed, close_new_pages, context_pages

        close_listed(list(getattr(self, "_retire", []) or []), keep=self.page)
        before = getattr(self, "_pages_before", None)
        if before is None:
            return
        close_new_pages(self.page, before, keep=self.page)
        # A swap that failed to close the old Manage Policies tab is in _retire.
        # Also drop a second FAO tab that appeared in this context after the snapshot.
        for page in context_pages(self.page):
            if page is self.page or id(page) in before:
                continue
            close_listed([page])

    def return_to_report(self) -> None:
        step_log("return to report")
        self.page.goto(self._list_url, wait_until="domcontentloaded", timeout=RETURN_TO_REPORT_MS)
        # Wait for report tabs first (they render before the data tables),
        # then wait for a visible table with data rows.
        try:
            self.page.get_by_role("tab").first.wait_for(timeout=20000)
        except Exception:
            pass

        def tables_ready() -> None:
            self.page.wait_for_function(
                """() => {
                    const tables = Array.from(document.querySelectorAll('table'));
                    return tables.some(t => t.offsetParent !== null && t.querySelector('tbody tr'));
                }""",
                timeout=25000,
            )

        _with_one_retry(tables_ready, what="pending-cancellation report table")

    def screenshot_report(self) -> bytes:
        from .carrier_page_capture import capture_png

        data = capture_png(self.page, full_page=True)
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

    def delivery_status(
        self, *, document_id: str, filename: str, issued_on: date, legacy_id: str | None = None,
    ) -> bool:
        """True when this exact document was already delivered.

        ``legacy_id`` is an older key for the same document; it only counts
        when its entry names this exact file and issued date.
        """
        self.ensure_private()
        path = self.pdf_path(issued_on, filename)
        items = self._load()["items"]
        entry = items.get(document_id)
        if entry is None and legacy_id:
            legacy = items.get(legacy_id)
            if (
                isinstance(legacy, dict)
                and legacy.get("filename") == filename
                and legacy.get("issued_date") == issued_on.isoformat()
            ):
                entry = legacy
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
        "cancel_reason": row.cancel_reason,
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
                document_id=memo_id, filename=doc.memo_filename, issued_on=doc.document_date,
                legacy_id=doc.legacy_memo_document_id,
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
        try:
            capture = browser.capture_document(doc)
        except Exception as exc:  # noqa: BLE001 - one memo holds, the pull goes on
            held.append(_row_payload(row, outcome="HELD", reason=str(exc) if isinstance(exc, IntakeHold) else (
                f"Progressive FAO memo {doc.document_name!r} capture failed ({type(exc).__name__})"
            )))
            continue
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

    global _STEP_LOG
    downloaded: list[dict[str, Any]] = []
    held: list[dict[str, Any]] = []
    skipped: list[str] = []
    targeted: list[str] = []
    rows_payload: list[dict[str, Any]] = []
    uw_memos: list[dict[str, Any]] = []
    all_rows: list[CancellationRow] = []
    progress = {"done": 0}
    _STEP_LOG = Path(getattr(ledger, "root", ".")) / "fao-progress.log"
    from .carrier_tabs import snapshot_ids

    browser._pages_before = snapshot_ids(getattr(browser, "page", None))

    def publish(extra: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {
            "status": "PULLED",
            "scope": SCOPE,
            "process": PROCESS,
            "as_of": as_of.isoformat(),
            "found": len(all_rows),
            "cancellation_count": len(all_rows),
            "targeted": len(targeted),
            "count": len(downloaded),
            "downloaded": downloaded,
            "uw_memos": uw_memos,
            "uw_memo_count": len([item for item in uw_memos if item.get("outcome") == "PULLED"]),
            "skipped_already_delivered": skipped,
            "held": held,
            "rows": rows_payload,
            "ezlynx": "not_run",
        }
        if extra:
            payload.update(extra)
        _LATEST.clear()
        _LATEST.update(payload)
        step_log(f"summary found={payload['found']} downloaded={payload['count']} held={len(held)}")
        return payload

    try:
        with time_budget(
            WORKER_DEADLINE_S,
            "Progressive FAO worker deadline reached before the pull finished",
            FaoDeadline,
        ):
            return _pull_policies(
                browser, ledger, archive, as_of=as_of,
                downloaded=downloaded, held=held, skipped=skipped, targeted=targeted,
                rows_payload=rows_payload, uw_memos=uw_memos, all_rows=all_rows,
                progress=progress, publish=publish,
            )
    except FaoDeadline as exc:
        left = max(0, len(all_rows) - progress["done"])
        reason = f"Progressive FAO worker deadline reached; {left} policies left unprocessed"
        step_log(reason)
        return publish({"status": "PARTIAL", "reason": reason, "unprocessed": left, "deadline": str(exc)})
    finally:
        finish = getattr(browser, "finish_tabs", None)
        if callable(finish):
            try:
                finish()
            except Exception:
                pass


def _pull_policies(
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
    uw_memos: list,
    all_rows: list,
    progress: dict,
    publish: Callable[..., dict],
) -> dict[str, Any]:
    """Walk the report. A page or policy budget holds that policy and continues."""
    step_log("load report")
    browser.load_report()
    png = browser.screenshot_report()
    ledger.save_screenshot(as_of, png)
    for tab_label, _reason_name in _REPORT_TABS:
        step_log(f"report tab {tab_label}")
        browser.select_tab(tab_label)
        rows = browser.load_current_tab(tab_label)
        step_log(f"report tab {tab_label}: {len(rows)} rows")
        all_rows.extend(rows)
    step_log(f"found {len(all_rows)} policies")
    publish()
    for row in all_rows:
        step_log(f"policy {row.policy_number} start ({row.reason})")
        publish()
        try:
            with time_budget(
                POLICY_BUDGET_S,
                f"Progressive FAO policy {row.policy_number} exceeded its time budget",
            ):
                if row.tab_label:
                    browser.select_tab(row.tab_label)
                browser.open_policy_summary(row.policy_number)
                browser.open_documents_tab()
                docs = browser.list_documents(row.policy_number)
                if row.reason == "UNDERWRITING":
                    _pull_underwriting_memos(
                        row, docs, browser, ledger, archive,
                        as_of=as_of, held=held, uw_memos=uw_memos,
                    )
                targets = newest_cancellation_documents(docs)
                if len(targets) != 1:
                    held.append(_row_payload(
                        row, outcome="HELD",
                        reason=(
                            f"Progressive FAO policy {row.policy_number} cancellation document "
                            f"is missing or ambiguous (found {len(targets)})"
                        ),
                    ))
                    step_log(f"policy {row.policy_number} held: document count {len(targets)}")
                    _recover_report_tab(browser)
                    progress["done"] += 1
                    continue
                doc = targets[0]
                try:
                    if ledger.delivery_status(
                        document_id=doc.document_id, filename=doc.filename, issued_on=doc.document_date
                    ):
                        skipped.append(doc.document_id)
                        targeted.append(doc.document_id)
                        rows_payload.append(_row_payload(row, outcome="ALREADY_DELIVERED", filename=doc.filename))
                        step_log(f"policy {row.policy_number} already delivered")
                        _recover_report_tab(browser)
                        progress["done"] += 1
                        continue
                except IntakeHold as exc:
                    held.append(_row_payload(row, outcome="HELD", reason=str(exc)))
                    _recover_report_tab(browser, exc)
                    progress["done"] += 1
                    continue
                step_log(f"policy {row.policy_number} capture {doc.document_name}")
                try:
                    capture = browser.capture_document(doc)
                except FaoDeadline:
                    raise
                except (Exception, FaoBudget) as exc:
                    held.append(_row_payload(row, outcome="HELD", reason=str(exc) if isinstance(exc, (IntakeHold, FaoBudget)) else (
                        f"Progressive FAO document {doc.document_name!r} capture failed ({type(exc).__name__})"
                    )))
                    _recover_report_tab(browser, exc)
                    progress["done"] += 1
                    continue
                pdfs = list(capture.downloads) + list(capture.viewer_pdfs)
                if len(pdfs) != 1:
                    held.append(_row_payload(
                        row, outcome="HELD",
                        reason=f"Progressive FAO document {doc.document_name!r} capture is missing or ambiguous",
                    ))
                    _recover_report_tab(browser)
                    progress["done"] += 1
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
                saved = ledger.record(source, issued_on=doc.document_date, insured_name=row.insured_name)
                archive.preserve(source)
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
                step_log(f"policy {row.policy_number} pulled")
                _recover_report_tab(browser)
            progress["done"] += 1
        except FaoDeadline:
            raise
        except (Exception, FaoBudget) as exc:
            reason = str(exc) if isinstance(exc, (IntakeHold, FaoBudget)) else (
                f"Progressive FAO policy {row.policy_number} page did not open ({type(exc).__name__})"
            )
            held.append(_row_payload(row, outcome="HELD", reason=reason))
            step_log(f"policy {row.policy_number} held: {reason}")
            _recover_report_tab(browser, exc)
            progress["done"] += 1
            continue
    return publish()


def _recover_report_tab(browser: Any, exc: BaseException | None = None) -> None:
    """Back to the report. A timed-out page is not used again.

    Opening the report on that page is what sat for 12 minutes after the
    summary-page timeout (the page or context was already closed). A fresh
    tab in the same context loads the report URL instead.
    """
    replace = getattr(browser, "replace_stuck_tab", None)
    if exc is not None and _needs_fresh_tab(exc):
        step_log("timed-out page will not be reused; opening a fresh report tab")
        if callable(replace):
            try:
                replace()
            except (Exception, FaoBudget):  # noqa: BLE001
                step_log("fresh report tab did not open")
        return
    try:
        browser.return_to_report()
        return
    except (Exception, FaoBudget) as err:  # noqa: BLE001
        step_log(f"return to report failed ({type(err).__name__}); opening a fresh report tab")
    if callable(replace):
        try:
            replace()
        except (Exception, FaoBudget):  # noqa: BLE001
            step_log("fresh report tab did not open")


def _fao_family_host(url: str) -> bool:
    host = (urllib.parse.urlsplit(str(url or "")).hostname or "").lower()
    if host == "foragentsonly.com" or host.endswith(".foragentsonly.com"):
        return True
    return host == "foragentsonlylogin.progressive.com" or host.endswith(".foragentsonlylogin.progressive.com")


def ensure_fao_page(cdp_browser: Any) -> Any:
    """Return one signed-in ForAgentsOnly tab, signing in once if there is none."""
    from . import progressive_login
    from .document_retrieval_filing import require_carrier_pull

    require_carrier_pull("fao")
    refuse_production_host()
    pages = [page for context in getattr(cdp_browser, "contexts", []) or [] for page in context.pages]
    family = [page for page in pages if _fao_family_host(getattr(page, "url", ""))]
    signed = [page for page in family if progressive_login.is_signed_in(page)]
    if len(signed) == 1:
        return signed[0]
    if len(signed) > 1:
        raise IntakeHold("Expected exactly one Progressive FAO tab")
    require_hermes_test_host()
    contexts = list(getattr(cdp_browser, "contexts", None) or [])
    if not contexts:
        raise IntakeHold("Progressive sign-in needs an open browser context")
    page = family[0] if family else contexts[0].new_page()
    created = page not in family
    try:
        progressive_login.login_progressive(page)
    except Exception:
        if created:
            try:
                page.close()
            except Exception:
                pass
        raise
    return page


def select_fao_page(pages: list[Any]) -> Any:
    """Use the single FAO tab."""
    # The FAO tab may sit on a policy page (CL Express / policy servicing)
    # from the previous run; it is still the one FAO tab.
    hosts = {FAO_HOST, *POLICY_PAGE_HOSTS}
    matches = [
        page for page in pages
        if (urllib.parse.urlsplit(str(getattr(page, "url", "") or "")).hostname or "").lower() in hosts
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
        return PlaywrightFaoCancellationBrowser(ensure_fao_page(browser), agent_code=agent_code), playwright.stop
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
    def _on_stop(signum: int, frame: Any) -> None:
        step_log("stop signal; writing summary")
        payload = dict(_LATEST) or {
            "status": "STOPPED",
            "found": 0,
            "downloaded": [],
            "held": [],
            "count": 0,
            "ezlynx": "not_run",
        }
        payload["reason"] = payload.get("reason") or "stopped by the outer timeout"
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        sys.stdout.flush()
        os._exit(124)

    previous_stop = signal.signal(signal.SIGTERM, _on_stop)
    try:
        if browser_factory is not None:
            browser = browser_factory(args)
            receipt = run_pull(browser, ledger, archive, as_of=as_of)
        else:
            browser, close = connect_cdp_browser(args.cdp_url, agent_code=args.agent_code)
            try:
                receipt = run_pull(browser, ledger, archive, as_of=as_of)
            finally:
                close()
        print(json.dumps(receipt, indent=2, sort_keys=True), flush=True)
        return 0
    finally:
        signal.signal(signal.SIGTERM, previous_stop)


if __name__ == "__main__":
    sys.exit(main())
