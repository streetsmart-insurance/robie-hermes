"""Test-only Progressive For Agents Only Communications memo list and download.

The manual prove on 2026-09-26 walked foragentsonly.com as agent CA33617:
Manage Policies, Policy Activity, an explicit processed-date window,
Communications, then each Memo row saved as
``[PolicyNumber] Progressive Memo [Reason].pdf``.

On the authenticated FAO header the Manage Policies control's accessible
name is ``Manage Policies Home`` (``aria-label`` wins over the visible text)
and it is hidden until Main Navigation is expanded once. The same link is
``a[data-at="header-nav__parent-link--manage-policies"]``. Policy Activity is
either that header name or the landing link ``View policy activity reports``.
Policy Activity then requires View Activity By ``Processed Date``
(``select#PDDateType``, option ``PROCESSEDDATE``), the preset
``select#PDDateRange`` option ``Select Date Range`` (its value is read from
that option), then the page-level Start Date and End Date inputs that the
option reveals, and ``Get Policy Activity``. Those date inputs are not
children of the preset select; they stay hidden until it is chosen.
Get Policy Activity navigates to Policy Activity processed-date results.
The live landing is ``.../processeddateresults/cancels/`` (Cancels, Lapses,
Reinstates). Sibling sections share that prefix. There is no Search button
on that page. The next control is the Communications tab. A missing results
URL, or a missing or ambiguous Communications tab, raises IntakeHold. This
module does not log in and does not submit OTP. Portal code does not upload,
note, task, or label in EZLynx.

``--pull-only`` stops after the local QA pack. The default path calls
:func:`robie_job_engine.document_retrieval_filing.file_progressive_memos`
after a successful pull. That filing stage is inert unless ``ROBIE_ENV=TEST``,
the host is ``hermes-test-01``, and ``ROBIE_DOCUMENT_RETRIEVAL_FILE_EZLYNX=1``.
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
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .intake_core import IntakeHold, SourceArchive, SourceItem, require_test
from .progressive_retrieval import ProgressiveRetrieval, require_bounded_scope


FAO_SCOPE = "fao_communications"
DEFAULT_AGENT_CODE = "CA33617"
DEFAULT_CDP_URL = "http://127.0.0.1:9222"
DOWNLOAD_TIMEOUT_MS = 8000
DATE_CONTROL_TIMEOUT_MS = 8000
LEDGER_NAME = "fao-memo-ledger.json"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
DEFAULT_QA_ROOT = Path(
    "/opt/streetsmart-hermes-test/robie-job-engine/data/artifacts/carrier-pull-qa/progressive"
)
# Shared Drive "Robie Carrier Pull QA (Nicole)" and its Progressive child.
# Folder upload is TODO. The recording uploader writes one video/webm file and
# is not a QA-folder helper, so --upload-drive fails closed and does not call Google.
DRIVE_QA_PARENT_ID = "1cLEpR-0T6KdiVjcdAr0qpGTO447MetI2"
DRIVE_PROGRESSIVE_FOLDER_ID = "1MMojqm99ft4DgxplMuBvz-eTnKdgpY9U"
DRIVE_QA_FOLDER_NAME = "Robie Carrier Pull QA (Nicole)/Progressive"
DRIVE_UPLOAD_UNAVAILABLE = (
    "Drive upload of the Progressive QA pack is not available; "
    "refusing to report the pack as uploaded"
)
_AGENT_CODE = re.compile(r"^CA\d{5}$")
_AGENT_CODE_IN_TEXT = re.compile(r"\bCA\d{5}\b")
# Live FAO Home renders StreetSmart as "Streetsmart Risk Mgr (33617)" and the
# login id "33617c", with no "CA33617" literal. Parenthesized ##### and a
# #####c login are agency displays for whichever agency they name. A bare
# 5-digit token counts only for this agency (33617); other bare numbers are
# not agent codes (ZIP codes and similar).
_PAREN_AGENCY_IN_TEXT = re.compile(r"\(\s*(\d{5})\s*\)")
_LOGIN_AGENCY_IN_TEXT = re.compile(r"\b(\d{5})c\b", re.IGNORECASE)
_BARE_STREETSMART_AGENCY = re.compile(rf"\b{DEFAULT_AGENT_CODE[2:]}\b")
_POLICY_NUMBER = re.compile(r"^\d{6,12}$")
# Live FAO header (hermes-test-01, authenticated Home): aria-label
# "Manage Policies Home" on a[data-at="header-nav__parent-link--manage-policies"].
# The prefix also matches a control whose accessible name is exactly
# "Manage Policies". Policy Activity on that landing is either the header
# name or "View policy activity reports".
MANAGE_POLICIES_LABEL = "Manage Policies"
MANAGE_POLICIES_CSS = 'a[data-at="header-nav__parent-link--manage-policies"]'
MANAGE_POLICIES_NAME = re.compile(r"^Manage Policies")
POLICY_ACTIVITY_LABEL = "Policy Activity"
POLICY_ACTIVITY_NAMES = ("Policy Activity", "View policy activity reports")
MAIN_NAVIGATION_NAME = "Main Navigation"
# Policy Activity date filter. Release 65740660 (includes #606) cleared the
# old "Processed date from" hold, then held on Start Date: #PDDateRange is a
# preset <select>, not a wrapper, and the date inputs stay hidden until the
# "Select Date Range" option is chosen. The option value is read from the
# live option. The same strings are the locator contract in
# locators/progressive_fao.json.
VIEW_ACTIVITY_BY_LABEL = "View Activity By"
VIEW_ACTIVITY_BY_CSS = 'select#PDDateType[name="DateType"]'
PROCESSED_DATE_OPTION_LABEL = "Processed Date"
PROCESSED_DATE_OPTION_VALUE = "PROCESSEDDATE"
PROCESSED_DATE_OPTION_CSS = 'option[value="PROCESSEDDATE"]'
PROCESSED_DATE_RANGE_LABEL = "Processed date range"
PROCESSED_DATE_RANGE_CSS = "select#PDDateRange"
CUSTOM_DATE_RANGE_LABEL = "Select Date Range"
START_DATE_LABEL = "Start Date"
START_DATE_CSS = (
    'input[type="date"]#js-datepicker__date-start'
    '[data-at="datatable-daterangepicker-startdate"]'
)
END_DATE_LABEL = "End Date"
END_DATE_CSS = 'input[type="date"][data-at="datatable-daterangepicker-enddate"]'
GET_POLICY_ACTIVITY_LABEL = "Get Policy Activity"
GET_POLICY_ACTIVITY_CSS = '[data-at="ProcessedDateButton"]'
COMMUNICATIONS_TAB_LABEL = "Communications"
PROCESSED_DATE_RESULTS_HOLD = (
    "Policy Activity processed-date results page is missing or ambiguous"
)
# Live landing after Get Policy Activity (hermes-test-01, release 00a0ae294ec0):
# https://www.foragentsonly.com/managepolicies/policyactivity/processeddateresults/cancels/
# Title: Policy Activity Processed Date Results – Cancels, Lapses, Reinstates.
# One section slug. Sibling sections use the same prefix. No Search control.
_PROCESSED_DATE_RESULTS_URL = re.compile(
    r"^https://(?:[a-z0-9-]+\.)*foragentsonly\.com"
    r"/managepolicies/policyactivity/processeddateresults/([a-z0-9_-]+)/?$",
    re.IGNORECASE,
)
_SHELL_NAV_ROLES = ("link", "button")
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


def agent_codes_in_text(text: str) -> frozenset[str]:
    """Canonical ``CA#####`` codes implied by FAO page text.

    ``CA33617``, ``(33617)``, bare ``33617``, and login ``33617c`` are one
    StreetSmart agency. Any other ``CA#####``, parenthesized ``(#####)``, or
    ``#####c`` login is a different agency. No recognized code is an empty set.
    """
    raw = str(text or "")
    found = set(_AGENT_CODE_IN_TEXT.findall(raw))
    found.update(f"CA{digits}" for digits in _PAREN_AGENCY_IN_TEXT.findall(raw))
    found.update(f"CA{digits}" for digits in _LOGIN_AGENCY_IN_TEXT.findall(raw))
    if _BARE_STREETSMART_AGENCY.search(raw):
        found.add(DEFAULT_AGENT_CODE)
    return frozenset(found)


def assert_agent_context(page: Any, agent_code: str) -> None:
    body = page.locator("body").inner_text()
    found = agent_codes_in_text(str(body or ""))
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


def _control_hold(label: str) -> IntakeHold:
    return IntakeHold(f"Progressive control {label!r} is missing or ambiguous")


def _union(locators: list[Any]) -> Any:
    merged = locators[0]
    for locator in locators[1:]:
        merged = merged.or_(locator)
    return merged


def _role_locator(page: Any, name: str | re.Pattern, *, exact: bool) -> Any:
    return _union([
        page.get_by_role(role, name=name, exact=exact)
        for role in _SHELL_NAV_ROLES
    ])


def _is_visible(locator: Any) -> bool:
    probe = getattr(locator, "is_visible", None)
    if not callable(probe):
        return False
    try:
        return bool(probe())
    except Exception:
        return False


def _survey(locator: Any) -> tuple[list[Any], int]:
    total = int(locator.count())
    visible: list[Any] = []
    for index in range(total):
        item = locator.nth(index)
        if _is_visible(item):
            visible.append(item)
    return visible, total


def _shell_ready(visible: list[Any], total: int, *, unique_element: bool) -> bool:
    if unique_element:
        return total == 1 and len(visible) == 1
    return len(visible) == 1


def _shell_ambiguous(visible: list[Any], total: int, *, unique_element: bool) -> bool:
    if len(visible) > 1:
        return True
    if unique_element:
        return total > 1
    return total > 1 and len(visible) == 0


def _expand_main_navigation(page: Any) -> None:
    visible, total = _survey(_role_locator(page, MAIN_NAVIGATION_NAME, exact=True))
    if len(visible) != 1 or total != 1:
        raise _control_hold(MAIN_NAVIGATION_NAME)
    visible[0].click()


def click_shell_nav(
    page: Any,
    locator_factory: Callable[[], Any],
    *,
    label: str,
    unique_element: bool,
) -> None:
    """Click one FAO shell control, expanding Main Navigation once if needed.

    Manage Policies is ``unique_element``: the data-at link and the
    ``^Manage Policies`` accessible name must be the same single element.
    Policy Activity is not: ``Policy Activity`` and ``View policy activity
    reports`` are the same destination, so one visible match is clicked and
    a second hidden match does not hold. Two visible matches, or two hidden
    matches, hold. A hidden or absent match expands Main Navigation once,
    then the same rule runs again. The hidden control is never clicked.
    """
    visible, total = _survey(locator_factory())
    if _shell_ready(visible, total, unique_element=unique_element):
        visible[0].click()
        return
    if _shell_ambiguous(visible, total, unique_element=unique_element):
        raise _control_hold(label)
    _expand_main_navigation(page)
    visible, total = _survey(locator_factory())
    if not _shell_ready(visible, total, unique_element=unique_element):
        raise _control_hold(label)
    visible[0].click()


def _manage_policies_locator(page: Any) -> Any:
    """One Manage Policies element, or an empty locator when nothing matches.

    The data-at selector and the ``^Manage Policies`` name are checked
    separately. A union would be ambiguous when those queries hit different
    elements, and it can also count one element twice. Both queries matching
    one shared element is the live header link. Either query matching more
    than one element holds.
    """
    css = page.locator(MANAGE_POLICIES_CSS)
    role = _role_locator(page, MANAGE_POLICIES_NAME, exact=False)
    css_count = int(css.count())
    role_count = int(role.count())
    if css_count > 1 or role_count > 1:
        raise _control_hold(MANAGE_POLICIES_LABEL)
    if css_count == 1 and role_count == 1 and int(css.and_(role).count()) != 1:
        raise _control_hold(MANAGE_POLICIES_LABEL)
    if css_count == 1:
        return css
    return role


def _policy_activity_locator(page: Any) -> Any:
    return _union([
        _role_locator(page, name, exact=True)
        for name in POLICY_ACTIVITY_NAMES
    ])


_ACCESSIBLE_NAME_JS = """(el) => {
  const norm = (value) => String(value || "").replace(/\\s+/g, " ").trim();
  const aria = norm(el.getAttribute("aria-label"));
  if (aria) return aria;
  const labels = el.labels ? Array.from(el.labels) : [];
  if (labels.length !== 1) return "";
  return norm(labels[0].innerText || labels[0].textContent || "");
}"""


def _accessible_name(locator: Any, label: str) -> str:
    evaluate = getattr(locator, "evaluate", None)
    if not callable(evaluate):
        raise _control_hold(label)
    try:
        return _norm(str(evaluate(_ACCESSIBLE_NAME_JS) or ""))
    except IntakeHold:
        raise
    except Exception as exc:
        raise _control_hold(label) from exc


def _require_expected_label(page: Any, located: Any, label: str) -> None:
    """Hold unless this one element is unlabeled or carries exactly ``label``.

    A unique ``data-at`` / id match is the control when no accessible name is
    readable. A readable name other than ``label`` holds. ``label`` on a
    different element, or more than one such label, holds.
    """
    if int(located.count()) != 1:
        raise _control_hold(label)
    named = page.get_by_label(label, exact=True)
    named_count = int(named.count())
    if named_count > 1 or (named_count == 1 and int(located.and_(named).count()) != 1):
        raise _control_hold(label)
    own = _accessible_name(located, label)
    if own and own != label:
        raise _control_hold(label)


def _unique_labeled(page: Any, css: str, label: str) -> Any:
    located = page.locator(css)
    if int(located.count()) != 1:
        raise _control_hold(label)
    _require_expected_label(page, located, label)
    return located


def _option_text(option: Any, label: str) -> str:
    try:
        return _norm(str(option.inner_text()))
    except IntakeHold:
        raise
    except Exception as exc:
        raise _control_hold(label) from exc


def _option_value(option: Any, label: str) -> str:
    try:
        return _norm(str(option.get_attribute("value") or ""))
    except IntakeHold:
        raise
    except Exception as exc:
        raise _control_hold(label) from exc


def _unique_visible_control(page: Any, css: str, label: str) -> Any:
    """One page-level match that is visible. Hidden or extra matches hold."""
    located = page.locator(css)
    wait = getattr(located, "wait_for", None)
    if not callable(wait):
        raise _control_hold(label)
    try:
        wait(state="visible", timeout=DATE_CONTROL_TIMEOUT_MS)
    except IntakeHold:
        raise
    except Exception as exc:
        raise _control_hold(label) from exc
    visible, total = _survey(located)
    if total != 1 or len(visible) != 1:
        raise _control_hold(label)
    return visible[0]


def _select_processed_date_view(page: Any) -> None:
    view = _unique_labeled(page, VIEW_ACTIVITY_BY_CSS, VIEW_ACTIVITY_BY_LABEL)
    option = view.locator(PROCESSED_DATE_OPTION_CSS)
    if int(option.count()) != 1 or _norm(str(option.inner_text())) != PROCESSED_DATE_OPTION_LABEL:
        raise _control_hold(PROCESSED_DATE_OPTION_LABEL)
    labeled = [
        item for item in view.locator("option").all()
        if _norm(str(item.inner_text())) == PROCESSED_DATE_OPTION_LABEL
    ]
    if (
        len(labeled) != 1
        or _norm(str(labeled[0].get_attribute("value") or "")) != PROCESSED_DATE_OPTION_VALUE
    ):
        raise _control_hold(PROCESSED_DATE_OPTION_LABEL)
    try:
        view.select_option(value=PROCESSED_DATE_OPTION_VALUE)
        selected = _norm(str(view.input_value()))
    except IntakeHold:
        raise
    except Exception as exc:
        raise _control_hold(PROCESSED_DATE_OPTION_LABEL) from exc
    if selected != PROCESSED_DATE_OPTION_VALUE:
        raise _control_hold(PROCESSED_DATE_OPTION_LABEL)


def _select_custom_date_range(page: Any) -> None:
    """Choose the preset that reveals page-level Start Date and End Date.

    ``select#PDDateRange`` is not a container. The option value is whatever
    the single ``Select Date Range`` option carries. A missing value, a
    second option with that text, or a second option with that value holds.
    """
    ranged = page.locator(PROCESSED_DATE_RANGE_CSS)
    if int(ranged.count()) != 1 or not _is_visible(ranged):
        raise _control_hold(PROCESSED_DATE_RANGE_LABEL)
    options = ranged.locator("option").all()
    matched = [
        item for item in options
        if _option_text(item, CUSTOM_DATE_RANGE_LABEL) == CUSTOM_DATE_RANGE_LABEL
    ]
    if len(matched) != 1:
        raise _control_hold(CUSTOM_DATE_RANGE_LABEL)
    value = _option_value(matched[0], CUSTOM_DATE_RANGE_LABEL)
    if not value:
        raise _control_hold(CUSTOM_DATE_RANGE_LABEL)
    same_value = [
        item for item in options
        if _option_value(item, CUSTOM_DATE_RANGE_LABEL) == value
    ]
    if len(same_value) != 1:
        raise _control_hold(CUSTOM_DATE_RANGE_LABEL)
    try:
        ranged.select_option(value=value)
        selected = _norm(str(ranged.input_value()))
    except IntakeHold:
        raise
    except Exception as exc:
        raise _control_hold(CUSTOM_DATE_RANGE_LABEL) from exc
    if selected != value:
        raise _control_hold(CUSTOM_DATE_RANGE_LABEL)


def _fill_html_date(page: Any, css: str, label: str, day: date) -> None:
    locator = _unique_visible_control(page, css, label)
    _require_expected_label(page, locator, label)
    try:
        locator.fill(day.isoformat())
        observed = parse_processed_date(_norm(str(locator.input_value())))
    except IntakeHold:
        observed = None
    except Exception as exc:
        raise _control_hold(label) from exc
    if observed != day:
        raise IntakeHold("Processed date filter did not stick")


def _click_get_policy_activity(page: Any) -> None:
    button = _unique_visible_control(page, GET_POLICY_ACTIVITY_CSS, GET_POLICY_ACTIVITY_LABEL)
    value = _norm(str(button.get_attribute("value") or ""))
    text = _norm(str(button.inner_text() or ""))
    names = {part for part in (value, text) if part}
    if names != {GET_POLICY_ACTIVITY_LABEL}:
        raise _control_hold(GET_POLICY_ACTIVITY_LABEL)
    try:
        button.click()
    except IntakeHold:
        raise
    except Exception as exc:
        raise _control_hold(GET_POLICY_ACTIVITY_LABEL) from exc


def apply_processed_date_window(page: Any, start: date, end: date) -> None:
    """Select Processed Date, reveal the custom range, fill it, and submit.

    ``select#PDDateRange`` is a preset list. ``Select Date Range`` is chosen
    by the value on that option, then Start Date and End Date must become
    visible at page scope before they are filled. Get Policy Activity is
    page-level as well. Ambiguous or still-hidden controls fail closed. A
    future HITL ladder may ask Gemini what the open page is showing, then
    Jev (TypeSafe System One) for a typed judgment (boolean, choice, or
    score, plus confidence) — for example whether this is the processed-date
    filter we expect, or quote-only versus complete. This function does not call Gemini or Jev.
    """
    _select_processed_date_view(page)
    _select_custom_date_range(page)
    _fill_html_date(page, START_DATE_CSS, START_DATE_LABEL, start)
    _fill_html_date(page, END_DATE_CSS, END_DATE_LABEL, end)
    _click_get_policy_activity(page)


def _processed_date_results_section(url: str) -> str:
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise IntakeHold(PROCESSED_DATE_RESULTS_HOLD)
    normalized = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    match = _PROCESSED_DATE_RESULTS_URL.fullmatch(normalized)
    if match is None:
        raise IntakeHold(PROCESSED_DATE_RESULTS_HOLD)
    return match.group(1).lower()


def require_processed_date_results(page: Any) -> str:
    """Return the results section after Get Policy Activity. Do not click Search.

    Release ``00a0ae294ec0`` (#612) submitted the date window and landed on
    ``/processeddateresults/cancels/``. The following ``click_named(..., "Search")``
    held with ``Progressive control 'Search' is missing or ambiguous`` because
    that button is not on the results page. Cancels is the default section.
    Any other single section under ``processeddateresults`` is the same page
    family. A missing URL, a query, or an extra path segment holds. The
    Communications tab is the next control.
    """
    wait = getattr(page, "wait_for_url", None)
    if callable(wait):
        try:
            wait(_PROCESSED_DATE_RESULTS_URL, timeout=DATE_CONTROL_TIMEOUT_MS)
        except IntakeHold:
            raise
        except Exception as exc:
            raise IntakeHold(PROCESSED_DATE_RESULTS_HOLD) from exc
    return _processed_date_results_section(str(getattr(page, "url", "") or ""))


def open_communications_tab(page: Any) -> None:
    """Open the Communications tab on the processed-date results page.

    Exact role ``tab``. A link with the same name is not this control and
    is not clicked. Zero tabs, or two tabs, hold. The tab must report
    ``aria-selected=true`` after the click.
    """
    click_named(page, COMMUNICATIONS_TAB_LABEL, roles=("tab",))
    locator = page.get_by_role("tab", name=COMMUNICATIONS_TAB_LABEL, exact=True)
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
        click_shell_nav(
            self.page,
            lambda: _manage_policies_locator(self.page),
            label=MANAGE_POLICIES_LABEL,
            unique_element=True,
        )
        click_shell_nav(
            self.page,
            lambda: _policy_activity_locator(self.page),
            label=POLICY_ACTIVITY_LABEL,
            unique_element=False,
        )
        apply_processed_date_window(self.page, start, end)
        require_processed_date_results(self.page)
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

    def screenshot_communications(self) -> bytes:
        """Full-page PNG of the Communications list while that tab is selected."""
        if self._grid is None or str(getattr(self.page, "url", "") or "") != self._list_url:
            raise IntakeHold("Communications list screenshot is missing or not a PNG")
        if self.page.locator("table").count() != 1:
            raise IntakeHold("Communications list screenshot is missing or not a PNG")
        tab = self.page.get_by_role("tab", name="Communications", exact=True)
        if tab.count() != 1 or tab.get_attribute("aria-selected") != "true":
            raise IntakeHold("Communications list screenshot is missing or not a PNG")
        data = self.page.screenshot(full_page=True, type="png")
        return require_png(data)


class LocalDeliveryLedger:
    """Private named-PDF ledger. A conflicting file is kept and the pull holds."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def ensure_private(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink() or not self.root.is_dir() or self.root.stat().st_mode & 0o077:
            raise IntakeHold("Memo output directory must be private (0700)")

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
            raise IntakeHold("Existing memo file conflicts with the pull ledger")
        return True

    def record(self, source: SourceItem, *, processed_on: date) -> Path:
        self.ensure_private()
        if source.filename == LEDGER_NAME:
            raise IntakeHold("Memo filename is missing or ambiguous")
        path = self.pdf_path(processed_on, source.filename)
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
            "processed_date": processed_on.isoformat(),
        }
        self._write(data)
        return path

    def pdf_ids_for_date(self, day: date) -> set[str]:
        """Verified memo PDFs whose ledger row is this processed date."""
        found: set[str] = set()
        for document_id, entry in self._load()["items"].items():
            if not isinstance(entry, dict) or entry.get("processed_date") != day.isoformat():
                continue
            filename = str(entry.get("filename") or "")
            processed = str(entry.get("processed_date") or "")
            if processed != day.isoformat():
                continue
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
            raise IntakeHold("Memo output directory must be private (0700)")
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not folder.is_dir() or folder.stat().st_mode & 0o077:
            raise IntakeHold("Memo output directory must be private (0700)")
        return folder

    def save_screenshot(self, start: date, end: date, png: bytes) -> dict[str, str]:
        """Write the list PNG into each processed-date folder. Never replace a different shot."""
        blob = require_png(png)
        saved: dict[str, str] = {}
        day = start
        while day <= end:
            name = communications_screenshot_name(day, day)
            saved[day.isoformat()] = str(self._save_png_named(self.date_dir(day), name, blob))
            day += timedelta(days=1)
        return saved

    def _save_png_named(self, folder: Path, name: str, blob: bytes) -> Path:
        primary = folder / self._basename(name)
        if primary.exists():
            if primary.is_symlink() or not primary.is_file():
                raise IntakeHold("Communications list screenshot is missing or not a PNG")
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
        raise IntakeHold("Communications list screenshot is missing or not a PNG")

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
            raise IntakeHold("Memo filename is missing or ambiguous")
        return filename

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
        self._list_png: bytes | None = None
        self.skipped_document_ids: tuple[str, ...] = ()
        self.verification: dict[str, Any] | None = None

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
        self._list_png = require_png(self.browser.screenshot_communications())
        rows = []
        found: dict[str, MemoRow] = {}
        skipped: list[str] = []
        for memo in memos:
            delivered = self.ledger.delivery_status(
                document_id=memo.document_id,
                filename=memo.filename,
                processed_on=memo.processed_on,
            )
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
        memo = self.memo(source.source_id)
        path = self.ledger.record(source, processed_on=memo.processed_on)
        self._mark_delivered(source.source_id)
        return str(path)

    def finish_fao_pull(self, start: date, end: date) -> dict[str, Any]:
        """Write one QA pack per processed date. A count mismatch stays HELD."""
        if self._list_png is None or self._cache_result is None:
            raise IntakeHold("Communications list screenshot is missing or not a PNG")
        held: str | None = None
        try:
            by_date = require_memo_pdf_parity(
                rows=self._cache_result.rows, ledger=self.ledger, start=start, end=end,
            )
            status = "PULLED"
        except IntakeHold as exc:
            held = str(exc)
            try:
                by_date = observed_memo_pdf_counts(
                    rows=self._cache_result.rows, ledger=self.ledger, start=start, end=end,
                )
            except IntakeHold:
                raise exc from None
            status = "HELD"
        screenshots = self.ledger.save_screenshot(start, end, self._list_png)
        packs = write_progressive_qa_packs(
            ledger=self.ledger,
            start=start,
            end=end,
            agent_code=self.agent_code,
            status=status,
            held=held,
            by_date=by_date,
            screenshots=screenshots,
            rows=self._cache_result.rows,
            skipped_document_ids=self.skipped_document_ids,
            screenshot_covers_window=start != end,
        )
        self.verification = {
            "gate": "memo_rows_equal_pdfs",
            "by_date": [dict(row) for row in by_date],
            "screenshot": screenshots.get(start.isoformat()),
            "screenshots": screenshots,
            "packs": packs,
        }
        if held:
            raise IntakeHold(held)
        return self.verification

    def memo(self, document_id: str) -> MemoRow:
        try:
            return self._memos[document_id]
        except KeyError as exc:
            raise IntakeHold("Selected carrier document is missing or ambiguous") from exc

    def filing_candidates(self, start: date, end: date) -> list[dict[str, Any]]:
        """Local in-window memo PDFs. One dict per file, in list order."""

        items: list[dict[str, Any]] = []
        for memo in self._memos.values():
            if not start <= memo.processed_on <= end:
                continue
            path = self.ledger.pdf_path(memo.processed_on, memo.filename)
            if not path.is_file():
                continue
            items.append({
                "policy_number": memo.policy_number,
                "insured_name": memo.insured_name,
                "reason": memo.reason,
                "filename": memo.filename,
                "processed_on": memo.processed_on.isoformat(),
                "path": str(path),
            })
        return items

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
    parser.add_argument("--start", help="Processed date window start, YYYY-MM-DD. Default: standing window.")
    parser.add_argument("--end", help="Processed date window end, YYYY-MM-DD. Default: standing window.")
    parser.add_argument(
        "--as-of",
        help="Eastern day treated as today for the standing window, YYYY-MM-DD. Default: today.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--pull-only",
        action="store_true",
        help="Portal pull and local QA pack only. Does not call EZLynx.",
    )
    mode.add_argument(
        "--file-ezlynx",
        action="store_true",
        help="After a successful pull, file via Documents API and Notes API. "
        "This is also the default when --pull-only is omitted. Writes stay off "
        "unless ROBIE_ENV=TEST, the host is hermes-test-01, and "
        "ROBIE_DOCUMENT_RETRIEVAL_FILE_EZLYNX=1.",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_QA_ROOT),
        help="Progressive QA root. One private subfolder per processed date "
        "(default: hermes-test carrier-pull-qa/progressive)",
    )
    parser.add_argument(
        "--upload-drive",
        action="store_true",
        help="Upload that day's QA folder to the Nicole shared Drive. Not implemented; fails closed.",
    )
    parser.add_argument("--agent-code", default=os.environ.get("PROGRESSIVE_FAO_AGENT_CODE", DEFAULT_AGENT_CODE))
    parser.add_argument("--cdp-url", default=None, help="Loopback CDP URL. Defaults to 127.0.0.1:9222")
    return parser


def resolve_processed_window(
    start: str | None, end: str | None, as_of: str | None
) -> tuple[date, date, date]:
    """Standing window: yesterday and today. Monday includes Friday through Monday."""

    from .document_retrieval_filing import (
        FilingHeld,
        eastern_today,
        require_retrieval_window,
        retrieval_date_window,
    )

    if as_of:
        try:
            today = date.fromisoformat(as_of)
        except ValueError as exc:
            raise IntakeHold("As-of date is missing or ambiguous") from exc
    else:
        today = eastern_today()
    if bool(start) != bool(end):
        raise IntakeHold("Processed date window needs both start and end, or neither")
    if not start and not end:
        window_start, window_end = retrieval_date_window(today)
        return window_start, window_end, today
    try:
        parsed_start = date.fromisoformat(str(start))
        parsed_end = date.fromisoformat(str(end))
    except ValueError as exc:
        raise IntakeHold("Processed date window is missing or ambiguous") from exc
    try:
        require_retrieval_window(parsed_start, parsed_end, as_of=today)
    except FilingHeld as exc:
        raise IntakeHold(str(exc)) from exc
    return parsed_start, parsed_end, today


def main(argv: list[str] | None = None, *, browser_factory: Callable[[argparse.Namespace], Any] | None = None) -> int:
    args = build_parser().parse_args(argv)
    closer: Callable[[], None] | None = None
    portal: FaoCommunicationsMemoPortal | None = None
    try:
        require_test()
        start, end, as_of = resolve_processed_window(args.start, args.end, args.as_of)
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
                "path": str(ledger.pdf_path(memo.processed_on, item.filename)),
            })
        if args.upload_drive:
            refuse_progressive_drive_upload(
                ledger, start=start, end=end, verification=portal.verification,
            )
        filing = {"status": "not_run", "attempted_writes": False, "results": []}
        if not args.pull_only:
            filing = _run_memo_filing(portal, start, end, as_of)
            record_filing_on_packs(ledger, start=start, end=end, filing=filing)
        ezlynx_status = "not_run" if args.pull_only else str(filing.get("status") or "held")
        command_status = "HELD" if ezlynx_status == "held" else "PULLED"
        _emit({
            "status": command_status,
            "scope": FAO_SCOPE,
            "process": ProgressiveRetrieval.process,
            "agent_code": agent_code,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "count": len(downloaded),
            "downloaded": downloaded,
            "skipped_already_delivered": list(portal.skipped_document_ids),
            "verification": portal.verification,
            "ezlynx": ezlynx_status,
            "filing": filing,
        })
        return 2 if command_status == "HELD" else 0
    except IntakeHold as exc:
        payload: dict[str, Any] = {"status": "HELD", "reason": str(exc), "ezlynx": "not_run"}
        if portal is not None and portal.verification is not None:
            payload["verification"] = portal.verification
        _emit(payload)
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


def communications_screenshot_name(start: date, end: date) -> str:
    if end < start:
        raise IntakeHold("Processed date window is missing or ambiguous")
    if start == end:
        name = f"fao-communications-memo-{start.isoformat()}.png"
    else:
        name = f"fao-communications-memo-{start.isoformat()}-to-{end.isoformat()}.png"
    if name != Path(name).name:
        raise IntakeHold("Communications list screenshot is missing or not a PNG")
    return name


def require_png(blob: bytes | bytearray | None) -> bytes:
    if not isinstance(blob, (bytes, bytearray)) or not bytes(blob).startswith(_PNG_MAGIC):
        raise IntakeHold("Communications list screenshot is missing or not a PNG")
    return bytes(blob)


def _memo_ids_by_date(rows, start: date, end: date) -> dict[str, set[str]]:
    grouped: dict[str, set[str]] = {}
    day = start
    while day <= end:
        grouped[day.isoformat()] = set()
        day += timedelta(days=1)
    for row in rows:
        processed = str(row.get("processed_or_effective_date") or "")
        document_id = str(row.get("document_id") or "")
        if processed not in grouped or not document_id or document_id in grouped[processed]:
            raise IntakeHold("Communications memo count does not match downloaded PDFs")
        grouped[processed].add(document_id)
    return grouped


def observed_memo_pdf_counts(*, rows, ledger: LocalDeliveryLedger, start: date, end: date) -> tuple[dict[str, Any], ...]:
    """Count Memo rows and verified PDFs per date without treating a mismatch as success."""
    evidence: list[dict[str, Any]] = []
    for processed, ids in _memo_ids_by_date(rows, start, end).items():
        pdf_ids = set(ledger.pdf_ids_for_date(date.fromisoformat(processed)))
        evidence.append({
            "processed_date": processed,
            "memo_rows": len(ids),
            "pdfs": len(pdf_ids),
        })
    return tuple(evidence)


def require_memo_pdf_parity(*, rows, ledger: LocalDeliveryLedger, start: date, end: date) -> tuple[dict[str, Any], ...]:
    """Each processed date in the window must have one verified PDF per Memo row.

    A short download, an extra PDF for that date, or a day inside the window
    whose page count and file count disagree holds the pull. Nothing is
    reported as successful when the counts differ.
    """
    evidence: list[dict[str, Any]] = []
    for row in observed_memo_pdf_counts(rows=rows, ledger=ledger, start=start, end=end):
        if row["memo_rows"] != row["pdfs"]:
            raise IntakeHold(
                "Communications memo count does not match downloaded PDFs "
                f"for {row['processed_date']}: {row['memo_rows']} memo rows and {row['pdfs']} PDFs"
            )
        evidence.append(row)
    return tuple(evidence)


def write_progressive_qa_packs(
    *,
    ledger: LocalDeliveryLedger,
    start: date,
    end: date,
    agent_code: str,
    status: str,
    held: str | None,
    by_date,
    screenshots: dict[str, str],
    rows,
    skipped_document_ids,
    screenshot_covers_window: bool,
) -> dict[str, str]:
    """Write README.md and manifest.json into each processed-date folder."""
    counts = {str(row["processed_date"]): row for row in by_date}
    skipped = set(skipped_document_ids)
    stored = ledger._load()["items"]
    packs: dict[str, str] = {}
    day = start
    while day <= end:
        key = day.isoformat()
        folder = ledger.date_dir(day)
        shot = screenshots.get(key)
        if not shot:
            raise IntakeHold("Communications list screenshot is missing or not a PNG")
        count = counts.get(key) or {"memo_rows": 0, "pdfs": 0}
        memos = _manifest_memos(rows, stored, skipped, processed=key)
        covers = f"{start.isoformat()} to {end.isoformat()}" if screenshot_covers_window else key
        manifest = {
            "carrier": "progressive",
            "scope": FAO_SCOPE,
            "agent_code": agent_code,
            "processed_date": key,
            "window": {"start": start.isoformat(), "end": end.isoformat()},
            "status": status,
            "gate": "memo_rows_equal_pdfs",
            "memo_rows": count["memo_rows"],
            "pdfs": count["pdfs"],
            "screenshot": Path(shot).name,
            "screenshot_covers": covers,
            "screenshot_covers_window": screenshot_covers_window,
            "memos": memos,
            "held": held,
            "ezlynx": "not_run",
            "drive": _drive_destination(key),
        }
        _write_private_file(folder / "manifest.json", json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n")
        _write_private_file(folder / "README.md", _qa_readme(manifest, memos).encode("utf-8"))
        packs[key] = str(folder)
        day += timedelta(days=1)
    return packs


def _run_memo_filing(portal: FaoCommunicationsMemoPortal, start: date, end: date, as_of: date) -> dict[str, Any]:
    """Call the shared filing stage. A missing module holds; it does not upload from here."""

    try:
        from .document_retrieval_filing import file_progressive_memos
    except Exception as exc:
        return {
            "status": "held",
            "reason": f"filing stage unavailable ({type(exc).__name__})",
            "attempted_writes": False,
            "results": [],
        }
    try:
        return file_progressive_memos(portal.filing_candidates(start, end), sheet_day=as_of)
    except Exception as exc:
        return {
            "status": "held",
            "reason": f"filing stage failed ({type(exc).__name__})",
            "attempted_writes": False,
            "results": [],
        }


def record_filing_on_packs(
    ledger: LocalDeliveryLedger,
    *,
    start: date,
    end: date,
    filing: dict[str, Any],
) -> None:
    """Stamp the QA pack with the filing receipt. Does not claim a write the stage did not make."""

    status = str(filing.get("status") or "held")
    reason = str(filing.get("reason") or "")
    day = start
    try:
        while day <= end:
            folder = ledger.date_dir(day)
            manifest_path = folder / "manifest.json"
            readme_path = folder / "README.md"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            readme = readme_path.read_text(encoding="utf-8")
            if not isinstance(manifest, dict):
                raise IntakeHold("QA manifest is missing")
            manifest["ezlynx"] = status
            manifest["filing"] = {
                "status": status,
                "reason": reason,
                "attempted_writes": bool(filing.get("attempted_writes")),
            }
            _write_private_file(
                manifest_path,
                json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n",
            )
            if "EZLynx: not_run\n" in readme:
                replacement = f"EZLynx: {status}\n"
                if reason:
                    replacement += f"EZLynx detail: {reason}\n"
                readme = readme.replace("EZLynx: not_run\n", replacement, 1)
                _write_private_file(readme_path, readme.encode("utf-8"))
            day += timedelta(days=1)
    except Exception as exc:
        filing["pack_record"] = f"held ({type(exc).__name__})"


def refuse_progressive_drive_upload(
    ledger: LocalDeliveryLedger,
    *,
    start: date,
    end: date,
    verification: dict[str, Any] | None = None,
) -> None:
    """TODO: upload the date folder into the Nicole Progressive Drive. Fail closed."""
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
        if "Drive: not_run\n" not in readme:
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


def _drive_destination(processed: str) -> dict[str, str]:
    return {
        "status": "not_run",
        "parent_id": DRIVE_QA_PARENT_ID,
        "folder_id": DRIVE_PROGRESSIVE_FOLDER_ID,
        "path": f"{DRIVE_QA_FOLDER_NAME}/{processed}/",
    }


def _manifest_memos(rows, stored: dict[str, Any], skipped: set[str], *, processed: str) -> list[dict[str, Any]]:
    memos: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("processed_or_effective_date") or "") != processed:
            continue
        document_id = str(row.get("document_id") or "")
        entry = stored.get(document_id)
        present = isinstance(entry, dict) and entry.get("processed_date") == processed
        if document_id in skipped and present:
            disposition = "already_present"
        elif present:
            disposition = "pulled"
        else:
            disposition = "missing"
        memos.append({
            "document_id": document_id,
            "policy_number": row.get("policy_number"),
            "insured_name": row.get("insured_name"),
            "reason": row.get("reason"),
            "filename": row.get("filename"),
            "sha256": entry.get("sha256") if present else None,
            "bytes": entry.get("bytes") if present else None,
            "disposition": disposition,
        })
    return memos


def _qa_readme(manifest: dict[str, Any], memos: list[dict[str, Any]]) -> str:
    lines = [
        f"# Progressive FAO Communications QA — {manifest['processed_date']}",
        "",
        "Carrier: progressive",
        f"Agent: {manifest['agent_code']}",
        f"Window: {manifest['window']['start']} through {manifest['window']['end']}",
        f"Status: {manifest['status']}",
        (
            "Gate: memo rows on the Communications list equal saved PDFs "
            f"({manifest['memo_rows']} memo rows, {manifest['pdfs']} PDFs)"
        ),
        "",
        "## Seen",
    ]
    if memos:
        for memo in memos:
            lines.append(
                f"- {memo['policy_number']} {memo['insured_name']} — {memo['reason']} "
                f"({memo['filename']}; {memo['disposition']})"
            )
    else:
        lines.append("- No Memo rows for this processed date.")
    lines.extend(["", "## Pulled this run"])
    pulled = [memo for memo in memos if memo["disposition"] == "pulled"]
    lines.extend([f"- {memo['filename']}" for memo in pulled] or ["- None"])
    lines.extend(["", "## Already present"])
    present = [memo for memo in memos if memo["disposition"] == "already_present"]
    lines.extend([f"- {memo['filename']}" for memo in present] or ["- None"])
    lines.extend(["", "## Held", manifest["held"] or "None", ""])
    lines.append(f"Screenshot: {manifest['screenshot']}")
    if manifest["screenshot_covers_window"]:
        lines.append(
            "This PNG is the Communications list for the whole window, copied into this date folder. "
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
        raise IntakeHold("Existing memo file conflicts with the pull ledger")
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
