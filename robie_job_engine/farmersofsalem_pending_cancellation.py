"""Test-only Farmers of Salem pending-cancellation notice pull.

Playbook (verified live 2026-10-02 from Nicole's training):

* farmersofsalem.com/agent_login.aspx signs in to "Farmers Of Salem ::
  Agent Home". ``farmersofsalem_login`` does that once, from
  ``farmers_of_salem_username`` / ``farmers_of_salem_password`` (the newer
  pair; ``farmers_of_salem_robie_*`` is not read).
* Open https://www.farmersofsalem.com/agent/agent_portal.aspx in a
  worker-owned tab. It lands on the Finys policy admin system at
  https://fos.finys.com/ ("THE FINYS SUITE"). A signed-in Finys tab is
  reused. The navbar toggler plus the FOS PORTAL link is the fallback
  when that navigation is not available.
* The Finys landing page shows "My Open Tasks - pending items" (sidebar:
  "My Pending Cancellation Items") with policy number, insured, product,
  and due date.
* For each policy: the top "Policy" menu or the policy-number Search box ->
  Policy Summary -> left nav "Document Summary" -> the document list.
  Click the target row's "View" link; the PDF opens in the Chrome viewer;
  the worker saves the original PDF bytes.

Target documents, one per policy (most recent first): Intent to Cancel
Notice, cancellation notices, underwriting memos, billing memos.

A missing or non-unique control raises IntakeHold. This module does not
upload, note, task, or label in EZLynx, and does not register a timer.
``ensure_finys_page`` signs in when Finys is not already open.
``open_finys_from_portal`` reuses a signed-in fos.finys.com tab, or
opens the agent portal URL in a new tab. The navbar toggler plus the
portal link is the fallback.
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
# Live 2026-10-08: the FOS PORTAL anchor is in the collapsed navbar when
# the Test window is under 992 px. Navigating here lands on fos.finys.com.
AGENT_PORTAL_URL = "https://www.farmersofsalem.com/agent/agent_portal.aspx"
PORTAL_LINK_HREF = 'a[href="agent_portal.aspx"]'
NAVBAR_TOGGLER = "nav div.navbar-toggler"
FOS_PORTAL_LINK = "FOS PORTAL"
DIARY_GRID_ID = "MyOpenTasks_DiaryGrid"
KENDO_PAGE_SIZE = 100
# 771 open tasks at 100 per page is eight pages. Stop rather than read forever.
GRID_PAGE_BUDGET_S = 90
PORTAL_GOTO_MS = 15000
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
DRIVE_QA_FOLDER_NAME = "Robie Carrier Pull QA (Nicole)/Farmers of Salem"
DRIVE_UPLOAD_UNAVAILABLE = (
    "Drive upload of the Farmers of Salem QA pack is not available; "
    "refusing to report the pack as uploaded"
)

# Live policy numbers look like HONJ038633: four letters, six digits.
# Live Finys 2026-10-08: HONJ017732 / CDNJ001979 (4 letters + 6 digits) and
# SCNJM07385 (5 letters + 5 digits); always 10 characters.
_POLICY_NUMBER = re.compile(r"^(?=.{10}$)[A-Za-z]{4,5}\d{5,6}$")
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
    # Live Finys 2026-10-08 (MyOpenTasks_DiaryGrid): Details | | Loss # |
    # Policy/Quote | Insured Name | Notes | Department | Type | Due Days |
    # Due On | Created By.
    ("policy_number", frozenset({"policy number", "policy #", "policy", "policynumber", "policy/quote", "policy / quote"})),
    ("insured_name", frozenset({"insured", "insured name", "named insured", "name"})),
    ("product", frozenset({"product", "line", "lob", "policy type"})),
    ("due_date", frozenset({"due date", "due", "due on", "cancel date", "cancellation date", "effective date"})),
    ("item_type", frozenset({"type", "task type", "item type"})),
    ("department", frozenset({"department", "dept"})),
)
# Open-task types that are cancellation notices. Referral, Reinstatement and
# other diary items on the same grid are not pulled.
_CANCELLATION_TASK_TYPES = ("cancellation", "cancel", "non-pay", "nonpay", "non pay")
# Header aliases for the Document Summary table.
_DOC_FIELDS = (
    ("description", frozenset({"description", "document", "document description", "type", "form", "title"})),
    ("doc_date", frozenset({"date", "document date", "created", "issued", "effective date", "process date"})),
    ("action", frozenset({"action", "view", ""})),
    # Live 2026-10-08 Finys Document Summary: "" | Email | Description |
    # Department | Department Group | Type | Process Date | Remove from list.
    # The notice kind is in Type (e.g. "Intent to Cancel Notice") while
    # Description holds the form code (e.g. "renewal reminder notice").
    ("doc_type", frozenset({"type", "document type"})),
)
# The row's download icon: <a id="dlink_<n>" onclick="...OnDownloadClick"><img></a>.
# The Email and "Remove from list" checkboxes in the same row are never touched.
FINYS_DOC_DOWNLOAD_LINK = "a[id^='dlink_']"


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


def _header_label(cell: Any) -> str:
    """Kendo puts the title in ``.k-link`` / ``.k-column-title``, not the th text."""
    for selector in (".k-link", ".k-column-title"):
        try:
            nodes = cell.locator(selector).all()
        except Exception:
            nodes = []
        texts = []
        for node in nodes or []:
            try:
                text = _norm(node.inner_text())
            except Exception:
                text = ""
            if text:
                texts.append(text)
        if len(texts) == 1:
            return texts[0]
    try:
        return _norm(cell.inner_text())
    except Exception:
        return ""


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
    return tuple(_header_label(cell) for cell in head_cells)


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


def _kendo_body_table(header_table: Any) -> Any | None:
    """Kendo grids split headers and rows into two tables (live Finys 2026-10-08):
    .k-grid-header table holds the <th> cells, .k-grid-content table the rows.
    Return the body table of the same grid, or None when not split."""
    try:
        body = header_table.locator(
            "xpath=ancestor::div[contains(concat(' ', normalize-space(@class), ' '), ' k-grid-header ')]"
            "/following-sibling::div[contains(concat(' ', normalize-space(@class), ' '), ' k-grid-content ')][1]//table"
        )
        count = int(body.count())
    except Exception:
        return None
    if count != 1:
        return None
    return body.first if hasattr(body, "first") else body


class _SplitGrid:
    """Header cells from one table, data rows from its Kendo body table."""

    def __init__(self, header_table: Any, body_table: Any):
        self.header_table = header_table
        self.body_table = body_table

    def locator(self, selector: str) -> Any:
        if selector == "thead th":
            return self.header_table.locator(selector)
        return self.body_table.locator(selector)


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
            body = _kendo_body_table(table)
            candidates.append((_SplitGrid(table, body) if body is not None else table, indexes))
    if len(candidates) != 1:
        raise IntakeHold("Finys table is missing or ambiguous")
    return candidates[0]


def is_cancellation_task_type(value: Any) -> bool:
    text = _norm(value).casefold()
    return any(term in text for term in _CANCELLATION_TASK_TYPES)


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


def _commercial_text(*parts: str) -> bool:
    return "commercial" in " ".join(_norm(part).casefold() for part in parts)


def extract_pending_items(page: Any) -> tuple[PendingItem, ...]:
    """Parse "My Open Tasks - pending items" on the Finys landing page.

    A cancellation row with no usable policy number is recorded on
    ``page.fos_row_holds`` and does not fail the rest of the grid. A
    commercial row with no policy says so.
    """
    require_finys_url(str(getattr(page, "url", "") or ""))
    table, indexes = _find_table_by_headers(
        page, _PENDING_FIELDS, required=("policy_number", "due_date")
    )
    items: list[PendingItem] = []
    holds: list[dict[str, str]] = []
    for row in _table_body_rows(table):
        item_type = _cell_text(row, indexes["item_type"]) if "item_type" in indexes else ""
        policy_text = _cell_text(row, indexes["policy_number"])
        insured = _cell_text(row, indexes["insured_name"]) if "insured_name" in indexes else ""
        product = _cell_text(row, indexes["product"]) if "product" in indexes else ""
        department = _cell_text(row, indexes["department"]) if "department" in indexes else ""
        commercial = _commercial_text(product, department, item_type)
        cancel = (not item_type) or is_cancellation_task_type(item_type)
        if not cancel and commercial:
            try:
                parse_policy_number(policy_text)
            except IntakeHold:
                who = insured or "an unnamed insured"
                holds.append({
                    "policy_number": _norm(policy_text),
                    "insured_name": insured,
                    "product": product or department or item_type,
                    "due_date": _cell_text(row, indexes["due_date"]),
                    "reason": (
                        f"Farmers of Salem commercial item for {who} has no policy number, "
                        "so no notice was pulled."
                    ),
                })
            continue
        if not cancel:
            continue  # Referral / Reinstatement / other diary items
        try:
            policy = parse_policy_number(policy_text)
            due_on = parse_carrier_date(_cell_text(row, indexes["due_date"]))
        except IntakeHold:
            who = insured or "an unnamed insured"
            if commercial:
                reason = (
                    f"Farmers of Salem commercial item for {who} has no policy number, "
                    "so no notice was pulled."
                )
            elif not _norm(policy_text):
                reason = (
                    f"Farmers of Salem pending item for {who} has no policy number, "
                    "so no notice was pulled."
                )
            else:
                reason = (
                    f"Farmers of Salem pending item for {who} has no usable policy number "
                    f"({_norm(policy_text)!r}), so no notice was pulled."
                )
            holds.append({
                "policy_number": _norm(policy_text),
                "insured_name": insured,
                "product": product or department,
                "due_date": _cell_text(row, indexes["due_date"]),
                "reason": reason,
            })
            continue
        items.append(PendingItem(policy_number=policy, insured_name=insured, product=product, due_on=due_on))
    try:
        page.fos_row_holds = holds
    except Exception:
        pass
    return tuple(items)


def _fos_log(message: str) -> None:
    sys.stderr.write(f"FoS {message}\n")
    sys.stderr.flush()


# pageSize(100) on the live Kendo widget, then the pager sizes <select>.
_KENDO_PAGE_SIZE_JS = """
(wanted) => {
  const root = document.querySelector('#MyOpenTasks_DiaryGrid');
  const jq = window.jQuery || window.$;
  if (root && jq) {
    const grid = jq(root).data('kendoGrid');
    if (grid && grid.dataSource && typeof grid.dataSource.pageSize === 'function') {
      if (grid.dataSource.pageSize() !== wanted) grid.dataSource.pageSize(wanted);
      return 'pageSize';
    }
  }
  const select = (root || document).querySelector('.k-pager-sizes select');
  if (!select || !select.options || !select.options.length) return 'unchanged';
  let best = select.options[0];
  for (const option of select.options) {
    const value = parseInt(option.value, 10);
    const bestValue = parseInt(best.value, 10);
    if (!Number.isNaN(value) && (Number.isNaN(bestValue) || value > bestValue)) best = option;
  }
  if (String(select.value) !== String(best.value)) {
    select.value = best.value;
    select.dispatchEvent(new Event('change', { bubbles: true }));
  }
  return 'dropdown';
}
"""

# Click the enabled Kendo next control. 'disabled' / 'absent' ends the walk.
_KENDO_NEXT_JS = """
() => {
  const root = document.querySelector('#MyOpenTasks_DiaryGrid') || document;
  const next = root.querySelector(
    '.k-pager-next, a[title="Go to the next page"], button[title="Go to the next page"]'
  );
  if (!next) return 'absent';
  const disabled = next.classList.contains('k-disabled')
    || next.classList.contains('k-state-disabled')
    || next.getAttribute('aria-disabled') === 'true'
    || next.hasAttribute('disabled');
  if (disabled) return 'disabled';
  next.click();
  return 'clicked';
}
"""


def _wait_for_grid_idle(page: Any) -> None:
    waiter = getattr(page, "wait_for_function", None)
    if not callable(waiter):
        return
    try:
        waiter(
            """() => {
              const grid = document.querySelector('#MyOpenTasks_DiaryGrid');
              if (!grid) return true;
              return !grid.querySelector('.k-loading-mask');
            }""",
            timeout=8000,
        )
    except Exception:
        pass


def _merge_page_holds(page: Any, holds: list[dict[str, str]], seen: set[tuple[str, str, str]]) -> None:
    current = getattr(page, "fos_row_holds", None)
    if not isinstance(current, list):
        return
    for hold in current:
        if not isinstance(hold, dict):
            continue
        key = (str(hold.get("policy_number") or ""), str(hold.get("due_date") or ""), str(hold.get("reason") or ""))
        if key in seen:
            continue
        seen.add(key)
        holds.append(hold)


def _page_evaluate(page: Any) -> Callable[..., Any] | None:
    """A real page script runner. A mock attribute is not one."""
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return None
    if type(evaluate).__module__.startswith("unittest.mock"):
        return None
    return evaluate


def read_all_pending_items(page: Any) -> tuple[PendingItem, ...]:
    """Read the open-tasks grid, including pages after the first ten rows.

    A page without ``evaluate`` (the fixture) is the visible page only.
    Live Finys shows ``1 - 10 of 771``. The page size is set to 100, then
    Next is clicked until it is disabled or the time budget is spent.
    """
    evaluate = _page_evaluate(page)
    if evaluate is None:
        return extract_pending_items(page)
    deadline = monotonic() + GRID_PAGE_BUDGET_S
    _fos_log(f"open tasks grid page size {KENDO_PAGE_SIZE}")
    try:
        evaluate(_KENDO_PAGE_SIZE_JS, KENDO_PAGE_SIZE)
    except Exception as exc:
        _fos_log(f"page size was not changed ({type(exc).__name__})")
    _wait_for_grid_idle(page)
    items: list[PendingItem] = []
    holds: list[dict[str, str]] = []
    seen_items: set[tuple[str, str]] = set()
    seen_holds: set[tuple[str, str, str]] = set()
    stopped = False
    page_index = 0
    while True:
        if monotonic() >= deadline:
            stopped = True
            break
        page_index += 1
        for item in extract_pending_items(page):
            key = (item.policy_number, item.due_on.isoformat())
            if key in seen_items:
                continue
            seen_items.add(key)
            items.append(item)
        _merge_page_holds(page, holds, seen_holds)
        try:
            state = evaluate(_KENDO_NEXT_JS)
        except Exception as exc:
            _fos_log(f"next page was not clicked ({type(exc).__name__})")
            break
        if state != "clicked":
            break
        _fos_log(f"open tasks grid page {page_index + 1}")
        _wait_for_grid_idle(page)
        if monotonic() >= deadline:
            stopped = True
            break
    if stopped:
        holds.append({
            "policy_number": "",
            "insured_name": "",
            "product": "",
            "due_date": "",
            "reason": (
                "Farmers of Salem open-tasks paging hit its time budget; "
                "later pages were not read."
            ),
        })
        _fos_log("open tasks paging stopped at the time budget")
    try:
        page.fos_row_holds = holds
    except Exception:
        pass
    return tuple(items)


# Live 2026-10-08 (f53568f6): the Finys landing has no accessible-named search
# textbox. The Policy Quick Search widget has a Quote row (Button1) and a
# Policy Number row (policyText, then Button2), read on hermes-test-01.
FINYS_POLICY_SEARCH_INPUT = "#Landing_PolicyQuickSearchWidget_policyText"
FINYS_POLICY_SEARCH_BUTTON = "#Landing_PolicyQuickSearchWidget_Button2"
# Policy Summary markers read live: the header label carries the policy number
# and the left-nav Document Summary anchor has this id.
FINYS_SUMMARY_POLICY_LABEL = "#SummaryHeader_PolicyNumberLabelLabelValue"
FINYS_DOCUMENT_SUMMARY_LINK = "#DocumentLibrarySummary"
FINYS_MESSAGE_OK = ".k-window"
FINYS_LANDING_URL = f"https://{FINYS_HOST}/"


def _has_pending_grid(page: Any) -> bool:
    try:
        _find_table_by_headers(page, _PENDING_FIELDS, required=("policy_number", "due_date"))
        return True
    except IntakeHold:
        return False


def _dismiss_finys_message(page: Any) -> None:
    """Close one open Finys "Message" window (Kendo modal) with its Ok button.

    Live 2026-10-08: a search with an empty box leaves "Please enter a Policy
    Number or Insured Name" open, and its overlay blocks every later click.
    """
    try:
        ok = page.locator(FINYS_MESSAGE_OK).get_by_role("button", name="Ok", exact=True)
        if _count(ok) == 1:
            ok.click()
    except Exception:  # noqa: BLE001 - nothing to dismiss
        pass


def _type_into(box: Any, value: str) -> None:
    """Type like a user; Finys ignores a programmatic fill (live 2026-10-08)."""
    box.click()
    box.fill("")
    typer = getattr(box, "press_sequentially", None)
    if callable(typer):
        typer(value, delay=40)
    else:
        box.fill(value)


def _count(locator: Any) -> int:
    try:
        return int(locator.count())
    except Exception:
        return -1


def _quick_search_widget(page: Any) -> tuple[Any, Any] | None:
    """The Policy Number box and its own Search button, exactly one of each."""
    box = page.locator(FINYS_POLICY_SEARCH_INPUT)
    button = page.locator(FINYS_POLICY_SEARCH_BUTTON)
    if _count(box) == 1 and _count(button) == 1:
        return box, button
    return None


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
    _dismiss_finys_message(page)
    widget = _quick_search_widget(page)
    search = widget[0] if widget else _find_search_box(page)
    try:
        if widget:
            _type_into(search, policy)
        else:
            search.fill(policy)
    except Exception as exc:
        raise IntakeHold("Finys policy search box is missing or ambiguous") from exc
    try:
        if widget:
            widget[1].click()
        else:
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
            label = page.locator(FINYS_SUMMARY_POLICY_LABEL)
            if _count(label) == 1 and _norm(label.inner_text()) == policy:
                return True
        except Exception:
            pass
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
    try:
        control = _control(page, "link", "Document Summary")
    except IntakeHold:
        control = page.locator(FINYS_DOCUMENT_SUMMARY_LINK)
        if _count(control) != 1:
            raise
    control.click()

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
        if notice_key is None and "doc_type" in indexes and indexes["doc_type"] != indexes["description"]:
            doc_type = _cell_text(row, indexes["doc_type"])
            if doc_type:
                notice_key = classify_notice(doc_type)
                if notice_key is not None:
                    description = f"{description} ({doc_type})"
        view: Any | None = None
        if notice_key is not None:
            try:
                links = row.get_by_role("link", name="View", exact=False).all()
            except Exception:
                links = []
            exact = [link for link in links if _norm(link.inner_text()).casefold() == "view"]
            candidates = exact or links
            if not candidates:
                try:
                    candidates = row.locator(FINYS_DOC_DOWNLOAD_LINK).all()
                except Exception:
                    candidates = []
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


def _is_pdf_url(url: str) -> bool:
    """A PDF address, query string ignored.

    Live 2026-10-08: the Finys download icon navigates the same tab to
    /FileManager/FileManager/GetFile/<name>.pdf?ft=<token>.
    """
    lowered = str(url or "").lower()
    if lowered.startswith("blob:"):
        return True
    path = urllib.parse.urlsplit(lowered).path
    return path.endswith(".pdf")


def _read_viewer_tab_pdf(page: Any) -> bytes | None:
    """Read PDF bytes when View opened the Chrome viewer (new tab or this tab)."""
    try:
        context = page.context
        pages = list(context.pages)
    except Exception:
        return None
    me = page
    if me not in pages:
        pages.append(me)
    # Other tabs first; this tab last (it is navigated back by the caller).
    pages.sort(key=lambda tab: tab is me)
    for tab in pages:
        url = str(getattr(tab, "url", "") or "")
        if not url:
            continue
        if tab is me and not url.lower().startswith(f"https://{FINYS_HOST}/"):
            continue
        if _is_pdf_url(url):
            try:
                response = context.request.get(url, timeout=DOWNLOAD_TIMEOUT_MS)
            except Exception:
                continue
            try:
                body = bytes(response.body())
            except Exception:
                continue
            if _is_pdf(body):
                if tab is not me:
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
    except Exception as exc:
        # Playwright's TimeoutError is not the builtin one; any "no download"
        # outcome falls through to the viewer (new tab or same-tab PDF).
        if type(exc).__name__ != "TimeoutError" and not isinstance(exc, TimeoutError):
            raise IntakeHold("Document View did not produce a PDF") from exc
        pdf = _read_viewer_tab_pdf(page)
        if pdf is None:
            raise IntakeHold("Document View did not produce a PDF") from None
        return pdf
    download = download_info.value
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "view.pdf"
        try:
            download.save_as(str(path))
            content = path.read_bytes()
        except Exception as exc:
            raise IntakeHold("Document View download is missing or ambiguous") from exc
    return content


def _context_page_list(portal_page: Any) -> list[Any]:
    context = getattr(portal_page, "context", None)
    pages = getattr(context, "pages", None)
    if not isinstance(pages, list):
        return []
    return list(pages)


def _signed_in_finys_tabs(portal_page: Any) -> list[Any]:
    found = []
    for page in _context_page_list(portal_page):
        try:
            require_finys_url(str(getattr(page, "url", "") or ""))
        except IntakeHold:
            continue
        found.append(page)
    return found


def _close_quietly(page: Any) -> None:
    closer = getattr(page, "close", None)
    if not callable(closer):
        return
    try:
        closer()
    except Exception:
        pass


def _goto_portal(page: Any) -> None:
    try:
        page.goto(AGENT_PORTAL_URL, wait_until="domcontentloaded", timeout=PORTAL_GOTO_MS)
    except TypeError:
        page.goto(AGENT_PORTAL_URL)


def _wait_until_finys(page: Any) -> None:
    url = str(getattr(page, "url", "") or "")
    if FINYS_HOST in url:
        return
    waiter = getattr(page, "wait_for_url", None)
    if not callable(waiter):
        return
    try:
        waiter(lambda current: FINYS_HOST in str(current or ""), timeout=PORTAL_GOTO_MS)
    except TypeError:
        waiter(f"**{FINYS_HOST}**", timeout=PORTAL_GOTO_MS)


def _open_portal_in_new_tab(portal_page: Any) -> Any | None:
    """Navigate a new tab to the agent portal. None when that path is unavailable."""
    context = getattr(portal_page, "context", None)
    new_page = getattr(context, "new_page", None) if context is not None else None
    if not callable(new_page):
        return None
    fresh = None
    try:
        fresh = new_page()
        _fos_log(f"open agent portal {AGENT_PORTAL_URL}")
        _goto_portal(fresh)
        _wait_until_finys(fresh)
        require_finys_url(str(getattr(fresh, "url", "") or ""))
    except Exception as exc:
        _fos_log(f"agent portal navigation did not reach Finys ({type(exc).__name__})")
        if fresh is not None:
            _close_quietly(fresh)
        return None
    return fresh


def _click_control(locator: Any) -> None:
    try:
        locator.click(timeout=5000)
    except TypeError:
        locator.click()


def _portal_link(page: Any) -> Any:
    """The one agent_portal.aspx anchor, including one Bootstrap has hidden."""
    try:
        links = page.locator(PORTAL_LINK_HREF)
        count = int(links.count())
    except Exception:
        count = 0
    if count > 1:
        raise IntakeHold("FOS PORTAL link is missing or ambiguous")
    if count == 1:
        return links.first if hasattr(links, "first") else links
    return _control(page, "link", FOS_PORTAL_LINK)


def _open_portal_by_click(portal_page: Any) -> Any:
    """Expand the collapsed navbar, then click the portal link."""
    try:
        toggler = portal_page.locator(NAVBAR_TOGGLER)
        count = int(toggler.count())
    except Exception:
        count = 0
    if count >= 1:
        target = toggler.first if hasattr(toggler, "first") else toggler
        try:
            _fos_log("expand navbar toggler")
            _click_control(target)
        except Exception:
            pass
    link = _portal_link(portal_page)
    try:
        with portal_page.expect_popup(timeout=PORTAL_GOTO_MS) as popup_info:
            _click_control(link)
        finys_page = popup_info.value
    except Exception as exc:
        raise IntakeHold("FOS PORTAL did not open the Finys tab") from exc
    if finys_page is None:
        raise IntakeHold("FOS PORTAL did not open the Finys tab")
    require_finys_url(str(getattr(finys_page, "url", "") or ""))
    return finys_page


def open_finys_from_portal(portal_page: Any) -> Any:
    """Return a signed-in Finys tab from the farmersofsalem.com session.

    An existing fos.finys.com tab is reused. Otherwise a new tab opens
    ``agent/agent_portal.aspx``. The navbar toggler plus the portal link
    is the fallback when that navigation cannot be started.
    """
    host = (urllib.parse.urlsplit(str(getattr(portal_page, "url", "") or "")).hostname or "").lower()
    if PORTAL_HOST not in host and FINYS_HOST not in host:
        raise IntakeHold("Farmers of Salem portal tab is missing or ambiguous")
    existing = _signed_in_finys_tabs(portal_page)
    if existing:
        _fos_log("reuse signed-in Finys tab")
        return existing[0]
    opened = _open_portal_in_new_tab(portal_page)
    if opened is not None:
        return opened
    return _open_portal_by_click(portal_page)

# --- Browser page object ----------------------------------------------------


class FinysFoSBrowser:
    """Playwright page object for an already-signed-in Finys tab."""

    def __init__(self, page: Any):
        self.page = page
        self._tasks_url: str | None = None

    def load_pending_items(self) -> tuple[PendingItem, ...]:
        require_finys_url(str(getattr(self.page, "url", "") or ""))
        try:
            items = read_all_pending_items(self.page)
        except IntakeHold:
            # Live 2026-10-08: the tab was left on a Policy Summary (same
            # https://fos.finys.com/ URL), so the task grid was absent. Close
            # any Finys message, reload the landing page once, and re-read.
            _dismiss_finys_message(self.page)
            self.page.goto(FINYS_LANDING_URL, wait_until="domcontentloaded")
            if not _wait_for(lambda: _has_pending_grid(self.page)):
                raise
            items = read_all_pending_items(self.page)
        holds = getattr(self.page, "fos_row_holds", None)
        self.row_holds = list(holds) if isinstance(holds, list) else []
        try:
            self._tasks_url = str(getattr(self.page, "url", "") or "") or None
        except Exception:
            self._tasks_url = None
        return items

    def screenshot_pending_items(self) -> bytes:
        try:
            from .carrier_page_capture import capture_png

            data = capture_png(self.page, full_page=True)
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
        # The task grid renders a few seconds after the landing loads
        # (live 2026-10-08); wait for it before re-reading.
        _wait_for(lambda: _has_pending_grid(self.page))
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
    held: list[dict[str, Any]] = list(getattr(browser, "row_holds", ()) or [])
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
        try:
            browser.open_policy(item.policy_number)
            browser.open_document_summary()
            documents = browser.list_documents()
        except Exception as exc:  # noqa: BLE001 - one policy holds, the pull goes on
            held.append(_held_row(item, str(exc) if isinstance(exc, IntakeHold) else (
                f"Finys policy {item.policy_number} did not open ({type(exc).__name__})"
            )))
            continue
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


def _finys_pages(browser: Any) -> list[Any]:
    found = []
    for context in getattr(browser, "contexts", []) or []:
        for page in context.pages:
            try:
                host = (urllib.parse.urlsplit(str(page.url or "")).hostname or "").lower()
            except Exception:
                continue
            if host == FINYS_HOST and "login" not in str(page.url or "").lower():
                found.append(page)
    return found


def _select_finys_page(browser: Any) -> Any:
    found = _finys_pages(browser)
    if len(found) != 1:
        raise IntakeHold("Finys tab is missing or ambiguous")
    return found[0]


def ensure_finys_page(browser: Any) -> Any:
    """Return one Finys tab, signing in once when it is not already open."""
    from . import farmersofsalem_login

    require_carrier_pull(CARRIER)
    refuse_production_host()
    found = _finys_pages(browser)
    if len(found) >= 1:
        chosen = found[0]
        for extra in found[1:]:
            closer = getattr(extra, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    pass
        return chosen
    require_hermes_test_host()
    contexts = list(getattr(browser, "contexts", None) or [])
    if not contexts:
        raise IntakeHold("Farmers of Salem sign-in needs an open browser context")
    portal = []
    for context in contexts:
        for page in context.pages:
            host = (urllib.parse.urlsplit(str(getattr(page, "url", "") or "")).hostname or "").lower()
            if PORTAL_HOST in host:
                portal.append(page)
    page = portal[0] if portal else contexts[0].new_page()
    created = page not in portal
    try:
        return farmersofsalem_login.login_farmers(page)
    except Exception:
        if created:
            try:
                page.close()
            except Exception:
                pass
        raise


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
            browser = FinysFoSBrowser(ensure_finys_page(playwright_browser))
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
