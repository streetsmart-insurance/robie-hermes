"""Test-only Travelers pre-cancellation alert pull.

The playbook opens an already signed-in foragents.travelers.com tab and
follows the verified queue to the Direct Bill Activity Details
("Pre-cancellation Alert") list:

    Dashboard -> "Agency Reports" tab
      -> "Direct Bill Activity Cancellation & Reinstatement Notices"
      -> Direct Bill Activity report (date selector) -> "view" link
      -> "Direct Bill Activity Details" listing policies with a
         Cancellation Notice Date.

Each policy row opens its pre-cancellation alert, and the alert's
"Save Document" button is the only download path this module uses.

VERIFIED QUIRK (2026-10-02, twice): the Customer Snapshot -> Policies tab
transaction table is NOT used for cancellation notices. Selecting the
"Cancel Notice" transaction row and clicking Download returned the Welcome
Letter ZIP instead of the cancellation notice. Filenames follow the selected
transaction for other types (New Business -> New_Business.zip), so the Cancel
Notice row specifically is unreliable. This module never navigates to
Customer Snapshot and never clicks a transaction Download for a
cancellation notice; a missing "Save Document" button holds instead of
falling back.

As of 10/02/2026 the details list showed 3 policies with Cancellation
Notice Date 10/12/2026: UB-B326957A (D SERVICE LANDSCAPING LLC, $864.84),
UB-0S764907 (CRYSTAL WOOD FLOORS LLC, $5,938.00), UB-C8291492
(EMPOWER GROUP, LLC, $556.16).

A missing or non-unique control raises IntakeHold. This module does not
log in, does not upload, note, task, or label in EZLynx, and does not
register a timer.
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
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .intake_core import IntakeHold, SourceArchive, SourceItem


TRAVELERS_SOURCE_ACCOUNT = "foragents.travelers.com"
DASHBOARD_HOST = "foragents.travelers.com"
DEFAULT_CDP_URL = "http://127.0.0.1:9223"
DOWNLOAD_TIMEOUT_MS = 8000
LEDGER_NAME = "travelers-pre-cancel-ledger.json"
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
# Travelers commercial, e.g. UB-B326957A. Verified shape is 2 letters, a dash,
# and 8 alphanumerics; the pattern is intentionally a little lenient and the
# pull still holds on anything it cannot parse.
_POLICY_NUMBER = re.compile(r"^[A-Za-z]{2}-[A-Za-z0-9]{8}$")
_EASTERN = ZoneInfo("America/New_York")

# Verified navigation labels (training guide, 2026-10-02).
_AGENCY_REPORTS_TAB = "Agency Reports"
_REPORT_LINK = "Direct Bill Activity Cancellation & Reinstatement Notices"
_DETAILS_HEADING = "Direct Bill Activity Details"
_SAVE_DOCUMENT = "Save Document"
_VIEW_LINK = "view"
# INFERENCE (flagged): the exact caption of the report-run button was not
# recorded during the walk. The worker tries these labels in order and
# requires exactly one match for the chosen label; anything else holds.
_REPORT_SUBMIT_NAMES = ("View Report", "Run Report", "Submit", "Search")
# INFERENCE (flagged): when the report returns more than one agency row, the
# worker selects the row naming STREETSMART and holds on zero or many.
_AGENCY_MATCH = "STREETSMART"

# Verified quirk (2026-10-02, twice): the Customer Snapshot transaction-list
# Download path returns the wrong document for Cancel Notice rows (Welcome
# Letter ZIP). It is never used here.
_CUSTOMER_SNAPSHOT_HOSTS = ("foragents.travelers.com",)
_REFUSED_TRANSACTION_DOWNLOAD = (
    "Travelers Customer Snapshot transaction Download is refused for "
    "cancellation notices: the Cancel Notice row returned the Welcome Letter "
    "ZIP (verified twice). Use the pre-cancellation alert Save Document."
)

_HEADER_FIELDS = (
    ("policy_number", frozenset({"policy number", "policy", "policy #"})),
    ("insured_name", frozenset({"insured", "insured name", "named insured", "customer name"})),
    ("amount_due", frozenset({"amount due", "amount", "balance due", "premium due"})),
    ("notice_date", frozenset({
        "cancellation notice date",
        "cancel notice date",
        "notice date",
    })),
)


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


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
        raise IntakeHold("Travelers pre-cancel pull refuses Production host hermes-poc-01")


def require_png(blob: bytes | bytearray | None) -> bytes:
    if not isinstance(blob, (bytes, bytearray)) or not bytes(blob).startswith(_PNG_MAGIC):
        raise IntakeHold("Pre-cancellation alert screenshot is missing or not a PNG")
    return bytes(blob)


def pending_screenshot_name(day: date) -> str:
    name = f"travelers-pre-cancellation-alert-{day.isoformat()}.png"
    if name != Path(name).name:
        raise IntakeHold("Pre-cancellation alert screenshot is missing or not a PNG")
    return name


def parse_notice_date(value: str) -> date:
    raw = _norm(value)
    for fmt in ("%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise IntakeHold("Cancellation Notice Date is missing or ambiguous")


def alert_filename(policy_number: str) -> str:
    policy = str(policy_number or "").strip()
    if not _POLICY_NUMBER.fullmatch(policy):
        raise IntakeHold("Alert policy number is missing or ambiguous")
    return f"{policy} Pre-Cancellation Alert Travelers.pdf"


def alert_document_id(policy_number: str, notice_on: date) -> str:
    policy = str(policy_number or "").strip()
    if not _POLICY_NUMBER.fullmatch(policy):
        raise IntakeHold("Alert policy number is missing or ambiguous")
    return f"travelers-pre-cancel-alert:{policy}:{notice_on.isoformat()}"


@dataclass(frozen=True)
class AlertGrid:
    list_url: str
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class AlertRow:
    document_id: str
    policy_number: str
    insured_name: str
    amount_due: str
    notice_on: date
    filename: str
    row_index: int
    source_url: str


@dataclass(frozen=True)
class NoticePath:
    kind: str
    notice_name: str = ""


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


def _header_indexes(headers: tuple[str, ...]) -> dict[str, int]:
    normalized = [_norm(h).casefold() for h in headers]
    indexes: dict[str, int] = {}
    for field, aliases in _HEADER_FIELDS:
        found = [i for i, h in enumerate(normalized) if h in aliases]
        if len(found) != 1:
            raise IntakeHold(
                f"Travelers alert table header {field!r} is missing or ambiguous"
            )
        indexes[field] = found[0]
    return indexes


def parse_alert_grid(grid: AlertGrid) -> tuple[AlertRow, ...]:
    """Parse the Direct Bill Activity Details table into alert rows."""
    if not grid.list_url:
        raise IntakeHold("Pre-cancellation alert list URL is missing or ambiguous")
    indexes = _header_indexes(grid.headers)
    rows: list[AlertRow] = []
    for position, cells in enumerate(grid.rows):
        if len(cells) <= max(indexes.values()):
            raise IntakeHold("Pre-cancellation alert row is missing or ambiguous")
        policy = _norm(cells[indexes["policy_number"]])
        if not _POLICY_NUMBER.fullmatch(policy):
            raise IntakeHold("Alert policy number is missing or ambiguous")
        insured = _norm(cells[indexes["insured_name"]])
        if not insured:
            raise IntakeHold("Alert insured name is missing or ambiguous")
        amount = _norm(cells[indexes["amount_due"]])
        notice_on = parse_notice_date(cells[indexes["notice_date"]])
        rows.append(
            AlertRow(
                document_id=alert_document_id(policy, notice_on),
                policy_number=policy,
                insured_name=insured,
                amount_due=amount,
                notice_on=notice_on,
                filename=alert_filename(policy),
                row_index=position,
                source_url=grid.list_url,
            )
        )
    seen = [row.policy_number for row in rows]
    if len(set(seen)) != len(seen):
        raise IntakeHold("Pre-cancellation alert list has a duplicate policy")
    return tuple(rows)


class LocalDeliveryLedger:
    """Private named-PDF ledger. A conflicting file is kept and the pull holds."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def ensure_private(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        if self.root.is_symlink() or not self.root.is_dir() or self.root.stat().st_mode & 0o077:
            raise IntakeHold("Alert output directory must be private (0700)")

    def _named_path(self, filename: str) -> Path:
        name = str(filename or "").strip()
        if not name or name != Path(name).name or name == LEDGER_NAME:
            raise IntakeHold("Alert filename is missing or ambiguous")
        return self.root / name

    def _ledger_path(self) -> Path:
        return self.root / LEDGER_NAME

    def _load(self) -> dict[str, Any]:
        self.ensure_private()
        path = self._ledger_path()
        if not path.exists():
            return {"items": {}}
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise IntakeHold("Alert ledger is missing or ambiguous") from exc
        if not isinstance(data, dict) or not isinstance(data.get("items"), dict):
            raise IntakeHold("Alert ledger is missing or ambiguous")
        return data

    def _write(self, data: dict[str, Any]) -> None:
        path = self._ledger_path()
        tmp = path.with_name(path.name + ".tmp")
        payload = json.dumps(data, indent=2, sort_keys=True).encode()
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        os.chmod(path, 0o600)

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
            raise IntakeHold("Existing alert file conflicts with the pull ledger")
        return True

    def record(self, source: SourceItem, *, notice_on: date, policy_number: str) -> Path:
        self.ensure_private()
        path = self._named_path(source.filename)
        if path.exists():
            raise IntakeHold("Existing alert file conflicts with the pull ledger")
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
            raise IntakeHold("Existing alert file conflicts with the pull ledger")
        data["items"][source.source_id] = {
            "filename": source.filename,
            "sha256": digest,
            "bytes": len(source.content),
            "notice_date": notice_on.isoformat(),
            "policy_number": policy_number,
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
                raise IntakeHold("Pre-cancellation alert screenshot is missing or not a PNG")
            if primary.read_bytes() == blob:
                return primary
            stem = pending_screenshot_name(day)[:-4]
            for index in range(2, 100):
                candidate = self.root / f"{stem}-{index}.png"
                if not candidate.exists():
                    candidate.write_bytes(blob)
                    os.chmod(candidate, 0o600)
                    return candidate
            raise IntakeHold("Pre-cancellation alert screenshot is missing or not a PNG")
        primary.write_bytes(blob)
        os.chmod(primary, 0o600)
        return primary


def _unique(locator: Any, what: str) -> Any:
    try:
        count = int(locator.count())
    except Exception as exc:
        raise IntakeHold(f"Travelers {what} is missing or ambiguous") from exc
    if count != 1:
        raise IntakeHold(f"Travelers {what} is missing or ambiguous")
    return locator


def _click_unique(page: Any, *, role: str, name: str, what: str, exact: bool = True) -> None:
    locator = page.get_by_role(role, name=name, exact=exact)
    _unique(locator, what).click()


class PlaywrightTravelersAlertBrowser:
    """Page object for the Travelers pre-cancellation alert queue.

    The page must already be signed in at foragents.travelers.com. The only
    download path is the alert's "Save Document" button; the Customer
    Snapshot transaction Download path is never used for cancellation
    notices (verified quirk).
    """

    def __init__(self, page: Any):
        self.page = page
        self._list_url: str | None = None
        self._alert_policy: str | None = None

    def _require_dashboard(self) -> None:
        host = (urllib.parse.urlsplit(str(getattr(self.page, "url", "") or "")).hostname or "").lower()
        if host != DASHBOARD_HOST:
            raise IntakeHold("Travelers dashboard URL is missing or ambiguous")

    def open_alert_list(self, as_of: date) -> None:
        """Dashboard -> Agency Reports -> report -> date -> view -> Details."""
        page = self.page
        self._require_dashboard()
        # 1. Agency Reports tab.
        _click_unique(page, role="tab", name=_AGENCY_REPORTS_TAB, what="Agency Reports tab")
        # 2. The Direct Bill Activity Cancellation & Reinstatement Notices report.
        _click_unique(page, role="link", name=_REPORT_LINK, what="Direct Bill Activity report link")
        # 3. Date selector: exactly one date field on the report form.
        try:
            date_fields = page.get_by_role("textbox")
            count = int(date_fields.count())
        except Exception as exc:
            raise IntakeHold("Travelers report date field is missing or ambiguous") from exc
        if count != 1:
            raise IntakeHold("Travelers report date field is missing or ambiguous")
        date_fields.first.fill(as_of.strftime("%m/%d/%Y"))
        # 4. Run the report. The exact caption was not recorded on the walk;
        # the first label below with exactly one match wins, else hold.
        submitted = False
        for label in _REPORT_SUBMIT_NAMES:
            try:
                matches = page.get_by_role("button", name=label, exact=True)
                if int(matches.count()) == 1:
                    matches.click()
                    submitted = True
                    break
            except Exception as exc:
                raise IntakeHold("Travelers report submit is missing or ambiguous") from exc
        if not submitted:
            raise IntakeHold("Travelers report submit is missing or ambiguous")
        # 5. Results table "view" link. One row -> its view link. Many rows ->
        # the single STREETSMART row's view link. Zero or many matches hold.
        view_links = page.get_by_role("link", name=_VIEW_LINK, exact=True)
        try:
            view_count = int(view_links.count())
        except Exception as exc:
            raise IntakeHold("Travelers report view link is missing or ambiguous") from exc
        if view_count == 1:
            view_links.first.click()
        elif view_count > 1:
            rows = page.locator("table tbody tr")
            try:
                row_count = int(rows.count())
            except Exception as exc:
                raise IntakeHold("Travelers report view link is missing or ambiguous") from exc
            street_rows = []
            for row in rows.all():
                try:
                    text = _norm(row.inner_text())
                except Exception:
                    continue
                if _AGENCY_MATCH in text.upper():
                    street_rows.append(row)
            if len(street_rows) != 1:
                raise IntakeHold("Travelers report view link is missing or ambiguous")
            link = street_rows[0].get_by_role("link", name=_VIEW_LINK, exact=True)
            _unique(link, "Travelers report view link").click()
        else:
            raise IntakeHold("Travelers report view link is missing or ambiguous")
        # 6. Post-action read-back: the Details heading must be present.
        heading = page.get_by_role("heading", name=_DETAILS_HEADING, exact=True)
        _unique(heading, "Direct Bill Activity Details heading")
        self._list_url = str(getattr(page, "url", "") or "")

    def load_alert_list(self) -> AlertGrid:
        """Parse the Direct Bill Activity Details table."""
        page = self.page
        heading = page.get_by_role("heading", name=_DETAILS_HEADING, exact=True)
        _unique(heading, "Direct Bill Activity Details heading")
        tables = page.locator("table")
        _unique(tables, "Direct Bill Activity Details table")
        header_nodes = tables.first.locator("thead th").all()
        if not header_nodes:
            raise IntakeHold("Pre-cancellation alert table is missing or ambiguous")
        headers = tuple(_norm(node.inner_text()) for node in header_nodes)
        body_rows = tables.first.locator("tbody tr").all()
        grid_rows = []
        for row in body_rows:
            cells = row.locator("td").all()
            grid_rows.append(tuple(_norm(cell.inner_text()) for cell in cells))
        if not grid_rows:
            raise IntakeHold("Pre-cancellation alert table is missing or ambiguous")
        list_url = self._list_url or str(getattr(page, "url", "") or "")
        return AlertGrid(list_url=list_url, headers=headers, rows=tuple(grid_rows))

    def screenshot_alert_list(self) -> bytes:
        return require_png(self.page.screenshot(full_page=True, type="png"))

    def inspect_notice_path(self, policy_number: str) -> NoticePath:
        """The Save Document button is the only supported path.

        Customer Snapshot transaction Download is refused for cancellation
        notices (verified quirk): it returned the Welcome Letter ZIP twice.
        """
        policy = str(policy_number or "").strip()
        if not _POLICY_NUMBER.fullmatch(policy):
            raise IntakeHold("Alert policy number is missing or ambiguous")
        if self._alert_policy != policy:
            link = self.page.get_by_role("link", name=policy, exact=True)
            _unique(link, f"Travelers alert policy link {policy}")
            link.click()
            self._alert_policy = policy
        save = self.page.get_by_role("button", name=_SAVE_DOCUMENT, exact=True)
        try:
            save_count = int(save.count())
        except Exception as exc:
            raise IntakeHold("Travelers Save Document is missing or ambiguous") from exc
        if save_count != 1:
            raise IntakeHold("Travelers Save Document is missing or ambiguous")
        return NoticePath("save_document", _SAVE_DOCUMENT)

    def download_alert(self, policy_number: str) -> bytes:
        """Click Save Document and capture the download. Never Customer Snapshot."""
        self.inspect_notice_path(policy_number)
        page = self.page
        save = page.get_by_role("button", name=_SAVE_DOCUMENT, exact=True)
        with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as download_info:
            _unique(save, "Travelers Save Document").click()
        download = download_info.value
        path = getattr(download, "path", None)
        if callable(path):
            blob = Path(str(path())).read_bytes()
        else:
            raise IntakeHold("Travelers Save Document download is missing or ambiguous")
        if not blob:
            raise IntakeHold("Travelers Save Document download is missing or ambiguous")
        return blob

    def return_to_alert_list(self) -> None:
        self.page.go_back()
        self._alert_policy = None
        heading = self.page.get_by_role("heading", name=_DETAILS_HEADING, exact=True)
        _unique(heading, "Direct Bill Activity Details heading")


def _row_payload(alert: AlertRow, *, outcome: str, reason: str = "") -> dict[str, Any]:
    payload = {
        "document_id": alert.document_id,
        "policy_number": alert.policy_number,
        "insured_name": alert.insured_name,
        "amount_due": alert.amount_due,
        "notice_date": alert.notice_on.isoformat(),
        "filename": alert.filename,
        "outcome": outcome,
    }
    if reason:
        payload["reason"] = reason
    return payload


def run_pull(
    browser: Any,
    ledger: LocalDeliveryLedger,
    archive: SourceArchive,
    *,
    as_of: date,
) -> dict[str, Any]:
    """List pre-cancellation alerts, download new ones, return the receipt."""
    from .document_retrieval_filing import require_carrier_pull

    require_carrier_pull("travelers")
    refuse_production_host()
    if not isinstance(as_of, date):
        raise IntakeHold("Pre-cancellation alert as-of date is missing or ambiguous")
    ledger.ensure_private()

    browser.open_alert_list(as_of)
    grid = browser.load_alert_list()
    alerts = parse_alert_grid(grid)
    png = require_png(browser.screenshot_alert_list())
    shot_path = ledger.save_screenshot(as_of, png)

    seen = {alert.policy_number: _row_payload(alert, outcome="LISTED") for alert in alerts}
    held: list[dict[str, Any]] = []
    downloaded: list[dict[str, Any]] = []

    def fail(reason: str) -> None:
        raise PullHeld(
            reason,
            held=held,
            downloaded=downloaded,
            rows=[seen[alert.policy_number] for alert in alerts],
        )

    for alert in alerts:
        try:
            already = ledger.delivery_status(document_id=alert.document_id, filename=alert.filename)
        except IntakeHold as exc:
            row = _row_payload(alert, outcome="HELD", reason=str(exc))
            held.append(row)
            seen[alert.policy_number] = row
            fail(str(exc))
        if already:
            row = _row_payload(alert, outcome="SKIPPED", reason="already pulled; dedup ledger hit")
            seen[alert.policy_number] = row
            continue
        try:
            path = browser.inspect_notice_path(alert.policy_number)
        except IntakeHold as exc:
            row = _row_payload(alert, outcome="HELD", reason=str(exc))
            held.append(row)
            seen[alert.policy_number] = row
            try:
                browser.return_to_alert_list()
            except IntakeHold:
                pass
            continue
        if path.kind != "save_document":
            row = _row_payload(
                alert,
                outcome="HELD",
                reason=_REFUSED_TRANSACTION_DOWNLOAD,
            )
            held.append(row)
            seen[alert.policy_number] = row
            try:
                browser.return_to_alert_list()
            except IntakeHold:
                pass
            continue
        blob = browser.download_alert(alert.policy_number)
        item = SourceItem(
            system="travelers",
            source_account=TRAVELERS_SOURCE_ACCOUNT,
            source_id=alert.document_id,
            source_url=alert.source_url,
            received_at=datetime.now(_EASTERN).isoformat(),
            filename=alert.filename,
            content=blob,
        )
        archive.preserve(item)
        ledger.record(item, notice_on=alert.notice_on, policy_number=alert.policy_number)
        try:
            browser.return_to_alert_list()
        except IntakeHold as exc:
            fail(f"could not return to the alert list: {exc}")
        row = _row_payload(alert, outcome="DOWNLOADED")
        seen[alert.policy_number] = row
        downloaded.append(row)

    if held:
        fail("; ".join(sorted({row.get("reason", "held") for row in held})))
    return {
        "status": "PULLED",
        "carrier": "travelers",
        "as_of": as_of.isoformat(),
        "count": len(downloaded),
        "alerts": [seen[alert.policy_number] for alert in alerts],
        "downloaded": downloaded,
        "held": held,
        "verification": {"screenshot": str(shot_path)},
    }


def qa_pack_dir(output_root: Path, day: date) -> Path:
    """Return ``{output_root}/{YYYY-MM-DD}``."""
    root = Path(output_root).expanduser()
    if not root.is_absolute():
        raise IntakeHold("QA pack output root must be an absolute path")
    if not isinstance(day, date):
        raise IntakeHold("Pre-cancellation alert as-of date is missing or ambiguous")
    pack = root / day.isoformat()
    resolved_root = root.resolve()
    resolved_pack = pack.resolve()
    if resolved_root != resolved_pack and resolved_root not in resolved_pack.parents:
        raise IntakeHold("QA pack path is missing or ambiguous")
    if pack.name != day.isoformat():
        raise IntakeHold("QA pack path is missing or ambiguous")
    return pack


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
    alerts: list[dict[str, Any]],
    downloaded: list[dict[str, Any]],
    held: list[dict[str, Any]],
    reason: str = "",
) -> str:
    lines = [
        f"# Travelers pre-cancellation alert pull — {as_of.isoformat()}",
        "",
        f"Run: {run_ts}",
        f"Status: {status}",
        f"Alerts listed: {len(alerts)}",
        f"Downloaded: {len(downloaded)}",
        f"Held: {len(held)}",
        "",
        "The alert Save Document button is the only download path. The "
        "Customer Snapshot transaction Download path is refused for "
        "cancellation notices (verified quirk: the Cancel Notice row "
        "returned the Welcome Letter ZIP).",
        "",
    ]
    if reason:
        lines += [f"Hold reason: {reason}", ""]
    for row in downloaded:
        lines.append(
            f"- DOWNLOADED {row.get('policy_number')} {row.get('insured_name')} "
            f"notice {row.get('notice_date')} -> {row.get('filename')}"
        )
    for row in held:
        lines.append(
            f"- HELD {row.get('policy_number')} {row.get('insured_name')}: {row.get('reason')}"
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
    alerts: list[dict[str, Any]],
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
        "alerts": alerts,
        "downloaded": downloaded,
        "held": held,
        "reason": reason,
        "drive": drive or drive_record(as_of),
    }
    _replace_private(pack / "manifest.json", json.dumps(manifest, indent=2, sort_keys=True).encode())
    _replace_private(
        pack / "README.md",
        render_qa_readme(
            as_of=as_of,
            run_ts=run_ts,
            status=status,
            alerts=alerts,
            downloaded=downloaded,
            held=held,
            reason=reason,
        ).encode(),
    )
    return pack


def require_loopback_cdp(url: str) -> str:
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.username or parsed.password or parsed.scheme not in {"http", "https"} or host not in {"127.0.0.1", "localhost"}:
        raise IntakeHold("Travelers browser attach must use the local Test CDP endpoint")
    return str(url).strip()


def require_hermes_test_host() -> None:
    """Live packs are produced on hermes-test-01. Fixture runs inject a browser."""

    from .document_retrieval_filing import live_filing_decision

    if live_filing_decision(os.environ, socket.gethostname(), "travelers").allowed:
        return
    raw = f"{socket.gethostname()} {socket.getfqdn()}".lower()
    labels = [label for label in re.split(r"[\s.]+", raw) if label]
    if HERMES_TEST_HOST not in labels:
        raise IntakeHold("Travelers QA pack must be produced on hermes-test-01")


def connect_cdp_browser(cdp_url: str | None) -> tuple[PlaywrightTravelersAlertBrowser, Callable[[], None]]:
    endpoint = require_loopback_cdp(cdp_url or DEFAULT_CDP_URL)
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()

    def close() -> None:
        playwright.stop()

    browser = playwright.chromium.connect_over_cdp(endpoint)
    contexts = browser.contexts
    if len(contexts) != 1:
        close()
        raise IntakeHold("Travelers browser attach is missing or ambiguous")
    pages = contexts[0].pages
    if len(pages) != 1:
        close()
        raise IntakeHold("Travelers browser attach is missing or ambiguous")
    return PlaywrightTravelersAlertBrowser(pages[0]), close


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Test-only Travelers pre-cancellation alert pull")
    parser.add_argument("--as-of", required=True, help="YYYY-MM-DD")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--cdp-url", default=DEFAULT_CDP_URL)
    parser.add_argument("--upload-drive", action="store_true")
    return parser


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _emit_drive_refusal(pack: Path, *, portal_gate: str, extra: dict[str, Any] | None = None) -> int:
    payload: dict[str, Any] = {
        "status": portal_gate,
        "drive": dict(drive_record(date.today()), status="refused"),
        "reason": DRIVE_UPLOAD_UNAVAILABLE,
        "pack": str(pack),
    }
    payload.update(extra or {})
    _emit(payload)
    return 3


def main(
    argv: list[str] | None = None,
    *,
    browser_factory: Callable[[argparse.Namespace], Any] | None = None,
    run_ts: str | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    closer: Callable[[], None] | None = None
    pack: Path | None = None
    try:
        from .document_retrieval_filing import FilingHeld, require_carrier_pull, resolve_pull_output

        require_carrier_pull("travelers")
        refuse_production_host()
        try:
            as_of = date.fromisoformat(args.as_of)
        except ValueError as exc:
            raise IntakeHold("Pre-cancellation alert as-of date is missing or ambiguous") from exc
        try:
            output_root = resolve_pull_output(args.output_root, DEFAULT_OUTPUT_ROOT)
        except FilingHeld as exc:
            raise IntakeHold(str(exc)) from exc
        pack = qa_pack_dir(output_root, as_of)
        stamped = run_ts or datetime.now(_EASTERN).isoformat()
        if browser_factory is None:
            require_hermes_test_host()
            browser, closer = connect_cdp_browser(args.cdp_url)
        else:
            browser = browser_factory(args)
        ledger = LocalDeliveryLedger(pack)
        archive = SourceArchive(pack / "sources")
        try:
            receipt = run_pull(browser, ledger, archive, as_of=as_of)
        except PullHeld as exc:
            publish_qa_pack(
                pack,
                as_of=as_of,
                run_ts=stamped,
                status="HELD",
                alerts=list(exc.details.get("rows") or []),
                downloaded=list(exc.details.get("downloaded") or []),
                held=list(exc.details.get("held") or []),
                reason=str(exc),
                drive=drive_record(as_of),
            )
            if args.upload_drive:
                return _emit_drive_refusal(pack, portal_gate="HELD", extra={"pull_reason": str(exc)})
            payload = {"status": "HELD", "reason": str(exc), "ezlynx": "not_run", "pack": str(pack)}
            payload.update(exc.details)
            payload["drive"] = drive_record(as_of)
            _emit(payload)
            return 2
        publish_qa_pack(
            pack,
            as_of=as_of,
            run_ts=stamped,
            status="PULLED",
            alerts=list(receipt.get("alerts") or []),
            downloaded=list(receipt.get("downloaded") or []),
            held=list(receipt.get("held") or []),
            drive=drive_record(as_of),
        )
        if args.upload_drive:
            return _emit_drive_refusal(
                pack,
                portal_gate="PULLED",
                extra={
                    "downloaded": receipt.get("downloaded") or [],
                    "held": receipt.get("held") or [],
                },
            )
        receipt["drive"] = drive_record(as_of)
        receipt["pack"] = str(pack)
        receipt["manifest"] = str(pack / "manifest.json")
        receipt["readme"] = str(pack / "README.md")
        _emit(receipt)
        return 0
    finally:
        if closer is not None:
            closer()


if __name__ == "__main__":
    sys.exit(main())
