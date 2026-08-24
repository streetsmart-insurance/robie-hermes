"""COMPLETE is allowed only from VERIFYING with authoritative postconditions."""

from __future__ import annotations

from typing import Any

from .models import ACTION_OUTCOME_UNKNOWN, JobStatus


PROHIBITED_FLAGS = frozenset(
    {
        "loading",
        "mfa",
        "mfa_required",
        "missing_schema",
        "stale_page",
        "timeout",
        "unknown_outcome",
        ACTION_OUTCOME_UNKNOWN,
    }
)
PROHIBITED_VALUES = frozenset(
    {
        "loading",
        "mfa",
        "mfa_required",
        "missing_schema",
        "stale",
        "stale_page",
        "timeout",
        "unknown",
        ACTION_OUTCOME_UNKNOWN,
    }
)
IDENTITY_KEYS = (
    "record_id",
    "resource_id",
    "proposal_id",
    "locator",
    "id",
)


def evidence_record_id(
    *,
    locator: str | None,
    expected: dict[str, Any] | None,
    observed: dict[str, Any] | None,
    job_id: str | None = None,
) -> str | None:
    if locator:
        return str(locator)
    for blob in (expected or {}, observed or {}):
        for key in IDENTITY_KEYS:
            if blob.get(key):
                return str(blob[key])
    return job_id


def complete_is_prohibited(observed: dict[str, Any] | None) -> str | None:
    """Return a reason if observed state must never become COMPLETE."""
    observed = dict(observed or {})
    for key, value in observed.items():
        key_l = str(key).casefold()
        if key_l in PROHIBITED_FLAGS and value:
            return f"COMPLETE prohibited: observed {key}"
        if isinstance(value, str) and value.strip().casefold() in PROHIBITED_VALUES:
            return f"COMPLETE prohibited: observed {key}={value}"
    outcome = str(observed.get("outcome") or observed.get("page_state") or "")
    if outcome in PROHIBITED_VALUES or outcome == ACTION_OUTCOME_UNKNOWN:
        return f"COMPLETE prohibited: {outcome or ACTION_OUTCOME_UNKNOWN}"
    return None


def require_complete_postcondition(
    *,
    current: JobStatus,
    authority: str,
    verified: bool,
    authoritative: bool,
    expected: dict[str, Any] | None,
    observed: dict[str, Any] | None,
    captured_at: str | None,
    evidence_ref: str | None,
    locator: str | None,
    job_id: str | None,
    verifier_authority: str,
) -> None:
    if current != JobStatus.VERIFYING or authority != verifier_authority:
        raise PermissionError(
            "action workers cannot authorize COMPLETE; "
            "only the independent verifier may. "
            "COMPLETE is only allowed from VERIFYING"
        )
    if not verified or not authoritative:
        raise PermissionError("COMPLETE requires independently stored authoritative evidence")
    if not captured_at or not evidence_ref:
        raise PermissionError("COMPLETE evidence is missing timestamp or evidence ref")
    if expected is None or observed is None:
        raise PermissionError("COMPLETE evidence must include expected and observed")
    if not evidence_record_id(
        locator=locator, expected=expected, observed=observed, job_id=job_id
    ):
        raise PermissionError("COMPLETE evidence is missing a record ID")
    prohibited = complete_is_prohibited(observed)
    if prohibited:
        raise PermissionError(prohibited)
