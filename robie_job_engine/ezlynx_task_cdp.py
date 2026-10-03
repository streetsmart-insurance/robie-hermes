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

The DOM flow below follows the manually verified path (2026-10-03):
applicant Activity page -> Upcoming task list -> task row -> Edit Task
dialog -> "Assign this task" combobox -> pick person -> Save ->
reopen dialog -> confirm assignee. Selectors are defensive (multiple
strategies) because EZLynx markup is not contractual.
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
    # Let the task list render.
    page.wait_for_timeout(2500)


def _find_task_row(page, task_id: str, description: str):
    """Locate the task row in the Upcoming list. Returns a Locator."""
    snippet = (description or "").strip()[:60]
    candidates = []
    if snippet:
        # Prefer a text match on the note snippet…
        candidates.append(page.get_by_text(snippet, exact=False).first)
    # …fall back to any row mentioning the task id.
    candidates.append(page.get_by_text(str(task_id), exact=False).first)
    for locator in candidates:
        try:
            locator.wait_for(state="visible", timeout=10_000)
            return locator
        except Exception:
            continue
    raise ReassignError(
        f"Task {task_id} not found in the activity task list"
    )


def _assignee_field(page):
    """Locate the assignee combobox inside the Edit Task dialog."""
    strategies = [
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
    """Pick a person in the assignee combobox."""
    field.click()
    page.wait_for_timeout(800)
    # If it is a native select, choose directly.
    try:
        tag = field.evaluate("el => el.tagName.toLowerCase()")
        if tag == "select":
            field.select_option(label=new_assignee)
            return
    except Exception:
        pass
    # Otherwise treat it as a searchable combobox: type, then pick.
    try:
        field.fill("")
    except Exception:
        field.click()
    field.type(new_assignee, delay=40)
    page.wait_for_timeout(1500)
    option = page.get_by_role("option", name=new_assignee, exact=False).first
    try:
        option.wait_for(state="visible", timeout=10_000)
        option.click()
        return
    except Exception:
        pass
    # Last resort: exact-text option anywhere in the popup.
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
    for name in ("Save", "Update", "Apply"):
        try:
            button = page.get_by_role("button", name=name, exact=True).first
            button.wait_for(state="visible", timeout=6_000)
            button.click()
            page.wait_for_timeout(2000)
            return
        except Exception:
            continue
    raise ReassignError("Save button not found in the Edit Task dialog")


def _cancel_dialog(page) -> None:
    for name in ("Cancel", "Close"):
        try:
            button = page.get_by_role("button", name=name, exact=True).first
            button.wait_for(state="visible", timeout=4_000)
            button.click()
            return
        except Exception:
            continue
    page.keyboard.press("Escape")


class PlaywrightTaskReassigner:
    """TaskReassigner implemented against the EZLynx UI (gated)."""

    def read_assignee(self, task_id: str, applicant_id: str) -> str:
        """Read-only: current assignee name. Never saves."""
        with _browser_page() as page:
            _goto_activity(page, applicant_id)
            row = _find_task_row(page, task_id, "")
            row.click()
            page.wait_for_timeout(1500)
            field = _assignee_field(page)
            value = _read_assignee_value(field)
            _cancel_dialog(page)
            if not value:
                raise ReassignError(f"Could not read assignee for task {task_id}")
            return value

    def reassign(self, task_id: str, applicant_id: str, new_assignee: str) -> str:
        """Set the assignee and prove it stuck. Returns the verified name."""
        if not reassign_enabled():
            raise ReassignError(
                f"Reassignment gate is off ({REASSIGN_GATE_ENV}=1 required)"
            )
        with _browser_page() as page:
            _goto_activity(page, applicant_id)
            row = _find_task_row(page, task_id, "")
            row.click()
            page.wait_for_timeout(1500)

            field = _assignee_field(page)
            before = _read_assignee_value(field)
            logger.info(f"Task {task_id}: assignee currently {before!r}; setting {new_assignee!r}")
            _set_assignee(page, field, new_assignee)
            _click_save(page)

            # Verify by re-reading: reopen the dialog fresh.
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            _assert_signed_in(page)
            row = _find_task_row(page, task_id, "")
            row.click()
            page.wait_for_timeout(1500)
            field = _assignee_field(page)
            verified = _read_assignee_value(field)
            _cancel_dialog(page)

        if verified.strip().lower() != new_assignee.strip().lower():
            raise ReassignError(
                f"Assignee re-read as {verified!r}, expected {new_assignee!r} — not confirmed"
            )
        logger.info(f"Task {task_id}: reassigned to {verified!r} (verified)")
        return verified
