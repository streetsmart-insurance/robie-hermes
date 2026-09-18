"""Fetch work-queue rows for the registered EZLynx Reports 5.0 reports.

``fetch_report_rows(*, report_id, fields=None, filters=None, db_path=None,
session=None) -> list[dict]`` is the stable entry point the four verification
workers import. It:

1. Resolves the report via :func:`report_registry.get_report_spec` (raises
   :class:`ReportRegistryError` for unknown or metadata-only ids).
2. Opens a run via ``ReportRunRegistry.start_run()`` — which FAILS CLOSED for
   report 4359 (``schema_verified=False``). The flag is never flipped here;
   the error propagates and no browser is touched.
3. Ensures an authenticated EZLynx session (:func:`ensure_ezlynx_session`;
   fail closed on auth failure), drives the Reports 5.0 Looker UI
   (https://app.ezlynx.com/web/looker-reports), applies the report's saved
   filter when one is registered, and exports the rows as CSV.
4. Parses and validates the CSV: every row is keyed by the registry's
   ``identity_fields`` (dedupe key); missing identity columns or missing
   identity values fail closed — rows are never guessed.

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
from pathlib import Path
from typing import Any

from .ezlynx_reports_5 import REPORTS_5_BASE_URL
from .ezlynx_session import (
    EzlynxSessionPort,
    PlaywrightEzlynxSession,
    ensure_ezlynx_session,
)
from .report_registry import (
    ReportRunRegistry,
    ReportSpec,
    get_report_spec,
)
from .store import utc_now

logger = logging.getLogger("robie.report_fetcher")


def _slug_header(value: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").casefold()).strip("_")


def _looker_frame(page: Any):
    for frame in page.frames:
        url = str(getattr(frame, "url", "") or "")
        if "looker.ezlynx.com/embed/looks" in url:
            return frame
    return None


def _scrape_looker_ag_grid_csv(page: Any, *, report_id: str) -> str:
    """Scrape Looker embed ag-grid into CSV text (Export UI is unreliable)."""
    import csv
    import io
    import time

    looker = None
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline and looker is None:
        looker = _looker_frame(page)
        if looker is None:
            page.wait_for_timeout(500)
    if looker is None:
        raise RuntimeError(
            f"report {report_id}: Looker embed iframe did not appear"
        )
    rows_loc = looker.locator(".ag-center-cols-container .ag-row")
    try:
        rows_loc.first.wait_for(timeout=45_000)
    except Exception as exc:
        raise RuntimeError(
            f"report {report_id}: no Looker ag-grid rows: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    # Allow virtualized rows to settle
    page.wait_for_timeout(2_000)
    headers = looker.eval_on_selector_all(
        ".ag-header-cell-text, [role=columnheader]",
        "els => els.map(e => (e.innerText||'').replace(/\\s+/g,' ').trim()).filter(Boolean)",
    )
    if not headers:
        raise RuntimeError(f"report {report_id}: Looker grid has no headers")
    raw_rows = looker.eval_on_selector_all(
        ".ag-center-cols-container .ag-row",
        """els => els.map(r => [...r.querySelectorAll('.ag-cell')].map(c => {
          const a = c.querySelector('a.cell-clickable-content');
          const t = ((a && a.innerText) || c.innerText || '').replace(/\\s+/g,' ').trim();
          return t.replace(/\\.+$/, m => m === '...' ? '' : m).replace(/\\.\\.\\.$/,'').trim();
        }))""",
    )
    # Prefer anchor text without trailing ellipsis bauble
    cleaned = []
    for row in raw_rows:
        cleaned.append([
            (cell[:-3].rstrip() if isinstance(cell, str) and cell.endswith("...") else cell)
            for cell in row
        ])
    if not cleaned:
        raise RuntimeError(f"report {report_id}: Looker grid scrape returned 0 rows")
    slug_headers = [_slug_header(h) for h in headers]
    # Alias Looker display headers onto worker-expected field names.
    # Values may be a string or an ordered list of source fallbacks.
    ALIASES: dict[str, str | list[str]] = {
        "insured_name": ["account_name", "account_name_ascending"],
        "carrier_name": "master_company",
        "carrier": "master_company",
        "expiration_date": "policy_expiration_date",
        "source": "policy_source",
        "expiring_premium": "premium_annualized",
        "assigned_agent": "assigned_producer",
    }
    # Shared Audit look 4604 has no audit_id column; worker already falls back
    # to policy_number for durable keys, but registry identity requires the
    # column to exist in the CSV.
    if str(report_id) == "4246":
        ALIASES["audit_id"] = "policy_number"
        ALIASES["insured_name"] = ["account_name_ascending", "account_name"]
    # Always emit these keys even when Looker has no matching column.
    EXTRA_EMPTY = ("underwriter_name", "underwriter_email", "policy_aliases")
    out_headers = list(slug_headers)
    for alias, src in ALIASES.items():
        if alias not in out_headers:
            out_headers.append(alias)
    for name in EXTRA_EMPTY:
        if name not in out_headers:
            out_headers.append(name)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(out_headers)
    width = len(slug_headers)
    for row in cleaned:
        cells = list(row)[:width]
        while len(cells) < width:
            cells.append("")
        if not any(str(c).strip() for c in cells):
            continue
        by_slug = {slug_headers[i]: cells[i] for i in range(len(slug_headers))}
        out_row = []
        for header in out_headers:
            if header in by_slug:
                out_row.append(by_slug[header])
            elif header in ALIASES:
                src = ALIASES[header]
                if isinstance(src, list):
                    val = ""
                    for key in src:
                        val = by_slug.get(key, "")
                        if str(val).strip():
                            break
                    out_row.append(val)
                else:
                    out_row.append(by_slug.get(src, ""))
            else:
                out_row.append("")
        writer.writerow(out_row)
    return buf.getvalue()



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


def _unique(locator: Any, description: str) -> Any:
    """Require exactly one match for a read-only locator; fail closed otherwise."""
    count = locator.count()
    if count != 1:
        raise RuntimeError(
            f"expected exactly one {description}, found {count}; "
            "refusing to drive an ambiguous Looker UI"
        )
    return locator


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
    missing_identity = [name for name in spec.identity_fields if name not in columns]
    if missing_identity:
        raise RuntimeError(
            f"report {spec.report_id}: exported CSV is missing identity columns "
            f"{missing_identity} (columns seen: {columns}); refusing to return rows"
        )
    wanted = list(fields) if fields else columns
    missing_fields = [name for name in wanted if name not in columns]
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
        identity = tuple(str(row.get(name) or "").strip() for name in spec.identity_fields)
        if not all(identity):
            raise RuntimeError(
                f"report {spec.report_id}: row {line_no} is missing an identity value "
                f"for {spec.identity_fields}; refusing to return unkeyed rows"
            )
        if identity in seen:
            continue  # dedupe on the registry identity key
        seen.add(identity)
        rows.append({name: row.get(name) for name in wanted})
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
    try:
        page.goto(REPORTS_5_BASE_URL, wait_until="domcontentloaded", timeout=60_000)
    except Exception as exc:
        raise RuntimeError(
            f"report {report_id}: failed to open Reports 5.0 hub "
            f"{REPORTS_5_BASE_URL}: {type(exc).__name__}: {exc}"
        ) from exc

    # Open the saved report. Prefer known Looker look ids (Shared Reports),
    # because SSRobie Saved Reports often has zero a[href*=report_id] links.
    LOOK_ID_BY_REPORT = {
        "4247": "4603",  # Manual Renewal Queue - ROBIE
        "4246": "4604",  # Audit Verification Queue - ROBIE
        "4359": "4602",  # Policy Change Request Confirmation Queue - ROBIE
        "4372": "4601",  # Mortgagee Verification Queue - ROBIE
    }
    look_id = LOOK_ID_BY_REPORT.get(str(report_id))
    if look_id:
        look_url = f"{REPORTS_5_BASE_URL.rstrip('/')}/report/{look_id}"
        try:
            page.goto(look_url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_load_state("domcontentloaded", timeout=30_000)
        except Exception as exc:
            raise RuntimeError(
                f"report {report_id}: failed to open Looker look {look_id} at "
                f"{look_url}: {type(exc).__name__}: {exc}"
            ) from exc
    else:
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

    if look_id:
        # Shared Reports Looker embed: scrape ag-grid (Export/Download is flaky).
        try:
            csv_text = _scrape_looker_ag_grid_csv(page, report_id=report_id)
        except Exception as exc:
            raise RuntimeError(
                f"report {report_id}: Looker scrape failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if spec.filter_name:
            try:
                looker = _looker_frame(page)
                body_text = (looker or page).locator("body").inner_text(timeout=10_000)
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
        download_dir.mkdir(parents=True, exist_ok=True)
        dest = download_dir / f"robie-report-{report_id}-{run['run_id']}.csv"
        dest.write_text(csv_text, encoding="utf-8")
    else:
        result_rows = page.locator("table tbody tr, [role='row']")
        try:
            result_rows.first.wait_for(timeout=30_000)
        except Exception as exc:
            raise RuntimeError(
                f"report {report_id}: no result rows rendered after opening the "
                f"saved report: {type(exc).__name__}: {exc}"
            ) from exc

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


def fetch_report_rows(
    *,
    report_id: str,
    fields: list[str] | None = None,
    filters: dict[str, Any] | None = None,
    db_path: str | None = None,
    session: EzlynxSessionPort | Any | None = None,
) -> list[dict[str, Any]]:
    """Fetch work-queue rows for a registered Reports 5.0 report.

    ``session`` may be an :class:`EzlynxSessionPort` (or anything exposing
    ``.page``); when omitted, a :class:`PlaywrightEzlynxSession` is attached to
    the persistent Hermes Chrome over CDP. Fails closed (raises, never returns
    guessed rows) on unknown reports, unverified schemas (4359), auth failure,
    or missing columns.
    """
    spec = get_report_spec(report_id)  # raises ReportRegistryError for unknown ids
    # Runtime filters are fingerprinted by start_run() but are NOT applied in
    # the Looker UI (the saved report's own filter scope is authoritative).
    # Silently ignoring caller-supplied filters would return wrong-scope rows,
    # so fail closed instead of pretending to filter.
    if filters:
        raise ValueError(
            f"report {spec.report_id}: runtime filters are not supported by the "
            "Looker CSV export path (saved-report scope is authoritative); "
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
