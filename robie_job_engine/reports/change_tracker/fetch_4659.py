"""Looker report 4659 export driver (Policy Change Request Summary).

Follows the proven ``robie_job_engine.report_fetcher`` CDP pattern:
CDP-attached persistent Chrome + ``expect_download`` CSV export, fail closed
on every navigation/filter/export mismatch.

The 4659 flow is NOT the generic Export-button flow — per the spec it is:
filter bar -> Change Request Created Date = last 1 year -> Reload -> read the
"Total Open Change Requests" tile -> click the tile -> detail modal ->
Advanced data options -> "All results" -> Download CSV.

STATUS: DOM selectors below are UNVERIFIED — they must be confirmed against
the live 4659 UI on a box with an EZLynx session before any live run. Each
step raises a descriptive error so a selector mismatch fails loudly. The
smoke test and --dry-run never touch this path (fixture CSV instead).
"""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path
from typing import Any

from ...ezlynx_session import EzlynxSessionPort, PlaywrightEzlynxSession, ensure_ezlynx_session
from ...report_registry import ReportRunRegistry, get_report_spec
from ...store import utc_now
from . import config as cfg

logger = logging.getLogger("robie.change_tracker.fetch_4659")

REPORT_DOWNLOAD_DIR = Path(
    os.environ.get("ROBIE_EZLYNX_REPORTS_DIR", "/tmp/ezlynx_reports")
)


def _cdp_url() -> str:
    return (
        os.environ.get("ROBIE_PLAYWRIGHT_CDP_URL")
        or os.environ.get("ROBIE_BROWSER_CDP_URL")
        or "http://127.0.0.1:9222"
    ).strip()


def _unique(locator: Any, description: str) -> Any:
    count = locator.count()
    if count != 1:
        raise RuntimeError(
            f"expected exactly one {description}, found {count}; "
            "refusing to drive an ambiguous Looker UI"
        )
    return locator


def _export_4659_csv(
    page: Any, *, download_dir: Path, run_id: str
) -> tuple[str, str, str]:
    """Drive the 4659 page and export the open-request detail table.

    Returns (csv_text, total_requests_tile, total_open_tile).
    Raises RuntimeError (fail closed) on any step mismatch.
    """
    report_id = "4659"
    spec = get_report_spec(report_id)

    try:
        page.goto(cfg.LOOKER_4659_URL, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_load_state("domcontentloaded", timeout=30_000)
    except Exception as exc:
        raise RuntimeError(
            f"report {report_id}: failed to open {cfg.LOOKER_4659_URL}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    # Scope marker: the look title must be present (UNVERIFIED selector).
    title = _unique(
        page.get_by_text(cfg.LOOKER_4659_TITLE, exact=True),
        f"look title {cfg.LOOKER_4659_TITLE!r}",
    )
    title.wait_for(timeout=30_000)

    # 1-year filter on Change Request Created Date (P2; fail closed if the
    # chip does not read back as the 1-year filter).
    filter_chip = page.locator(
        "[data-filter-field*='Created Date'], [aria-label*='Change Request Created Date']"
    ).first
    try:
        filter_chip.click(timeout=15_000)
    except Exception as exc:
        raise RuntimeError(
            f"report {report_id}: could not open the Change Request Created Date "
            f"filter chip: {type(exc).__name__}: {exc}"
        ) from exc
    # Best-effort: pick the "1 year" option; the exact controls are DOM-UNVERIFIED.
    try:
        page.get_by_text("is in the last 1 year", exact=False).first.click(timeout=15_000)
    except Exception as exc:
        raise RuntimeError(
            f"report {report_id}: could not set the 1-year filter option: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    # Reload to apply the filter (the circular-arrow control by the "X ago" stamp).
    reload_btn = _unique(
        page.locator("button[aria-label*='Reload'], button[title*='Reload']"),
        "Reload control",
    )
    try:
        reload_btn.click()
        page.wait_for_load_state("domcontentloaded", timeout=30_000)
    except Exception as exc:
        raise RuntimeError(
            f"report {report_id}: Reload after the 1-year filter failed: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    # Verify the filter chip now reads as the 1-year filter.
    try:
        filter_text = page.locator("body").inner_text(timeout=10_000)
    except Exception as exc:
        raise RuntimeError(
            f"report {report_id}: could not read page text to verify the filter: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if "last 1 year" not in filter_text and "last year" not in filter_text.casefold():
        raise RuntimeError(
            f"report {report_id}: the 1-year filter is not visible after Reload; "
            "refusing to export a 30-day-default (silent under-report) slice"
        )

    # Read the two summary tiles.
    def _read_tile(label: str) -> str:
        tile = page.locator(f"text={label}").first
        try:
            tile.wait_for(timeout=30_000)
        except Exception as exc:
            raise RuntimeError(
                f"report {report_id}: summary tile {label!r} not found: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        # The tile's value sits near its label; take the nearest number.
        container = tile.locator("xpath=ancestor::*[contains(@class,'tile') or contains(@class,'Tile')][1]")
        text = ""
        try:
            text = container.inner_text(timeout=10_000)
        except Exception:
            text = tile.inner_text()
        import re
        numbers = re.findall(r"[\d,]+", text or "")
        if not numbers:
            raise RuntimeError(
                f"report {report_id}: tile {label!r} has no numeric value in {text!r}"
            )
        return numbers[0].replace(",", "")

    total_requests = _read_tile(cfg.TILE_TOTAL_REQUESTS)
    total_open = _read_tile(cfg.TILE_TOTAL_OPEN)

    # Click the "Total Open Change Requests" tile -> detail modal.
    try:
        page.get_by_text(cfg.TILE_TOTAL_OPEN, exact=True).click(timeout=15_000)
        modal = page.locator("[role='dialog'], .modal").first
        modal.wait_for(timeout=30_000)
    except Exception as exc:
        raise RuntimeError(
            f"report {report_id}: could not open the open-requests detail table: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    # Advanced data options -> "All results" -> Download.
    try:
        page.get_by_text("Advanced data options", exact=False).first.click(timeout=15_000)
        rows_option = page.locator(
            "select[name*='rows'], [data-testid*='rows'], button:has-text('All results')"
        ).first
        rows_option.click(timeout=15_000)
        all_results = page.get_by_text("All results", exact=True).first
        if all_results.count():
            all_results.click(timeout=15_000)
    except Exception as exc:
        raise RuntimeError(
            f"report {report_id}: could not set 'All results' in Advanced data "
            f"options (partial export risk): {type(exc).__name__}: {exc}"
        ) from exc

    download_dir.mkdir(parents=True, exist_ok=True)
    dest = download_dir / f"robie-report-{report_id}-{run_id}.csv"
    try:
        download_btn = _unique(
            page.locator("button:has-text('Download'), a:has-text('Download')"),
            "Download control",
        )
        with page.expect_download(timeout=60_000) as download_info:
            download_btn.click()
        download = download_info.value
        download.save_as(str(dest))
    except Exception as exc:
        raise RuntimeError(
            f"report {report_id}: CSV download failed: {type(exc).__name__}: {exc}"
        ) from exc

    try:
        csv_text = dest.read_text(encoding="utf-8-sig")
    except Exception as exc:
        raise RuntimeError(
            f"report {report_id}: could not read exported CSV {dest}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    logger.info(
        "report %s: exported CSV (%d chars), tiles total=%s open=%s (run %s)",
        report_id, len(csv_text), total_requests, total_open, run_id,
    )
    return csv_text, total_requests, total_open


def fetch_4659_export(
    *,
    db_path: str,
    session: EzlynxSessionPort | Any | None = None,
) -> tuple[str, str, str]:
    """Fetch the 4659 open-change-request export via the Looker UI.

    Returns (csv_text, total_requests_tile, total_open_tile). Registers the
    run in the report registry (fails closed for unknown reports). The caller
    validates the CSV against the schema fingerprint before any sheet write.
    """
    spec = get_report_spec("4659")  # raises ReportRegistryError for unknown ids
    run_id = f"report-run:4659:{uuid.uuid4().hex[:12]}:{utc_now()}"
    ReportRunRegistry(db_path).start_run(run_id=run_id, report_id="4659")

    browser = session
    close_browser = False
    if browser is None:
        browser = PlaywrightEzlynxSession(_cdp_url())
        close_browser = True
    try:
        ensure_ezlynx_session(browser)
        page = getattr(browser, "page", None) or getattr(browser, "_page", None)
        if page is None and hasattr(browser, "goto"):
            page = browser
        if page is None:
            raise RuntimeError(
                "the EZLynx session object does not expose a Playwright page"
            )
        return _export_4659_csv(page, download_dir=REPORT_DOWNLOAD_DIR, run_id=run_id)
    finally:
        if close_browser:
            try:
                browser.close()
            except Exception:
                pass
