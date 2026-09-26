"""Fetch work-queue rows for the registered EZLynx Reports 5.0 reports.

``fetch_report_rows(*, report_id, fields=None, filters=None, db_path=None,
session=None) -> list[dict]`` is the stable entry point the four verification
workers import. It:

1. Resolves the report via :func:`report_registry.get_report_spec` (raises
   :class:`ReportRegistryError` for unknown or metadata-only ids).
2. Opens a run via ``ReportRunRegistry.start_run()`` — which FAILS CLOSED for
   report 4359 (``schema_verified=False``). The flag is never flipped here;
   the error propagates and no browser is touched.
3. For email-first report ids (4247 manual renewals, 4246 audits / 4360
   Active daily feed, 4372 mortgagee, 4359 policy change) prefers today's
   robie@ CSV via :mod:`robie_job_engine.report_email_source`. 4372 requires
   the ``Mortgagee Verification Queue - ROBIE`` subject; 4246/4247/4359 keep
   ``ROBIE daily CSV`` + header fingerprint. Looker saved-report
   favorites are not the system of record. 4372 may fall back to Shared
   look 4601, and 4359 to look 4602, only when that email is missing —
   never when the subject is present but the CSV is stale or the wrong
   schema. Report 4359 still fail-closes in ``start_run()`` while
   ``schema_verified`` is False, before any email or Looker fetch.
4. Other reports still drive the Reports 5.0 Looker UI
   (https://app.ezlynx.com/web/looker-reports). Mapped reports open the
   Shared Looker look by look id (4372 → look 4601, 4359 → look 4602)
   instead of searching the hub for a saved-report link named or numbered
   with the report id.
5. Parses and validates the CSV: email path uses the Gmail header
   fingerprint; Looker path keys rows by the registry ``identity_fields``.
   Missing identity columns or missing identity values fail closed — rows
   are never guessed.

The Playwright driving below follows the proven ``ezlynx_reports_crawler.py``
pattern (CDP-attached persistent Chrome, ``expect_download`` export). DOM
selectors for the Looker 5.0 surface must be confirmed against the live UI on
the Test server (hermes-test-01); each step raises a descriptive error so a
selector mismatch fails loudly instead of returning wrong rows.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .ezlynx_reports_5 import REPORTS_5_BASE_URL
from .ezlynx_session import (
    EzlynxSessionPort,
    PlaywrightEzlynxSession,
    ensure_ezlynx_session,
)
from .gmail_report_ingestion import GmailReportMissingError, IngestedReport
from .report_email_source import (
    fetch_email_report_rows,
    uses_email_source,
)
from .report_registry import (
    LOOK_ID_BY_REPORT,
    ReportRegistryError,
    ReportRunRegistry,
    ReportSpec,
    get_report_spec,
)
from .store import utc_now


def _normalize_column_name(name: str) -> str:
    """Fold CSV / registry column names so ``Policy Number`` == ``policy_number``."""
    return "".join(ch for ch in str(name).casefold() if ch.isalnum())


def _column_lookup(columns: list[str]) -> dict[str, str]:
    """Map a normalized column name to the first matching original header."""
    lookup: dict[str, str] = {}
    for column in columns:
        key = _normalize_column_name(column)
        if key and key not in lookup:
            lookup[key] = column
    return lookup


def _resolve_column(name: str, lookup: dict[str, str]) -> str | None:
    return lookup.get(_normalize_column_name(name))


def _apply_header_aliases(report_id: str, lookup: dict[str, str]) -> None:
    """Register CSV short names for Looker headers that are actually present.

    A target is added only when its Looker source column is in the file.
    Existing columns are left in place. Missing sources are not invented.
    """
    for source, target in ALIASES.get(str(report_id), {}).items():
        actual = lookup.get(_normalize_column_name(source))
        if not actual:
            continue
        lookup.setdefault(_normalize_column_name(target), actual)


logger = logging.getLogger("robie.report_fetcher")

# Looker qualified header -> Gmail CSV short header.
# Report 4359 / Look 4602, verified 2026-09-18 (19 columns). The CSV export
# strips view-name prefixes, and one column is renamed rather than merely
# prefixed: "Policy Transaction Data Annualized Premium" is the CSV header
# "Premium - Annualized". Map only these. Do not add phantom columns
# (request_id, change_description, change_action, and the rest).
ALIASES: dict[str, dict[str, str]] = {
    "4359": {
        "Applicant Data Account Name": "Account Name",
        "Policy Transaction Data Applicant ID": "Applicant ID",
        "Policy Change Request Detail Policy Number": "Policy Number",
        "Policy Transaction Data Line Of Business": "Line Of Business",
        "Policy Change Request Detail Policy Effective Date": "Effective Date",
        "Policy Transaction Data Master Company": "Master Company",
        "Policy Change Request Detail Request Status": "Request Status",
        "Policy Change Request Detail Created By": "Created By",
        "Policy Transaction Data Written Premium": "Written Premium",
        "Policy Transaction Data Annualized Premium": "Premium - Annualized",
        "Applicant Data Branch": "Branch",
        "Policy Transaction Data Department": "Department",
        "Serviceteampolicychangerequest Service Team": "Service Team",
        "Applicant Data Assigned Producer": "Assigned Producer",
        "Applicant Data CSR": "CSR",
        "Applicant Data Preferred Language": "Preferred Language",
        "Applicant Labels Applicant Labels": "Applicant Labels",
        "Policy Labels Policy Labels": "Policy Labels",
        "Policy Change Request Detail Change Request Created Date": (
            "Change Request Created Date"
        ),
    },
}

REPORT_DOWNLOAD_DIR = Path(
    os.environ.get("ROBIE_EZLYNX_REPORTS_DIR", "/tmp/ezlynx_reports")
)
"""Transient CSV staging (same convention as ezlynx_reports_crawler). Rows are
what matter; they are persisted to the job DB by the workers."""


def _cdp_url() -> str:
    return (
        os.environ.get("ROBIE_PLAYWRIGHT_CDP_URL")
        or os.environ.get("ROBIE_BROWSER_CDP_URL")
        or "http://127.0.0.1:9222"
    ).strip()


def _resolve_db_path(db_path: str | None) -> str:
    resolved = db_path or os.environ.get("ROBIE_JOB_DB") or ""
    resolved = str(resolved).strip()
    if not resolved:
        raise ValueError(
            "fail closed: report fetcher requires an explicit db_path "
            "(or ROBIE_JOB_DB); the report run registry has no default"
        )
    return resolved


def look_id_for_report(report_id: str) -> str | None:
    """Return the Shared Looker look id for a registered report, if mapped."""
    report_id = str(report_id).strip()
    mapped = str(LOOK_ID_BY_REPORT.get(report_id) or "").strip()
    if mapped:
        return mapped
    try:
        spec = get_report_spec(report_id)
    except ReportRegistryError:
        return None
    look_id = str(spec.look_id or "").strip()
    return look_id or None


def looker_look_url(look_id: str) -> str:
    """Reports 5.0 URL for a Shared Looker look (Test-proven ``/report/{id}``)."""
    return f"{REPORTS_5_BASE_URL.rstrip('/')}/report/{str(look_id).strip()}"


def _unique(locator: Any, description: str) -> Any:
    """Require exactly one match for a read-only locator; fail closed otherwise."""
    count = locator.count()
    if count != 1:
        raise RuntimeError(
            f"expected exactly one {description}, found {count}; "
            "refusing to drive an ambiguous Looker UI"
        )
    return locator


def _look_title_locator(page: Any, title: str) -> Any:
    """Exact look-title locator. ``get_by_text`` when present; CSS text engine else."""
    getter = getattr(page, "get_by_text", None)
    if callable(getter):
        return getter(title, exact=True)
    return page.locator(f"text={title}")


def _require_unique_mapped_look(page: Any, *, spec: ReportSpec, look_id: str) -> None:
    """Fail closed unless look ``look_id`` / registered title is uniquely present."""
    title = str(spec.filter_name or "").strip()
    current_url = str(getattr(page, "url", "") or "")
    if look_id not in current_url:
        raise RuntimeError(
            f"expected exactly one look {look_id} for report {spec.report_id}, "
            f"found 0 (opened {current_url!r}); refusing to drive an ambiguous Looker UI"
        )
    if not title:
        return
    _unique(
        _look_title_locator(page, title),
        f"look {look_id} title {title!r}",
    )


def _page_of(browser: Any) -> Any:
    for attr in ("page", "_page"):
        page = getattr(browser, attr, None)
        if page is not None:
            return page
    if hasattr(browser, "goto"):
        return browser
    raise RuntimeError(
        "the EZLynx session object does not expose a Playwright page; "
        "pass a PlaywrightEzlynxSession (or a test double with .page)"
    )


def _parse_report_csv(
    csv_text: str, *, spec: ReportSpec, fields: list[str] | None
) -> list[dict[str, Any]]:
    """Parse exported CSV rows; key/dedupe by the registry identity fields.

    Fail closed: missing identity columns, missing requested fields, or a
    non-blank row without an identity value all raise — rows are never guessed.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    columns = list(reader.fieldnames or [])
    lookup = _column_lookup(columns)
    _apply_header_aliases(spec.report_id, lookup)
    identity_actual: list[str] = []
    missing_identity: list[str] = []
    for name in spec.identity_fields:
        actual = _resolve_column(name, lookup)
        if actual is None:
            missing_identity.append(name)
        else:
            identity_actual.append(actual)
    if missing_identity:
        raise RuntimeError(
            f"report {spec.report_id}: exported CSV is missing identity columns "
            f"{missing_identity} (columns seen: {columns}); refusing to return rows"
        )
    wanted = list(fields) if fields else list(columns)
    # Always expose registry identity names (snake_case) so workers can key
    # rows even when Looker/Gmail CSV uses "Policy Number".
    for name in spec.identity_fields:
        if name not in wanted:
            wanted.append(name)
    # Emit the CSV short name beside each Looker header that was present so
    # workers can read one shape from either export. Targets whose source
    # column is absent are not added.
    for source, target in ALIASES.get(spec.report_id, {}).items():
        if lookup.get(_normalize_column_name(source)) and target not in wanted:
            wanted.append(target)
    missing_fields = [
        name for name in wanted if _resolve_column(name, lookup) is None
    ]
    if missing_fields:
        raise RuntimeError(
            f"report {spec.report_id}: exported CSV is missing requested fields "
            f"{missing_fields} (columns seen: {columns}); refusing to return rows"
        )
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for line_no, raw in enumerate(reader, start=2):
        if raw is None:
            continue
        row = {key: (value.strip() if isinstance(value, str) else value) for key, value in raw.items()}
        if not any(str(value or "").strip() for value in row.values()):
            continue  # skip fully blank lines
        identity = tuple(
            str(row.get(actual) or "").strip() for actual in identity_actual
        )
        if not all(identity):
            raise RuntimeError(
                f"report {spec.report_id}: row {line_no} is missing an identity value "
                f"for {spec.identity_fields}; refusing to return unkeyed rows"
            )
        if identity in seen:
            continue  # dedupe on the registry identity key
        seen.add(identity)
        emitted: dict[str, Any] = {}
        for name in wanted:
            actual = _resolve_column(name, lookup) or name
            emitted[name] = row.get(actual)
        rows.append(emitted)
    return rows


def _export_looker_report_csv(
    page: Any,
    *,
    spec: ReportSpec,
    run: dict[str, Any],
    download_dir: Path,
    fields: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Drive the Reports 5.0 Looker UI and export the report as CSV.

    Raises RuntimeError (fail closed) on any navigation, filter, or export
    failure. Never returns guessed rows.
    """
    report_id = spec.report_id
    look_id = look_id_for_report(report_id)
    if look_id:
        # Shared Looker look: open by look id. Do not search the hub for a
        # saved-report link named/numbered with the EZLynx report id (4372
        # is look 4601; SSRobie Saved Reports has zero href*=4372 links).
        look_url = looker_look_url(look_id)
        try:
            page.goto(look_url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_load_state("domcontentloaded", timeout=30_000)
        except Exception as exc:
            raise RuntimeError(
                f"report {report_id}: failed to open Looker look {look_id} at "
                f"{look_url}: {type(exc).__name__}: {exc}"
            ) from exc
        _require_unique_mapped_look(page, spec=spec, look_id=look_id)
    else:
        try:
            page.goto(REPORTS_5_BASE_URL, wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:
            raise RuntimeError(
                f"report {report_id}: failed to open Reports 5.0 hub "
                f"{REPORTS_5_BASE_URL}: {type(exc).__name__}: {exc}"
            ) from exc

        # Open the saved report by id. DOM refinement: verify selectors on Test.
        entry = _unique(
            page.locator(f"a[href*='{report_id}'], [data-report-id='{report_id}']"),
            f"saved-report link for {report_id}",
        )
        try:
            entry.click()
            page.wait_for_load_state("domcontentloaded", timeout=30_000)
        except Exception as exc:
            raise RuntimeError(
                f"report {report_id}: failed to open the saved report: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    result_rows = page.locator("table tbody tr, [role='row']")
    try:
        result_rows.first.wait_for(timeout=30_000)
    except Exception as exc:
        raise RuntimeError(
            f"report {report_id}: no result rows rendered after opening the "
            f"saved report: {type(exc).__name__}: {exc}"
        ) from exc

    # Require the registered scope marker on the page (saved Custom Filter
    # Set, or the 4372 look-4601 title). Do not return unfiltered rows.
    if spec.filter_name:
        try:
            body_text = page.locator("body").inner_text(timeout=10_000)
        except Exception as exc:
            raise RuntimeError(
                f"report {report_id}: could not read page text to confirm saved "
                f"filter {spec.filter_name!r}: {type(exc).__name__}: {exc}"
            ) from exc
        if spec.filter_name.casefold() not in body_text.casefold():
            raise RuntimeError(
                f"report {report_id}: saved filter {spec.filter_name!r} is not "
                "visible on the report page; refusing to return unfiltered rows"
            )

    export_btn = _unique(
        page.locator(
            "button:has-text('Export'), a:has-text('Export'), [aria-label*='Export']"
        ),
        "Export control",
    )
    try:
        export_btn.click()
        # Valid Playwright selector list: text-engine locators for the visible
        # "CSV" option plus CSS attribute fallbacks. (A bare ``text=CSV`` mixed
        # with CSS in one string is not valid; keep each alternative well-formed.)
        csv_option = _unique(
            page.locator(
                "button:has-text('CSV'), a:has-text('CSV'), "
                "[role='menuitem']:has-text('CSV'), "
                "[data-format='csv'], [data-export-format='csv']"
            ),
            "CSV export option",
        )
        download_dir.mkdir(parents=True, exist_ok=True)
        dest = download_dir / f"robie-report-{report_id}-{run['run_id']}.csv"
        with page.expect_download(timeout=60_000) as download_info:
            csv_option.click()
        download = download_info.value
        download.save_as(str(dest))
    except Exception as exc:
        raise RuntimeError(
            f"report {report_id}: CSV export failed: {type(exc).__name__}: {exc}"
        ) from exc

    try:
        csv_text = dest.read_text(encoding="utf-8-sig")
    except Exception as exc:
        raise RuntimeError(
            f"report {report_id}: could not read exported CSV {dest}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    # Parse with the CALLER's requested fields, not start_run()'s resolved
    # fields: when fields=None, start_run() resolves to the registry identity
    # fields only, which would silently drop every other exported column.
    rows = _parse_report_csv(csv_text, spec=spec, fields=fields)
    logger.info("report %s: exported %d rows (run %s)", report_id, len(rows), run["run_id"])
    return rows


def _looker_fetch_report_rows(
    *,
    spec: ReportSpec,
    run: dict[str, Any],
    fields: list[str] | None,
    session: EzlynxSessionPort | Any | None,
) -> list[dict[str, Any]]:
    """Drive Reports 5.0 / a mapped look and export CSV rows."""
    browser = session
    close_browser = False
    if browser is None:
        browser = PlaywrightEzlynxSession(_cdp_url())
        close_browser = True
    try:
        # Fail closed on any auth problem (login required, MFA, unverified shell).
        ensure_ezlynx_session(browser)
        page = _page_of(browser)
        return _export_looker_report_csv(
            page, spec=spec, run=run, download_dir=REPORT_DOWNLOAD_DIR, fields=fields
        )
    finally:
        if close_browser:
            try:
                browser.close()
            except Exception:
                pass


def fetch_report_rows(
    *,
    report_id: str,
    fields: list[str] | None = None,
    filters: dict[str, Any] | None = None,
    db_path: str | None = None,
    session: EzlynxSessionPort | Any | None = None,
    csv_bytes: bytes | None = None,
    gmail_service: Any | None = None,
    ingested: IngestedReport | None = None,
    day: date | None = None,
    now: datetime | None = None,
    source: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch work-queue rows for a registered report.

    Email-first ids (4246, 4247, 4372, 4359) ingest today's robie@ CSV and do not
    open Looker favorites unless the email is missing and a look id is mapped
    (4372 → 4601, 4359 → 4602). ``session`` is unused on the email path. Optional
    ``csv_bytes`` / ``gmail_service`` / ``ingested`` inject the email source
    for tests. ``source="looker"`` forces the Reports 5.0 path;
    ``source="email"`` refuses Looker fallback. Report 4359 still raises from
    ``start_run()`` while ``schema_verified`` is False, before either source.

    ``session`` may be an :class:`EzlynxSessionPort` (or anything exposing
    ``.page``); when omitted on the Looker path, a
    :class:`PlaywrightEzlynxSession` is attached to the persistent Hermes
    Chrome over CDP. Fails closed (raises, never returns guessed rows) on
    unknown reports, unverified schemas (4359), missing/stale/wrong-schema
    email CSVs, auth failure, or missing columns.
    """
    spec = get_report_spec(report_id)  # raises ReportRegistryError for unknown ids
    # Runtime filters are fingerprinted by start_run() but are NOT applied in
    # the email schedule or the Looker UI. Silently ignoring them would return
    # wrong-scope rows, so fail closed instead of pretending to filter.
    if filters:
        raise ValueError(
            f"report {spec.report_id}: runtime filters are not supported "
            "(email schedule / saved-report scope is authoritative); "
            "refusing to fetch rather than silently ignoring filters"
        )
    db = _resolve_db_path(db_path)
    run_id = f"report-run:{spec.report_id}:{uuid.uuid4().hex[:12]}:{utc_now()}"
    # start_run() FAILS CLOSED for schema_verified=False (report 4359).
    # Do NOT flip that flag; let the error propagate before any browser work.
    run = ReportRunRegistry(db).start_run(
        run_id=run_id,
        report_id=spec.report_id,
        filters=filters,
        fields=fields,
    )

    source_norm = str(source or "").strip().casefold() or None
    inject_email = (
        csv_bytes is not None or gmail_service is not None or ingested is not None
    )
    prefer_email = source_norm == "email" or inject_email or (
        source_norm is None and uses_email_source(spec.report_id)
    )
    if prefer_email:
        try:
            rows = fetch_email_report_rows(
                report_id=spec.report_id,
                fields=fields,
                csv_bytes=csv_bytes,
                gmail_service=gmail_service,
                ingested=ingested,
                day=day,
                now=now,
            )
            logger.info(
                "report %s: email CSV source returned %d row(s) (run %s)",
                spec.report_id,
                len(rows),
                run["run_id"],
            )
            return rows
        except GmailReportMissingError:
            # Email absent: mapped looks may fall back (4372 → 4601, 4359 → 4602).
            # 4246/4247 have no look map — the 0 saved-report-link miss is not a fallback.
            can_fallback = (
                source_norm != "email"
                and not inject_email
                and look_id_for_report(spec.report_id)
            )
            if not can_fallback:
                raise
            logger.warning(
                "report %s: morning email CSV missing; falling back to Looker look %s",
                spec.report_id,
                look_id_for_report(spec.report_id),
            )

    return _looker_fetch_report_rows(
        spec=spec, run=run, fields=fields, session=session
    )
