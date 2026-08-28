"""Ascend Producer / Account Manager from the Chat sender (requested_by).

Production-useful. Jobs already store the Google Chat sender as
``payload.requested_by`` (adapter: ``event.source.user_name`` or
``user_id``). This module maps that field to the unique Ascend
listbox option. It does not invent a second identity system.

Carlo rule (2026-08-28): LOGIN is Robie (browser session only). Do not
put Robie AI on Producer or Account Manager. Those fields are whoever
sent the PFA (requested_by / Chat sender). The unique option is the
concatenated Name+email label, not the display name alone — two
``Carlo Ferrara`` rows exist (streetsmart vs ssinj). Name-only is FAIL.
If the sender email is missing from the list, HITL/FAIL that field.
Do not fall back to Robie AI or the other Carlo.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .ascend_customer_type import customer_type_instruction
from .hitl import dry_playwright_hitl_text


CARLO_FERRARA = "Carlo Ferrara"
JAKE_FERRARA = "Jake Ferrara"
ROBIE_AI = "Robie AI"
SSROBIE = "SSRobie"
# Proven 2026-08-28 on hermes-test-01 /create/new (Robie logged in).
# react-select options render as Name + email concatenated. Two Carlo rows.
CARLO_EMAIL = "carlo@streetsmart.insurance"
JAKE_EMAIL = "jake@streetsmart.insurance"
CARLO_SSINJ_EMAIL = "carlo@ssinj.com"
ROBIE_EMAIL = "robie@streetsmart.insurance"
CARLO_OPTION = f"{CARLO_FERRARA} {CARLO_EMAIL}"
JAKE_OPTION = f"{JAKE_FERRARA} {JAKE_EMAIL}"
CARLO_SSINJ_OPTION = f"{CARLO_FERRARA} {CARLO_SSINJ_EMAIL}"
ROBIE_OPTION = f"{ROBIE_AI} {ROBIE_EMAIL}"

ROLES_SCENARIO_ID = "ascend-roles:sender-not-robie-ai"
SPINNER_SCENARIO_ID = "ascend-new-program:wait-spinner"
ACCESSIBLE_NAME_SCENARIO_ID = "ascend-new-program:accessible-name"

PROGRAMS_URL = "https://dashboard.useascend.com/programs"
CREATE_URL = "https://dashboard.useascend.com/create/new"
# Verified 2026-08-28 on hermes-test-01 Test Chrome (Robie logged in,
# dashboard.useascend.com/programs, readyState complete, no spinner).
# Unique primary BUTTON innerText / accessible name is exactly "New program"
# (char codes 78,101,119,32,112,114,111,103,114,97,109). The plus is an
# icon/SVG, not text. No aria-label. Job f7653a85 waited for "+ New program"
# exact and timed out — that locator never matches this page.
NEW_PROGRAM_ACCESSIBLE_NAME = "New program"
NEW_PROGRAM_ACCESSIBLE_NAME_CHAR_CODES = (
    78,
    101,
    119,
    32,
    112,
    114,
    111,
    103,
    114,
    97,
    109,
)
PLUS_PREFIXED_NEW_PROGRAM_NAME = "+ New program"
CARET_ACCESSIBLE_NAME = "Open split button menu"
NEW_PROGRAM_LOCATOR = (
    f'get_by_role("button", name="{NEW_PROGRAM_ACCESSIBLE_NAME}", exact=True)'
)
PLUS_PREFIXED_NEW_PROGRAM_LOCATOR = (
    f'get_by_role("button", name="{PLUS_PREFIXED_NEW_PROGRAM_NAME}", exact=True)'
)
DUMPED_PROGRAMS_PRIMARY_BUTTON = {
    "role": "button",
    "accessible_name": NEW_PROGRAM_ACCESSIBLE_NAME,
    "accessible_name_char_codes": NEW_PROGRAM_ACCESSIBLE_NAME_CHAR_CODES,
    "aria_label": None,
    "visible": True,
    "enabled": True,
    "plus_is_icon": True,
}
PRODUCER_LOCATOR = 'get_by_label("Producer")'
ACCOUNT_MANAGER_LOCATOR = 'get_by_label("Account Manager")'
COMMERCIAL_LOCATOR = 'get_by_role("radio", name="Commercial customer")'
# Verified 2026-08-28 on hermes-test-01 Test Chrome (Robie logged in,
# dashboard.useascend.com/create/new). Unique primary BUTTON accessible
# name is exactly "Import document" (space is ASCII 32). Visible+enabled.
# After wait_for_url /create/new the form is not painted yet — a too-soon
# get_by_role query resolves to 0 elements. Same class as the programs
# spinner. Also present when ready: unique "Upload document", dropzone
# "Upload a file or drag and drop / PDF and DOCX up to 20MB", hidden
# input#file_upload accept pdf/docx.
IMPORT_DOCUMENT_ACCESSIBLE_NAME = "Import document"
IMPORT_DOCUMENT_ACCESSIBLE_NAME_CHAR_CODES = (
    73,
    109,
    112,
    111,
    114,
    116,
    32,
    100,
    111,
    99,
    117,
    109,
    101,
    110,
    116,
)
IMPORT_DOCUMENT_LOCATOR = (
    f'get_by_role("button", name="{IMPORT_DOCUMENT_ACCESSIBLE_NAME}", exact=True)'
)
DUMPED_CREATE_FORM_IMPORT_BUTTON = {
    "role": "button",
    "accessible_name": IMPORT_DOCUMENT_ACCESSIBLE_NAME,
    "accessible_name_char_codes": IMPORT_DOCUMENT_ACCESSIBLE_NAME_CHAR_CODES,
    "visible": True,
    "enabled": True,
}
UPLOAD_DOCUMENT_ACCESSIBLE_NAME = "Upload document"
UPLOAD_DOCUMENT_LOCATOR = (
    f'get_by_role("button", name="{UPLOAD_DOCUMENT_ACCESSIBLE_NAME}", exact=True)'
)
CREATE_FORM_READY_TIMEOUT_MS = 30_000
PROGRAMS_KPI_LOCATOR = 'get_by_text("Programs at risk")'
PROGRAMS_TABLE_LOCATOR = 'get_by_role("table")'
PROGRAMS_READY_TIMEOUT_MS = 30_000

REQUESTED_BY_KEYS = ("requested_by", "sender", "from")
UNKNOWN_SENDER_ALIASES = frozenset(
    {
        "",
        "robie ai",
        "ssrobie",
        "robie",
        "google chat user",
        "unknown",
    }
)
ROBIE_ROLE_MARKERS = ("robie ai", "ssrobie", "robie@")
CARET_MARKERS = (
    "caret",
    "chevron",
    "split-menu",
    "split menu",
    "arrow",
    ".first",
    ".nth(",
    ".last",
    "nth=",
)
ASCEND_MARKERS = (
    "ascend",
    "useascend",
    "new program",
    "create a program",
    "dashboard.useascend.com",
    "producer",
    "account manager",
    "premium finance",
)

_CARLO_EXACT = frozenset(
    {
        "carlo",
        "carlo ferrara",
        "carlo@streetsmart.insurance",
    }
)
_JAKE_EXACT = frozenset(
    {
        "jake",
        "jake ferrara",
        "jake@streetsmart.insurance",
        "streetsmartjake",
    }
)
_EMAIL_LOCAL = re.compile(r"^([^@]+)@")
_EMAIL_IN_TEXT = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"
)
_WORD = re.compile(r"[a-z0-9]+")


def requested_by_from_payload(payload: dict[str, Any] | None) -> str:
    """Use the existing Chat sender field. Do not invent another identity."""
    blob = dict(payload or {})
    for key in REQUESTED_BY_KEYS:
        value = str(blob.get(key) or "").strip()
        if value:
            return value
    return ""


def requested_by_from_job(job: dict[str, Any] | None) -> str:
    job = dict(job or {})
    payload = dict(job.get("payload") or {})
    return requested_by_from_payload(payload) or str(job.get("requested_by") or "").strip()


def _normalized(value: str) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _identity_keys(value: str) -> set[str]:
    folded = _normalized(value)
    if not folded:
        return set()
    keys = {folded}
    email = _EMAIL_LOCAL.match(folded)
    if email:
        keys.add(email.group(1))
        keys.add(folded)
    keys.update(_WORD.findall(folded.replace("@", " ")))
    return {item for item in keys if item}


def is_unknown_or_robie_sender(value: str) -> bool:
    folded = _normalized(value)
    if folded in UNKNOWN_SENDER_ALIASES:
        return True
    if folded.startswith("robie@"):
        return True
    if "ssrobie" in folded:
        return True
    if folded.startswith("users/"):
        return True
    return False


def is_robie_ai_role(value: str) -> bool:
    folded = _normalized(value)
    return any(marker in folded for marker in ROBIE_ROLE_MARKERS)


def extract_email(value: str) -> str:
    match = _EMAIL_IN_TEXT.search(str(value or "").strip())
    return match.group(0) if match else ""


def canonical_email_for_agent(resolved: str | None) -> str:
    folded = _normalized(str(resolved or ""))
    if folded == _normalized(CARLO_FERRARA):
        return CARLO_EMAIL
    if folded == _normalized(JAKE_FERRARA):
        return JAKE_EMAIL
    return ""


def role_option_label(*, resolved: str | None = None, requested_by: str = "") -> str:
    """Exact visible Producer / AM option: ``Name email``, never name-only.

    Name-only Carlo / Jake map to the streetsmart address. If requested_by
    already carries an email, use that email so a missing streetsmart row
    does not silently select carlo@ssinj.com or Robie AI.
    """
    agent = resolved if resolved is not None else resolve_sender_agent(requested_by)
    if not agent:
        return ""
    email = extract_email(requested_by) or canonical_email_for_agent(agent)
    if not email:
        return ""
    return f"{agent} {email}"


def role_value_matches_unique_option(
    value: str,
    *,
    option: str,
    email: str = "",
) -> bool:
    folded = _normalized(value)
    if not folded:
        return False
    if option and folded == _normalized(option):
        return True
    if email and email.casefold() in folded:
        return True
    return False


def resolve_sender_agent(*values: Any) -> str | None:
    """Map requested_by to an Ascend display name, or None if unknown.

    Carlo / carlo@ / Carlo Ferrara → Carlo Ferrara
    Jake / jake@ / StreetSmartJake → Jake Ferrara
    Robie AI / SSRobie / empty / Google Chat user → None (HITL, do not write Robie AI)

    The unique listbox option is ``role_option_label``, not this display name.
    """
    for raw in values:
        text = str(raw or "").strip()
        if not text or is_unknown_or_robie_sender(text):
            continue
        keys = _identity_keys(text)
        if keys & _CARLO_EXACT or "carlo ferrara" in _normalized(text):
            return CARLO_FERRARA
        if keys & _JAKE_EXACT or "jake ferrara" in _normalized(text):
            return JAKE_FERRARA
        if text.casefold().endswith("@streetsmart.insurance"):
            local = text.split("@", 1)[0].casefold()
            if local == "carlo":
                return CARLO_FERRARA
            if local == "jake":
                return JAKE_FERRARA
    return None


def unknown_sender_hitl(*, requested_by: str = "") -> str:
    """Dry HITL. Do not guess. Do not write Robie AI."""
    who = str(requested_by or "").strip() or "empty"
    return dry_playwright_hitl_text(
        reason=(
            "Producer and Account Manager cannot be set. "
            f"requested_by {who!r} did not resolve to Carlo Ferrara or Jake Ferrara. "
            "Do not write Robie AI. Reply with the unique Name+email option "
            f"({CARLO_OPTION} or {JAKE_OPTION})."
        )
    )


def roles_for_requested_by(*values: Any) -> dict[str, Any]:
    requested = ""
    for raw in values:
        text = str(raw or "").strip()
        if text:
            requested = text
            break
    resolved = resolve_sender_agent(*values)
    if resolved is None:
        return {
            "requested_by": requested,
            "resolved": None,
            "email": None,
            "option": None,
            "producer": None,
            "account_manager": None,
            "hitl_required": True,
            "hitl_text": unknown_sender_hitl(requested_by=requested),
            "write_robie_ai": False,
        }
    option = role_option_label(resolved=resolved, requested_by=requested)
    return {
        "requested_by": requested,
        "resolved": resolved,
        "email": extract_email(option) or None,
        "option": option,
        "producer": option,
        "account_manager": option,
        "hitl_required": False,
        "hitl_text": "",
        "write_robie_ai": False,
    }


def should_overwrite_role(current: str, resolved: str) -> bool:
    """Leave the field only if it already equals the resolved sender name."""
    return _normalized(current) != _normalized(resolved)


def refuse_robie_ai_when_sender_known(
    *,
    requested_by: str,
    producer: str,
    account_manager: str,
) -> str | None:
    """FAIL if roles stay Robie AI when requested_by is Jake or Carlo."""
    resolved = resolve_sender_agent(requested_by)
    if resolved is None:
        if is_robie_ai_role(producer) or is_robie_ai_role(account_manager):
            return (
                "unknown requested_by must HITL; do not write Robie AI on "
                "Producer or Account Manager"
            )
        return None
    option = role_option_label(resolved=resolved, requested_by=requested_by)
    email = extract_email(option)
    if is_robie_ai_role(producer) or is_robie_ai_role(account_manager):
        return (
            f"roles stayed {ROBIE_AI} when requested_by resolved to {option}; "
            f"scenario {ROLES_SCENARIO_ID} FAIL"
        )
    if not role_value_matches_unique_option(producer, option=option, email=email):
        return f"Producer {producer!r} is not {option}"
    if not role_value_matches_unique_option(
        account_manager, option=option, email=email
    ):
        return f"Account Manager {account_manager!r} is not {option}"
    return None


def locator_is_new_program_caret(locator: str) -> bool:
    folded = str(locator or "").casefold()
    if not folded:
        return False
    return any(marker in folded for marker in CARET_MARKERS)


def playwright_exact_name_matches(locator_name: str, accessible_name: str) -> bool:
    """Playwright get_by_role(..., name=..., exact=True) string equality."""
    return str(locator_name) == str(accessible_name)


def plus_prefixed_exact_locator_matches_dump(
    accessible_name: str | None = None,
) -> bool:
    """The old plus-exact locator never matches the dumped programs primary."""
    dumped = (
        accessible_name
        if accessible_name is not None
        else str(DUMPED_PROGRAMS_PRIMARY_BUTTON["accessible_name"])
    )
    return playwright_exact_name_matches(PLUS_PREFIXED_NEW_PROGRAM_NAME, dumped)


def new_program_locator_is_unique_primary(locator: str) -> bool:
    folded = str(locator or "")
    if locator_is_new_program_caret(folded):
        return False
    if PLUS_PREFIXED_NEW_PROGRAM_NAME in folded:
        return False
    return (
        f'name="{NEW_PROGRAM_ACCESSIBLE_NAME}"' in folded
        and "exact=True" in folded.replace(" ", "")
    )


def programs_spinner_timeout_error(detail: str = "") -> str:
    extra = f": {detail}" if detail else ""
    return (
        "PLAYWRIGHT_BLOCKED: programs page spinner or New program "
        f"not ready{extra}. Do not click a nearby control. No Gemini."
    )


def create_form_timeout_error(detail: str = "") -> str:
    extra = f": {detail}" if detail else ""
    return (
        "PLAYWRIGHT_BLOCKED: /create/new form or Import document "
        f"not ready{extra}. Do not click a nearby control. No Gemini."
    )


def import_document_locator_is_unique_primary(locator: str) -> bool:
    folded = str(locator or "")
    if locator_is_new_program_caret(folded):
        return False
    return (
        f'name="{IMPORT_DOCUMENT_ACCESSIBLE_NAME}"' in folded
        and "exact=True" in folded.replace(" ", "")
    )


def programs_page_ready_instruction() -> str:
    return (
        "On https://dashboard.useascend.com/programs do not click New program "
        "until the unique primary button is visible and enabled AND the programs "
        "table or KPI cards (Programs at risk) are present — the page can hang "
        f"on a spinner ~12–20 seconds. Log those seconds. Timeout "
        f"{PROGRAMS_READY_TIMEOUT_MS}ms. Locator: {NEW_PROGRAM_LOCATOR}. "
        "The plus is an icon/SVG, not text. Accessible name is exactly "
        f"{NEW_PROGRAM_ACCESSIBLE_NAME!r}. Never the split-menu caret "
        f"({CARET_ACCESSIBLE_NAME!r}). After click, wait_for_url /create/new. "
        "The create form is not instant after that URL — wait until the unique "
        f"exact {IMPORT_DOCUMENT_ACCESSIBLE_NAME!r} primary is visible and "
        "enabled, and log those seconds. A too-soon 0-element lookup is FAIL. "
        "If spinner or button is not ready past timeout: "
        f"{programs_spinner_timeout_error()} then HITL. "
        "No Gemini. No .first/.nth/.last."
    )


def ascend_role_overwrite_instruction(
    resolved: str | None,
    option: str | None = None,
) -> str:
    target = str(option or "").strip() or (
        role_option_label(resolved=resolved) if resolved else ""
    )
    if not target:
        return (
            "After Create a program loads, do not leave Producer or Account "
            "Manager as Robie AI / SSRobie. requested_by did not resolve. "
            "HITL in dry English. Do not guess. Do not write Robie AI."
        )
    return (
        f"After Create a program loads, overwrite Producer and Account Manager "
        f"with {target} (unique locators {PRODUCER_LOCATOR} and "
        f"{ACCOUNT_MANAGER_LOCATOR}; no .first/.nth/.last). The unique option "
        "is the concatenated Name+email label, not the display name alone "
        f"(two {CARLO_FERRARA} rows exist). Leave them only if they already "
        f"equal {target}."
    )


def _looks_like_ascend(text: str, payload: dict[str, Any] | None = None) -> bool:
    blob = " ".join(
        [
            str(text or ""),
            str((payload or {}).get("text") or ""),
            str((payload or {}).get("programs_url") or ""),
            str((payload or {}).get("scenario") or ""),
            str((payload or {}).get("action_type") or ""),
        ]
    ).casefold()
    return any(marker in blob for marker in ASCEND_MARKERS)


def ascend_new_program_contract_lines(
    text: str,
    payload: dict[str, Any] | None = None,
) -> list[str]:
    """Injected into Chat jobs that touch Ascend. Zip PYTHONPATH, like EZLynx nav."""
    payload = dict(payload or {})
    if not _looks_like_ascend(text, payload) and not str(
        payload.get("action_type") or ""
    ).startswith("ascend."):
        return []
    requested = requested_by_from_payload(payload)
    roles = roles_for_requested_by(requested)
    from .ascend_create_defaults import create_program_contract_lines

    lines = [
        programs_page_ready_instruction(),
        ascend_role_overwrite_instruction(
            roles.get("resolved"), option=roles.get("option")
        ),
        customer_type_instruction(payload),
        (
            "Ascend Import document only (that panel has no Hawksoft/AMS360/Epic). "
            "After wait_for_url /create/new wait until the unique exact "
            f"{IMPORT_DOCUMENT_LOCATOR} is visible — the form is not instant. "
            "Log those seconds. A too-soon 0-element lookup is FAIL. "
            "Log Import document vs Upload document vs dropzone. "
            "Stop before Save program, Send email, Copy checkout, payment, or bind."
        ),
    ]
    lines.extend(
        create_program_contract_lines(roles.get("option") or roles.get("resolved"))
    )
    if roles.get("hitl_required"):
        lines.append(roles["hitl_text"])
    return lines


def run_sender_not_robie_ai_scenario() -> dict[str, Any]:
    """Named scenario: roles stay Robie AI when requested_by is Jake/Carlo → FAIL."""
    cases = (
        ("Jake", JAKE_FERRARA, JAKE_OPTION),
        ("jake@streetsmart.insurance", JAKE_FERRARA, JAKE_OPTION),
        ("StreetSmartJake", JAKE_FERRARA, JAKE_OPTION),
        ("Jake Ferrara", JAKE_FERRARA, JAKE_OPTION),
        ("Carlo", CARLO_FERRARA, CARLO_OPTION),
        ("carlo@streetsmart.insurance", CARLO_FERRARA, CARLO_OPTION),
        ("Carlo Ferrara", CARLO_FERRARA, CARLO_OPTION),
    )
    errors: list[str] = []
    for requested, expected, option in cases:
        roles = roles_for_requested_by(requested)
        if roles.get("resolved") != expected:
            errors.append(f"{requested!r} resolved to {roles.get('resolved')!r}")
            continue
        if roles.get("option") != option or roles.get("producer") != option:
            errors.append(
                f"{requested!r} option {roles.get('option')!r} is not {option!r}"
            )
            continue
        leak = refuse_robie_ai_when_sender_known(
            requested_by=requested,
            producer=ROBIE_AI,
            account_manager=ROBIE_AI,
        )
        if leak is None:
            errors.append(f"{requested!r} allowed Robie AI roles")
        name_only = refuse_robie_ai_when_sender_known(
            requested_by=requested,
            producer=expected,
            account_manager=expected,
        )
        if name_only is None:
            errors.append(f"{requested!r} accepted name-only {expected}")
        ok = refuse_robie_ai_when_sender_known(
            requested_by=requested,
            producer=option,
            account_manager=option,
        )
        if ok is not None:
            errors.append(f"{requested!r} rejected correct {option}: {ok}")
        from .ascend_create_defaults import log_role_defaults

        logged = log_role_defaults(
            requested_by=requested,
            producer=ROBIE_AI,
            account_manager=ROBIE_AI,
        )
        if logged.get("producer_default") != ROBIE_AI or not logged.get("logged"):
            errors.append(f"{requested!r} did not log Robie AI prefills")
        if logged.get("error") is None:
            errors.append(f"{requested!r} allowed leaving logged Robie AI prefills")
    for unknown in ("Robie AI", "SSRobie", "robie@streetsmart.insurance", "", "Google Chat user"):
        roles = roles_for_requested_by(unknown)
        if roles.get("resolved") is not None or not roles.get("hitl_required"):
            errors.append(f"{unknown!r} should HITL and not resolve")
        if roles.get("producer") == ROBIE_AI or roles.get("write_robie_ai"):
            errors.append(f"{unknown!r} wrote Robie AI")
        leak = refuse_robie_ai_when_sender_known(
            requested_by=unknown,
            producer=ROBIE_AI,
            account_manager=SSROBIE,
        )
        if leak is None:
            errors.append(f"{unknown!r} allowed writing Robie AI")
    ok = not errors
    return {
        "id": ROLES_SCENARIO_ID,
        "kind": "logic",
        "ok": ok,
        "outcome": "PASS" if ok else "FAILED",
        "evidence": (
            "Jake/Carlo requested_by overwrite Producer and Account Manager; "
            "log the Robie AI prefill; FAIL if it stays; unknown sender HITL "
            "and never writes Robie AI"
            if ok
            else "; ".join(errors)
        ),
        "finance_agreement": False,
    }


def run_wait_spinner_scenario() -> dict[str, Any]:
    """Named scenario: wait out programs spinner; unique New program only."""
    instruction = programs_page_ready_instruction()
    errors: list[str] = []
    if "spinner" not in instruction.casefold():
        errors.append("instruction missing spinner wait")
    if NEW_PROGRAM_LOCATOR not in instruction:
        errors.append("instruction missing unique New program locator")
    if PLUS_PREFIXED_NEW_PROGRAM_NAME in NEW_PROGRAM_LOCATOR:
        errors.append("live locator still uses plus-prefixed name")
    if "caret" not in instruction.casefold():
        errors.append("instruction does not refuse the caret")
    if "gemini" not in instruction.casefold():
        errors.append("instruction missing no-Gemini")
    if "PLAYWRIGHT_BLOCKED" not in instruction:
        errors.append("timeout is not PLAYWRIGHT_BLOCKED")
    if locator_is_new_program_caret('page.locator("button").first'):
        pass
    else:
        errors.append("caret/.first locator was not refused")
    if locator_is_new_program_caret("split-menu caret"):
        pass
    else:
        errors.append("split-menu caret was not refused")
    if new_program_locator_is_unique_primary(NEW_PROGRAM_LOCATOR):
        pass
    else:
        errors.append("primary New program locator was not accepted")
    if new_program_locator_is_unique_primary(PLUS_PREFIXED_NEW_PROGRAM_LOCATOR):
        errors.append("plus-prefixed locator was accepted as the unique primary")
    timeout = programs_spinner_timeout_error("TimeoutError after 30000ms")
    if not timeout.startswith("PLAYWRIGHT_BLOCKED"):
        errors.append("spinner timeout is not PLAYWRIGHT_BLOCKED")
    if "gemini" not in timeout.casefold():
        errors.append("timeout error missing no-Gemini")
    ok = not errors
    return {
        "id": SPINNER_SCENARIO_ID,
        "kind": "logic",
        "ok": ok,
        "outcome": "PASS" if ok else "FAILED",
        "evidence": (
            "wait spinner then unique New program; timeout is PLAYWRIGHT_BLOCKED"
            if ok
            else "; ".join(errors)
        ),
        "finance_agreement": False,
    }


def run_accessible_name_scenario() -> dict[str, Any]:
    """Named scenario: plus-exact locator must FAIL against the dumped name.

    Live job f7653a85 waited 30s for get_by_role(button, name="+ New program",
    exact=True). The dumped accessible name is exactly "New program".
    Putting the plus back must FAIL this class in CI.
    """
    from .ascend_locator_audit import FLOW_STEPS

    errors: list[str] = []
    dumped = str(DUMPED_PROGRAMS_PRIMARY_BUTTON["accessible_name"])
    dumped_codes = tuple(ord(char) for char in dumped)
    expected_codes = tuple(
        DUMPED_PROGRAMS_PRIMARY_BUTTON["accessible_name_char_codes"]
    )
    if dumped != NEW_PROGRAM_ACCESSIBLE_NAME:
        errors.append(f"dumped accessible name drifted: {dumped!r}")
    if dumped_codes != expected_codes:
        errors.append(f"dumped name char codes drifted: {dumped_codes}")
    if dumped_codes != NEW_PROGRAM_ACCESSIBLE_NAME_CHAR_CODES:
        errors.append("NEW_PROGRAM_ACCESSIBLE_NAME char codes drifted")
    if "+" in dumped:
        errors.append("dumped accessible name contains a plus")
    if DUMPED_PROGRAMS_PRIMARY_BUTTON.get("aria_label") is not None:
        errors.append("dumped primary unexpectedly has an aria-label")
    if plus_prefixed_exact_locator_matches_dump(dumped):
        errors.append("plus-exact locator unexpectedly matched the dumped name")
    if not playwright_exact_name_matches(NEW_PROGRAM_ACCESSIBLE_NAME, dumped):
        errors.append("unique New program name did not match the dump")
    if PLUS_PREFIXED_NEW_PROGRAM_NAME in NEW_PROGRAM_LOCATOR:
        errors.append("NEW_PROGRAM_LOCATOR still requires the plus prefix")
    if not new_program_locator_is_unique_primary(NEW_PROGRAM_LOCATOR):
        errors.append("current NEW_PROGRAM_LOCATOR is not the unique primary")
    if new_program_locator_is_unique_primary(PLUS_PREFIXED_NEW_PROGRAM_LOCATOR):
        errors.append("plus-prefixed locator was accepted as unique primary")
    new_program_step = next(
        (item for item in FLOW_STEPS if item.get("id") == "new_program"),
        None,
    )
    if new_program_step is None:
        errors.append("FLOW_STEPS missing new_program")
    else:
        step_locator = str(new_program_step.get("locator") or "")
        if step_locator != NEW_PROGRAM_LOCATOR:
            errors.append(
                f"FLOW_STEPS new_program locator drifted: {step_locator!r}"
            )
        if PLUS_PREFIXED_NEW_PROGRAM_NAME in step_locator:
            errors.append("FLOW_STEPS new_program still uses plus-prefixed name")
    runner = Path(__file__).with_name("ascend_locator_audit_runner.py")
    runner_src = runner.read_text(encoding="utf-8")
    if 'name="+ New program"' in runner_src:
        errors.append("live runner still waits for plus-prefixed name")
    if "NEW_PROGRAM_ACCESSIBLE_NAME" not in runner_src:
        errors.append("live runner does not use NEW_PROGRAM_ACCESSIBLE_NAME")
    ok = not errors
    return {
        "id": ACCESSIBLE_NAME_SCENARIO_ID,
        "kind": "logic",
        "ok": ok,
        "outcome": "PASS" if ok else "FAILED",
        "evidence": (
            "plus-exact locator fails against dumped accessible name "
            f"{NEW_PROGRAM_ACCESSIBLE_NAME!r}; live locator is unique exact "
            "New program; caret stays out"
            if ok
            else "; ".join(errors)
        ),
        "finance_agreement": False,
        "observed": {
            "accessible_name": dumped,
            "plus_exact_matches_dump": False,
            "caret": CARET_ACCESSIBLE_NAME,
        },
    }
