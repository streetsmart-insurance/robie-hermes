"""Unique listbox options on Ascend /create/new (job 38c0fa79).

Live Production job 38c0fa79 HITL'd PLAYWRIGHT_BLOCKED at 12:18Z on
hermes-poc-01: create/new dropdown options were not unique (multiple
matching options / selector not unique for the active listbox). Still
on /create/new. No Save. Carlo will not RETRY that live job.

The Test pass must open each create-form combobox the job would use,
assert the visible list has a unique locator for the intended option,
and FAIL or dry-HITL if two options match the same selector. Log the
blocked field. Test account only. Never PAWIVA. Stop before Save.
"""

from __future__ import annotations

from typing import Any

from .ascend_sender_roles import (
    CARLO_FERRARA,
    CARLO_OPTION,
    CARLO_SSINJ_OPTION,
    JAKE_OPTION,
    ROBIE_OPTION,
    extract_email,
    role_option_label,
    roles_for_requested_by,
)
from .hitl import dry_playwright_hitl_text


COMBOBOX_SCENARIO_ID = "ascend-create:unique-listbox-option"
CREATE_PATH = "/create/new"

COMBOBOX_FIELDS: tuple[dict[str, Any], ...] = (
    {
        "id": "producer",
        "label": "Producer",
        "aliases": ("Producer",),
        "payload_keys": ("producer", "test_producer"),
        "role_key": "producer",
    },
    {
        "id": "account_manager",
        "label": "Account Manager",
        "aliases": ("Account Manager",),
        "payload_keys": ("account_manager", "test_account_manager"),
        "role_key": "account_manager",
    },
    {
        "id": "carrier",
        "label": "Carrier",
        "aliases": ("Carrier", "Writing company", "Writing Company"),
        "payload_keys": ("test_carrier", "carrier", "writing_company"),
    },
    {
        "id": "coverage_type",
        "label": "Coverage type",
        "aliases": ("Coverage type", "Coverage Type"),
        "payload_keys": ("test_coverage", "coverage_type", "coverage"),
    },
    {
        "id": "state",
        "label": "State",
        "aliases": ("State",),
        "payload_keys": ("test_state", "state"),
    },
    {
        "id": "wholesaler",
        "label": "Wholesaler",
        "aliases": ("Wholesaler",),
        "payload_keys": ("test_wholesaler", "wholesaler"),
    },
)

COMBOBOX_CHAT = (
    "ASCEND create/new listbox class returned (38c0fa79): open each "
    "create-form combobox; the intended option needs a unique locator "
    "in the visible list. Producer / Account Manager unique option is "
    "the concatenated Name+email label, not the display name alone "
    f"({CARLO_OPTION}). Two options matching the same selector is "
    "PLAYWRIGHT_BLOCKED. Log the blocked field. Dry HITL if unclear. "
    "No Save / email / bind. No PAWIVA."
)

# 38c0fa79 class: non-exact name="Progressive" also matches "Progressive Specialty".
DUPLICATE_PREFIX_FIXTURE = {
    "field": "Carrier",
    "intended": "Progressive",
    "options": ("Progressive", "Progressive Specialty"),
    "exact": False,
}

# Proven 2026-08-28 on hermes-test-01 /create/new (Robie logged in).
# Two Carlo Ferrara rows. Name-only is FAIL. Never pick Robie AI or ssinj
# when requested_by is carlo@streetsmart.insurance.
LIVE_ROLE_LISTBOX_OPTIONS = (
    CARLO_OPTION,
    CARLO_SSINJ_OPTION,
    JAKE_OPTION,
    ROBIE_OPTION,
)


def option_locator(name: str, *, exact: bool = True) -> str:
    flag = ", exact=True" if exact else ""
    return f'get_by_role("option", name="{name}"{flag})'


def _folded_option(value: str) -> str:
    return " ".join(str(value or "").split()).casefold()


def option_matches(option: str, intended: str, *, exact: bool) -> bool:
    left = _folded_option(option)
    right = _folded_option(intended)
    if not left or not right:
        return False
    if exact:
        return left == right
    return right in left


def matching_option_count(
    options: list[str] | tuple[str, ...],
    intended: str,
    *,
    exact: bool = True,
) -> int:
    return sum(1 for item in options if option_matches(item, intended, exact=exact))


def intended_option_for_field(field: dict[str, Any], payload: dict[str, Any] | None) -> str:
    blob = dict(payload or {})
    for key in field.get("payload_keys") or ():
        value = str(blob.get(key) or "").strip()
        if value:
            return value
    role_key = str(field.get("role_key") or "")
    if role_key:
        value = str(blob.get(role_key) or "").strip()
        if value:
            return value
    return ""


def classify_listbox_options(
    *,
    field: str,
    intended: str,
    options: list[str] | tuple[str, ...],
    exact: bool = True,
    field_present: bool = True,
) -> dict[str, Any]:
    """Unique intended option or FAIL / dry HITL. Always log the field."""
    locator = option_locator(intended, exact=exact) if intended else ""
    if not field_present:
        return {
            "field": field,
            "intended": intended,
            "options": list(options),
            "match_count": 0,
            "locator": locator,
            "status": "SKIP",
            "blocked_field": None,
            "error": None,
            "hitl_required": False,
            "hitl_text": "",
        }
    if not intended:
        text = unique_option_hitl(field=field, intended="", match_count=0)
        return {
            "field": field,
            "intended": intended,
            "options": list(options),
            "match_count": 0,
            "locator": locator,
            "status": "HITL",
            "blocked_field": field,
            "error": text,
            "hitl_required": True,
            "hitl_text": text,
        }
    count = matching_option_count(options, intended, exact=exact)
    matched = next(
        (item for item in options if option_matches(item, intended, exact=exact)),
        "",
    )
    intended_email = extract_email(intended)
    if count != 1 and exact and intended and not intended_email:
        # Name-only "Carlo Ferrara" vs two Name+email rows (38c0fa79 class).
        loose = matching_option_count(options, intended, exact=False)
        if loose >= 2:
            count = loose
    elif count != 1 and exact and intended_email:
        email_hits = [
            item
            for item in options
            if intended_email.casefold() in _folded_option(item)
        ]
        if len(email_hits) == 1:
            matched = email_hits[0]
            intended = matched
            locator = option_locator(intended, exact=True)
            count = 1
        else:
            count = len(email_hits)
    if count == 1:
        return {
            "field": field,
            "intended": intended,
            "matched": matched or intended,
            "options": list(options),
            "match_count": 1,
            "locator": locator,
            "status": "PASS",
            "blocked_field": None,
            "error": None,
            "hitl_required": False,
            "hitl_text": "",
        }
    reason = unique_option_block_reason(
        field=field, intended=intended, match_count=count, locator=locator
    )
    hitl = count == 0
    return {
        "field": field,
        "intended": intended,
        "options": list(options),
        "match_count": count,
        "locator": locator,
        "status": "HITL" if hitl else "FAIL",
        "blocked_field": field,
        "error": reason,
        "hitl_required": hitl,
        "hitl_text": unique_option_hitl(
            field=field, intended=intended, match_count=count
        )
        if hitl
        else "",
    }


def unique_option_block_reason(
    *,
    field: str,
    intended: str,
    match_count: int,
    locator: str = "",
) -> str:
    where = f" ({locator})" if locator else ""
    if match_count <= 0:
        return (
            f"PLAYWRIGHT_BLOCKED: create/new listbox {field!r} has no option "
            f"for {intended!r}{where}. Logged blocked field: {field}."
        )
    return (
        f"PLAYWRIGHT_BLOCKED: create/new listbox {field!r} selector is not "
        f"unique for the active listbox ({match_count} matching options for "
        f"{intended!r}){where}. Logged blocked field: {field}."
    )


def unique_option_hitl(*, field: str, intended: str, match_count: int) -> str:
    detail = unique_option_block_reason(
        field=field, intended=intended, match_count=match_count
    )
    return dry_playwright_hitl_text(
        reason=(
            f"{detail} Do not guess. Do not use .first/.nth/.last. "
            "Reply with the exact visible option for this Test account."
        )
    )


def refuse_non_unique_listbox(
    *,
    field: str,
    intended: str,
    options: list[str] | tuple[str, ...],
    exact: bool = True,
    field_present: bool = True,
) -> str | None:
    report = classify_listbox_options(
        field=field,
        intended=intended,
        options=options,
        exact=exact,
        field_present=field_present,
    )
    if report["status"] in {"FAIL", "HITL"}:
        return str(report.get("error") or report.get("hitl_text") or "")
    return None


def default_test_combobox_payload(*, requested_by: str = "Carlo Ferrara") -> dict[str, Any]:
    roles = roles_for_requested_by(requested_by)
    option = str(roles.get("option") or role_option_label(requested_by=requested_by) or "")
    return {
        "requested_by": requested_by,
        "producer": option,
        "account_manager": option,
        "test_carrier": "Test Carrier",
        "test_coverage": "Commercial Auto",
        "test_state": "Florida",
        "test_wholesaler": "Test Wholesaler",
    }


def fixture_options_for_field(field_id: str, intended: str) -> tuple[str, ...]:
    """CI options. Producer/AM use the live two-Carlo Name+email dump."""
    if field_id in {"producer", "account_manager"}:
        options = list(LIVE_ROLE_LISTBOX_OPTIONS)
        if intended and intended not in options and extract_email(intended):
            options.insert(0, intended)
        return tuple(options)
    extras = {
        "carrier": ("Hartford", "Travelers"),
        "coverage_type": ("General Liability", "Homeowners"),
        "state": ("Georgia", "New York"),
        "wholesaler": ("Other Wholesaler",),
    }
    others = tuple(item for item in extras.get(field_id, ()) if item != intended)
    if not intended:
        return others
    return (intended, *others)


def audit_fixture_comboboxes(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """CI mock: each create-form combobox has a unique exact option locator."""
    blob = {**default_test_combobox_payload(), **dict(payload or {})}
    reports: list[dict[str, Any]] = []
    blocked: list[str] = []
    for field in COMBOBOX_FIELDS:
        intended = intended_option_for_field(field, blob)
        options = list(blob.get(f"fixture_options_{field['id']}") or fixture_options_for_field(field["id"], intended))
        report = classify_listbox_options(
            field=str(field["label"]),
            intended=intended,
            options=options,
            exact=True,
            field_present=True,
        )
        reports.append(report)
        if report.get("blocked_field"):
            blocked.append(str(report["blocked_field"]))
    ok = not blocked
    return {
        "ok": ok,
        "blocked_fields": blocked,
        "reports": reports,
        "logged": True,
    }


def combobox_instruction() -> str:
    names = ", ".join(str(item["label"]) for item in COMBOBOX_FIELDS)
    return (
        f"On /create/new open each create-form combobox / listbox the job "
        f"would use ({names}, etc.). Assert the visible listbox has a unique "
        "locator for the intended option (get_by_role option + exact=True). "
        "Producer and Account Manager intended option is the concatenated "
        f"Name+email label ({CARLO_OPTION} / {JAKE_OPTION}), not "
        f"{CARLO_FERRARA!r} alone — two Carlo rows exist. If the sender "
        "email is missing from the list, HITL/FAIL that field. Do not fall "
        "back to Robie AI or the other Carlo. "
        "FAIL or dry HITL if two options match the same selector. "
        "Log the blocked field. Job 38c0fa79 HITL'd here on a live "
        "client — do not RETRY that job. Test account only. Never PAWIVA. "
        "Stop before Save program, Send email, checkout, payment, or bind. "
        "No Gemini. No .first/.nth/.last."
    )


def run_unique_listbox_option_scenario() -> dict[str, Any]:
    """Named scenario: 38c0fa79 non-unique create/new listbox option."""
    errors: list[str] = []
    instruction = combobox_instruction()
    for needle in (
        "38c0fa79",
        "unique",
        "listbox",
        "blocked field",
        "PAWIVA",
        CARLO_OPTION,
        "Name+email",
    ):
        if needle.casefold() not in instruction.casefold():
            errors.append(f"instruction missing {needle!r}")
    if "exact=True" not in instruction.replace(" ", ""):
        errors.append("instruction missing exact=True option locator")
    leak = refuse_non_unique_listbox(
        field=DUPLICATE_PREFIX_FIXTURE["field"],
        intended=DUPLICATE_PREFIX_FIXTURE["intended"],
        options=DUPLICATE_PREFIX_FIXTURE["options"],
        exact=False,
    )
    if leak is None:
        errors.append("non-exact Progressive prefix collision was accepted")
    elif "Carrier" not in leak:
        errors.append(f"blocked field was not logged: {leak}")
    exact_ok = refuse_non_unique_listbox(
        field="Carrier",
        intended="Progressive",
        options=DUPLICATE_PREFIX_FIXTURE["options"],
        exact=True,
    )
    if exact_ok is not None:
        errors.append(f"exact Progressive should be unique: {exact_ok}")
    dup = refuse_non_unique_listbox(
        field="State",
        intended="Florida",
        options=("Florida", "Florida"),
        exact=True,
    )
    if dup is None or "State" not in dup:
        errors.append("duplicate exact option names were accepted")
    missing = classify_listbox_options(
        field="Coverage type", intended="", options=("Commercial Auto",), exact=True
    )
    if not missing.get("hitl_required") or "PLAYWRIGHT_BLOCKED" not in str(
        missing.get("hitl_text") or ""
    ):
        errors.append("missing intended option must dry HITL")
    if missing.get("blocked_field") != "Coverage type":
        errors.append("missing intended did not log Coverage type")
    name_only = classify_listbox_options(
        field="Producer",
        intended=CARLO_FERRARA,
        options=LIVE_ROLE_LISTBOX_OPTIONS,
        exact=True,
    )
    if name_only.get("status") != "FAIL" or int(name_only.get("match_count") or 0) < 2:
        errors.append(
            "name-only Carlo Ferrara must FAIL against the two-email fixture"
        )
    email_ok = classify_listbox_options(
        field="Producer",
        intended=CARLO_OPTION,
        options=LIVE_ROLE_LISTBOX_OPTIONS,
        exact=True,
    )
    if email_ok.get("status") != "PASS" or email_ok.get("intended") != CARLO_OPTION:
        errors.append("email-qualified Carlo option must PASS")
    email_from_sender = classify_listbox_options(
        field="Account Manager",
        intended="carlo@streetsmart.insurance",
        options=LIVE_ROLE_LISTBOX_OPTIONS,
        exact=True,
    )
    if (
        email_from_sender.get("status") != "PASS"
        or email_from_sender.get("intended") != CARLO_OPTION
    ):
        errors.append("sender email must uniquely match the streetsmart Carlo option")
    missing_email = classify_listbox_options(
        field="Producer",
        intended=CARLO_OPTION,
        options=(CARLO_SSINJ_OPTION, ROBIE_OPTION, JAKE_OPTION),
        exact=True,
    )
    if missing_email.get("status") not in {"HITL", "FAIL"}:
        errors.append("missing sender email must HITL/FAIL")
    if missing_email.get("status") == "PASS":
        errors.append("missing streetsmart email fell back to another option")
    carlo_payload = default_test_combobox_payload(
        requested_by="carlo@streetsmart.insurance"
    )
    if carlo_payload.get("producer") != CARLO_OPTION:
        errors.append(
            f"requested_by carlo@streetsmart.insurance produced "
            f"{carlo_payload.get('producer')!r}"
        )
    audit = audit_fixture_comboboxes(
        default_test_combobox_payload(requested_by="carlo@streetsmart.insurance")
    )
    if not audit.get("ok"):
        errors.append(f"fixture comboboxes failed: {audit.get('blocked_fields')}")
    if len(audit.get("reports") or []) < len(COMBOBOX_FIELDS):
        errors.append("fixture audit skipped a create-form combobox")
    labels = {item["label"] for item in COMBOBOX_FIELDS}
    for required in (
        "Producer",
        "Account Manager",
        "Carrier",
        "Coverage type",
        "State",
    ):
        if required not in labels:
            errors.append(f"missing combobox {required}")
    ok = not errors
    return {
        "id": COMBOBOX_SCENARIO_ID,
        "kind": "logic",
        "ok": ok,
        "outcome": "PASS" if ok else "FAILED",
        "evidence": (
            "open each create/new combobox; unique exact option locator; "
            f"Producer/AM is {CARLO_OPTION} not name-only; "
            "two matches is PLAYWRIGHT_BLOCKED and logs the field (38c0fa79)"
            if ok
            else "; ".join(errors)
        ),
        "finance_agreement": False,
        "observed": {
            "blocked_field": "Carrier",
            "match_count": 2,
            "job_id": "38c0fa79",
        },
    }
