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

import re
from pathlib import Path
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
        "payload_keys": ("quote_carrier",),
        "quote_sourced": True,
    },
    {
        "id": "coverage_type",
        "label": "Coverage type",
        "aliases": ("Coverage type", "Coverage Type"),
        "payload_keys": ("quote_coverage", "quote_coverage_type"),
        "quote_sourced": True,
    },
    {
        "id": "state",
        "label": "State",
        "aliases": ("State",),
        "payload_keys": ("quote_state",),
        "quote_sourced": True,
    },
    {
        "id": "wholesaler",
        "label": "Wholesaler",
        "aliases": ("Wholesaler",),
        "payload_keys": ("test_wholesaler", "wholesaler", "quote_wholesaler"),
        "optional": True,
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

QUOTE_SOURCED_FIELD_IDS = frozenset({"carrier", "coverage_type", "state"})
QUOTE_PATH_KEYS = ("quote_path", "test_quote_path", "import_quote_path")
QUOTE_TEXT_KEYS = ("quote_text", "imported_quote_text")
FORBIDDEN_QUOTE_MARKERS = ("pawiva", "221398001")

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

# Live option facts from hermes-test-01 job 1bb06c17 (read-only dump).
# These are listbox tails / leak fixtures, not Test defaults. Do not invent
# intended Carrier / State / Coverage from this list.
LIVE_STATE_LISTBOX_OPTIONS = (
    "Alabama",
    "Alaska",
    "Arizona",
    "Arkansas",
    "California",
    "Colorado",
    "Connecticut",
    "Delaware",
    "District of Columbia",
    "Florida",
    "Georgia",
    "Hawaii",
    "Idaho",
    "Illinois",
    "Indiana",
    "Iowa",
    "Kansas",
    "Kentucky",
    "Louisiana",
    "Maine",
    "Maryland",
    "Massachusetts",
    "Michigan",
    "Minnesota",
    "Mississippi",
    "Missouri",
    "Montana",
    "Nebraska",
    "Nevada",
    "New Hampshire",
    "New Jersey",
    "New Mexico",
    "New York",
    "North Carolina",
    "North Dakota",
    "Ohio",
    "Oklahoma",
    "Oregon",
    "Pennsylvania",
    "Rhode Island",
    "South Carolina",
    "South Dakota",
    "Tennessee",
    "Texas",
    "Utah",
    "Vermont",
    "Virginia",
    "Washington",
    "West Virginia",
    "Wisconsin",
    "Wyoming",
    "%",
    "$",
)
LIVE_CARRIER_LISTBOX_TAILS = (
    "USLI US Liability Insurance Company",
    "CNA",
    "Canal Insurance Company",
    "Philadelphia Indemnity Insurance Company",
)
LIVE_COVERAGE_LISTBOX_TAILS = (
    "General Liability",
    "Commercial Package",
    "Commercial Property",
    "Non-Truck Liability",
    "Excess Liability",
)

# CI / Test-account quote fixture. Values are what THIS quote says — not
# live dropdown guesses. Loom example coverage is Commercial Package only
# because this quote says Commercial Package. Never PAWIVA.
TEST_QUOTE_TEXT = (
    "ROBIE Test LLC\n"
    "Test account quote\n"
    "Carrier: Philadelphia Indemnity Insurance Company\n"
    "Coverage type: Commercial Package\n"
    "State: Georgia\n"
    "Quote number: TEST-ASCEND-AUDIT\n"
)
TEST_QUOTE_FIELDS = {
    "carrier": "Philadelphia Indemnity Insurance Company",
    "coverage_type": "Commercial Package",
    "state": "Georgia",
}

_QUOTE_LABEL_RE = re.compile(
    r"^(?P<label>writing\s+company(?:\s+name)?|carrier|coverage\s+type|"
    r"line\s+of\s+business|coverage|risk\s+state|governing\s+state|state)"
    r"\s*[:\-]\s*(?P<value>.+)$",
    re.IGNORECASE | re.MULTILINE,
)

_QUOTE_FROM_CARRIER_RE = re.compile(
    r"\bquote\s+from\s+(?P<value>[A-Z][A-Za-z0-9&,'’.\- ]{2,160}?"
    r"(?:Insurance Company|Indemnity Company|Insurance Companies))\b",
    re.IGNORECASE,
)
_INSURANCE_QUOTE_HEADING_RE = re.compile(
    r"\b(?P<value>Commercial Auto|Personal Auto|Commercial Package|"
    r"General Liability|Workers(?:'|’) Compensation)\s+Insurance Quote\b",
    re.IGNORECASE,
)
_FORM_QUOTE_STATE_RE = re.compile(
    r"\bForm\s+QUOTE\s+(?P<code>[A-Z]{2})\b",
    re.IGNORECASE,
)
_US_STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}


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


def option_starts_with(option: str, intended: str) -> bool:
    left = _folded_option(option)
    right = _folded_option(intended)
    if not left or not right:
        return False
    return left.startswith(right)


def prefix_matching_options(
    options: list[str] | tuple[str, ...], intended: str
) -> list[str]:
    return [item for item in options if option_starts_with(item, intended)]


def quote_mentions_forbidden_account(*values: Any) -> str | None:
    blob = " ".join(str(item or "") for item in values).casefold()
    for banned in FORBIDDEN_QUOTE_MARKERS:
        if banned in blob:
            return "refusing real client account PAWIVA; Test account only"
    return None


def _quote_label_field(label: str) -> str | None:
    folded = " ".join(str(label or "").casefold().split())
    if folded in {"carrier", "writing company", "writing company name"}:
        return "carrier"
    if folded in {"coverage type", "coverage", "line of business"}:
        return "coverage_type"
    if folded in {"state", "risk state", "governing state"}:
        return "state"
    return None


def parse_quote_fields(text: str) -> dict[str, str]:
    """Labeled quote values only. Do not invent a carrier or coverage."""
    leak = quote_mentions_forbidden_account(text)
    if leak:
        raise ValueError(leak)
    found: dict[str, set[str]] = {}
    for match in _QUOTE_LABEL_RE.finditer(str(text or "")):
        field = _quote_label_field(match.group("label"))
        value = " ".join(str(match.group("value") or "").split())
        if not field or not value:
            continue
        found.setdefault(field, set()).add(value)
    resolved: dict[str, str] = {}
    for field, values in found.items():
        if len(values) == 1:
            resolved[field] = next(iter(values))
    flattened = " ".join(str(text or "").split())
    if not resolved.get("carrier"):
        match = _QUOTE_FROM_CARRIER_RE.search(flattened)
        if match:
            resolved["carrier"] = " ".join(match.group("value").split())
    if not resolved.get("coverage_type"):
        match = _INSURANCE_QUOTE_HEADING_RE.search(flattened)
        if match:
            resolved["coverage_type"] = " ".join(match.group("value").split()).title()
    if not resolved.get("state"):
        match = _FORM_QUOTE_STATE_RE.search(flattened)
        if match:
            resolved["state"] = _US_STATE_NAMES.get(match.group("code").upper(), "")
    return resolved


def read_quote_text(path: str) -> str:
    target = Path(str(path or "").strip())
    if not target.is_file():
        return ""
    leak = quote_mentions_forbidden_account(str(target), target.name)
    if leak:
        raise ValueError(leak)
    data = target.read_bytes()
    leak = quote_mentions_forbidden_account(data[:4096])
    if leak:
        raise ValueError(leak)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1", errors="replace")
    if text.lstrip().startswith("%PDF"):
        try:
            from pypdf import PdfReader

            pages = PdfReader(target)
            extracted = "\n".join(
                str(page.extract_text() or "") for page in pages.pages
            ).strip()
            if extracted:
                return extracted
        except Exception:  # noqa: BLE001 — unreadable PDF stays empty intended
            pass
    return text


def quote_path_from_payload(payload: dict[str, Any] | None) -> str:
    blob = dict(payload or {})
    for key in QUOTE_PATH_KEYS:
        value = str(blob.get(key) or "").strip()
        if value:
            return value
    return ""


def quote_text_from_payload(payload: dict[str, Any] | None) -> str:
    blob = dict(payload or {})
    for key in QUOTE_TEXT_KEYS:
        value = str(blob.get(key) or "").strip()
        if value:
            return value
    path = quote_path_from_payload(blob)
    if path:
        return read_quote_text(path)
    return ""


def quote_fields_from_payload(payload: dict[str, Any] | None) -> dict[str, str]:
    """Carrier / State / Coverage type from the imported quote only."""
    blob = dict(payload or {})
    leak = quote_mentions_forbidden_account(
        blob.get("quote_path"),
        blob.get("quote_text"),
        blob.get("quote_fields"),
        blob.get("test_quote_path"),
    )
    if leak:
        raise ValueError(leak)
    parsed: dict[str, str] = {}
    existing = blob.get("quote_fields")
    if isinstance(existing, dict):
        for field in ("carrier", "coverage_type", "state"):
            value = str(existing.get(field) or "").strip()
            if value:
                parsed[field] = value
    if not parsed:
        text = quote_text_from_payload(blob)
        if text:
            parsed.update(parse_quote_fields(text))
    for field, keys in (
        ("carrier", ("quote_carrier",)),
        ("coverage_type", ("quote_coverage", "quote_coverage_type")),
        ("state", ("quote_state",)),
    ):
        if parsed.get(field):
            continue
        for key in keys:
            value = str(blob.get(key) or "").strip()
            if value:
                parsed[field] = value
                break
    return {
        "carrier": str(parsed.get("carrier") or "").strip(),
        "coverage_type": str(parsed.get("coverage_type") or "").strip(),
        "state": str(parsed.get("state") or "").strip(),
    }


def intended_option_for_field(field: dict[str, Any], payload: dict[str, Any] | None) -> str:
    blob = dict(payload or {})
    if field.get("quote_sourced") or field.get("id") in QUOTE_SOURCED_FIELD_IDS:
        quote = quote_fields_from_payload(blob)
        return str(quote.get(str(field.get("id") or "")) or "").strip()
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
    optional: bool = False,
) -> dict[str, Any]:
    """Unique intended option or FAIL / dry HITL. Always log the field."""
    locator = option_locator(intended, exact=exact) if intended else ""
    if not field_present or (not intended and optional):
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
    if count == 0 and exact and intended:
        prefix_hits = prefix_matching_options(options, intended)
        if len(prefix_hits) == 1:
            matched = prefix_hits[0]
            intended = matched
            locator = option_locator(intended, exact=True)
            count = 1
        else:
            count = len(prefix_hits)
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
    optional: bool = False,
) -> str | None:
    report = classify_listbox_options(
        field=field,
        intended=intended,
        options=options,
        exact=exact,
        field_present=field_present,
        optional=optional,
    )
    if report["status"] in {"FAIL", "HITL"}:
        return str(report.get("error") or report.get("hitl_text") or "")
    return None


def default_test_combobox_payload(*, requested_by: str = "Carlo Ferrara") -> dict[str, Any]:
    """Producer / AM from requested_by. Do not invent Carrier / State / Coverage."""
    roles = roles_for_requested_by(requested_by)
    option = str(roles.get("option") or role_option_label(requested_by=requested_by) or "")
    return {
        "requested_by": requested_by,
        "producer": option,
        "account_manager": option,
    }


def fixture_options_for_field(field_id: str, intended: str) -> tuple[str, ...]:
    """CI options. Producer/AM use the live two-Carlo Name+email dump."""
    if field_id in {"producer", "account_manager"}:
        options = list(LIVE_ROLE_LISTBOX_OPTIONS)
        if intended and intended not in options and extract_email(intended):
            options.insert(0, intended)
        return tuple(options)
    extras = {
        "carrier": LIVE_CARRIER_LISTBOX_TAILS,
        "coverage_type": LIVE_COVERAGE_LISTBOX_TAILS,
        "state": ("Georgia", "New York", "Florida"),
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
            optional=bool(field.get("optional")),
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
        "Carrier, State, and Coverage type intended values come from the "
        "imported Test-account quote. If Import did not run or the quote "
        "has no value, intended stays empty and that field is HITL. "
        "Do not invent a carrier or coverage. "
        "Log the blocked field. Job 38c0fa79 HITL'd here on a live "
        "client — do not RETRY that job. Test account only. Never PAWIVA. "
        "Stop before Save program, Send email, checkout, payment, or bind. "
        "No Gemini. No .first/.nth/.last."
    )


INVENTED_COMBOBOX_DEFAULTS = (
    "Test Carrier",
    "Commercial Auto",
    "Florida",
    "USLI US Liability Insurance Company",
    "General Liability",
    "New Jersey",
    "Test Wholesaler",
)


def invented_combobox_defaults(payload: dict[str, Any] | None) -> list[str]:
    """Names that must not be filled as Test defaults when no quote exists."""
    blob = dict(payload or {})
    found: list[str] = []
    for key in (
        "test_carrier",
        "test_coverage",
        "test_state",
        "test_wholesaler",
        "carrier",
        "coverage_type",
        "state",
    ):
        value = str(blob.get(key) or "").strip()
        if value in INVENTED_COMBOBOX_DEFAULTS:
            found.append(value)
    return found


def combobox_payload_from_audit(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Merge requested_by into Producer/AM. Quote fields stay quote-sourced."""
    blob = dict(payload or {})
    sender = str(blob.get("requested_by") or blob.get("sender") or blob.get("from") or "")
    roles = roles_for_requested_by(sender)
    merged = {**default_test_combobox_payload(requested_by=sender), **blob}
    if roles.get("option"):
        merged["producer"] = roles["option"]
        merged["account_manager"] = roles["option"]
    elif roles.get("resolved"):
        merged.setdefault("producer", roles["resolved"])
        merged.setdefault("account_manager", roles["resolved"])
    quote = quote_fields_from_payload(merged)
    merged["quote_fields"] = quote
    if quote.get("carrier"):
        merged["quote_carrier"] = quote["carrier"]
    if quote.get("coverage_type"):
        merged["quote_coverage"] = quote["coverage_type"]
        merged.setdefault("coverage_type", quote["coverage_type"])
        merged.setdefault("line_of_business", quote["coverage_type"])
    if quote.get("state"):
        merged["quote_state"] = quote["state"]
    return merged


def listbox_audit_should_abort(observed: dict[str, Any] | None) -> bool:
    """Non-unique FAIL aborts. Empty-intended HITL / SKIP continues to Agency Fee."""
    for item in (observed or {}).get("fields") or []:
        if str(item.get("status") or "") == "FAIL":
            return True
    return False


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
    invented = invented_combobox_defaults(carlo_payload)
    if invented:
        errors.append(f"invented combobox defaults: {invented}")
    empty_quote = audit_fixture_comboboxes(
        default_test_combobox_payload(requested_by="carlo@streetsmart.insurance")
    )
    empty_by_field = {
        str(item.get("field")): item for item in empty_quote.get("reports") or []
    }
    for required_hitl in ("Carrier", "Coverage type", "State"):
        report = empty_by_field.get(required_hitl) or {}
        if report.get("status") != "HITL" or report.get("intended"):
            errors.append(
                f"{required_hitl} without a quote must HITL empty intended, "
                f"got {report.get('status')!r} intended={report.get('intended')!r}"
            )
    wholesaler = empty_by_field.get("Wholesaler") or {}
    if wholesaler.get("status") != "SKIP":
        errors.append(f"optional Wholesaler without intended must SKIP, got {wholesaler}")
    quote_audit = audit_fixture_comboboxes(
        {
            **default_test_combobox_payload(
                requested_by="carlo@streetsmart.insurance"
            ),
            "quote_text": TEST_QUOTE_TEXT,
            "fixture_options_carrier": (
                *LIVE_STATE_LISTBOX_OPTIONS,
                *LIVE_CARRIER_LISTBOX_TAILS,
            ),
            "fixture_options_coverage_type": (
                *LIVE_STATE_LISTBOX_OPTIONS,
                *LIVE_COVERAGE_LISTBOX_TAILS,
            ),
        }
    )
    if not quote_audit.get("ok"):
        errors.append(f"quote-sourced fixture comboboxes failed: {quote_audit.get('blocked_fields')}")
    quote_by_field = {
        str(item.get("field")): item for item in quote_audit.get("reports") or []
    }
    if quote_by_field.get("Coverage type", {}).get("intended") != "Commercial Package":
        errors.append("quote-sourced coverage must be Commercial Package")
    if quote_by_field.get("Carrier", {}).get("intended") != TEST_QUOTE_FIELDS["carrier"]:
        errors.append("quote-sourced carrier drifted from the Test quote")
    if quote_by_field.get("State", {}).get("intended") != TEST_QUOTE_FIELDS["state"]:
        errors.append("quote-sourced state drifted from the Test quote")
    prefix_ok = classify_listbox_options(
        field="Carrier",
        intended=TEST_QUOTE_FIELDS["carrier"],
        options=(
            f"{TEST_QUOTE_FIELDS['carrier']}\nAccounts Payable",
            "CNA",
        ),
        exact=True,
    )
    if prefix_ok.get("status") != "PASS":
        errors.append("unique office/payable prefix must PASS")
    prefix_dup = classify_listbox_options(
        field="Carrier",
        intended=TEST_QUOTE_FIELDS["carrier"],
        options=(
            f"{TEST_QUOTE_FIELDS['carrier']} Newark",
            f"{TEST_QUOTE_FIELDS['carrier']} Freehold",
        ),
        exact=True,
    )
    if prefix_dup.get("status") != "FAIL":
        errors.append("two company-name prefixes must FAIL")
    if listbox_audit_should_abort({"fields": empty_quote.get("reports") or []}):
        errors.append("empty-intended HITL must not abort the walk")
    if not listbox_audit_should_abort(
        {"fields": [{"field": "Carrier", "status": "FAIL"}]}
    ):
        errors.append("non-unique FAIL must abort the walk")
    if len(empty_quote.get("reports") or []) < len(COMBOBOX_FIELDS):
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
