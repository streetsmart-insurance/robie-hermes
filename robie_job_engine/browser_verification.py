"""Reusable browser-verification contracts for EZLynx mutating actions.

Preconditions, action receipts, postconditions, retry classification, and
evidence helpers shared by the Job Engine, Skills, and CDP ports.
Worker prose never authorizes COMPLETE — only destination read-back does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


EZLYNX_MUTATING_ACTIONS = frozenset(
    {
        "ezlynx.reassign",
        "ezlynx.move_document",
        "ezlynx.apply_label",
    }
)

# Schema fields that must be present before the worker may touch the browser.
ACTION_PRECONDITIONS: dict[str, tuple[str, ...]] = {
    "ezlynx.reassign": (
        "resource_id",
        "applicant_id",
        "assignee_id",
        "assignee_name",
    ),
    "ezlynx.move_document": (
        "document_id",
        "document_name",
        "account_id",
        "destination_id",
        "destination_name",
        "move_control",
    ),
    "ezlynx.apply_label": (
        "resource_id",
        "account_id",
        "document_name",
        "label_id",
        "label",
        "label_control",
    ),
}

# Destination keys the verifier must observe after an independent re-read.
ACTION_POSTCONDITIONS: dict[str, tuple[str, ...]] = {
    "ezlynx.reassign": (
        "resource_id",
        "applicant_id",
        "assignee_id",
        "assignee_name",
    ),
    "ezlynx.move_document": (
        "resource_id",
        "account_id",
        "destination_id",
        "destination_name",
    ),
    "ezlynx.apply_label": (
        "resource_id",
        "account_id",
        "label_id",
        "label",
    ),
}

# Prefer API/network capture; otherwise live-page + reload persistence.
PREFERRED_EVIDENCE_METHODS = (
    "EZLYNX_API_READBACK",
    "FRESH_PAGE_READBACK",
)


@dataclass(frozen=True)
class BrowserActionContract:
    action_type: str
    preconditions: tuple[str, ...]
    postconditions: tuple[str, ...]
    max_attempts: int = 3
    retry_on_network: bool = True
    evidence_methods: tuple[str, ...] = PREFERRED_EVIDENCE_METHODS


CONTRACTS: dict[str, BrowserActionContract] = {
    action: BrowserActionContract(
        action_type=action,
        preconditions=ACTION_PRECONDITIONS[action],
        postconditions=ACTION_POSTCONDITIONS[action],
    )
    for action in EZLYNX_MUTATING_ACTIONS
}


@dataclass
class BrowserVerificationPlan:
    """What the worker must leave and what the verifier must prove."""

    action_type: str
    expected_destination: dict[str, Any]
    preconditions_ok: bool
    missing_preconditions: tuple[str, ...] = ()
    notes: list[str] = field(default_factory=list)


def missing_preconditions(action_type: str, payload: Mapping[str, Any]) -> tuple[str, ...]:
    required = ACTION_PRECONDITIONS.get(action_type) or ()
    return tuple(key for key in required if not payload.get(key))


def plan_browser_verification(
    action_type: str, payload: Mapping[str, Any]
) -> BrowserVerificationPlan:
    missing = missing_preconditions(action_type, payload)
    expected = {
        key: payload.get(key)
        for key in ACTION_POSTCONDITIONS.get(action_type, ())
        if key in payload or key == "resource_id"
    }
    if action_type == "ezlynx.move_document" and "document_id" in payload:
        expected["resource_id"] = payload["document_id"]
    return BrowserVerificationPlan(
        action_type=action_type,
        expected_destination=expected,
        preconditions_ok=not missing,
        missing_preconditions=missing,
        notes=[
            "COMPLETE requires independent destination read-back",
            "Prefer EZLYNX_API_READBACK when network capture is available",
            "Otherwise require FRESH_PAGE_READBACK after reload",
        ],
    )


def classify_browser_retry(error: str | None) -> dict[str, Any]:
    """Map a browser failure string to retry guidance."""
    text = str(error or "").casefold()
    if not text:
        return {"retryable": False, "reason": "empty_error"}
    if "playwright_blocked" in text or "locator" in text and "matched" in text:
        return {"retryable": False, "reason": "locator_ambiguity"}
    if "auth_challenge" in text or "needs_auth" in text:
        return {"retryable": False, "reason": "auth_expired"}
    if any(token in text for token in ("timeout", "net::", "network", "econnreset")):
        return {"retryable": True, "reason": "transient_network"}
    return {"retryable": False, "reason": "unknown_fail_closed"}
