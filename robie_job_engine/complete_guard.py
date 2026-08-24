"""COMPLETE is allowed only from VERIFYING with authoritative postconditions."""

from __future__ import annotations

from datetime import datetime, timezone
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


def postcondition_mismatch(
    expected: dict[str, Any] | None,
    observed: dict[str, Any] | None,
) -> str | None:
    """Require an exact expected-versus-observed postcondition match."""
    expected = dict(expected or {})
    observed = dict(observed or {})
    for key, value in expected.items():
        if observed.get(key) != value:
            return (
                f"COMPLETE prohibited: expected {key}={value!r} "
                f"observed {observed.get(key)!r}"
            )
    return None


def parse_evidence_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def evidence_is_stale(
    *,
    captured_at: str | None,
    not_before: str | None,
    stored_at: str | None = None,
    stored_not_before: str | None = None,
) -> str | None:
    """Reject year-2000 / pre-job timestamps and prior-attempt rows."""
    captured = parse_evidence_timestamp(captured_at)
    if captured is None:
        return "COMPLETE prohibited: captured_at is missing or unparseable"
    captured_floor = parse_evidence_timestamp(not_before)
    if captured_floor is not None and captured < captured_floor:
        return "COMPLETE prohibited: evidence timestamp is stale"
    stored = parse_evidence_timestamp(stored_at)
    stored_floor = parse_evidence_timestamp(stored_not_before)
    if stored is not None and stored_floor is not None and stored < stored_floor:
        return "COMPLETE prohibited: evidence is not from the current action attempt"
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
    not_before: str | None = None,
    stored_at: str | None = None,
    stored_not_before: str | None = None,
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
    mismatch = postcondition_mismatch(expected, observed)
    if mismatch:
        raise PermissionError(mismatch)
    stale = evidence_is_stale(
        captured_at=captured_at,
        not_before=not_before,
        stored_at=stored_at,
        stored_not_before=stored_not_before,
    )
    if stale:
        raise PermissionError(stale)
