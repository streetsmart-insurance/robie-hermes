"""Ascend Producer / Account Manager from the Chat sender (requested_by).

Production-useful. Jobs already store the Google Chat sender as
``payload.requested_by`` (adapter: ``event.source.user_name`` or
``user_id``). This module maps that field to the Ascend display name.
It does not invent a second identity system.

Carlo rule: default Producer AND Account Manager to the agent who SENT
the job. Never leave Robie AI / SSRobie when requested_by is known.
Unknown sender → dry HITL. Do not guess.
"""

from __future__ import annotations

import re
from typing import Any

from .ascend_customer_type import customer_type_instruction
from .hitl import dry_playwright_hitl_text


CARLO_FERRARA = "Carlo Ferrara"
JAKE_FERRARA = "Jake Ferrara"
ROBIE_AI = "Robie AI"
SSROBIE = "SSRobie"

ROLES_SCENARIO_ID = "ascend-roles:sender-not-robie-ai"
SPINNER_SCENARIO_ID = "ascend-new-program:wait-spinner"

PROGRAMS_URL = "https://dashboard.useascend.com/programs"
CREATE_URL = "https://dashboard.useascend.com/create/new"
NEW_PROGRAM_LOCATOR = 'get_by_role("button", name="+ New program", exact=True)'
PRODUCER_LOCATOR = 'get_by_label("Producer")'
ACCOUNT_MANAGER_LOCATOR = 'get_by_label("Account Manager")'
COMMERCIAL_LOCATOR = 'get_by_role("radio", name="Commercial customer")'
IMPORT_DOCUMENT_LOCATOR = 'get_by_role("button", name="Import document")'
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


def resolve_sender_agent(*values: Any) -> str | None:
    """Map requested_by to an Ascend display name, or None if unknown.

    Carlo / carlo@ / Carlo Ferrara → Carlo Ferrara
    Jake / jake@ / StreetSmartJake → Jake Ferrara
    Robie AI / SSRobie / empty / Google Chat user → None (HITL, do not write Robie AI)
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
            "Do not write Robie AI. Reply with the agent display name."
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
            "producer": None,
            "account_manager": None,
            "hitl_required": True,
            "hitl_text": unknown_sender_hitl(requested_by=requested),
            "write_robie_ai": False,
        }
    return {
        "requested_by": requested,
        "resolved": resolved,
        "producer": resolved,
        "account_manager": resolved,
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
    if is_robie_ai_role(producer) or is_robie_ai_role(account_manager):
        return (
            f"roles stayed {ROBIE_AI} when requested_by resolved to {resolved}; "
            f"scenario {ROLES_SCENARIO_ID} FAIL"
        )
    if _normalized(producer) != _normalized(resolved):
        return f"Producer {producer!r} is not {resolved}"
    if _normalized(account_manager) != _normalized(resolved):
        return f"Account Manager {account_manager!r} is not {resolved}"
    return None


def locator_is_new_program_caret(locator: str) -> bool:
    folded = str(locator or "").casefold()
    if not folded:
        return False
    return any(marker in folded for marker in CARET_MARKERS)


def new_program_locator_is_unique_primary(locator: str) -> bool:
    folded = str(locator or "")
    if locator_is_new_program_caret(folded):
        return False
    return "+ New program" in folded and "exact=True" in folded.replace(" ", "")


def programs_spinner_timeout_error(detail: str = "") -> str:
    extra = f": {detail}" if detail else ""
    return (
        "PLAYWRIGHT_BLOCKED: programs page spinner or + New program "
        f"not ready{extra}. Do not click a nearby control. No Gemini."
    )


def programs_page_ready_instruction() -> str:
    return (
        "On https://dashboard.useascend.com/programs do not click + New program "
        "until the unique primary button is visible and enabled AND the programs "
        "table or KPI cards (Programs at risk) are present — the page can hang "
        f"on a spinner ~12–20 seconds. Log those seconds. Timeout "
        f"{PROGRAMS_READY_TIMEOUT_MS}ms. Locator: {NEW_PROGRAM_LOCATOR}. "
        "Never the split-menu caret. After click, wait_for_url /create/new. "
        "If spinner or button is not ready past timeout: "
        f"{programs_spinner_timeout_error()} then HITL. "
        "No Gemini. No .first/.nth/.last."
    )


def ascend_role_overwrite_instruction(resolved: str | None) -> str:
    if not resolved:
        return (
            "After Create a program loads, do not leave Producer or Account "
            "Manager as Robie AI / SSRobie. requested_by did not resolve. "
            "HITL in dry English. Do not guess."
        )
    return (
        f"After Create a program loads, overwrite Producer and Account Manager "
        f"with {resolved} (unique locators {PRODUCER_LOCATOR} and "
        f"{ACCOUNT_MANAGER_LOCATOR}; no .first/.nth/.last). Leave them only if "
        f"they already equal {resolved}."
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
        ascend_role_overwrite_instruction(roles.get("resolved")),
        customer_type_instruction(payload),
        (
            "Ascend Import document only (that panel has no Hawksoft/AMS360/Epic). "
            "Log Import document vs Upload document vs dropzone. "
            "Stop before Save program, Send email, Copy checkout, payment, or bind."
        ),
    ]
    lines.extend(create_program_contract_lines(roles.get("resolved")))
    if roles.get("hitl_required"):
        lines.append(roles["hitl_text"])
    return lines


def run_sender_not_robie_ai_scenario() -> dict[str, Any]:
    """Named scenario: roles stay Robie AI when requested_by is Jake/Carlo → FAIL."""
    cases = (
        ("Jake", JAKE_FERRARA),
        ("jake@streetsmart.insurance", JAKE_FERRARA),
        ("StreetSmartJake", JAKE_FERRARA),
        ("Jake Ferrara", JAKE_FERRARA),
        ("Carlo", CARLO_FERRARA),
        ("carlo@streetsmart.insurance", CARLO_FERRARA),
        ("Carlo Ferrara", CARLO_FERRARA),
    )
    errors: list[str] = []
    for requested, expected in cases:
        roles = roles_for_requested_by(requested)
        if roles.get("resolved") != expected:
            errors.append(f"{requested!r} resolved to {roles.get('resolved')!r}")
            continue
        leak = refuse_robie_ai_when_sender_known(
            requested_by=requested,
            producer=ROBIE_AI,
            account_manager=ROBIE_AI,
        )
        if leak is None:
            errors.append(f"{requested!r} allowed Robie AI roles")
        ok = refuse_robie_ai_when_sender_known(
            requested_by=requested,
            producer=expected,
            account_manager=expected,
        )
        if ok is not None:
            errors.append(f"{requested!r} rejected correct {expected}: {ok}")
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
    """Named scenario: wait out programs spinner; unique + New program only."""
    instruction = programs_page_ready_instruction()
    errors: list[str] = []
    if "spinner" not in instruction.casefold():
        errors.append("instruction missing spinner wait")
    if NEW_PROGRAM_LOCATOR not in instruction:
        errors.append("instruction missing unique + New program locator")
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
        errors.append("primary + New program locator was not accepted")
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
            "wait spinner then unique + New program; timeout is PLAYWRIGHT_BLOCKED"
            if ok
            else "; ".join(errors)
        ),
        "finance_agreement": False,
    }
