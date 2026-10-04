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

Identity contract: the activity row and Edit Task dialog must both expose
exact data-task-id and data-applicant-id attributes. Missing/ambiguous machine
identity fails closed. This contract needs a read-only Test DOM validation
before enabling reassignment; no description or page-wide Edit fallback.
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


class AssigneeUnresolvedError(ReassignError):
    """The target person is missing or ambiguous in the picker; nothing was saved."""


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


def validate_identity(task_id: str, applicant_id: str) -> None:
    for label, value in (("task", task_id), ("applicant", applicant_id)):
        if not re.fullmatch(r"[1-9][0-9]*", str(value or "").strip()):
            raise ReassignError(f"Invalid {label} ID; reassignment not sent")


def _unique(locator, label: str):
    if locator.count() != 1:
        raise ReassignError(f"Missing or ambiguous {label}; no edit")
    locator.wait_for(state="visible", timeout=STEP_TIMEOUT_MS)
    return locator


def _assert_identity(locator, task_id: str, applicant_id: str) -> None:
    if (locator.get_attribute("data-task-id") != task_id
            or locator.get_attribute("data-applicant-id") != applicant_id):
        raise ReassignError("Task/applicant identity changed; no edit")


def _search_and_open_edit(page, task_id: str, applicant_id: str):
    """Only exact machine identity may authorize a row and its edit panel.

    If the live DOM does not expose this identity, stop for a supported
    locator contract. Description text and page-wide controls are never used.
    """
    validate_identity(task_id, applicant_id)
    expected_url = _activity_url(applicant_id)
    if page.url.rstrip("/") != expected_url:
        raise ReassignError("Foreign applicant activity page; no edit")
    row = _unique(page.locator(f'[data-task-id="{task_id}"]'), "task row")
    _assert_identity(row, task_id, applicant_id)
    row.click()
    _assert_identity(row, task_id, applicant_id)
    _unique(row.get_by_role("button", name="Edit this task", exact=True), "row edit").click()
    panel = _unique(page.get_by_role("dialog", name="Edit Task", exact=True), "task panel")
    _assert_identity(panel, task_id, applicant_id)
    return panel


def _assignee_field(panel):
    return _unique(panel.get_by_label("Assign this task", exact=True), "assignee field")


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
    try:
        field.fill("")
    except Exception:
        pass
    field.type(new_assignee, delay=40)
    # The suggestion listbox opens beneath the field; each row shows a
    # person icon plus the name. Click the matching row.
    try:
        option = _unique(page.get_by_role("option", name=new_assignee, exact=True), "assignee option")
    except ReassignError as exc:
        raise AssigneeUnresolvedError(str(exc)) from exc
    option.click()


def _click_save(panel) -> None:
    _unique(panel.get_by_role("button", name="Save", exact=True), "task Save").click()


def _cancel_dialog(panel) -> None:
    _unique(panel.get_by_role("button", name="Cancel", exact=True), "task Cancel").click()


# The label(s) the Edit Task dialog uses for the task's instructions. UNVERIFIED against the
# live DOM: Test must observe the real field. Until a label matches exactly one field, the
# live request cannot be read and a handed-back task never opens a new round (fail closed).
TASK_DESCRIPTION_LABELS = ("Description", "Task description", "Task note", "Note")


def _read_task_description(panel) -> str:
    """The task's CURRENT instructions from the Edit Task dialog, whitespace-normalized."""
    for label in TASK_DESCRIPTION_LABELS:
        field = panel.get_by_label(label, exact=True)
        found = field.count()
        if found == 0:
            continue
        if found != 1:
            raise ReassignError(f"Ambiguous task description field {label!r}; request not verifiable")
        text = ""
        try:
            text = field.input_value() or ""
        except Exception:
            pass
        if not text.strip():
            try:
                text = field.text_content() or ""
            except Exception:
                text = ""
        return " ".join(text.split())
    raise ReassignError("Task description field not found; the live request cannot be verified")


class PlaywrightTaskReassigner:
    """TaskReassigner implemented against the EZLynx UI (gated)."""

    def read_task_state(
        self, task_id: str, applicant_id: str, description: str = ""
    ) -> dict:
        """Read-only: the task's current assignee AND current instructions.

        Used to prove a handed-back task is really back with Robie, under the same
        instructions the report claims, before a new round opens.
        """
        validate_identity(task_id, applicant_id)
        with _browser_page() as page:
            _goto_activity(page, applicant_id)
            panel = _search_and_open_edit(page, task_id, applicant_id)
            assignee = _read_assignee_value(_assignee_field(panel))
            request = _read_task_description(panel)
            _cancel_dialog(panel)
        if not assignee:
            raise ReassignError(f"Could not read assignee for task {task_id}")
        return {"assignee": assignee, "description": request}

    def read_assignee(
        self, task_id: str, applicant_id: str, description: str = ""
    ) -> str:
        """Read-only: current assignee name. Opens the edit dialog, reads
        the combobox, cancels without saving."""
        validate_identity(task_id, applicant_id)
        with _browser_page() as page:
            _goto_activity(page, applicant_id)
            panel = _search_and_open_edit(page, task_id, applicant_id)
            field = _assignee_field(panel)
            value = _read_assignee_value(field)
            _cancel_dialog(panel)
            if not value:
                raise ReassignError(f"Could not read assignee for task {task_id}")
            return value

    def reassign(
        self, task_id: str, applicant_id: str, new_assignee: str,
        description: str = "", expected_assignee: str = "Robie AI",
    ) -> str:
        """Set the assignee and prove it stuck. Returns the verified name."""
        if not reassign_enabled():
            raise ReassignError(
                f"Reassignment gate is off ({REASSIGN_GATE_ENV}=1 required)"
            )
        validate_identity(task_id, applicant_id)
        from .ezlynx_write_scope import require_allowed_ezlynx_write_applicant

        require_allowed_ezlynx_write_applicant(applicant_id)
        with _browser_page() as page:
            _goto_activity(page, applicant_id)
            panel = _search_and_open_edit(page, task_id, applicant_id)

            field = _assignee_field(panel)
            before = _read_assignee_value(field)
            if not before or before.casefold() != expected_assignee.strip().casefold():
                _cancel_dialog(panel)
                raise ReassignError("Assignee changed since intake; reassignment not sent")
            _assert_identity(panel, task_id, applicant_id)
            _set_assignee(panel, field, new_assignee)
            _assert_identity(panel, task_id, applicant_id)
            _click_save(panel)

            # Verify by re-reading: reload and walk the documented flow again.
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            _assert_signed_in(page)
            panel = _search_and_open_edit(page, task_id, applicant_id)
            field = _assignee_field(panel)
            verified = _read_assignee_value(field)
            _cancel_dialog(panel)

        if verified.strip().lower() != new_assignee.strip().lower():
            raise ReassignError(
                f"Assignee re-read as {verified!r}, expected {new_assignee!r} — not confirmed"
            )
        logger.info(f"Task {task_id}: reassigned to {verified!r} (verified)")
        return verified
