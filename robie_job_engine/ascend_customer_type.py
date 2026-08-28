"""Ascend Commercial vs Personal radio from line of business.

Carlo 2026-08-28: pick the radio from LOB, not from the form default and
not from LLC vs person-name. A person can own a commercial policy; an
LLC name is not enough.

Unknown / missing LOB → dry HITL. Do not guess.
"""

from __future__ import annotations

from typing import Any

from .hitl import dry_playwright_hitl_text


COMMERCIAL = "commercial"
PERSONAL = "personal"
CUSTOMER_TYPE_SCENARIO_ID = "ascend-customer-type:lob"

COMMERCIAL_LOCATOR = 'get_by_role("radio", name="Commercial customer")'
PERSONAL_LOCATOR = 'get_by_role("radio", name="Personal customer")'

LOB_KEYS = (
    "line_of_business",
    "lob",
    "coverage_type",
    "coverage",
    "policy_type",
    "test_coverage",
)

COMMERCIAL_PHRASES = (
    "commercial auto",
    "comm auto",
    "business auto",
    "commercial package",
    "commercial general liability",
    "businessowners",
    "business owner's",
    "workers compensation",
    "workers' compensation",
    "workers comp",
    "work comp",
    "commercial inland marine",
    "garage keepers",
    "garage liability",
)
COMMERCIAL_TOKENS = frozenset(
    {
        "bop",
        "cgl",
        "garage",
        "trucking",
    }
)
PERSONAL_PHRASES = (
    "personal auto",
    "homeowners",
    "homeowner",
    "renters",
    "renter's",
    "personal umbrella",
    "dwelling fire",
)

CUSTOMER_TYPE_CHAT = (
    "ASCEND customer type class returned: Commercial vs Personal is from "
    "line of business, not the form default and not LLC vs person-name. "
    "Missing LOB is HITL. Do not guess."
)


def lob_from_payload(payload: dict[str, Any] | None) -> str:
    blob = dict(payload or {})
    parts: list[str] = []
    for key in LOB_KEYS:
        value = str(blob.get(key) or "").strip()
        if value:
            parts.append(value)
    for key in ("text", "quote_text", "coverage_label"):
        value = str(blob.get(key) or "").strip()
        if value:
            parts.append(value)
    return " ".join(parts)


def _normalized(value: str) -> str:
    return " ".join(str(value or "").replace("_", " ").replace("-", " ").casefold().split())


def _has_phrase(blob: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase in blob for phrase in phrases)


def _has_token(blob: str, tokens: frozenset[str]) -> bool:
    words = set(blob.split())
    return bool(words & tokens)


def resolve_customer_type_from_lob(*values: Any) -> str | None:
    """Return ``commercial``, ``personal``, or None (HITL). Never uses name/LLC."""
    blob = _normalized(" ".join(str(item or "") for item in values if item))
    if not blob:
        return None
    commercial = _has_phrase(blob, COMMERCIAL_PHRASES) or _has_token(blob, COMMERCIAL_TOKENS)
    personal = _has_phrase(blob, PERSONAL_PHRASES)
    if commercial and personal:
        return None
    if commercial:
        return COMMERCIAL
    if personal:
        return PERSONAL
    return None


def resolve_customer_type(payload: dict[str, Any] | None) -> dict[str, Any]:
    raw = lob_from_payload(payload)
    resolved = resolve_customer_type_from_lob(raw)
    if resolved is None:
        return {
            "resolved": None,
            "radio": None,
            "locator": "",
            "lob": raw,
            "hitl_required": True,
            "hitl_text": unknown_lob_hitl(lob=raw),
        }
    locator = COMMERCIAL_LOCATOR if resolved == COMMERCIAL else PERSONAL_LOCATOR
    radio = "Commercial customer" if resolved == COMMERCIAL else "Personal customer"
    return {
        "resolved": resolved,
        "radio": radio,
        "locator": locator,
        "lob": raw,
        "hitl_required": False,
        "hitl_text": "",
    }


def unknown_lob_hitl(*, lob: str = "") -> str:
    detail = str(lob or "").strip() or "missing"
    return dry_playwright_hitl_text(
        reason=(
            "Commercial vs Personal customer radio cannot be set. "
            f"line of business is {detail!r}. "
            "Do not use the form default. Do not use LLC vs person-name. "
            "Reply with commercial or personal."
        )
    )


def customer_type_locator(resolved: str | None) -> str:
    if resolved == COMMERCIAL:
        return COMMERCIAL_LOCATOR
    if resolved == PERSONAL:
        return PERSONAL_LOCATOR
    return ""


def customer_type_instruction(payload: dict[str, Any] | None = None) -> str:
    decision = resolve_customer_type(payload)
    if decision["hitl_required"]:
        return (
            "After Create a program loads, set Commercial vs Personal from "
            "line of business, not the form default and not the insured name. "
            f"{decision['hitl_text']}"
        )
    return (
        f"After Create a program loads, select the unique "
        f"{decision['radio']} radio ({decision['locator']}) because LOB is "
        f"{decision['lob']!r}. Do not leave Ascend's Commercial default if "
        "LOB is personal. No .first/.nth/.last. No Gemini."
    )


def run_customer_type_lob_scenario() -> dict[str, Any]:
    """Named scenario: ascend-customer-type:lob."""
    errors: list[str] = []
    commercial_cases = (
        "commercial auto",
        "Commercial Auto",
        "commercial package",
        "BOP",
        "CGL",
        "workers comp",
        "trucking",
        "garage",
    )
    personal_cases = (
        "homeowners",
        "personal auto",
        "renters",
        "personal umbrella",
        "dwelling fire",
    )
    for raw in commercial_cases:
        if resolve_customer_type_from_lob(raw) != COMMERCIAL:
            errors.append(f"{raw!r} should be commercial")
    for raw in personal_cases:
        if resolve_customer_type_from_lob(raw) != PERSONAL:
            errors.append(f"{raw!r} should be personal")
    for missing in ("", None, "Acme", "PAWIVA INVESTMENT LLC", "Jane Smith"):
        if resolve_customer_type_from_lob(missing) is not None:
            errors.append(f"{missing!r} must be unknown without LOB")
    package = resolve_customer_type(
        {"line_of_business": "commercial package", "insured": "PAWIVA INVESTMENT LLC"}
    )
    if package.get("resolved") != COMMERCIAL:
        errors.append("commercial package (PAWIVA account context) should be commercial")
    missing = resolve_customer_type({})
    if not missing.get("hitl_required") or "PLAYWRIGHT_BLOCKED" not in str(
        missing.get("hitl_text") or ""
    ):
        errors.append("missing LOB must HITL")
    name_only = resolve_customer_type({"insured": "PAWIVA INVESTMENT LLC", "name": "Jane Smith"})
    if name_only.get("resolved") is not None:
        errors.append("insured/LLC name must not decide customer type")
    ok = not errors
    return {
        "id": CUSTOMER_TYPE_SCENARIO_ID,
        "kind": "logic",
        "ok": ok,
        "outcome": "PASS" if ok else "FAILED",
        "evidence": (
            "LOB commercial auto/package → commercial; homeowners/personal auto "
            "→ personal; missing LOB HITL; LLC/name is not the decider"
            if ok
            else "; ".join(errors)
        ),
        "finance_agreement": False,
    }
