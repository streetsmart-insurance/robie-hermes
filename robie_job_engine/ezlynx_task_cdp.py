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


# ENFORCED FIELD CONTRACT. Nothing here guesses a selector. The page labels, scope and read
# method for each consequential field are declared in `deploy/ezlynx_task_field_contract.json`,
# only after a reviewed read-only Test inspection (docs/EZLYNX_TASK_FIELD_DISCOVERY.md), each with
# an observation record. A field with no valid declaration cannot be read, and the live read refuses
# BEFORE any browser use, so task reassignment stays disabled until every required field is declared.
CONTRACT_PATH_ENV = "ROBIE_TASK_FIELD_CONTRACT_PATH"
REQUIRED_FIELDS = ("description", "created_by", "assigned_producer", "csr", "activity_labels")
_SCOPES = ("dialog", "page")
_READ_METHODS = ("input_value", "text_content")


class FieldContractNotEstablished(ReassignError):
    """A required field has no verified declaration, so it must not be read or relied on."""


def default_contract_path() -> str:
    import pathlib

    return os.environ.get(CONTRACT_PATH_ENV) or str(
        pathlib.Path(__file__).resolve().parents[1] / "deploy" / "ezlynx_task_field_contract.json")


def _valid_entry(entry) -> bool:
    if not isinstance(entry, dict):
        return False
    if entry.get("scope") not in _SCOPES or entry.get("read") not in _READ_METHODS:
        return False
    if not all(str(entry.get(key) or "").strip() for key in ("label", "meaning", "report_column")):
        return False
    observed = entry.get("verified")
    return (isinstance(observed, dict) and observed.get("environment") in ("TEST", "PRODUCTION")
            and all(str(observed.get(key) or "").strip()
                    for key in ("applicant_id", "observed_on", "observed_by", "evidence")))


def load_field_contract(path: str | None = None) -> dict:
    """The VALID declarations only; anything malformed or unobserved is treated as undeclared."""
    import json

    try:
        with open(path or default_contract_path(), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    fields = data.get("fields") if isinstance(data, dict) else None
    if not isinstance(fields, dict):
        return {}
    return {name: entry for name, entry in fields.items() if name in REQUIRED_FIELDS and _valid_entry(entry)}


def field_contract_status(path: str | None = None) -> dict:
    declared = load_field_contract(path)
    missing = [name for name in REQUIRED_FIELDS if name not in declared]
    return {"established": not missing, "missing": missing, "path": path or default_contract_path()}


def require_field_contract(path: str | None = None) -> dict:
    status = field_contract_status(path)
    if not status["established"]:
        raise FieldContractNotEstablished(
            f"the EZLynx task field contract is not established (undeclared: {', '.join(status['missing'])}); "
            "reassignment stays disabled until a reviewed read-only Test inspection fills it")
    return load_field_contract(path)


def _read_value(name: str, field, method: str) -> str:
    """The field's text. A failed read raises; only a successful read of nothing is blank."""
    try:
        value = field.input_value() if method == "input_value" else field.text_content()
    except Exception as exc:  # noqa: BLE001
        raise ReassignError(f"could not read {name}: {type(exc).__name__}: {exc}") from exc
    if value is None:
        raise ReassignError(f"could not read {name}: the page returned no value")
    return " ".join(str(value).split())


def _read_consequential_fields(panel, page, contract: dict | None = None) -> dict:
    """Every field the handback depends on, each through its declared selector and read method."""
    declared = contract if contract is not None else require_field_contract()
    state = {}
    for name in REQUIRED_FIELDS:
        entry = declared.get(name)
        if entry is None:
            raise FieldContractNotEstablished(f"{name} has no verified declaration")
        scope = panel if entry["scope"] == "dialog" else page
        field = scope.get_by_label(entry["label"], exact=True)
        found = field.count()
        if found != 1:
            raise ReassignError(f"{name}: expected exactly one field labelled {entry['label']!r}, found {found}")
        state[name] = _read_value(name, field, entry["read"])
    return state


class PlaywrightTaskReassigner:
    """TaskReassigner implemented against the EZLynx UI (gated)."""

    def read_task_state(
        self, task_id: str, applicant_id: str, description: str = ""
    ) -> dict:
        """Read-only: the task's current assignee AND every field the handback depends on.

        Used to prove a handed-back task is really back with Robie, under the same
        instructions and routing the report claims, before a new round opens. The page
        selectors it uses come only from the verified field contract.
        """
        validate_identity(task_id, applicant_id)
        contract = require_field_contract()  # refuses BEFORE any browser use
        with _browser_page() as page:
            _goto_activity(page, applicant_id)
            panel = _search_and_open_edit(page, task_id, applicant_id)
            assignee = _read_assignee_value(_assignee_field(panel))
            state = _read_consequential_fields(panel, page, contract)
            _cancel_dialog(panel)
        if not assignee:
            raise ReassignError(f"Could not read assignee for task {task_id}")
        return {"assignee": assignee, **state}

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
        require_field_contract()  # one boundary: no Save, from any caller, until the contract is established
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
