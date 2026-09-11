"""COMPLETE is allowed only from VERIFYING with authoritative postconditions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
EVIDENCE_CLOCK_SKEW = timedelta(minutes=5)


IDENTITY_KEYS = (
    "record_id",
    "resource_id",
    "proposal_id",
    "report_id",
    "locator",
    "id",
)
IDENTITY_FALLBACK_KEYS = (
    "url",
    "document_id",
    "target_path",
    "destination_root",
    "destination_id",
    "policy_number",
)
WEAK_ONLY_EXPECTED_KEYS = frozenset({"ok"})
WORKFLOW_EXPECTED_KEYS = frozenset(
    IDENTITY_KEYS
    + (
        "status",
        "ok",
        "outcome",
        "page_count",
        "agency_fee_usd",
        "agency_fee_occurrences",
        "title",
        "url",
        "account_id",
        "applicant_id",
        "policy_number",
        "document_id",
        "assignee_id",
        "assignee_name",
        "destination_id",
        "destination_name",
        "label_id",
        "label",
        "target_path",
        "sha256",
        "content",
        "mtime_ns",
        "authenticated",
        "scope",
        "postcondition",
    )
)


def _identity_from_blobs(*blobs: Any) -> str | None:
    keys = IDENTITY_KEYS + IDENTITY_FALLBACK_KEYS
    for blob in blobs:
        if isinstance(blob, str) and blob.strip():
            return blob.strip()
        if not isinstance(blob, dict):
            continue
        nested = blob.get("locator")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
        if isinstance(nested, dict):
            for key in keys:
                if nested.get(key):
                    return str(nested[key])
        for key in keys:
            if blob.get(key):
                return str(blob[key])
    return None


def evidence_record_id(
    *,
    locator: str | None,
    expected: dict[str, Any] | None,
    observed: dict[str, Any] | None,
    job_id: str | None = None,
) -> str | None:
    """Return a destination record identity. Never fall back to the Job ID."""
    del job_id
    if locator and str(locator).strip():
        return str(locator).strip()
    return _identity_from_blobs(expected or {}, observed or {})


def intended_destination_identity(
    *,
    action: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> str | None:
    """Identity of the action target the evidence must bind to."""
    action = dict(action or {})
    payload = dict(payload or {})
    return _identity_from_blobs(
        action.get("destination"),
        payload.get("locator"),
        payload,
    )


def identities_bound(record_id: str, intended: str) -> bool:
    """True when evidence names the intended record or a path under it."""
    if record_id == intended:
        return True
    if not record_id or not intended:
        return False
    prefix = str(intended).rstrip("/") + "/"
    return str(record_id).startswith(prefix)


def destination_identity_missing(
    *,
    locator: str | None,
    expected: dict[str, Any] | None,
    observed: dict[str, Any] | None,
    intended: str | None = None,
    job_id: str | None = None,
) -> str | None:
    """Refuse COMPLETE when destination identity is missing or unbound."""
    record_id = evidence_record_id(
        locator=locator, expected=expected, observed=observed
    )
    if not record_id:
        return "COMPLETE prohibited: destination record identity is missing"
    if job_id and record_id == str(job_id):
        return "COMPLETE prohibited: destination record identity cannot be the Job ID"
    if not intended:
        return "COMPLETE prohibited: action target identity is missing"
    if not identities_bound(record_id, str(intended)):
        return (
            f"COMPLETE prohibited: evidence identity {record_id!r} "
            f"does not match action target {intended!r}"
        )
    return None


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


def expected_postcondition_missing(expected: dict[str, Any] | None) -> str | None:
    """Empty or non-workflow expected must never authorize COMPLETE."""
    if not isinstance(expected, dict) or not expected:
        return "COMPLETE prohibited: expected postcondition is empty"
    if not any(key in WORKFLOW_EXPECTED_KEYS for key in expected):
        return "COMPLETE prohibited: expected postcondition has no workflow-relevant keys"
    material = {
        key: value
        for key, value in expected.items()
        if key not in WEAK_ONLY_EXPECTED_KEYS
    }
    if not material:
        return (
            "COMPLETE prohibited: expected postcondition is only a success flag; "
            "the intended record was not proven"
        )
    return None


def postcondition_mismatch(
    expected: dict[str, Any] | None,
    observed: dict[str, Any] | None,
) -> str | None:
    """Require a non-empty, workflow-relevant expected-versus-observed match."""
    missing = expected_postcondition_missing(expected)
    if missing:
        return missing
    expected = dict(expected)
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
    now: datetime | None = None,
) -> str | None:
    """Reject stale, prior-attempt, and far-future evidence timestamps."""
    captured = parse_evidence_timestamp(captured_at)
    if captured is None:
        return "COMPLETE prohibited: captured_at is missing or unparseable"
    captured_floor = parse_evidence_timestamp(not_before)
    if captured_floor is not None and captured < captured_floor:
        return "COMPLETE prohibited: evidence timestamp is stale"
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    if captured > current + EVIDENCE_CLOCK_SKEW:
        return "COMPLETE prohibited: evidence timestamp is in the future"
    stored = parse_evidence_timestamp(stored_at)
    stored_floor = parse_evidence_timestamp(stored_not_before)
    if stored is not None and stored_floor is not None and stored < stored_floor:
        return "COMPLETE prohibited: evidence is not from the current action attempt"
    if stored is not None and stored > current + EVIDENCE_CLOCK_SKEW:
        return "COMPLETE prohibited: evidence timestamp is in the future"
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
    intended: str | None = None,
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
    missing = expected_postcondition_missing(expected)
    if missing:
        raise PermissionError(missing)
    identity = destination_identity_missing(
        locator=locator,
        expected=expected,
        observed=observed,
        intended=intended,
        job_id=job_id,
    )
    if identity:
        raise PermissionError(identity)
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
