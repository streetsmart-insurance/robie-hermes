"""Read-only Playwright runner for the fixed EZLynx Submission Center scope."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime
import json
import re
import sys
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from playwright.sync_api import (
    Error as PlaywrightError,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from .ezlynx_auth_evidence import authenticated_app_evidence
from .store import report_current_job_perform_progress
from .submission_center_controls import (
    activate_mdc_checkbox,
    activate_mdc_combobox,
    activate_sort_header,
)


CDP_URL = "http://127.0.0.1:9222"
SUBMISSION_URL = "https://app.ezlynx.com/web/submission-center/overview/submissions"
CLOSED = {"Closed - Not Sold", "Closed - Bound"}
RED_OVERDUE_COLOR = "rgb(211, 47, 47)"
DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d")
REQUIRED_HEADERS = {
    "applicant": "Applicant",
    "assigned_producer": "Assigned Producer",
    "status": "Status",
    "quote_due_date": "Quote Due Date",
    "effective_date": "Effective Date",
}


def _report_pagination_progress(
    *,
    pages_reviewed: int,
    rows_inspected: int,
    extra: dict[str, Any] | None = None,
) -> None:
    """Tell Job Engine the Submission Center walk is still advancing."""
    payload = {
        "source": "submission_audit_runner",
        "pages_reviewed": pages_reviewed,
        "rows_inspected": rows_inspected,
    }
    if extra:
        payload.update(extra)
    report_current_job_perform_progress(payload, source="submission_audit_runner")


def _matching_page(browser) -> Page:
    pages = [page for context in browser.contexts for page in context.pages]
    for page in pages:
        if page.url.startswith(SUBMISSION_URL):
            return page
    if not browser.contexts:
        raise RuntimeError("PLAYWRIGHT_BLOCKED: Chrome has no browser context")
    page = browser.contexts[0].new_page()
    page.goto(SUBMISSION_URL, wait_until="domcontentloaded")
    return page


def _authenticated(page: Page) -> bool:
    return authenticated_app_evidence(page)


def _first_visible(locator: Locator, label: str) -> Locator:
    for index in range(locator.count()):
        candidate = locator.nth(index)
        if candidate.is_visible():
            return candidate
    raise RuntimeError(f"PLAYWRIGHT_BLOCKED: visible {label} not found")


def _activate(locator: Locator, label: str, *, key: str = "Enter") -> None:
    target = _first_visible(locator, label)
    target.scroll_into_view_if_needed()
    try:
        target.press(key, timeout=5_000)
    except PlaywrightError:
        try:
            target.click(timeout=5_000)
        except PlaywrightError as exc:
            raise RuntimeError(
                f"PLAYWRIGHT_BLOCKED: visible {label} could not be activated"
            ) from exc


def _click_control(page: Page, label: str) -> None:
    candidates = (
        page.get_by_role("combobox", name=re.compile(label, re.I)),
        page.locator("mat-form-field").filter(has_text=re.compile(label, re.I)).locator("mat-select"),
        page.locator("mat-select").filter(has_text=re.compile(label, re.I)),
    )
    for candidate in candidates:
        if any(candidate.nth(index).is_visible() for index in range(candidate.count())):
            _activate(candidate, label)
            return
    raise RuntimeError(f"PLAYWRIGHT_BLOCKED: {label} control not found")


def _select_single(page: Page, label: str, option: str) -> None:
    if page.get_by_text(option, exact=True).count() and label.casefold() in page.locator("body").inner_text().casefold():
        field = page.locator("mat-form-field").filter(has_text=re.compile(label, re.I))
        visible_fields = [
            field.nth(index) for index in range(field.count()) if field.nth(index).is_visible()
        ]
        if visible_fields and option.casefold() in visible_fields[0].inner_text().casefold():
            return
    _click_control(page, label)
    choice = page.get_by_role("option", name=re.compile(rf"^{re.escape(option)}$", re.I))
    if not choice.count():
        choice = page.locator("mat-option").filter(has_text=re.compile(rf"^{re.escape(option)}$", re.I))
    if not choice.count():
        raise RuntimeError(f"PLAYWRIGHT_BLOCKED: {option} option not found")
    _activate(choice, f"{option} option")
    page.wait_for_timeout(750)


def _option_selected(option: Locator) -> bool:
    return (
        option.get_attribute("aria-selected") == "true"
        or "mat-selected" in (option.get_attribute("class") or "")
        or option.locator("input:checked").count() > 0
    )


def _normalize_option_label(value: str) -> str:
    return " ".join(value.split()).casefold()


def _exact_visible_option(options: Locator, expected: str) -> Locator | None:
    normalized_expected = _normalize_option_label(expected)
    for index in range(options.count()):
        option = options.nth(index)
        if (
            option.is_visible()
            and _normalize_option_label(option.inner_text()) == normalized_expected
        ):
            return option
    return None


def _live_agency_options(page: Page) -> Locator:
    for _ in range(10):
        for selector in (".cdk-overlay-container mat-checkbox", "mat-checkbox"):
            options = page.locator(selector)
            if (
                _exact_visible_option(options, "My Submissions") is not None
                and _exact_visible_option(options, "Streetsmart Insurance") is not None
            ):
                return options
        page.wait_for_timeout(500)
    raise RuntimeError("PLAYWRIGHT_BLOCKED: visible agency options not found")


def _set_agency_scope(page: Page) -> None:
    field = page.locator("mat-form-field").filter(
        has_text=re.compile("Submissions by assigned producer", re.I)
    )
    if not field.count():
        raise RuntimeError("PLAYWRIGHT_BLOCKED: agency field not found")
    visible_field = _first_visible(field, "agency field")
    picker_button = visible_field.locator("button")
    live_checkbox_picker = picker_button.count() > 0
    if live_checkbox_picker:
        _activate(picker_button, "agency picker")
        options = _live_agency_options(page)
    else:
        _click_control(page, "Submissions by assigned producer")
        options = page.locator("mat-option, [role=option]")
    mine = _exact_visible_option(options, "My Submissions")
    agency = _exact_visible_option(options, "Streetsmart Insurance")
    if mine is None or agency is None:
        raise RuntimeError("PLAYWRIGHT_BLOCKED: agency options not found")
    if _option_selected(mine):
        # Live mat-mdc-checkbox ignores Space/Enter. Force-click the nested input.
        activate_mdc_checkbox(mine, "My Submissions checkbox")
    if not _option_selected(agency):
        activate_mdc_checkbox(agency, "Streetsmart Insurance checkbox")
    apply_button = page.get_by_role(
        "button", name=re.compile(r"^(Apply|Done|Select)$", re.I)
    )
    if apply_button.count():
        _activate(apply_button, "agency picker apply button")
    else:
        page.keyboard.press("Escape")
    page.wait_for_timeout(1_000)
    if live_checkbox_picker:
        # The live MDC picker collapses to an icon and does not render the
        # chosen producer names in the form-field text. Reopen it and reread
        # the actual checkbox state instead of trusting the prior clicks.
        _activate(picker_button, "agency picker")
        live_options = _live_agency_options(page)
        live_mine = _exact_visible_option(live_options, "My Submissions")
        live_agency = _exact_visible_option(live_options, "Streetsmart Insurance")
        if (
            live_mine is None
            or live_agency is None
            or _option_selected(live_mine)
            or not _option_selected(live_agency)
        ):
            raise RuntimeError("PLAYWRIGHT_BLOCKED: agency scope did not apply")
        cancel = page.get_by_role("button", name=re.compile(r"^Cancel$", re.I))
        if cancel.count():
            _activate(cancel, "agency picker cancel button")
        else:
            page.keyboard.press("Escape")
    else:
        field_text = visible_field.inner_text().casefold()
        if "streetsmart insurance" not in field_text:
            raise RuntimeError("PLAYWRIGHT_BLOCKED: agency scope did not apply")
        if "my submissions" in field_text:
            raise RuntimeError("PLAYWRIGHT_BLOCKED: My Submissions remained selected")


def _set_page_size(page: Page) -> None:
    paginator = page.locator("mat-paginator")
    if not paginator.count():
        raise RuntimeError("PLAYWRIGHT_BLOCKED: paginator not found")
    selector = paginator.locator("mat-select")
    if not selector.count():
        selector = paginator.get_by_role("combobox")
    if not selector.count():
        raise RuntimeError("PLAYWRIGHT_BLOCKED: page-size control not found")
    selected_value = selector.first.locator(
        ".mat-mdc-select-value-text, .mat-select-value-text"
    )
    if selected_value.count() and selected_value.first.inner_text().strip() == "100":
        return
    # Live MDC paginator: a touch-target layer intercepts pointer clicks and
    # Enter is a no-op. Force-click the combobox and option 100, then the
    # caller verifies rendered row count fail-closed.
    activate_mdc_combobox(selector.first, "page-size control")
    option = page.get_by_role("option", name=re.compile(r"^100$"))
    if not option.count():
        option = page.locator("mat-option").filter(has_text=re.compile(r"^100$"))
    if not option.count():
        raise RuntimeError("PLAYWRIGHT_BLOCKED: 100 page-size option not found")
    activate_mdc_combobox(option, "100 page-size option")
    page.wait_for_timeout(1_000)


def _verify_page_size_result(page: Page) -> None:
    total = _pager_total(page)
    expected_rows = min(100, total)
    try:
        page.wait_for_function(
            "expected => document.querySelectorAll('mat-row').length === expected",
            arg=expected_rows,
            timeout=15_000,
        )
    except PlaywrightTimeoutError as exc:
        rows = page.locator("mat-row").count()
        raise RuntimeError(
            f"PLAYWRIGHT_BLOCKED: page-size selection expected {expected_rows} rows and rendered {rows}"
        ) from exc
    paginator = page.locator("mat-paginator")
    selected_value = paginator.locator(
        ".mat-mdc-select-value-text, .mat-select-value-text"
    )
    if not selected_value.count() or selected_value.first.inner_text().strip() != "100":
        raise RuntimeError("PLAYWRIGHT_BLOCKED: page-size control did not retain 100")
    range_label = paginator.locator(
        ".mat-paginator-range-label, .mat-mdc-paginator-range-label"
    )
    if not range_label.count():
        raise RuntimeError("PLAYWRIGHT_BLOCKED: paginator range label is missing")
    observed = " ".join(range_label.first.inner_text().split())
    match = re.search(r"^1\s*[\-–—]\s*([\d,]+)\s+of\s+([\d,]+)$", observed, re.I)
    if not match or int(match.group(1).replace(",", "")) != expected_rows or int(match.group(2).replace(",", "")) != total:
        raise RuntimeError("PLAYWRIGHT_BLOCKED: paginator did not confirm the selected page size")


def _headers(page: Page) -> list[str]:
    return [item.inner_text().strip() for item in page.locator("mat-header-cell").all()]


def _status_column(page: Page) -> tuple[Locator, int]:
    headers = page.locator("mat-header-cell")
    for index in range(headers.count()):
        header = headers.nth(index)
        if header.inner_text().strip().casefold() == "status":
            return header, index
    raise RuntimeError("PLAYWRIGHT_BLOCKED: Status header not found")


def _row_statuses(page: Page, status_index: int) -> list[str]:
    result: list[str] = []
    rows = page.locator("mat-row")
    for index in range(rows.count()):
        cells = rows.nth(index).locator("mat-cell")
        if cells.count() <= status_index:
            raise RuntimeError("PLAYWRIGHT_BLOCKED: Status cell missing")
        result.append(cells.nth(status_index).inner_text().strip())
    return result


def _normalize_status_sort(page: Page) -> tuple[int, list[str]]:
    header, status_index = _status_column(page)
    last_statuses: list[str] = []
    refreshed_ascending = False
    for _ in range(5):
        state = (header.get_attribute("aria-sort") or "none").casefold()
        if state == "ascending":
            statuses = _row_statuses(page, status_index)
            if statuses and statuses[0] not in CLOSED:
                return status_index, statuses
            if not refreshed_ascending:
                # Like page-size changes, the live table can update aria-sort
                # without refetching its server-backed rows. Reapply the
                # unchanged read-only scope once, then independently reread.
                _set_agency_scope(page)
                _verify_page_size_result(page)
                refreshed_ascending = True
                header, status_index = _status_column(page)
                statuses = _row_statuses(page, status_index)
                if (
                    (header.get_attribute("aria-sort") or "none").casefold()
                    == "ascending"
                    and statuses
                    and statuses[0] not in CLOSED
                ):
                    return status_index, statuses
            # The table can preserve an ascending aria state while replacing
            # its rows after a scope/page-size refresh. Cycle away and back so
            # the server reapplies the sort to the refreshed 100-row result.
            last_statuses = statuses
        before = page.locator("mat-row").first.inner_text() if page.locator("mat-row").count() else ""
        activate_sort_header(header, "Status sort header")
        page.wait_for_timeout(1_000)
        try:
            page.wait_for_function(
                "before => { const row=document.querySelector('mat-row'); return row && row.innerText !== before; }",
                arg=before,
                timeout=5_000,
            )
        except PlaywrightTimeoutError:
            pass
        header, status_index = _status_column(page)
    if last_statuses and last_statuses[0] in CLOSED:
        raise RuntimeError("PLAYWRIGHT_BLOCKED: ascending sort showed a closed first row")
    raise RuntimeError("PLAYWRIGHT_BLOCKED: Status sort did not reach ascending")


def _pager_total(page: Page) -> int:
    text = page.locator(".mat-paginator-range-label, .mat-mdc-paginator-range-label").first.inner_text()
    match = re.search(r"of\s+([\d,]+)", text, re.I)
    if not match:
        raise RuntimeError("PLAYWRIGHT_BLOCKED: live pager total not found")
    return int(match.group(1).replace(",", ""))


def _pager_range(page: Page) -> tuple[int, int, int]:
    text = page.locator(
        ".mat-paginator-range-label, .mat-mdc-paginator-range-label"
    ).first.inner_text()
    match = re.search(r"([\d,]+)\s*[\-–—]\s*([\d,]+)\s+of\s+([\d,]+)", text, re.I)
    if not match:
        raise RuntimeError("PLAYWRIGHT_BLOCKED: live pager range not found")
    return tuple(int(value.replace(",", "")) for value in match.groups())


def _header_positions(headers: list[str]) -> dict[str, int]:
    normalized = {" ".join(value.split()).casefold(): index for index, value in enumerate(headers)}
    positions: dict[str, int] = {}
    for key, label in REQUIRED_HEADERS.items():
        index = normalized.get(label.casefold())
        if index is None:
            raise RuntimeError(f"PLAYWRIGHT_BLOCKED: {label} header not found")
        positions[key] = index
    return positions


def _parse_visible_date(value: str) -> date | None:
    cleaned = " ".join(value.split())
    for pattern in DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, pattern).date()
        except ValueError:
            continue
    return None


def _direct_submission_url(page: Page, row: Locator) -> str:
    links = row.locator("a[href]")
    matches: list[str] = []
    for index in range(links.count()):
        href = str(links.nth(index).get_attribute("href") or "").strip()
        if href and "submission" in href.casefold():
            matches.append(urljoin(page.url, href))
    unique = list(dict.fromkeys(matches))
    return unique[0] if len(unique) == 1 else ""


def _read_submission_row(
    page: Page,
    row: Locator,
    positions: dict[str, int],
    *,
    run_date: date,
    page_number: int,
    row_number: int,
) -> dict[str, Any]:
    cells = row.locator("mat-cell")
    if cells.count() <= max(positions.values()):
        raise RuntimeError("PLAYWRIGHT_BLOCKED: Submission Center row is missing required cells")

    def value(key: str) -> str:
        return " ".join(cells.nth(positions[key]).inner_text().split())

    quote_cell = cells.nth(positions["quote_due_date"])
    quote_due_date = _parse_visible_date(value("quote_due_date"))
    effective_date = _parse_visible_date(value("effective_date"))
    cell_class = str(quote_cell.get_attribute("class") or "")
    computed_color = str(
        quote_cell.evaluate("element => getComputedStyle(element).color") or ""
    ).strip()
    overdue_class = bool(re.search(r"(?:^|\s)[^\s]*overdue[^\s]*(?:\s|$)", cell_class, re.I))
    red_overdue = overdue_class and computed_color == RED_OVERDUE_COLOR
    age_days = (run_date - quote_due_date).days if quote_due_date else None
    direct_url = _direct_submission_url(page, row)
    reasons: list[str] = []
    if quote_due_date is None:
        reasons.append("Quote Due Date was not a supported visible date")
    elif age_days is not None and age_days <= 30:
        reasons.append(f"Quote Due Date was {age_days} day(s) old; day 31 qualifies")
    if not overdue_class:
        reasons.append("Quote Due Date cell lacked the live overdue class")
    elif computed_color != RED_OVERDUE_COLOR:
        reasons.append(f"Quote Due Date overdue class rendered unexpected color {computed_color or 'missing'}")
    if not direct_url:
        reasons.append("unique direct submission link was not available")
    qualifies = bool(
        quote_due_date
        and age_days is not None
        and age_days > 30
        and red_overdue
        and direct_url
    )
    return {
        "applicant": value("applicant"),
        "assigned_producer": value("assigned_producer") or "Unassigned",
        "status": value("status"),
        "quote_due_date": quote_due_date.isoformat() if quote_due_date else value("quote_due_date"),
        "effective_date": effective_date.isoformat() if effective_date else value("effective_date"),
        "age_days": age_days,
        "submission_url": direct_url,
        "red_state_evidence": {
            "overdue_class": overdue_class,
            "computed_color": computed_color,
            "expected_color": RED_OVERDUE_COLOR,
        },
        "source_page": page_number,
        "source_row": row_number,
        "qualifies": qualifies,
        "exclusion_reasons": reasons,
    }


def _advance_page(page: Page, previous_start: int) -> None:
    button = page.get_by_role("button", name=re.compile(r"Next page", re.I))
    if not button.count():
        button = page.locator(
            ".mat-paginator-navigation-next, .mat-mdc-paginator-navigation-next"
        )
    if not button.count():
        raise RuntimeError("PLAYWRIGHT_BLOCKED: next-page control not found")
    next_button = button.first
    if (
        next_button.get_attribute("disabled") is not None
        or next_button.get_attribute("aria-disabled") == "true"
    ):
        raise RuntimeError("PLAYWRIGHT_BLOCKED: non-closed group exceeded the available live pages")
    _activate(next_button, "next Submission Center page")
    try:
        page.wait_for_function(
            "previous => { const label=document.querySelector('.mat-paginator-range-label, .mat-mdc-paginator-range-label'); const match=label && label.textContent.match(/([\\d,]+)\\s*[\\-–—]/); return match && Number(match[1].replace(/,/g, '')) > previous; }",
            arg=previous_start,
            timeout=15_000,
        )
    except PlaywrightTimeoutError as exc:
        raise RuntimeError("PLAYWRIGHT_BLOCKED: next Submission Center page did not load") from exc


def audit(*, fresh: bool) -> dict[str, Any]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(CDP_URL, timeout=15_000)
        page = _matching_page(browser)
        page.set_default_timeout(15_000)
        if fresh:
            page.reload(wait_until="domcontentloaded")
        if not _authenticated(page):
            raise PermissionError("NEEDS_AUTH")
        _select_single(page, "Time frame", "All Submissions")
        # The live Submission Center does not render its paginator until the
        # agency scope has been applied. Apply the scope once to reveal it,
        # select 100, then reapply the same scope so its server-backed refresh
        # uses the new page size instead of leaving a stale ten-row data set
        # under a visible value of 100.
        _set_agency_scope(page)
        _set_page_size(page)
        _set_agency_scope(page)
        _verify_page_size_result(page)
        _normalize_status_sort(page)
        run_date = datetime.now(ZoneInfo("America/New_York")).date()
        positions = _header_positions(_headers(page))
        qualifying: list[dict[str, Any]] = []
        dispositions: list[dict[str, Any]] = []
        non_closed_statuses: list[str] = []
        pages_reviewed = 0
        rows_inspected = 0
        non_closed_inspected = 0
        first_page_row_count = page.locator("mat-row").count()
        first_closed_page: int | None = None
        first_closed_index: int | None = None
        first_closed_status = ""
        full_dataset_exhausted = False

        while first_closed_page is None:
            pages_reviewed += 1
            start, end, _ = _pager_range(page)
            expected_page_rows = end - start + 1
            rows = page.locator("mat-row")
            rendered_statuses = _row_statuses(page, positions["status"])
            if len(rendered_statuses) < expected_page_rows:
                raise RuntimeError(
                    "PLAYWRIGHT_BLOCKED: pager range exceeded rendered Submission Center statuses"
                )
            statuses = rendered_statuses[:expected_page_rows]
            if not statuses:
                raise RuntimeError("PLAYWRIGHT_BLOCKED: Submission Center page rendered no statuses")
            for index, status in enumerate(statuses):
                rows_inspected += 1
                if status in CLOSED:
                    first_closed_page = pages_reviewed
                    first_closed_index = index
                    first_closed_status = status
                    break
                non_closed_inspected += 1
                non_closed_statuses.append(status)
                record = _read_submission_row(
                    page,
                    rows.nth(index),
                    positions,
                    run_date=run_date,
                    page_number=pages_reviewed,
                    row_number=index + 1,
                )
                if record["qualifies"]:
                    qualifying.append({key: value for key, value in record.items() if key not in {"qualifies", "exclusion_reasons"}})
                elif (
                    record.get("age_days") is None
                    or int(record.get("age_days") or 0) > 30
                    or record["red_state_evidence"]["overdue_class"]
                ):
                    # Preserve only evidence needed to explain an old/red/ambiguous
                    # candidate. Do not retain every current non-closed applicant.
                    dispositions.append(record)
            _report_pagination_progress(
                pages_reviewed=pages_reviewed,
                rows_inspected=rows_inspected,
                extra={
                    "non_closed_rows_inspected": non_closed_inspected,
                    "first_closed_row_inspected": first_closed_page is not None,
                },
            )
            if first_closed_page is None:
                try:
                    _advance_page(page, start)
                except RuntimeError as exc:
                    if "non-closed group exceeded the available live pages" not in str(exc):
                        raise
                    full_dataset_exhausted = True
                    break

        if not non_closed_statuses:
            raise RuntimeError("PLAYWRIGHT_BLOCKED: no visible non-closed first row")
        qualifying = list({item["submission_url"]: item for item in qualifying}.values())
        producer_counts = Counter(item["assigned_producer"] for item in qualifying)
        status_counts = Counter(item["status"] for item in qualifying)
        return {
            "read_only": True,
            "source_status": "available",
            "scope_time_frame": "All Submissions",
            "scope_assigned_producer": "Streetsmart Insurance",
            "scope_my_submissions": False,
            "run_date": run_date.isoformat(),
            "qualifying_due_date_on_or_before": date.fromordinal(run_date.toordinal() - 31).isoformat(),
            "day_31_qualifies": True,
            "mat_row_count": first_page_row_count,
            "pager_total_present": True,
            "pager_total": _pager_total(page),
            "status_aria_sort": "ascending",
            "first_row_non_closed": non_closed_statuses[0] not in CLOSED,
            "first_row_status": non_closed_statuses[0],
            "first_closed_row_inspected": first_closed_page is not None,
            "full_dataset_exhausted": full_dataset_exhausted,
            "boundary_kind": "first_closed_row" if first_closed_page is not None else "pager_exhausted",
            "first_closed_row_index": first_closed_index,
            "first_closed_row_page": first_closed_page,
            "first_closed_row_status": first_closed_status,
            "pages_reviewed": pages_reviewed,
            "rows_inspected_through_boundary": rows_inspected,
            "non_closed_rows_inspected": non_closed_inspected,
            "distinct_non_closed_statuses": sorted(set(non_closed_statuses)),
            "headers_present": bool(_headers(page)),
            "open_over_30_count": len(qualifying),
            "qualifying_records": qualifying,
            "counts_by_producer": dict(sorted(producer_counts.items())),
            "counts_by_status": dict(sorted(status_counts.items())),
            "candidate_dispositions": dispositions,
            "emails_sent": 0,
            "email_delivery_enabled": False,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--reuse", action="store_true")
    parser.add_argument("--weekly-report", action="store_true")
    args = parser.parse_args()
    try:
        result = weekly_report_audit(fresh=args.fresh) if args.weekly_report else audit(fresh=args.fresh)
        print(json.dumps(result, sort_keys=True))
        return 0
    except PermissionError:
        print("NEEDS_AUTH")
        return 24
    except Exception as exc:
        safe = str(exc).splitlines()[0][:500]
        print(safe if safe.startswith("PLAYWRIGHT_BLOCKED:") else "PLAYWRIGHT_BLOCKED")
        return 20


if __name__ == "__main__":
    raise SystemExit(main())
