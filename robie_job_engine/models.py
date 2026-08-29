from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class JobStatus(str, Enum):
    PENDING = "PENDING"
    NEEDS_SKILL = "NEEDS_SKILL"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    NEEDS_AUTH = "NEEDS_AUTH"
    AWAITING_HUMAN_INPUT = "AWAITING_HUMAN_INPUT"
    WAITING = "WAITING"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    RETRY_WAIT = "RETRY_WAIT"
    PAUSED = "PAUSED"
    COMPLETE = "COMPLETE"
    UNVERIFIED = "UNVERIFIED"
    FAILED = "FAILED"


TERMINAL_STATUSES = {JobStatus.COMPLETE, JobStatus.UNVERIFIED, JobStatus.FAILED}
WAITING_STATUSES = {
    JobStatus.PAUSED,
    JobStatus.NEEDS_SKILL,
    JobStatus.NEEDS_CLARIFICATION,
    JobStatus.NEEDS_AUTH,
    JobStatus.AWAITING_HUMAN_INPUT,
    JobStatus.WAITING,
}
# The action worker never receives this token. JobEngine._verify is the
# only caller allowed to pass it into JobStore.transition.
VERIFIER_AUTHORITY = "independent-verifier"
ACTION_OUTCOME_UNKNOWN = "ACTION_OUTCOME_UNKNOWN"


class ReconciliationOutcome(str, Enum):
    """Authoritative read-before-write result after an interrupted action intent."""

    APPLIED = "APPLIED"
    NOT_APPLIED = "NOT_APPLIED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class WorkerResult:
    """A worker may report only what it attempted, never job completion."""

    succeeded: bool
    action: str
    destination: dict[str, Any]
    detail: dict[str, Any] = field(default_factory=dict)
    retryable: bool = True
    error: str | None = None
    hold_status: JobStatus | None = None


@dataclass(frozen=True)
class ReconciliationResult:
    """Result of reconciling destination state before a possible repeat write.

    ``APPLIED`` and ``NOT_APPLIED`` are actionable only when ``authoritative``
    is true. Any missing, stale, ambiguous, or unavailable read-back is
    ``UNKNOWN`` and must not allow the worker to repeat the consequence.
    """

    outcome: ReconciliationOutcome
    action: str
    destination: dict[str, Any] = field(default_factory=dict)
    detail: dict[str, Any] = field(default_factory=dict)
    authoritative: bool = False
    error: str | None = None
    hold_status: JobStatus = JobStatus.WAITING


@dataclass(frozen=True)
class VerificationEvidence:
    method: str
    source: str
    expected: dict[str, Any]
    observed: dict[str, Any]
    authoritative: bool
    captured_at: str
    locator: str | None = None


@dataclass(frozen=True)
class VerificationResult:
    verified: bool
    evidence: VerificationEvidence
    retryable: bool = False
    error: str | None = None
    hold_status: JobStatus | None = None
