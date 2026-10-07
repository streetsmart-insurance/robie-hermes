"""Test-only Travelers Direct Bill Activity document pull.

The playbook opens an already signed-in Travelers foragents portal tab
(foragents.travelers.com) and follows the verified flow to the Direct Bill
Activity Details pages:

    Agency Reports tab
      -> "Direct Bill Activity -- Cancellation & Reinstatement Notices"
      -> Direct Bill Activity list (select#select_date dropdown)
      -> pick a recent date -> Search -> "view" link
         (data-agentcode + data-datereceived; opens a NEW TAB)
      -> "Direct Bill Activity Details" (div#billingActivityReportDetail)

VERIFIED MAPPING (2026-10-05, live DOM):
- The date filter is a native <select id="select_date"> with ~25 discrete
  activity dates (observed 06/29/2026 -> 10/05/2026). No date-range picker,
  no "last 2 weeks" preset: the worker iterates the dropdown client-side
  and filters to the recent window.
- The "view" link is href="javascript:void(0);" class="gridActionLink"
  carrying data-agentcode (e.g. "0X4688") and data-datereceived
  (ISO timestamp). Clicking opens a NEW TAB at
  /Business/BillingAndPolicyServices/DirectBillActivityDetails -- the URL
  carries no query params; state travels via the data attributes.
- One section per detail page (the report "stage" for that date):
  1. "Pre-cancellation Alert": policies a cancellation notice will be sent for.
     Columns: Account Number | Policy Number | Account Name |
     DNOC Minimum Due | Partial Payment | Account Balance |
     Cancellation Notice Date.
  2. "Cancellation Alert": policies that will cancel if payment is not received.
     Columns: Account Number | Account Name | Policy Number |
     DNOC Minimum Due | Partial Payment | Account Balance | Cancellation Date.
  3. "Reinstatement" (no bold title): policies that appeared previously as
     pre-cancellation but cleared. Columns: Account Number | Account Name |
     Policy Number | Agent Activity Date. Reinstatement rows are recorded
     HELD -- they cleared a prior cancellation and are not cancellation notices.
- The "document" is a per-date Word report: the Save Document button posts
  a form to /Business/BillingAndPolicyServices/SaveReportToWordDocument
  with hidden inputs agentCode, date, and __RequestVerificationToken. The
  worker POSTs the form's own inputs (honoring the anti-forgery token) and
  requires Word (.docx ZIP magic) bytes; anything else holds.

VERIFIED QUIRK (2026-10-02, twice): the Customer Snapshot -> Policies tab
transaction table is NOT used for cancellation notices. Selecting the
"Cancel Notice" transaction row and clicking Download returned the Welcome
Letter ZIP instead of the cancellation notice. This module never navigates
to Customer Snapshot and never clicks a transaction Download for a
cancellation notice.

A missing or non-unique control raises IntakeHold. This module does not
log in, does not handle MFA, does not upload, note, task, or label in
EZLynx, and does not register a timer.
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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .intake_core import IntakeHold, SourceArchive, SourceItem

PROCESS = "travelers"
SCOPE = "pending_cancellation"
TRAVELERS_HOST = "foragents.travelers.com"
LIST_PATH = "/Business/billingandpolicyservices/directbillactivity"
LIST_URL = f"https://{TRAVELERS_HOST}{LIST_PATH}"
DETAIL_PATH = "/Business/BillingAndPolicyServices/DirectBillActivityDetails"
SAVE_DOC_PATH = "/Business/BillingAndPolicyServices/SaveReportToWordDocument"
DEFAULT_CDP_URL = "http://127.0.0.1:9223"
DOWNLOAD_TIMEOUT_MS = 15000
LEDGER_NAME = "travelers-cancellation-ledger.json"
HERMES_TEST_HOST = "hermes-test-01"
DEFAULT_OUTPUT_ROOT = Path(
    "/opt/streetsmart-hermes-test/robie-job-engine/data/artifacts/carrier-pull-qa/travelers"
)
# Shared Drive "Robie Carrier Pull QA (Nicole)". The Travelers child folder id
# is UNVERIFIED. Folder upload is TODO. --upload-drive fails closed and does
# not call Google.
CARRIER_QA_DRIVE_PARENT_ID = "1cLEpR-0T6KdiVjcdAr0qpGTO447MetI2"
DRIVE_UPLOAD_UNAVAILABLE = (
    "Drive upload of the Travelers QA pack is not available; "
    "refusing to report the pack as uploaded"
)
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# The per-date report downloads as a Word .docx (ZIP archive).
_ZIP_MAGIC = b"PK\x03\x04"
_EASTERN = ZoneInfo("America/New_York")
# Recent window for the select_date dropdown (client-side; the portal offers
# only ~25 discrete dates, no range picker).
_RECENT_WINDOW_DAYS = 14

# Verified navigation labels (2026-10-05 mapping).
_AGENCY_REPORTS_TAB = "Agency Reports"
_REPORT_LINK = "Direct Bill Activity Cancellation & Reinstatement Notices"
_SEARCH_BUTTON = "Search"
_VIEW_LINK_CLASS = "gridActionLink"
_DETAIL_SECTION_ID = "billingActivityReportDetail"

# Section types: one per detail page (the report "stage" for that date).
_SECTION_PRE_CANCELLATION = "PRE_CANCELLATION"
_SECTION_CANCELLATION = "CANCELLATION"
_SECTION_REINSTATEMENT = "REINSTATEMENT"
_SECTION_TITLES = {
    "pre-cancellation alert": _SECTION_PRE_CANCELLATION,
    "cancellation alert": _SECTION_CANCELLATION,
}
_REINSTATEMENT_MARKERS = (
    "appeared previously as pre-cancellation",
    "subsequent activity has taken place to clear the cancellation",
)

# Detail table headers, by alias. Column order differs per section type.
_DETAIL_HEADERS = (
    ("account_number", frozenset({"account number", "acct number", "account #"})),
    ("policy_number", frozenset({"policy number", "policy #", "pol #", "policy"})),
    ("account_name", frozenset({"account name", "insured", "insured name", "named insured", "name"})),
    ("dnoc_minimum_due", frozenset({"dnoc minimum due", "minimum due", "dnoc min due"})),
    ("partial_payment", frozenset({"partial payment"})),
    ("account_balance", frozenset({"account balance", "balance"})),
    ("notice_date", frozenset({
        "cancellation notice date", "notice date",
        "cancellation date", "cancel date",
        "agent activity date", "activity date",
    })),
)
# policy_number and account_name are required in every section layout.
_REQUIRED_DETAIL_FIELDS = ("policy_number", "account_name")

# Verified quirk (2026-10-02, twice): the Customer Snapshot transaction-list
# Download path returns the wrong document for Cancel Notice rows (Welcome
# Letter ZIP). It is never used here.
_REFUSED_TRANSACTION_DOWNLOAD = (
    "Travelers Customer Snapshot transaction Download is refused for "
    "cancellation notices: the Cancel Notice row returned the Welcome Letter "
    "ZIP (verified twice). Use the Direct Bill Activity Save Document form."
)


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


def _is_word_doc(content: bytes) -> bool:
    return bytes(content or b"")[:4] == _ZIP_MAGIC


def refuse_production_host() -> None:
    """Refuse a hermes-poc host unless Travelers Production filing is enabled.

    Travelers is not on the Production filing allowlist, so this always
    refuses hermes-poc-01. Test-only pulls stay on hermes-test-01.
    """
    from .document_retrieval_filing import live_filing_decision

    if live_filing_decision(os.environ, socket.gethostname(), "travelers").allowed:
        return
    raw = f"{socket.gethostname()} {socket.getfqdn()}".lower()
    labels = [label for label in re.split(r"[\s.]+", raw) if label]
    if any(label == "hermes-poc-01" or label.startswith("hermes-poc") for label in labels):
        raise IntakeHold("Travelers document pull refuses Production host hermes-poc-01")


def require_hermes_test_host() -> None:
    """Live packs are produced on hermes-test-01. Fixture runs inject a browser."""
    from .document_retrieval_filing import live_filing_decision

    if live_filing_decision(os.environ, socket.gethostname(), "travelers").allowed:
        return
    raw = f"{socket.gethostname()} {socket.getfqdn()}".lower()
    labels = [label for label in re.split(r"[\s.]+", raw) if label]
    if HERMES_TEST_HOST not in labels:
        raise IntakeHold("Travelers QA pack must be produced on hermes-test-01")


def require_travelers_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise IntakeHold("Travelers portal URL is missing or ambiguous")
    if host != TRAVELERS_HOST:
        raise IntakeHold("Travelers portal URL is missing or ambiguous")
    if "login" in parsed.path.lower() or "auth" in parsed.path.lower():
        raise IntakeHold("Travelers session is not authenticated")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def parse_carrier_date(text: str) -> date:
    cleaned = _norm(text)
    for fmt in ("%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    raise IntakeHold(f"Travelers date is missing or ambiguous: {cleaned!r}")


def require_agent_code(text: str) -> str:
    cleaned = _norm(text).upper()
    if not re.fullmatch(r"[A-Z0-9]{4,10}", cleaned):
        raise IntakeHold(f"Travelers agent code is missing or ambiguous: {text!r}")
    return cleaned


def require_policy_number(text: str) -> str:
    cleaned = _norm(text).upper().replace(" ", "")
    if not cleaned or len(cleaned) > 40:
        raise IntakeHold(f"Travelers policy number is missing or ambiguous: {text!r}")
    return cleaned


def classify_section(title: str, intro: str) -> str:
    """Map the detail page section to a canonical section type.

    Raises IntakeHold when the section is missing or ambiguous.
    """
    key = _norm(title).casefold()
    for marker, section in _SECTION_TITLES.items():
        if marker in key:
            return section
    body = _norm(intro).casefold()
    if any(marker in body for marker in _REINSTATEMENT_MARKERS):
        return _SECTION_REINSTATEMENT
    raise IntakeHold(f"Travelers detail section is missing or ambiguous: {title!r}")


def report_filename(agent_code: str, activity_date: date) -> str:
    code = require_agent_code(agent_code)
    return f"Travelers Direct Bill Activity {code} {activity_date.isoformat()}.docx"


def report_document_id(agent_code: str, activity_date: date) -> str:
    code = require_agent_code(agent_code)
    return f"travelers:{code}:{activity_date.isoformat()}:report"


def row_document_id(agent_code: str, activity_date: date, policy_number: str) -> str:
    code = require_agent_code(agent_code)
    policy = require_policy_number(policy_number)
    return f"travelers:{code}:{activity_date.isoformat()}:{policy}"


@dataclass(frozen=True)
class ActivityDate:
    """One discrete date from the select_date dropdown."""

    value: str  # option value as carried by the select element
    activity_date: date


@dataclass(frozen=True)
class ListRow:
    """One agency row on the Direct Bill Activity list page."""

    agent_code: str
    date_received: str  # ISO timestamp from data-datereceived


@dataclass(frozen=True)
class DetailRow:
    """One policy row on the Direct Bill Activity Details page."""

    agent_code: str
    activity_date: date
    policy_number: str
    account_name: str
    section: str
    account_number: str = ""
    dnoc_minimum_due: str = ""
    partial_payment: str = ""
    account_balance: str = ""
    notice_date: str = ""

    @property
    def document_id(self) -> str:
        return row_document_id(self.agent_code, self.activity_date, self.policy_number)


class PullHeld(ValueError):
    """The pull stopped before completion; details carry held/downloaded rows."""

    def __init__(
        self,
        reason: str,
        *,
        held: list[dict[str, Any]] | None = None,
        downloaded: list[dict[str, Any]] | None = None,
        rows: list[dict[str, Any]] | None = None,
    ):
        super().__init__(reason)
        self.details: dict[str, Any] = {
            "held": list(held or []),
            "downloaded": list(downloaded or []),
            "rows": list(rows or []),
        }


def parse_activity_dates(
    options: tuple[tuple[str, str], ...],
    *,
    as_of: date,
    window_days: int = _RECENT_WINDOW_DAYS,
) -> tuple[ActivityDate, ...]:
    """Parse the select_date dropdown options into recent activity dates.

    ``options`` is (option value, option display text). Only dates within
    ``window_days`` of ``as_of`` are returned, newest first. An empty result
    raises IntakeHold -- the portal offers ~25 discrete dates, so an empty
    window means the page is wrong, not that there is no work.
    """
    if window_days < 1:
        raise IntakeHold("Travelers date window is missing or ambiguous")
    if not isinstance(as_of, date):
        raise IntakeHold("Travelers as-of date is missing or ambiguous")
    cutoff = as_of - timedelta(days=window_days)
    parsed: list[ActivityDate] = []
    seen: set[date] = set()
    for value, display in options:
        text = _norm(display) or _norm(value)
        if not text:
            continue
        try:
            activity = parse_carrier_date(text)
        except IntakeHold:
            continue
        if activity < cutoff or activity > as_of:
            continue
        if activity in seen:
            continue
        seen.add(activity)
        parsed.append(ActivityDate(value=_norm(value) or text, activity_date=activity))
    if not parsed:
        raise IntakeHold("Travelers activity dates are missing or ambiguous")
    parsed.sort(key=lambda item: item.activity_date, reverse=True)
    return tuple(parsed)


def parse_list_rows(
    rows: tuple[tuple[str, str], ...],
) -> tuple[ListRow, ...]:
    """Validate (agent_code, date_received) pairs from the list grid.

    Each input row is (data-agentcode, data-datereceived ISO timestamp).
    """
    parsed: list[ListRow] = []
    for agent_code, date_received in rows:
        code = require_agent_code(agent_code)
        stamp = _norm(date_received)
        if not stamp:
            raise IntakeHold("Travelers date-received is missing or ambiguous")
        try:
            datetime.fromisoformat(stamp)
        except ValueError:
            raise IntakeHold(
                f"Travelers date-received is missing or ambiguous: {stamp!r}"
            )
        parsed.append(ListRow(agent_code=code, date_received=stamp))
    if not parsed:
        raise IntakeHold("Travelers activity list has no rows")
    return tuple(parsed)


def parse_detail_table(
    section: str,
    headers: tuple[str, ...],
    rows: tuple[tuple[str, ...], ...],
    *,
    agent_code: str,
    activity_date: date,
) -> tuple[DetailRow, ...]:
    """Parse the div#billingActivityReportDetail table into typed rows.

    Header matching is by alias so column order is not load-bearing. The
    alias sets cover all three observed section layouts.
    """
    if section not in (_SECTION_PRE_CANCELLATION, _SECTION_CANCELLATION, _SECTION_REINSTATEMENT):
        raise IntakeHold(f"Travelers section is missing or ambiguous: {section!r}")
    normed = [_norm(h).casefold() for h in headers]
    indexes: dict[str, int] = {}
    for field, aliases in _DETAIL_HEADERS:
        matches = [i for i, h in enumerate(normed) if h in aliases]
        if field in _REQUIRED_DETAIL_FIELDS:
            if len(matches) != 1:
                raise IntakeHold(
                    "Travelers detail table headers are missing or ambiguous"
                )
            indexes[field] = matches[0]
        elif matches:
            if len(matches) != 1:
                raise IntakeHold(
                    "Travelers detail table headers are missing or ambiguous"
                )
            indexes[field] = matches[0]
    parsed: list[DetailRow] = []
    for cells in rows:
        if len(cells) < len(headers):
            raise IntakeHold("Travelers detail table row is missing or ambiguous")
        policy = require_policy_number(cells[indexes["policy_number"]])
        account_name = _norm(cells[indexes["account_name"]])
        if not account_name:
            raise IntakeHold("Travelers detail account name is missing or ambiguous")

        def _cell(field: str) -> str:
            return _norm(cells[indexes[field]]) if field in indexes else ""

        parsed.append(
            DetailRow(
                agent_code=require_agent_code(agent_code),
                activity_date=activity_date,
                policy_number=policy,
                account_name=account_name,
                section=section,
                account_number=_cell("account_number"),
                dnoc_minimum_due=_cell("dnoc_minimum_due"),
                partial_payment=_cell("partial_payment"),
                account_balance=_cell("account_balance"),
                notice_date=_cell("notice_date"),
            )
        )
    if not parsed:
        raise IntakeHold("Travelers detail table has no rows")
    return tuple(parsed)


@dataclass(frozen=True)
class SaveForm:
    """The Save Document form's hidden inputs, as posted."""

    agent_code: str
    date: str
    verification_token: str

    def payload(self) -> dict[str, str]:
        if not self.agent_code or not self.date or not self.verification_token:
            raise IntakeHold("Travelers save form fields are missing or ambiguous")
        return {
            "agentCode": self.agent_code,
            "date": self.date,
            "__RequestVerificationToken": self.verification_token,
        }


def parse_save_form(fields: dict[str, str]) -> SaveForm:
    """Validate the Save Document form's hidden inputs."""
    form = SaveForm(
        agent_code=_norm(fields.get("agentCode", "")),
        date=_norm(fields.get("date", "")),
        verification_token=_norm(fields.get("__RequestVerificationToken", "")),
    )
    # payload() raises on missing fields.
    form.payload()
    return form


class TravelersDeliveryLedger:
    """Private named-document ledger. A conflicting file is kept and the pull holds."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def ensure_private(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        if self.root.is_symlink() or not self.root.is_dir() or self.root.stat().st_mode & 0o077:
            raise IntakeHold("Travelers output directory must be private (0700)")

    def _load(self) -> dict[str, Any]:
        self.ensure_private()
        path = self.root / LEDGER_NAME
        if not path.exists():
            return {"items": {}}
        try:
            data = json.loads(path.read_text())
        except Exception as exc:
            raise IntakeHold("Travelers delivery ledger is missing or ambiguous") from exc
        if not isinstance(data, dict) or not isinstance(data.get("items"), dict):
            raise IntakeHold("Travelers delivery ledger is missing or ambiguous")
        return data

    def _write(self, data: dict[str, Any]) -> None:
        path = self.root / LEDGER_NAME
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def doc_path(self, day: date, filename: str) -> Path:
        folder = self.root / day.isoformat()
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(folder, 0o700)
        safe = re.sub(r"[^\w.\- ]+", "_", filename).strip()
        if not safe or safe == LEDGER_NAME:
            raise IntakeHold("Travelers filename is missing or ambiguous")
        return folder / safe

    def delivery_status(self, *, document_id: str, filename: str, issued_on: date) -> bool:
        """True when this exact document was already delivered."""
        self.ensure_private()
        path = self.doc_path(issued_on, filename)
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
            raise IntakeHold("Existing Travelers file conflicts with the pull ledger")
        return True

    def record(self, source: SourceItem, *, issued_on: date, insured_name: str = "") -> Path:
        self.ensure_private()
        path = self.doc_path(issued_on, source.filename)
        if path.exists():
            raise IntakeHold("Existing Travelers file conflicts with the pull ledger")
        digest = hashlib.sha256(source.content).hexdigest()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(source.content)
        data = self._load()
        if source.source_id in data["items"]:
            raise IntakeHold("Existing Travelers file conflicts with the pull ledger")
        data["items"][source.source_id] = {
            "filename": source.filename,
            "insured_name": insured_name,
            "sha256": digest,
            "bytes": len(source.content),
            "issued_date": issued_on.isoformat(),
        }
        self._write(data)
        return path

    def record_row(self, *, document_id: str, filename: str, issued_on: date,
                   digest: str, size: int, report_document_id: str,
                   insured_name: str = "") -> None:
        """Ledger a per-policy row against its date's shared Word document."""
        self.ensure_private()
        data = self._load()
        if document_id in data["items"]:
            raise IntakeHold("Existing Travelers file conflicts with the pull ledger")
        data["items"][document_id] = {
            "filename": filename,
            "insured_name": insured_name,
            "sha256": digest,
            "bytes": size,
            "issued_date": issued_on.isoformat(),
            "report_document_id": report_document_id,
        }
        self._write(data)

    def row_delivered(self, *, document_id: str) -> bool:
        """True when this per-policy row was already ledgered.

        Rows share their date's Word document file, so only the ledger
        entry is checked -- not file existence (the file belongs to the
        date-level document_id).
        """
        self.ensure_private()
        return isinstance(self._load()["items"].get(document_id), dict)

    def save_screenshot(self, day: date, png: bytes) -> Path:
        if bytes(png or b"")[:8] != _PNG_MAGIC:
            raise IntakeHold("Travelers list screenshot is missing or not a PNG")
        path = self.doc_path(day, f"travelers-direct-bill-activity-{day.isoformat()}.png")
        if path.exists():
            return path
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(png)
        return path


def _unique_control(page: Any, role: str, name: str, *, exact: bool = True) -> Any:
    locator = page.get_by_role(role, name=name, exact=exact)
    try:
        count = int(locator.count())
    except Exception as exc:
        raise IntakeHold(f"Travelers control {name!r} is missing or ambiguous") from exc
    if count != 1:
        raise IntakeHold(f"Travelers control {name!r} is missing or ambiguous")
    return locator


def _read_text(node: Any) -> str:
    getter = getattr(node, "inner_text", None)
    if callable(getter):
        try:
            return str(getter())
        except Exception:
            return ""
    return str(getattr(node, "text", "") or "")


class PlaywrightTravelersBrowser:
    """Page object for the Travelers Direct Bill Activity flow.

    The page must already be signed in at foragents.travelers.com. The only
    download path is the detail page's Save Document form POST; the Customer
    Snapshot transaction Download path is never used for cancellation
    notices (verified quirk).
    """

    def __init__(self, page: Any):
        self.page = page
        self._list_url = ""

    # -- list page ------------------------------------------------------
    def open_direct_bill_activity(self) -> None:
        page = self.page
        url = require_travelers_url(str(getattr(page, "url", "") or ""))
        if "directbillactivity" not in url.lower():
            try:
                _unique_control(page, "tab", _AGENCY_REPORTS_TAB).click()
                _unique_control(page, "link", _REPORT_LINK).click()
            except Exception:
                page.goto("https://foragents.travelers.com/Business/billingandpolicyservices/directbillactivity")
        try:
            try:
                page.wait_for_selector("select#select_date", timeout=1000)
            except Exception:
                page.wait_for_selector("select#billingActivityDatesDDL", timeout=14000)
        except Exception as exc:
            txt = getattr(page, "inner_text", lambda *a: "")("body")
            if "system is currently unavailable" in txt.lower():
                raise IntakeHold("Travelers direct bill system is currently unavailable (portal alert)") from exc
            raise IntakeHold("Travelers date selector is missing or ambiguous") from exc
        self._list_url = require_travelers_url(str(getattr(page, "url", "") or ""))

    def list_activity_dates(
        self, *, as_of: date, window_days: int = _RECENT_WINDOW_DAYS
    ) -> tuple[ActivityDate, ...]:
        """Read the select_date dropdown; return recent activity dates."""
        page = self.page
        select = page.locator("select#select_date")
        if select.count() == 0:
            select = page.locator("select#billingActivityDatesDDL")
        try:
            if int(select.count()) != 1:
                raise IntakeHold("Travelers date selector is missing or ambiguous")
        except IntakeHold:
            raise
        except Exception as exc:
            raise IntakeHold("Travelers date selector is missing or ambiguous") from exc
        pairs: list[tuple[str, str]] = []
        for option in select.locator("option").all():
            try:
                value = option.get_attribute("value") or ""
                text = _read_text(option)
            except Exception:
                continue
            pairs.append((value, text))
        return parse_activity_dates(tuple(pairs), as_of=as_of, window_days=window_days)

    def search_date(self, activity: ActivityDate) -> tuple[ListRow, ...]:
        """Select a date, click Search, return the agency rows."""
        page = self.page
        select = page.locator("select#select_date")
        if select.count() == 0:
            select = page.locator("select#billingActivityDatesDDL")
        select.first.select_option(activity.value)
        submit = page.locator("input#getBillingActivitybutton")
        if submit.count() > 0:
            submit.click()
        else:
            _unique_control(page, "button", _SEARCH_BUTTON).click()
        try:
            page.wait_for_selector("text=Showing", timeout=15000)
        except Exception as exc:
            txt = getattr(page, "inner_text", lambda *a: "")("body")
            if "system is currently unavailable" in txt.lower():
                raise IntakeHold("Travelers direct bill system is currently unavailable (portal alert)") from exc
            raise IntakeHold("Travelers results table did not appear") from exc
        links = page.locator(f'a.{_VIEW_LINK_CLASS}[href="javascript:void(0);"]').all()
        rows: list[tuple[str, str]] = []
        for link in links:
            try:
                agent_code = link.get_attribute("data-agentcode") or ""
                date_received = link.get_attribute("data-datereceived") or ""
            except Exception:
                continue
            if agent_code and date_received:
                rows.append((agent_code, date_received))
        return parse_list_rows(tuple(rows))

    # -- detail page ----------------------------------------------------
    def open_detail(self, row: ListRow) -> Any:
        """Click the view link; return the new detail tab."""
        page = self.page
        link = page.locator(f'a.{_VIEW_LINK_CLASS}[data-agentcode="{row.agent_code}"]')
        target = None
        try:
            for candidate in link.all():
                try:
                    if (candidate.get_attribute("data-datereceived") or "") == row.date_received:
                        target = candidate
                        break
                except Exception:
                    continue
        except Exception as exc:
            raise IntakeHold("Travelers view link is missing or ambiguous") from exc
        if target is None:
            raise IntakeHold("Travelers view link is missing or ambiguous")
        with page.expect_popup() as popup_info:
            target.click()
        detail = popup_info.value
        detail.wait_for_selector(f"div#{_DETAIL_SECTION_ID}", timeout=15000)
        require_travelers_url(str(getattr(detail, "url", "") or ""))
        return detail

    def parse_detail(
        self, detail: Any, *, agent_code: str, activity_date: date
    ) -> tuple[str, tuple[DetailRow, ...]]:
        """Parse the section type and policy rows from the detail tab."""
        section_node = detail.locator(f"div#{_DETAIL_SECTION_ID}")
        try:
            if int(section_node.count()) != 1:
                raise IntakeHold("Travelers detail section is missing or ambiguous")
        except IntakeHold:
            raise
        except Exception as exc:
            raise IntakeHold("Travelers detail section is missing or ambiguous") from exc
        title = ""
        intro = ""
        try:
            title = _norm(detail.locator(f"div#{_DETAIL_SECTION_ID} b").first.inner_text())
        except Exception:
            pass
        try:
            intro = _norm(section_node.first.inner_text())
        except Exception:
            pass
        section = classify_section(title, intro)
        table = section_node.locator("table").first
        try:
            headers = tuple(
                _norm(_read_text(node)) for node in table.locator("thead th").all()
            )
            rows = tuple(
                tuple(_norm(_read_text(cell)) for cell in row.locator("td").all())
                for row in table.locator("tbody tr").all()
            )
        except Exception as exc:
            raise IntakeHold("Travelers detail table is missing or ambiguous") from exc
        return section, parse_detail_table(
            section, headers, rows, agent_code=agent_code, activity_date=activity_date
        )

    def download_word_doc(self, detail: Any) -> bytes:
        """POST the Save Document form; return the Word document bytes.

        Uses the form's own hidden inputs (agentCode, date,
        __RequestVerificationToken) so the anti-forgery token is honored.
        Non-Word bytes raise IntakeHold and are never kept.
        """
        form = detail.locator(f'form[action="{SAVE_DOC_PATH}"]')
        try:
            if int(form.count()) != 1:
                raise IntakeHold("Travelers Save Document form is missing or ambiguous")
        except IntakeHold:
            raise
        except Exception as exc:
            raise IntakeHold("Travelers Save Document form is missing or ambiguous") from exc
        fields: dict[str, str] = {}
        for name in ("agentCode", "date", "__RequestVerificationToken"):
            node = form.locator(f'input[name="{name}"]')
            try:
                if int(node.count()) != 1:
                    raise IntakeHold(
                        f"Travelers save form field {name!r} is missing or ambiguous"
                    )
                fields[name] = node.get_attribute("value") or ""
            except IntakeHold:
                raise
            except Exception as exc:
                raise IntakeHold(
                    f"Travelers save form field {name!r} is missing or ambiguous"
                ) from exc
        save_form = parse_save_form(fields)
        request = getattr(getattr(detail, "context", None), "request", None)
        poster = getattr(request, "post", None)
        if not callable(poster):
            raise IntakeHold("Travelers document download is missing or ambiguous")
        try:
            response = poster(
                f"https://{TRAVELERS_HOST}{SAVE_DOC_PATH}",
                form=save_form.payload(),
                timeout=DOWNLOAD_TIMEOUT_MS,
            )
            body = response.body() if callable(getattr(response, "body", None)) else b""
        except Exception as exc:
            raise IntakeHold(f"Travelers document download failed: {exc}") from exc
        content = bytes(body or b"")
        if not _is_word_doc(content):
            raise IntakeHold("Travelers document download is not a Word document")
        return content

    def screenshot_list(self) -> bytes:
        data = self.page.screenshot(full_page=True, type="png")
        if not bytes(data or b"")[:8] == _PNG_MAGIC:
            raise IntakeHold("Travelers list screenshot is missing or not a PNG")
        return bytes(data)


def _row_payload(row: DetailRow, *, outcome: str, reason: str = "", filename: str = "") -> dict[str, Any]:
    payload = {
        "agent_code": row.agent_code,
        "activity_date": row.activity_date.isoformat(),
        "policy_number": row.policy_number,
        "account_name": row.account_name,
        "section": row.section,
        "notice_date": row.notice_date,
        "document_id": row.document_id,
        "outcome": outcome,
    }
    if reason:
        payload["hold_reason"] = reason
    if filename:
        payload["filename"] = filename
    return payload


def run_pull(
    browser: PlaywrightTravelersBrowser,
    ledger: TravelersDeliveryLedger,
    archive: SourceArchive,
    *,
    as_of: date,
    window_days: int = _RECENT_WINDOW_DAYS,
) -> dict[str, Any]:
    """Pull Travelers Direct Bill Activity cancellation documents.

    For each recent activity date: open the detail tab, parse the section
    type and policy rows, download the per-date Word document. Cancellation
    sections (pre-cancellation + cancellation alerts) produce per-policy
    PULLED rows sharing the date's Word document; reinstatement rows are
    HELD (they cleared a prior cancellation -- not a cancellation notice).
    A second run skips ledger hits without re-downloading.
    """
    from .document_retrieval_filing import require_carrier_pull

    require_carrier_pull("travelers")
    refuse_production_host()
    if not isinstance(as_of, date):
        raise IntakeHold("Travelers as-of date is missing or ambiguous")
    ledger.ensure_private()

    downloaded: list[dict[str, Any]] = []
    held: list[dict[str, Any]] = []
    skipped: list[str] = []
    dates_processed: list[str] = []
    rows_payload: list[dict[str, Any]] = []

    def fail(reason: str) -> None:
        raise PullHeld(reason, held=held, downloaded=downloaded, rows=rows_payload)

    browser.open_direct_bill_activity()
    png = browser.screenshot_list()
    ledger.save_screenshot(as_of, png)
    activity_dates = browser.list_activity_dates(as_of=as_of, window_days=window_days)

    for activity in activity_dates:
        dates_processed.append(activity.activity_date.isoformat())
        try:
            list_rows = browser.search_date(activity)
        except IntakeHold as exc:
            held.append({
                "activity_date": activity.activity_date.isoformat(),
                "outcome": "HELD",
                "hold_reason": str(exc),
            })
            rows_payload.append({
                "activity_date": activity.activity_date.isoformat(),
                "outcome": "HELD",
                "hold_reason": str(exc),
            })
            continue
        for list_row in list_rows:
            try:
                detail = browser.open_detail(list_row)
            except IntakeHold as exc:
                entry = {
                    "agent_code": list_row.agent_code,
                    "activity_date": activity.activity_date.isoformat(),
                    "outcome": "HELD",
                    "hold_reason": str(exc),
                }
                held.append(entry)
                rows_payload.append(entry)
                continue
            try:
                section, rows = browser.parse_detail(
                    detail, agent_code=list_row.agent_code,
                    activity_date=activity.activity_date,
                )
            except IntakeHold as exc:
                entry = {
                    "agent_code": list_row.agent_code,
                    "activity_date": activity.activity_date.isoformat(),
                    "outcome": "HELD",
                    "hold_reason": str(exc),
                }
                held.append(entry)
                rows_payload.append(entry)
                try:
                    detail.close()
                except Exception:
                    pass
                continue

            if section == _SECTION_REINSTATEMENT:
                for row in rows:
                    entry = _row_payload(
                        row, outcome="HELD",
                        reason=(
                            "Travelers reinstatement row cleared a prior "
                            "cancellation; not a cancellation notice"
                        ),
                    )
                    held.append(entry)
                    rows_payload.append(entry)
                try:
                    detail.close()
                except Exception:
                    pass
                continue

            # Cancellation sections: one Word document per (agent, date),
            # shared by that page's policy rows. Ledger the document once;
            # ledger each policy row individually for duplicate skipping.
            filename = report_filename(list_row.agent_code, activity.activity_date)
            date_doc_id = report_document_id(list_row.agent_code, activity.activity_date)
            try:
                date_already = ledger.delivery_status(
                    document_id=date_doc_id, filename=filename,
                    issued_on=activity.activity_date,
                )
            except IntakeHold as exc:
                for row in rows:
                    entry = _row_payload(row, outcome="HELD", reason=str(exc))
                    held.append(entry)
                    rows_payload.append(entry)
                try:
                    detail.close()
                except Exception:
                    pass
                continue

            content: bytes | None = None
            saved_path = ""
            if not date_already:
                try:
                    content = browser.download_word_doc(detail)
                except IntakeHold as exc:
                    for row in rows:
                        entry = _row_payload(row, outcome="HELD", reason=str(exc))
                        held.append(entry)
                        rows_payload.append(entry)
                    try:
                        detail.close()
                    except Exception:
                        pass
                    continue
                source = SourceItem(
                    system=PROCESS,
                    source_account=TRAVELERS_HOST,
                    source_id=date_doc_id,
                    source_url=f"{LIST_URL}#date={activity.activity_date.isoformat()}",
                    received_at=datetime.now(_EASTERN).isoformat(),
                    filename=filename,
                    content=content,
                )
                source.validate()
                try:
                    saved = ledger.record(source, issued_on=activity.activity_date)
                    archive.preserve(source)
                    saved_path = str(saved)
                except IntakeHold as exc:
                    for row in rows:
                        entry = _row_payload(row, outcome="HELD", reason=str(exc))
                        held.append(entry)
                        rows_payload.append(entry)
                    try:
                        detail.close()
                    except Exception:
                        pass
                    continue
            else:
                saved_path = str(ledger.doc_path(activity.activity_date, filename))
                content = ledger.doc_path(activity.activity_date, filename).read_bytes()

            digest = hashlib.sha256(content).hexdigest()
            for row in rows:
                if ledger.row_delivered(document_id=row.document_id):
                    skipped.append(row.document_id)
                    rows_payload.append(_row_payload(
                        row, outcome="ALREADY_DELIVERED", filename=filename
                    ))
                    continue
                try:
                    ledger.record_row(
                        document_id=row.document_id, filename=filename,
                        issued_on=activity.activity_date, digest=digest,
                        size=len(content), report_document_id=date_doc_id,
                        insured_name=row.account_name,
                    )
                except IntakeHold as exc:
                    entry = _row_payload(row, outcome="HELD", reason=str(exc))
                    held.append(entry)
                    rows_payload.append(entry)
                    continue
                downloaded.append({
                    "document_id": row.document_id,
                    "filename": filename,
                    "sha256": digest,
                    "bytes": len(content),
                    "agent_code": row.agent_code,
                    "activity_date": row.activity_date.isoformat(),
                    "policy_number": row.policy_number,
                    "account_name": row.account_name,
                    "section": row.section,
                    "notice_date": row.notice_date,
                    "path": saved_path,
                })
                rows_payload.append(_row_payload(row, outcome="PULLED", filename=filename))
            try:
                detail.close()
            except Exception:
                pass

    if held:
        fail("; ".join(sorted({str(row.get("hold_reason", "held")) for row in held})))
    return {
        "status": "PULLED",
        "scope": SCOPE,
        "process": PROCESS,
        "as_of": as_of.isoformat(),
        "window_days": window_days,
        "dates_processed": dates_processed,
        "count": len(downloaded),
        "downloaded": downloaded,
        "skipped_already_delivered": skipped,
        "held": held,
        "rows": rows_payload,
        "ezlynx": "not_run",
    }


def select_travelers_page(pages: list[Any]) -> Any:
    """Use the Travelers foragents portal tab."""
    matches = [
        page for page in pages
        if (urllib.parse.urlsplit(str(getattr(page, "url", "") or "")).hostname or "").lower() == TRAVELERS_HOST
    ]
    if not matches:
        raise IntakeHold("Expected at least one Travelers portal tab")
    for p in matches:
        u = str(getattr(p, "url", "") or "").lower()
        if "directbillactivity" in u:
            return p
    return matches[0]


def connect_cdp_browser(cdp_url: str | None) -> tuple[PlaywrightTravelersBrowser, Callable[[], None]]:
    """Attach to the local carrier Chrome. Exactly one Travelers tab."""
    from .document_retrieval_filing import require_carrier_pull

    require_carrier_pull("travelers")
    refuse_production_host()
    require_hermes_test_host()
    url = (cdp_url or os.environ.get("ROBIE_BROWSER_CDP_URL") or DEFAULT_CDP_URL).strip()
    parsed = urllib.parse.urlsplit(url)
    if parsed.username or parsed.password or parsed.scheme not in {"http", "https"}:
        raise IntakeHold("Travelers browser attach must use the local Test CDP endpoint")
    if (parsed.hostname or "").lower() not in {"127.0.0.1", "localhost"}:
        raise IntakeHold("Travelers browser attach must use the local Test CDP endpoint")
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(url)
        pages = [page for context in browser.contexts for page in context.pages]
        return PlaywrightTravelersBrowser(select_travelers_page(pages)), playwright.stop
    except Exception:
        playwright.stop()
        raise


def qa_pack_dir(output_root: Path, as_of: date) -> Path:
    folder = output_root / "Travelers" / as_of.isoformat()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(folder, 0o700)
    return folder


def drive_record(day: date) -> dict[str, Any]:
    return {
        "status": "not_run",
        "parent_id": CARRIER_QA_DRIVE_PARENT_ID,
        "folder_id": None,
        "path": f"Robie Carrier Pull QA (Nicole)/Travelers/{day.isoformat()}/",
    }


def render_qa_readme(
    *,
    as_of: date,
    run_ts: str,
    status: str,
    rows: list[dict[str, Any]],
    downloaded: list[dict[str, Any]],
    held: list[dict[str, Any]],
    reason: str = "",
) -> str:
    lines = [
        f"# Travelers Direct Bill Activity pull — {as_of.isoformat()}",
        "",
        f"Run: {run_ts}",
        f"Status: {status}",
        f"Rows: {len(rows)}",
        f"Downloaded: {len(downloaded)}",
        f"Held: {len(held)}",
        "",
        "The per-date Word report (Save Document form POST) is the only "
        "download path. The Customer Snapshot transaction Download path is "
        "refused for cancellation notices (verified quirk: the Cancel Notice "
        "row returned the Welcome Letter ZIP). Reinstatement rows are held: "
        "they cleared a prior cancellation and are not cancellation notices.",
        "",
    ]
    if reason:
        lines += [f"Hold reason: {reason}", ""]
    for row in downloaded:
        lines.append(
            f"- DOWNLOADED {row.get('policy_number')} {row.get('account_name')} "
            f"({row.get('section')}) -> {row.get('filename')}"
        )
    for row in held:
        lines.append(
            f"- HELD {row.get('policy_number', row.get('activity_date', '?'))}: "
            f"{row.get('hold_reason', row.get('reason', ''))}"
        )
    return "\n".join(lines) + "\n"


def _replace_private(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(path.name + ".tmp")
    if temporary.is_symlink():
        raise IntakeHold("QA pack file is missing or ambiguous")
    if temporary.exists():
        temporary.unlink()
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


def publish_qa_pack(
    pack: Path,
    *,
    as_of: date,
    run_ts: str,
    status: str,
    rows: list[dict[str, Any]],
    downloaded: list[dict[str, Any]],
    held: list[dict[str, Any]],
    reason: str = "",
    drive: dict[str, Any] | None = None,
) -> Path:
    manifest = {
        "carrier": "travelers",
        "as_of": as_of.isoformat(),
        "run_ts": run_ts,
        "status": status,
        "rows": rows,
        "downloaded": downloaded,
        "held": held,
        "reason": reason,
        "drive": drive or drive_record(as_of),
    }
    _replace_private(pack / "manifest.json", json.dumps(manifest, indent=2, sort_keys=True).encode())
    _replace_private(
        pack / "README.md",
        render_qa_readme(
            as_of=as_of, run_ts=run_ts, status=status, rows=rows,
            downloaded=downloaded, held=held, reason=reason,
        ).encode(),
    )
    return pack


def main(
    argv: list[str] | None = None,
    *,
    browser_factory: Callable[[argparse.Namespace], Any] | None = None,
    run_ts: str | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Test-only Travelers cancellation document pull")
    parser.add_argument("--as-of", default=date.today().isoformat())
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--cdp-url", default=None)
    parser.add_argument("--window-days", type=int, default=_RECENT_WINDOW_DAYS)
    parser.add_argument("--upload-drive", action="store_true")
    args = parser.parse_args(argv)
    if args.upload_drive:
        raise IntakeHold(DRIVE_UPLOAD_UNAVAILABLE)
    closer: Callable[[], None] | None = None
    try:
        from .document_retrieval_filing import require_carrier_pull

        require_carrier_pull("travelers")
        refuse_production_host()
        try:
            as_of = date.fromisoformat(args.as_of)
        except ValueError as exc:
            raise IntakeHold("Travelers as-of date is missing or ambiguous") from exc
        output_root = qa_pack_dir(Path(args.output_root), as_of)
        stamped = run_ts or datetime.now(_EASTERN).isoformat()
        if browser_factory is None:
            require_hermes_test_host()
            browser, closer = connect_cdp_browser(args.cdp_url)
        else:
            browser = browser_factory(args)
        ledger = TravelersDeliveryLedger(output_root)
        archive = SourceArchive(output_root / "sources")
        try:
            receipt = run_pull(browser, ledger, archive, as_of=as_of, window_days=args.window_days)
        except PullHeld as exc:
            publish_qa_pack(
                pack=output_root,
                as_of=as_of,
                run_ts=stamped,
                status="HELD",
                rows=list(exc.details.get("rows") or []),
                downloaded=list(exc.details.get("downloaded") or []),
                held=list(exc.details.get("held") or []),
                reason=str(exc),
                drive=drive_record(as_of),
            )
            payload = {"status": "HELD", "reason": str(exc), "ezlynx": "not_run",
                       "pack": str(output_root)}
            payload.update(exc.details)
            payload["drive"] = drive_record(as_of)
            sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            return 2
        publish_qa_pack(
            pack=output_root,
            as_of=as_of,
            run_ts=stamped,
            status="PULLED",
            rows=list(receipt.get("rows") or []),
            downloaded=list(receipt.get("downloaded") or []),
            held=list(receipt.get("held") or []),
            drive=drive_record(as_of),
        )
        receipt["drive"] = drive_record(as_of)
        receipt["pack"] = str(output_root)
        receipt["manifest"] = str(output_root / "manifest.json")
        receipt["readme"] = str(output_root / "README.md")
        sys.stdout.write(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        return 0
    finally:
        if closer is not None:
            closer()


if __name__ == "__main__":
    sys.exit(main())
