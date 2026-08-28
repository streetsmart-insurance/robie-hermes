"""Create-program defaults and timings the PAWIVA live job never logged.

PR 37 already walked locators. A live PAWIVA job then showed three things
Dusty never caught on hermes-test-01:

1. Programs spinner ~12s before the unique primary + New program is ready
2. Agency Fee default $0.00 / empty on /create/new
3. Producer and Account Manager prefilled Robie AI

This module is the Test-only recorder for those three. CI is a mock /
locator battery. The live walk is hermes-test-01 only. Never PAWIVA /
221398001 / a live client. Never bind. Never email. Stop before Save.

Follow-tab (recorder / Playwright tab rebind) is a separate proof — do
not treat a clean create-program audit as follow-tab evidence.
"""

from __future__ import annotations

import re
from typing import Any

from .ascend_sender_roles import (
    ACCOUNT_MANAGER_LOCATOR,
    CARLO_FERRARA,
    CREATE_URL,
    JAKE_FERRARA,
    NEW_PROGRAM_LOCATOR,
    PRODUCER_LOCATOR,
    ROBIE_AI,
    locator_is_new_program_caret,
    new_program_locator_is_unique_primary,
    refuse_robie_ai_when_sender_known,
    resolve_sender_agent,
    roles_for_requested_by,
)
from .hitl import dry_playwright_hitl_text


SPINNER_TIMING_SCENARIO_ID = "ascend-new-program:spinner-timing"
AGENCY_FEE_SCENARIO_ID = "ascend-create:agency-fee-default"
ROLES_DEFAULTS_SCENARIO_ID = "ascend-roles:sender-not-robie-ai"
DOCUMENT_LABELS_ID = "import_document"

CREATE_PATH = "/create/new"
CREATE_URL_RE = re.compile(r"/create/new(?:[/?#].*)?$", re.IGNORECASE)
WAIT_FOR_URL = f'page.wait_for_url("{CREATE_PATH}")'
TEST_AGENCY_FEE = "500"
AGENCY_FEE_LOCATOR = 'get_by_label("Agency Fee")'
EXPECTED_FEE_DEFAULTS = frozenset(
    {
        "",
        "0",
        "0.0",
        "0.00",
        "$0",
        "$0.0",
        "$0.00",
        "0,00",
        "$0,00",
    }
)
IMPORT_DOCUMENT_LABEL = "Import document"
UPLOAD_DOCUMENT_LABEL = "Upload document"
DROPZONE_MARKERS = (
    "dropzone",
    "drop files",
    "drop file",
    "drag and drop",
    "drag file",
    "drop your file",
    "drop a file",
)
PAWIVA_SPINNER_SECONDS = 12.0

SPINNER_TIMING_CHAT = (
    "ASCEND programs spinner timing class returned: log seconds until the "
    "unique primary + New program is ready; click that primary, never the "
    "caret; wait_for_url /create/new. Missing seconds is FAIL."
)

AGENCY_FEE_CHAT = (
    "ASCEND Agency Fee default class returned: log the /create/new default "
    "(expect $0.00 or empty); set 500 in the Test run if the field exists; "
    "then STOP before Save program / Send email / checkout / payment / bind."
)


def spinner_seconds(started_monotonic: float, ready_monotonic: float) -> float:
    """Seconds until + New program primary is ready. Always log this."""
    raw = float(ready_monotonic) - float(started_monotonic)
    if raw < 0:
        raw = 0.0
    return round(raw, 3)


def require_spinner_seconds_logged(seconds: Any) -> str | None:
    """Missing / non-numeric seconds is FAIL. That is the PR 37 gap."""
    if seconds is None or seconds == "":
        return (
            f"{SPINNER_TIMING_SCENARIO_ID} FAIL: programs spinner seconds "
            "were not logged"
        )
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return (
            f"{SPINNER_TIMING_SCENARIO_ID} FAIL: spinner seconds "
            f"{seconds!r} is not a number"
        )
    if value < 0:
        return f"{SPINNER_TIMING_SCENARIO_ID} FAIL: spinner seconds {value} is negative"
    return None


def create_url_is_new_program(url: str) -> bool:
    text = str(url or "").strip()
    if not text:
        return False
    return bool(CREATE_URL_RE.search(text.split("#", 1)[0]))


def require_create_new_url(url: str) -> str | None:
    if create_url_is_new_program(url):
        return None
    return (
        f"{SPINNER_TIMING_SCENARIO_ID} FAIL: after + New program expected "
        f"{CREATE_PATH}, got {url!r}"
    )


def new_program_click_is_primary(locator: str) -> bool:
    if locator_is_new_program_caret(locator):
        return False
    return new_program_locator_is_unique_primary(locator)


def normalize_fee_default(value: Any) -> str:
    text = str(value or "").strip()
    text = text.replace(",", "")
    text = re.sub(r"\s+", "", text)
    return text


def agency_fee_default_is_empty_or_zero(value: Any) -> bool:
    folded = normalize_fee_default(value).casefold()
    return folded in {item.casefold().replace(",", "") for item in EXPECTED_FEE_DEFAULTS}


def refuse_unlogged_agency_fee_default(default: Any, *, field_present: bool) -> str | None:
    if not field_present:
        return None
    if default is None:
        return (
            f"{AGENCY_FEE_SCENARIO_ID} FAIL: Agency Fee default was not logged"
        )
    return None


def refuse_unexpected_agency_fee_default(default: Any, *, field_present: bool) -> str | None:
    missing = refuse_unlogged_agency_fee_default(default, field_present=field_present)
    if missing:
        return missing
    if not field_present:
        return None
    if agency_fee_default_is_empty_or_zero(default):
        return None
    return (
        f"{AGENCY_FEE_SCENARIO_ID} FAIL: Agency Fee default {default!r} "
        "is not $0.00 / empty"
    )


def test_agency_fee_set_value() -> str:
    return TEST_AGENCY_FEE


def should_set_test_agency_fee(*, field_present: bool) -> bool:
    return bool(field_present)


def stop_before_save_after_fee() -> tuple[str, ...]:
    return (
        "Save program",
        "Send email",
        "Copy checkout",
        "payment",
        "bind",
    )


def classify_document_labels(*visible: Any) -> dict[str, Any]:
    """Log Import document vs Upload document vs dropzone. Do not guess."""
    blob = " ".join(str(item or "") for item in visible).casefold()
    import_document = IMPORT_DOCUMENT_LABEL.casefold() in blob
    upload_document = UPLOAD_DOCUMENT_LABEL.casefold() in blob
    dropzone = any(marker in blob for marker in DROPZONE_MARKERS)
    labels: list[str] = []
    if import_document:
        labels.append(IMPORT_DOCUMENT_LABEL)
    if upload_document:
        labels.append(UPLOAD_DOCUMENT_LABEL)
    if dropzone:
        labels.append("dropzone")
    return {
        "import_document": import_document,
        "upload_document": upload_document,
        "dropzone": dropzone,
        "labels": labels,
        "logged": True,
    }


def log_role_defaults(
    *,
    requested_by: str,
    producer: str | None,
    account_manager: str | None,
) -> dict[str, Any]:
    """Log the prefilled values, then FAIL if they stay Robie AI for Carlo/Jake."""
    leak = None
    if producer is None or account_manager is None:
        leak = (
            f"{ROLES_DEFAULTS_SCENARIO_ID} FAIL: Producer/Account Manager "
            "prefilled values were not logged"
        )
    else:
        leak = refuse_robie_ai_when_sender_known(
            requested_by=requested_by,
            producer=producer,
            account_manager=account_manager,
        )
    resolved = resolve_sender_agent(requested_by)
    return {
        "requested_by": requested_by,
        "resolved": resolved,
        "producer_default": producer,
        "account_manager_default": account_manager,
        "logged": producer is not None and account_manager is not None,
        "error": leak,
        "hitl_required": resolved is None,
    }


def programs_spinner_timing_instruction() -> str:
    return (
        "On https://dashboard.useascend.com/programs log the seconds until "
        f"the unique primary {NEW_PROGRAM_LOCATOR} is visible and enabled. "
        "Click that primary, never the split-menu caret. Then "
        f"{WAIT_FOR_URL} ({CREATE_URL}). A ~12s spinner is normal. "
        "Missing seconds is FAIL. Timeout is PLAYWRIGHT_BLOCKED. No Gemini."
    )


def agency_fee_instruction() -> str:
    return (
        f"On {CREATE_URL} log the Agency Fee default "
        f"({AGENCY_FEE_LOCATOR}). Expect $0.00 or empty. If the field "
        f"exists, set {TEST_AGENCY_FEE} in this Test run, then STOP before "
        "Save program, Send email, Copy checkout, payment, or bind. "
        "Do not overwrite Loom ascend-finance."
    )


def document_label_instruction() -> str:
    return (
        f"On {CREATE_URL} log whether the page shows "
        f"{IMPORT_DOCUMENT_LABEL}, {UPLOAD_DOCUMENT_LABEL}, and/or a "
        "dropzone. Prefer Import document. Do not guess. Dry HITL if unclear."
    )


def role_default_instruction(resolved: str | None) -> str:
    if not resolved:
        return (
            f"On {CREATE_URL} log the prefilled Producer "
            f"({PRODUCER_LOCATOR}) and Account Manager "
            f"({ACCOUNT_MANAGER_LOCATOR}) values. requested_by did not "
            "resolve. HITL in dry English. Do not write Robie AI."
        )
    return (
        f"On {CREATE_URL} log the prefilled Producer and Account Manager "
        f"values, then overwrite both with {resolved} unless they already "
        f"equal {resolved}. FAIL if they stay {ROBIE_AI} when the sender "
        f"is {CARLO_FERRARA} or {JAKE_FERRARA}."
    )


def unclear_document_label_hitl(*, labels: list[str] | None = None) -> str:
    seen = ", ".join(labels or []) or "none"
    return dry_playwright_hitl_text(
        reason=(
            "Create-program document control is unclear. "
            f"Logged labels: {seen}. "
            "Reply whether to use Import document, Upload document, or the dropzone."
        )
    )


def run_spinner_timing_scenario() -> dict[str, Any]:
    """Named scenario: log spinner seconds, click primary, wait_for_url /create/new."""
    errors: list[str] = []
    instruction = programs_spinner_timing_instruction()
    if "seconds" not in instruction.casefold():
        errors.append("instruction missing seconds log")
    if WAIT_FOR_URL not in instruction and CREATE_PATH not in instruction:
        errors.append("instruction missing wait_for_url /create/new")
    if "caret" not in instruction.casefold():
        errors.append("instruction does not refuse the caret")
    if not new_program_click_is_primary(NEW_PROGRAM_LOCATOR):
        errors.append("primary + New program locator was not accepted")
    if new_program_click_is_primary("split-menu caret"):
        errors.append("caret locator was accepted")
    missing = require_spinner_seconds_logged(None)
    if missing is None:
        errors.append("missing seconds was accepted")
    logged = require_spinner_seconds_logged(PAWIVA_SPINNER_SECONDS)
    if logged is not None:
        errors.append(f"12s PAWIVA timing was rejected: {logged}")
    seconds = spinner_seconds(100.0, 112.0)
    if seconds != PAWIVA_SPINNER_SECONDS:
        errors.append(f"spinner_seconds(100, 112) produced {seconds}, expected 12.0")
    if require_spinner_seconds_logged(seconds) is not None:
        errors.append("computed 12s was not accepted")
    if create_url_is_new_program(CREATE_URL) is False:
        errors.append("CREATE_URL was not accepted")
    if require_create_new_url("https://dashboard.useascend.com/programs") is None:
        errors.append("programs URL was accepted as /create/new")
    if require_create_new_url(f"{CREATE_URL}?x=1") is not None:
        errors.append("create/new with query was rejected")
    ok = not errors
    return {
        "id": SPINNER_TIMING_SCENARIO_ID,
        "kind": "logic",
        "ok": ok,
        "outcome": "PASS" if ok else "FAILED",
        "evidence": (
            "log seconds until + New program; click primary not caret; "
            "wait_for_url /create/new; missing seconds is FAIL"
            if ok
            else "; ".join(errors)
        ),
        "finance_agreement": False,
        "observed": {
            "seconds": PAWIVA_SPINNER_SECONDS,
            "wait_for_url": CREATE_PATH,
        },
    }


def run_agency_fee_default_scenario() -> dict[str, Any]:
    """Named scenario: log Agency Fee default, expect $0.00/empty, set 500, stop."""
    errors: list[str] = []
    instruction = agency_fee_instruction()
    if "500" not in instruction:
        errors.append("instruction missing set 500")
    if "$0.00" not in instruction and "empty" not in instruction.casefold():
        errors.append("instruction missing $0.00 / empty expect")
    if "stop" not in instruction.casefold():
        errors.append("instruction missing stop-before-save")
    for default in ("$0.00", "", "0", "0.00", "$0"):
        leak = refuse_unexpected_agency_fee_default(default, field_present=True)
        if leak:
            errors.append(f"{default!r} should be an expected default: {leak}")
    if refuse_unlogged_agency_fee_default(None, field_present=True) is None:
        errors.append("unlogged default was accepted")
    unexpected = refuse_unexpected_agency_fee_default("25.00", field_present=True)
    if unexpected is None:
        errors.append("non-zero default $25.00 was accepted")
    if refuse_unexpected_agency_fee_default(None, field_present=False) is not None:
        errors.append("missing field should not fail the default expect")
    if test_agency_fee_set_value() != TEST_AGENCY_FEE:
        errors.append("Test set value is not 500")
    if not should_set_test_agency_fee(field_present=True):
        errors.append("present field did not request set 500")
    if should_set_test_agency_fee(field_present=False):
        errors.append("missing field still requested set 500")
    for banned in stop_before_save_after_fee():
        if banned.casefold() not in instruction.casefold() and banned not in {
            "Copy checkout",
            "payment",
            "bind",
        }:
            if banned == "Save program" and "save" not in instruction.casefold():
                errors.append(f"instruction missing {banned}")
    if "Save program" not in instruction and "Save" not in instruction:
        errors.append("instruction missing Save program stop")
    labels = classify_document_labels(
        "Import document", "Upload document", "Drop files here"
    )
    if not labels["import_document"] or not labels["upload_document"] or not labels["dropzone"]:
        errors.append(f"document labels were not classified: {labels}")
    if not classify_document_labels("Create a program")["logged"]:
        errors.append("empty document labels were not logged")
    ok = not errors
    return {
        "id": AGENCY_FEE_SCENARIO_ID,
        "kind": "logic",
        "ok": ok,
        "outcome": "PASS" if ok else "FAILED",
        "evidence": (
            "log Agency Fee default ($0.00 / empty); set 500 if the field "
            "exists; stop before Save; also log Import vs Upload vs dropzone"
            if ok
            else "; ".join(errors)
        ),
        "finance_agreement": False,
        "observed": {
            "default": "$0.00",
            "set_value": TEST_AGENCY_FEE,
            "stop_before": list(stop_before_save_after_fee()),
        },
    }


def run_role_default_log_scenario() -> dict[str, Any]:
    """Named scenario expansion: log prefills; FAIL if they stay Robie AI."""
    errors: list[str] = []
    for requested, expected in (
        ("Carlo Ferrara", CARLO_FERRARA),
        ("Jake Ferrara", JAKE_FERRARA),
        ("carlo@streetsmart.insurance", CARLO_FERRARA),
        ("StreetSmartJake", JAKE_FERRARA),
    ):
        logged = log_role_defaults(
            requested_by=requested,
            producer=ROBIE_AI,
            account_manager=ROBIE_AI,
        )
        if logged.get("error") is None:
            errors.append(f"{requested!r} allowed logged Robie AI defaults")
        if logged.get("producer_default") != ROBIE_AI:
            errors.append(f"{requested!r} did not log Producer default")
        overwritten = log_role_defaults(
            requested_by=requested,
            producer=expected,
            account_manager=expected,
        )
        if overwritten.get("error") is not None:
            errors.append(f"{requested!r} rejected correct overwrite: {overwritten['error']}")
        missing = log_role_defaults(
            requested_by=requested,
            producer=None,
            account_manager=None,
        )
        if missing.get("error") is None or missing.get("logged"):
            errors.append(f"{requested!r} accepted unlogged role defaults")
        roles = roles_for_requested_by(requested)
        if roles.get("resolved") != expected:
            errors.append(f"{requested!r} resolved to {roles.get('resolved')!r}")
    ok = not errors
    return {
        "id": ROLES_DEFAULTS_SCENARIO_ID,
        "kind": "logic",
        "ok": ok,
        "outcome": "PASS" if ok else "FAILED",
        "evidence": (
            "log Producer/Account Manager prefills; FAIL if they stay "
            "Robie AI when requested_by is Carlo Ferrara or Jake Ferrara"
            if ok
            else "; ".join(errors)
        ),
        "finance_agreement": False,
    }


def create_program_contract_lines(resolved: str | None) -> list[str]:
    return [
        programs_spinner_timing_instruction(),
        role_default_instruction(resolved),
        document_label_instruction(),
        agency_fee_instruction(),
    ]
