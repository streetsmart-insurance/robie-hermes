"""Read-only Playwright runner for the fixed EZLynx Submission Center scope."""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from .ezlynx_auth_evidence import authenticated_app_evidence


CDP_URL = "http://127.0.0.1:9222"
SUBMISSION_URL = "https://app.ezlynx.com/web/submission-center/overview/submissions"
CLOSED = {"Closed - Not Sold", "Closed - Bound"}


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


def _click_control(page: Page, label: str) -> None:
    candidates = (
        page.get_by_role("combobox", name=re.compile(label, re.I)),
        page.locator("mat-form-field").filter(has_text=re.compile(label, re.I)).locator("mat-select"),
        page.locator("mat-select").filter(has_text=re.compile(label, re.I)),
    )
    for candidate in candidates:
        if candidate.count():
            candidate.first.click()
            return
    raise RuntimeError(f"PLAYWRIGHT_BLOCKED: {label} control not found")


def _select_single(page: Page, label: str, option: str) -> None:
    if page.get_by_text(option, exact=True).count() and label.casefold() in page.locator("body").inner_text().casefold():
        field = page.locator("mat-form-field").filter(has_text=re.compile(label, re.I))
        if field.count() and option.casefold() in field.first.inner_text().casefold():
            return
    _click_control(page, label)
    choice = page.get_by_role("option", name=re.compile(rf"^{re.escape(option)}$", re.I))
    if not choice.count():
        choice = page.locator("mat-option").filter(has_text=re.compile(rf"^{re.escape(option)}$", re.I))
    if not choice.count():
        raise RuntimeError(f"PLAYWRIGHT_BLOCKED: {option} option not found")
    choice.first.click()
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
    last_error: PlaywrightTimeoutError | None = None
    for selector in (".cdk-overlay-container mat-checkbox", "mat-checkbox"):
        options = page.locator(selector)
        try:
            options.first.wait_for(state="visible", timeout=5_000)
            if (
                _exact_visible_option(options, "My Submissions") is not None
                and _exact_visible_option(options, "Streetsmart Insurance") is not None
            ):
                return options
        except PlaywrightTimeoutError as exc:
            last_error = exc
    raise RuntimeError("PLAYWRIGHT_BLOCKED: agency options not found") from last_error


def _set_agency_scope(page: Page) -> None:
    field = page.locator("mat-form-field").filter(
        has_text=re.compile("Submissions by assigned producer", re.I)
    )
    if not field.count():
        raise RuntimeError("PLAYWRIGHT_BLOCKED: agency field not found")
    picker_button = field.first.locator("button")
    live_checkbox_picker = picker_button.count() > 0
    if live_checkbox_picker:
        picker_button.first.click()
        options = _live_agency_options(page)
    else:
        _click_control(page, "Submissions by assigned producer")
        options = page.locator("mat-option, [role=option]")
    mine = _exact_visible_option(options, "My Submissions")
    agency = _exact_visible_option(options, "Streetsmart Insurance")
    if mine is None or agency is None:
        raise RuntimeError("PLAYWRIGHT_BLOCKED: agency options not found")
    if _option_selected(mine):
        mine.click()
    if not _option_selected(agency):
        agency.click()
    apply_button = page.get_by_role(
        "button", name=re.compile(r"^(Apply|Done|Select)$", re.I)
    )
    if apply_button.count():
        apply_button.last.click()
    else:
        page.keyboard.press("Escape")
    page.wait_for_timeout(1_000)
    if live_checkbox_picker:
        # The live MDC picker collapses to an icon and does not render the
        # chosen producer names in the form-field text. Reopen it and reread
        # the actual checkbox state instead of trusting the prior clicks.
        picker_button.first.click()
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
            cancel.last.click()
        else:
            page.keyboard.press("Escape")
    else:
        field_text = field.first.inner_text().casefold()
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
    if re.search(r"\b100\b", selector.first.inner_text() or ""):
        return
    selector.first.click()
    option = page.get_by_role("option", name=re.compile(r"^100$"))
    if not option.count():
        option = page.locator("mat-option").filter(has_text=re.compile(r"^100$"))
    if not option.count():
        raise RuntimeError("PLAYWRIGHT_BLOCKED: 100 page-size option not found")
    option.first.click()
    page.wait_for_timeout(1_000)


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
    for _ in range(3):
        state = (header.get_attribute("aria-sort") or "none").casefold()
        if state == "ascending":
            statuses = _row_statuses(page, status_index)
            if statuses and statuses[0] not in CLOSED:
                return status_index, statuses
            raise RuntimeError("PLAYWRIGHT_BLOCKED: ascending sort showed a closed first row")
        before = page.locator("mat-row").first.inner_text() if page.locator("mat-row").count() else ""
        header.click()
        page.wait_for_timeout(1_000)
        try:
            page.wait_for_function(
                "before => { const row=document.querySelector('mat-row'); return row && row.innerText !== before; }",
                before,
                timeout=5_000,
            )
        except PlaywrightTimeoutError:
            pass
        header, status_index = _status_column(page)
    raise RuntimeError("PLAYWRIGHT_BLOCKED: Status sort did not reach ascending")


def _pager_total(page: Page) -> int:
    text = page.locator(".mat-paginator-range-label, .mat-mdc-paginator-range-label").first.inner_text()
    match = re.search(r"of\s+([\d,]+)", text, re.I)
    if not match:
        raise RuntimeError("PLAYWRIGHT_BLOCKED: live pager total not found")
    return int(match.group(1).replace(",", ""))


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
        _set_agency_scope(page)
        _set_page_size(page)
        status_index, statuses = _normalize_status_sort(page)
        rows = page.locator("mat-row")
        if rows.count() != 100:
            raise RuntimeError(f"PLAYWRIGHT_BLOCKED: expected 100 mat-row elements, saw {rows.count()}")
        first_closed_index = next((i for i, value in enumerate(statuses) if value in CLOSED), None)
        inspected = len(statuses) if first_closed_index is None else first_closed_index + 1
        if first_closed_index is None:
            raise RuntimeError("PLAYWRIGHT_BLOCKED: first closed row was not reached on the 100-row page")
        non_closed = statuses[:first_closed_index]
        if not non_closed:
            raise RuntimeError("PLAYWRIGHT_BLOCKED: no visible non-closed first row")
        return {
            "read_only": True,
            "scope_time_frame": "All Submissions",
            "scope_assigned_producer": "Streetsmart Insurance",
            "scope_my_submissions": False,
            "mat_row_count": rows.count(),
            "pager_total_present": True,
            "pager_total": _pager_total(page),
            "status_aria_sort": "ascending",
            "first_row_non_closed": statuses[0] not in CLOSED,
            "first_row_status": statuses[0],
            "first_closed_row_inspected": True,
            "first_closed_row_index": first_closed_index,
            "first_closed_row_status": statuses[first_closed_index],
            "rows_inspected_through_boundary": inspected,
            "distinct_non_closed_statuses": sorted(set(non_closed)),
            "headers_present": bool(_headers(page)),
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--reuse", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(audit(fresh=args.fresh), sort_keys=True))
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
