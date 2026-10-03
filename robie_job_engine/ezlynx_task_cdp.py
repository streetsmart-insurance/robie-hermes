#!/usr/bin/env python3
"""EZLynx task reassignment via the persistent browser (Playwright/CDP).

Used ONLY for the task assignee form edit — a form, which is the
repo-sanctioned Playwright/CDP use. Notes and documents are API-only
and never go through this module.

Safety:
- Gated by EZLYNX_TASK_REASSIGN_ENABLED=1 (default off). With the gate
  off, the worker flags the task for a human instead of reassigning.
- Every step verifies its precondition; any failure raises and the job
  goes FAILED/UNVERIFIED — the assignee is never assumed changed.
- reassign() re-reads the assignee after saving and returns the
  verified name. A mismatch raises.
- read_assignee() is strictly read-only: it opens the edit dialog,
  reads the field, and cancels without saving.
- A dead/expired EZLynx session raises immediately (fail-closed).

DOM FLOW (documented read-only on 2026-10-03 against the live test
task 63429523; nothing was created, saved, or modified):
applicant Activity page -> "Search Activities" textbox (type a snippet
of the task description) -> "Search" button (magnifying glass) -> click
the task row to expand it -> "Edit this task" button in the expanded
row's icon toolbar -> right-side slide-in panel titled "Edit Task"
(URL unchanged) -> under "Task Assignee", the combobox labeled
"Assign this task" -> click it, type the new person's name, click the
matching row in the suggestion listbox -> teal "Save" button
(bottom-right) applies the change; "Cancel" closes immediately with no
confirmation dialog. The combobox is type-to-search: pressing ArrowDown
alone does NOT open the list; typing characters does.
"""

from __future__ import annotations

import logging
import os
import re
from contextlib import contextmanager

logger = logging.getLogger(__name__)

CDP_URL = os.environ.get("EZLYNX_CDP_URL", "http://127.0.0.1:9222")
REASSIGN_GATE_ENV = "EZLYNX_TASK_REASSIGN_ENABLED"
STEP_TIMEOUT_MS = 30_000


class ReassignError(Exception):
    """The assignee could not be changed or verified."""


def reassign_enabled() -> bool:
    return os.environ.get(REASSIGN_GATE_ENV, "").strip() == "1"


@contextmanager
def _browser_page():
    """A fresh page on the persistent EZLynx browser via CDP."""
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    browser = None
    page = None
    try:
        browser = pw.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        page.set_default_timeout(STEP_TIMEOUT_MS)
        yield page
    finally:
        try:
            if page is not None:
                page.close()
        except Exception:
            pass
        try:
            if browser is not None:
                browser.close()
        except Exception:
            pass
        pw.stop()


def _activity_url(applicant_id: str) -> str:
    return f"https://app.ezlynx.com/web/account/{applicant_id}/activity"


def _assert_signed_in(page) -> None:
    """Fail closed when the EZLynx session has expired."""
    url = page.url or ""
    html = page.content()
    if "login" in url.lower() or re.search(
        r"sign in|log in|username.*password", html, re.IGNORECASE
    ) and "activity" not in url.lower():
        # Heuristic: login pages carry credential fields; the activity
        # page does not. Require positive evidence of the app shell too.
        if not re.search(r"activity|upcoming|applicant", html, re.IGNORECASE):
            raise ReassignError("EZLynx session appears expired (login page shown)")


def _goto_activity(page, applicant_id: str) -> None:
    page.goto(_activity_url(applicant_id), wait_until="domcontentloaded")
    _assert_signed_in(page)
    page.wait_for_timeout(2500)


def _description_snippet(description: str) -> str:
    """A search-box-sized snippet of the task description."""
    text = (description or "").strip()
    # The search box matches on words; take the first ~40 chars.
    return text[:40].strip()


def _search_and_open_edit(page, task_id: str, description: str) -> None:
    """Search Activities, expand the task row, open the Edit Task panel."""
    snippet = _description_snippet(description)

    # 1. Type the description snippet into "Search Activities" and search.
    search_box = page.get_by_label(re.compile(r"search activities", re.I)).first
    try:
        search_box.wait_for(state="visible", timeout=10_000)
    except Exception:
        raise ReassignError("Search Activities box not found on the activity page")
    search_box.fill("")
    if snippet:
        search_box.type(snippet, delay=30)
    search_button = page.get_by_role("button", name=re.compile(r"search", re.I)).first
    try:
        search_button.wait_for(state="visible", timeout=8_000)
        search_button.click()
    except Exception:
        # Fallback: Enter submits the search.
        search_box.press("Enter")
    page.wait_for_timeout(2000)

    # 2. Click the task row (found by its description text) to expand it.
    row = None
    if snippet:
        candidate = page.get_by_text(snippet, exact=False).first
        try:
            candidate.wait_for(state="visible", timeout=10_000)
            row = candidate
        except Exception:
            pass
    if row is None:
        # Fallback: any row mentioning the task id.
        candidate = page.get_by_text(str(task_id), exact=False).first
        try:
            candidate.wait_for(state="visible", timeout=8_000)
            row = candidate
        except Exception:
            raise ReassignError(
                f"Task {task_id} not found in the activity task list"
            )
    row.click()
    page.wait_for_timeout(1500)

    # 3. Click "Edit this task" in the expanded row's icon toolbar.
    edit_button = page.get_by_role("button", name=re.compile(r"edit this task", re.I)).first
    try:
        edit_button.wait_for(state="visible", timeout=10_000)
        edit_button.click()
    except Exception as e:
        raise ReassignError(f"Edit this task button not found for task {task_id}: {e}")

    # 4. The Edit Task panel slides in on the right; the URL does not change.
    try:
        page.get_by_role("heading", name=re.compile(r"edit task", re.I)).first.wait_for(
            state="visible", timeout=10_000
        )
    except Exception:
        raise ReassignError("Edit Task panel did not open")


def _assignee_field(page):
    """Locate the assignee combobox inside the Edit Task panel."""
    strategies = [
        # Documented accessible name.
        lambda: page.get_by_label("Assign this task", exact=False),
        lambda: page.get_by_label(re.compile(r"assign this task", re.I)),
        lambda: page.get_by_label(re.compile(r"assigned to", re.I)),
        lambda: page.locator('[aria-label*="ssign this task" i]'),
        lambda: page.locator('input[placeholder*="ssign" i]'),
    ]
    for make in strategies:
        try:
            locator = make().first
            locator.wait_for(state="visible", timeout=8_000)
            return locator
        except Exception:
            continue
    raise ReassignError("Assignee field not found in the Edit Task dialog")


def _read_assignee_value(field) -> str:
    """Read the current assignee from the combobox (no changes)."""
    try:
        value = field.input_value()
        if value and value.strip():
            return value.strip()
    except Exception:
        pass
    # Some comboboxes render the selection as text.
    try:
        text = field.text_content() or ""
        text = text.strip()
        if text:
            return text
    except Exception:
        pass
    return ""


def _set_assignee(page, field, new_assignee: str) -> None:
    """Pick a person in the type-to-search assignee combobox.

    Documented: pressing ArrowDown alone does not open the suggestion
    list; typing characters filters and opens it.
    """
    field.click()
    page.wait_for_timeout(800)
    try:
        field.fill("")
    except Exception:
        pass
    field.type(new_assignee, delay=40)
    page.wait_for_timeout(1500)
    # The suggestion listbox opens beneath the field; each row shows a
    # person icon plus the name. Click the matching row.
    option = page.get_by_role("option", name=new_assignee, exact=False).first
    try:
        option.wait_for(state="visible", timeout=10_000)
        option.click()
        return
    except Exception:
        pass
    popup_option = page.get_by_text(new_assignee, exact=True).first
    try:
        popup_option.wait_for(state="visible", timeout=8_000)
        popup_option.click()
        return
    except Exception as e:
        raise ReassignError(
            f"Could not pick {new_assignee!r} in the assignee list: {e}"
        )


def _click_save(page) -> None:
    """Click the teal Save button (bottom-right). The change is not
    applied until Save; document-first strategies."""
    button = page.get_by_role("button", name="Save", exact=True).first
    try:
        button.wait_for(state="visible", timeout=10_000)
        button.click()
        page.wait_for_timeout(2000)
        return
    except Exception:
        pass
    for name in ("Update", "Apply"):
        try:
            fallback = page.get_by_role("button", name=name, exact=True).first
            fallback.wait_for(state="visible", timeout=6_000)
            fallback.click()
            page.wait_for_timeout(2000)
            return
        except Exception:
            continue
    raise ReassignError("Save button not found in the Edit Task dialog")


def _cancel_dialog(page) -> None:
    """Click Cancel (documented: closes immediately, no confirmation)."""
    button = page.get_by_role("button", name="Cancel", exact=True).first
    try:
        button.wait_for(state="visible", timeout=6_000)
        button.click()
        return
    except Exception:
        pass
    try:
        page.get_by_role("button", name="Close", exact=True).first.click(timeout=4_000)
        return
    except Exception:
        pass
    page.keyboard.press("Escape")


class PlaywrightTaskReassigner:
    """TaskReassigner implemented against the EZLynx UI (gated)."""

    def read_assignee(
        self, task_id: str, applicant_id: str, description: str = ""
    ) -> str:
        """Read-only: current assignee name. Opens the edit dialog, reads
        the combobox, cancels without saving."""
        with _browser_page() as page:
            _goto_activity(page, applicant_id)
            _search_and_open_edit(page, task_id, description)
            field = _assignee_field(page)
            value = _read_assignee_value(field)
            _cancel_dialog(page)
            if not value:
                raise ReassignError(f"Could not read assignee for task {task_id}")
            return value

    def reassign(
        self, task_id: str, applicant_id: str, new_assignee: str,
        description: str = "",
    ) -> str:
        """Set the assignee and prove it stuck. Returns the verified name."""
        if not reassign_enabled():
            raise ReassignError(
                f"Reassignment gate is off ({REASSIGN_GATE_ENV}=1 required)"
            )
        with _browser_page() as page:
            _goto_activity(page, applicant_id)
            _search_and_open_edit(page, task_id, description)

            field = _assignee_field(page)
            before = _read_assignee_value(field)
            logger.info(f"Task {task_id}: assignee currently {before!r}; setting {new_assignee!r}")
            _set_assignee(page, field, new_assignee)
            _click_save(page)

            # Verify by re-reading: reload and walk the documented flow again.
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            _assert_signed_in(page)
            _search_and_open_edit(page, task_id, description)
            field = _assignee_field(page)
            verified = _read_assignee_value(field)
            _cancel_dialog(page)

        if verified.strip().lower() != new_assignee.strip().lower():
            raise ReassignError(
                f"Assignee re-read as {verified!r}, expected {new_assignee!r} — not confirmed"
            )
        logger.info(f"Task {task_id}: reassigned to {verified!r} (verified)")
        return verified
