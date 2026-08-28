"""Failure classification and retry policy for ROBIE durable execution.

Distinguishes between:
1. Positional / locator ambiguity -> Non-retryable (fail closed to HITL immediately; guessing is forbidden).
2. Empty or corrupt artifacts -> Non-retryable (fail closed immediately).
3. Transient network / transport failures -> Bounded retry with backoff.
4. Auth checkpoints / 2SV -> Non-retryable hold (NEEDS_AUTH).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .models import JobStatus


class FailureClass(str, Enum):
    LOCATOR_AMBIGUITY = "LOCATOR_AMBIGUITY"
    EMPTY_OR_CORRUPT_ARTIFACT = "EMPTY_OR_CORRUPT_ARTIFACT"
    TRANSIENT_NETWORK = "TRANSIENT_NETWORK"
    AUTH_CHALLENGE = "AUTH_CHALLENGE"
    SCHEMA_VIOLATION = "SCHEMA_VIOLATION"
    GENERIC_FAILURE = "GENERIC_FAILURE"


@dataclass(frozen=True)
class FailureClassification:
    failure_class: FailureClass
    is_retryable: bool
    reason: str
    suggested_hold_status: JobStatus | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "failure_class": self.failure_class.value,
            "is_retryable": self.is_retryable,
            "reason": self.reason,
            "suggested_hold_status": self.suggested_hold_status.value if self.suggested_hold_status else None,
        }


def classify_failure(error: Exception | str | None) -> FailureClassification:
    """Classify an error to determine whether it is safe to retry."""
    if error is None:
        return FailureClassification(
            failure_class=FailureClass.GENERIC_FAILURE,
            is_retryable=False,
            reason="No error provided",
        )

    text = str(error)
    lowered = text.casefold()

    # 1. Empty or corrupt artifacts
    if "playwright_fail_closed" in lowered or "emptyfileerror" in lowered or "cannot read an empty file" in lowered:
        return FailureClassification(
            failure_class=FailureClass.EMPTY_OR_CORRUPT_ARTIFACT,
            is_retryable=False,
            reason="Empty or corrupt file artifact; do not retry the same file parse",
        )

    # 2. Auth checkpoint / 2SV
    if any(k in lowered for k in ("2sv", "mfa", "two-step", "two-factor", "verification code", "session expired")):
        return FailureClassification(
            failure_class=FailureClass.AUTH_CHALLENGE,
            is_retryable=False,
            reason="Authentication / 2SV challenge required",
            suggested_hold_status=JobStatus.NEEDS_AUTH,
        )

    # 3. Locator ambiguity & strict mode violations (Non-retryable)
    if any(
        k in lowered
        for k in (
            "playwright_blocked",
            "strict mode violation",
            "resolved to 2 elements",
            "resolved to more than one",
            "positional guess",
            ".first",
            ".nth(",
            ".last",
            "is not unique",
            "selector not unique",
        )
    ):
        return FailureClassification(
            failure_class=FailureClass.LOCATOR_AMBIGUITY,
            is_retryable=False,
            reason="Locator ambiguity or positional guess detected; retrying will not fix ambiguity",
        )

    # 4. Transient network and connection errors (Retryable with backoff)
    if any(
        k in lowered
        for k in (
            "connection refused",
            "connection reset",
            "econnreset",
            "econnrefused",
            "etimedout",
            "network socket",
            "dns lookup",
            "getaddrinfo",
            "502 bad gateway",
            "503 service unavailable",
            "504 gateway timeout",
            "remote end closed connection",
            "timed out after",
            "timeout exceeded",
        )
    ):
        return FailureClassification(
            failure_class=FailureClass.TRANSIENT_NETWORK,
            is_retryable=True,
            reason="Transient network / connection error; safe for bounded retry",
        )

    # Default fallback: non-retryable by default unless explicitly safe
    return FailureClassification(
        failure_class=FailureClass.GENERIC_FAILURE,
        is_retryable=False,
        reason=f"Unclassified failure: {text[:100]}",
    )
