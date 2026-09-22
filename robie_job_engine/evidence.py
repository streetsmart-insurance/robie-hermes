"""ROBIE verification pattern: Plan -> Execute -> Evidence -> Outcome.

Every job locks a checklist (the plan) before any work starts, then the
work is checked field by field against independently re-fetched destination
evidence. The comparison itself is generic; per-job-type work lives in the
plan extractor (which fields matter) and the evidence fetcher (which API or
page proves them).

Outcomes are a fixed set -- MATCHED, MISMATCH, NO_EVIDENCE,
PENDING_SETTLEMENT -- decided by comparison against the locked plan. There
is no confidence score and no second AI opinion.

Dispositions (decided by the caller/engine from the outcome + attempt +
the job type's idempotency declaration):
  MATCHED           -> CLOSE_CLEAN
  MISMATCH          -> RETRY_ONCE (first attempt only, and only if the job
                       type declares re-execution safe), then HUMAN_QUEUE
  NO_EVIDENCE       -> ALERT_AND_RETRY_DELAYED (alert immediately; the
                       delayed retry re-runs only the evidence READ, never
                       the executor -- reads are safe. Then HUMAN_QUEUE.)
  PENDING_SETTLEMENT-> RETRY_DELAYED (destination hasn't caught up yet;
                       re-run evidence after the settle delay)

Retry idempotency: "retry once" re-executes against a system the first
attempt may have partially changed -- a note job that retries posts the
note twice. So every job type must declare, up front, its answer to "is
re-running this safe" (see Idempotency). Automatic retry is allowed only
for IDEMPOTENT and VERIFY_BEFORE_RETRY jobs; anything else -- including
undeclared job types, which default to NON_IDEMPOTENT -- goes straight
to a human on MISMATCH instead of auto-retrying.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Mapping


class EvidenceOutcome(str, Enum):
    MATCHED = "MATCHED"
    MISMATCH = "MISMATCH"
    NO_EVIDENCE = "NO_EVIDENCE"
    PENDING_SETTLEMENT = "PENDING_SETTLEMENT"


class Disposition(str, Enum):
    CLOSE_CLEAN = "CLOSE_CLEAN"
    RETRY_ONCE = "RETRY_ONCE"
    RETRY_DELAYED = "RETRY_DELAYED"
    HUMAN_QUEUE = "HUMAN_QUEUE"
    ALERT_AND_RETRY_DELAYED = "ALERT_AND_RETRY_DELAYED"


class Idempotency(str, Enum):
    """A job type's declared answer to "is re-running this safe?"

    Declared by the plan extractor before any retry is automatic.

    IDEMPOTENT: re-execution sets the same end state, e.g. writing a
        field to an exact value. Safe to retry blindly.
    VERIFY_BEFORE_RETRY: re-execution is safe only if the retry first
        re-reads evidence and re-executes only the still-missing delta,
        e.g. a note append that checks the discussion for identical
        content before posting. The executor owns that verify-first
        contract; the disposition only permits the retry.
    NON_IDEMPOTENT: any re-execution has side effects (sends another
        email, posts a second note). Never auto-retried: MISMATCH goes
        straight to HUMAN_QUEUE.
    """

    IDEMPOTENT = "IDEMPOTENT"
    VERIFY_BEFORE_RETRY = "VERIFY_BEFORE_RETRY"
    NON_IDEMPOTENT = "NON_IDEMPOTENT"


@dataclass(frozen=True)
class LockedPlan:
    """The checklist, locked before Execute starts.

    ``fields`` maps field name -> expected value. ``field_tiers`` maps field
    name -> "A" (API read-back) or "B" (fresh page read). Fields the plan
    extractor cannot map to any evidence tier are recorded with tier "B" and
    a note -- never silently dropped.

    ``idempotency`` is the job type's declared answer to "is re-running
    this safe" (see Idempotency). It gates automatic retry on MISMATCH.
    Undeclared job types default to NON_IDEMPOTENT: fail closed, no blind
    retry.
    """

    job_id: str
    job_type: str
    fields: dict[str, Any]
    field_tiers: dict[str, str] = field(default_factory=dict)
    field_notes: dict[str, str] = field(default_factory=dict)
    settle_delay_seconds: int = 0
    locked_at: str = ""
    locked_by: str = "planner"
    idempotency: Idempotency = Idempotency.NON_IDEMPOTENT


@dataclass(frozen=True)
class FieldCheck:
    field: str
    expected: Any
    actual: Any
    matched: bool
    note: str = ""
    pending_settle: bool = False


@dataclass(frozen=True)
class EvidenceResult:
    outcome: EvidenceOutcome
    checks: tuple[FieldCheck, ...]
    detail: str
    captured_at: str
    plan_job_id: str


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _looks_like_date(text: str) -> bool:
    text = text.strip()
    if len(text) < 10:
        return False
    head = text[:10]
    return (
        len(head) == 10
        and head[4] == "-"
        and head[7] == "-"
        and head.replace("-", "").isdigit()
    )


def _to_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        cleaned = value.strip().replace(",", "").replace("$", "")
        if not cleaned:
            return None
        try:
            result = float(cleaned)
        except ValueError:
            return None
    else:
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return result


def values_equal(expected: Any, actual: Any) -> bool:
    """Compare one planned value against one observed value.

    Rules, in order:
    - both missing (None) -> equal
    - both numeric-like ("100.00" vs 100 vs "$100") -> numeric compare
    - both date-like ("2026-09-01" vs "2026-09-01T00:00:00") -> date-part compare
    - otherwise -> stripped, case-insensitive string compare
    """

    if expected is None and actual is None:
        return True
    if expected is None or actual is None:
        return False
    expected_num = _to_float(expected)
    actual_num = _to_float(actual)
    if expected_num is not None and actual_num is not None:
        return abs(expected_num - actual_num) < 0.005
    expected_text = str(expected).strip()
    actual_text = str(actual).strip()
    if _looks_like_date(expected_text) and _looks_like_date(actual_text):
        return expected_text[:10] == actual_text[:10]
    return expected_text.casefold() == actual_text.casefold()


def compare_plan_to_evidence(
    plan: LockedPlan,
    observed: Mapping[str, Any],
    *,
    now: str | None = None,
) -> EvidenceResult:
    """Compare a locked plan against freshly fetched evidence.

    ``observed`` maps field name -> value the destination actually shows.
    A field absent from ``observed`` (or None) means the destination did not
    show it: within the settle window that is PENDING_SETTLEMENT, after the
    window it is MISMATCH. Precedence: any MISMATCH wins, then
    PENDING_SETTLEMENT, otherwise MATCHED.
    """

    captured_at = now or utc_now_iso()
    locked_at = _parse_iso(plan.locked_at) if plan.locked_at else None
    captured_dt = _parse_iso(captured_at)
    if locked_at is not None and captured_dt is not None:
        elapsed = (captured_dt - locked_at).total_seconds()
    else:
        elapsed = float("inf")  # unknown lock time -> no settle grace

    checks: list[FieldCheck] = []
    for name, expected in plan.fields.items():
        actual = observed.get(name) if isinstance(observed, Mapping) else None
        if actual is None:
            if elapsed < plan.settle_delay_seconds:
                checks.append(
                    FieldCheck(
                        field=name,
                        expected=expected,
                        actual=None,
                        matched=False,
                        pending_settle=True,
                        note=(
                            "not yet visible at destination; "
                            f"{int(plan.settle_delay_seconds - elapsed)}s of settle delay remain"
                        ),
                    )
                )
            else:
                checks.append(
                    FieldCheck(
                        field=name,
                        expected=expected,
                        actual=None,
                        matched=False,
                        note="field not present in destination evidence after settle delay",
                    )
                )
        elif values_equal(expected, actual):
            checks.append(
                FieldCheck(field=name, expected=expected, actual=actual, matched=True)
            )
        else:
            checks.append(
                FieldCheck(
                    field=name,
                    expected=expected,
                    actual=actual,
                    matched=False,
                    note="destination value differs from locked plan",
                )
            )

    mismatched = [c for c in checks if not c.matched and not c.pending_settle]
    pending = [c for c in checks if c.pending_settle]

    if mismatched:
        outcome = EvidenceOutcome.MISMATCH
    elif pending:
        outcome = EvidenceOutcome.PENDING_SETTLEMENT
    else:
        outcome = EvidenceOutcome.MATCHED

    if outcome is EvidenceOutcome.MATCHED:
        detail = f"all {len(checks)} planned field(s) confirmed at the destination"
    elif outcome is EvidenceOutcome.PENDING_SETTLEMENT:
        detail = (
            "destination has not caught up yet; "
            f"waiting on field(s): {', '.join(c.field for c in pending)}"
        )
    else:
        detail = "; ".join(
            f"{c.field}: expected {c.expected!r}, observed {c.actual!r} ({c.note})"
            for c in mismatched
        )

    return EvidenceResult(
        outcome=outcome,
        checks=tuple(checks),
        detail=detail,
        captured_at=captured_at,
        plan_job_id=plan.job_id,
    )


class EvidenceUnavailable(RuntimeError):
    """The evidence check itself could not run (session dead, API down,
    page didn't load). Never a verdict on the work -- always NO_EVIDENCE."""


def run_evidence_check(
    plan: LockedPlan,
    fetch: Callable[[], Mapping[str, Any]],
    *,
    now: str | None = None,
) -> EvidenceResult:
    """Fetch fresh evidence and compare it against the locked plan.

    ``fetch`` must re-read the destination independently -- never from the
    executing agent's memory. Any failure inside ``fetch`` (including
    unexpected exceptions) becomes NO_EVIDENCE, never a fabricated pass.
    """

    captured_at = now or utc_now_iso()
    try:
        observed = fetch()
    except EvidenceUnavailable as exc:
        return EvidenceResult(
            outcome=EvidenceOutcome.NO_EVIDENCE,
            checks=(),
            detail=f"evidence fetch failed: {exc}",
            captured_at=captured_at,
            plan_job_id=plan.job_id,
        )
    except Exception as exc:  # fail closed: an exploding check is NO_EVIDENCE
        return EvidenceResult(
            outcome=EvidenceOutcome.NO_EVIDENCE,
            checks=(),
            detail=f"evidence fetch raised unexpectedly: {type(exc).__name__}: {exc}",
            captured_at=captured_at,
            plan_job_id=plan.job_id,
        )
    if not isinstance(observed, Mapping):
        return EvidenceResult(
            outcome=EvidenceOutcome.NO_EVIDENCE,
            checks=(),
            detail=f"evidence fetch returned unusable shape: {type(observed).__name__}",
            captured_at=captured_at,
            plan_job_id=plan.job_id,
        )
    return compare_plan_to_evidence(plan, observed, now=captured_at)


def next_disposition(
    outcome: EvidenceOutcome,
    attempt_number: int,
    idempotency: Idempotency = Idempotency.NON_IDEMPOTENT,
) -> Disposition:
    """Decide what happens next for an evidence outcome.

    attempt_number counts evidence attempts already made (0 = first check).
    idempotency is the job type's declared answer to "is re-running this
    safe" -- undeclared job types default to NON_IDEMPOTENT (fail closed:
    no automatic retry; straight to a human on MISMATCH).

    The NO_EVIDENCE and PENDING_SETTLEMENT retries only re-run the
    evidence READ, never the executor, so idempotency does not gate them.
    """

    if outcome is EvidenceOutcome.MATCHED:
        return Disposition.CLOSE_CLEAN
    if outcome is EvidenceOutcome.PENDING_SETTLEMENT:
        return Disposition.RETRY_DELAYED
    if outcome is EvidenceOutcome.NO_EVIDENCE:
        # Alert immediately either way; one delayed retry of the READ,
        # then a human.
        if attempt_number < 1:
            return Disposition.ALERT_AND_RETRY_DELAYED
        return Disposition.HUMAN_QUEUE
    # MISMATCH: retry once only if the job type declares re-execution
    # safe. A blind retry of a non-idempotent job re-applies partial work
    # (e.g. posts the note a second time) -- those go to a human instead.
    if idempotency is Idempotency.NON_IDEMPOTENT:
        return Disposition.HUMAN_QUEUE
    if attempt_number < 1:
        return Disposition.RETRY_ONCE
    return Disposition.HUMAN_QUEUE
