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

import hashlib
import hmac
import math
import re
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

    ``field_spans`` maps field name -> the EvidenceSpan binding that value
    to the exact source text it was extracted from (H1). A field without a
    span has no provenance and must not grade clean.

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
    field_spans: dict[str, EvidenceSpan] = field(default_factory=dict)
    settle_delay_seconds: int = 0
    locked_at: str = ""
    locked_by: str = "planner"
    idempotency: Idempotency = Idempotency.NON_IDEMPOTENT


@dataclass(frozen=True)
class EvidenceSpan:
    """A provenance pointer binding one plan field to its source text.

    ``source_id`` names the authoritative source the value was extracted
    from (e.g. "change_request_text" for the requester's message,
    "agency_context" for identifiers the agency supplied, "policy_api_search"
    for an API fetch). ``source_hash`` is the SHA-256 hex digest of the
    exact source bytes the offsets index into -- see
    :func:`canonical_source_text`; offsets are character positions in that
    canonical text. ``quote`` is the indexed text verbatim. A span whose
    hash does not match the source it is checked against fails closed
    (see :func:`validate_span`): it can never quietly grade a value against
    text the value did not come from.
    """

    source_id: str
    source_hash: str
    offset_start: int
    offset_end: int
    quote: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": str(self.source_id),
            "source_hash": str(self.source_hash),
            "offset_start": int(self.offset_start),
            "offset_end": int(self.offset_end),
            "quote": str(self.quote),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EvidenceSpan":
        if not isinstance(data, Mapping):
            raise ValueError(
                f"evidence span must be a mapping, got {type(data).__name__}"
            )
        try:
            return cls(
                source_id=str(data["source_id"]),
                source_hash=str(data["source_hash"]),
                offset_start=int(data["offset_start"]),
                offset_end=int(data["offset_end"]),
                quote=str(data["quote"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"malformed evidence span {data!r}: {exc}") from exc


class SpanValidationError(RuntimeError):
    """An evidence span does not bind to its claimed source.

    Raised -- never swallowed -- so a tampered or misbound span refuses to
    grade instead of grading against the wrong bytes.
    """


def canonical_source_text(source_text: str | None) -> str:
    """The canonical bytes evidence-span offsets index into.

    Whitespace (including line breaks) is collapsed to single spaces and
    the ends are stripped; case is preserved. The canonical text -- not the
    raw input -- is what ``source_hash`` digests and what ``offset_start``
    / ``offset_end`` address, so a quote that differs only in line breaks
    still indexes deterministically.
    """
    return re.sub(r"\s+", " ", str(source_text or "")).strip()


def span_source_hash(source_text: str | None) -> str:
    """SHA-256 hex of the canonical source bytes (see canonical_source_text)."""
    return hashlib.sha256(canonical_source_text(source_text).encode("utf-8")).hexdigest()


def span_for_quote(
    source_text: str,
    quote: str,
    *,
    source_id: str,
) -> EvidenceSpan:
    """Build the span binding ``quote`` to its occurrence in ``source_text``.

    Finds the quote in the canonical source text: exact match first, then a
    case-insensitive match (the extractor's anti-hallucination check is
    case-insensitive, so a span must be computable for anything it accepts).
    Raises ValueError when the quote is not found -- the caller turns that
    into a human-review reason, never a silently span-less field.
    """
    canonical = canonical_source_text(source_text)
    wanted = canonical_source_text(quote)
    if not wanted:
        raise ValueError("cannot build an evidence span for an empty quote")
    start = canonical.find(wanted)
    if start < 0:
        # Case-insensitive fallback: mirrors the validator's casefold check.
        folded = canonical.casefold()
        start = folded.find(wanted.casefold())
    if start < 0:
        raise ValueError("quote not found in the source text")
    end = start + len(wanted)
    return EvidenceSpan(
        source_id=str(source_id),
        source_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        offset_start=start,
        offset_end=end,
        quote=canonical[start:end],
    )


def validate_span(span: EvidenceSpan, source_text: str | None) -> None:
    """Check one span against its claimed source. Fail closed.

    Raises SpanValidationError when the source bytes no longer hash to the
    span's ``source_hash`` (tampered or wrong source), when the offsets are
    out of range, or when the indexed text differs from the span's quote.
    """
    if not isinstance(span, EvidenceSpan):
        raise SpanValidationError(
            f"expected an EvidenceSpan, got {type(span).__name__}"
        )
    canonical = canonical_source_text(source_text)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(digest, str(span.source_hash)):
        raise SpanValidationError(
            f"span for source {span.source_id!r}: source hash mismatch -- "
            "the source text changed (or is not the text the value came "
            "from); refusing to grade"
        )
    start, end = span.offset_start, span.offset_end
    if (
        not isinstance(start, int)
        or not isinstance(end, int)
        or isinstance(start, bool)
        or isinstance(end, bool)
        or start < 0
        or end < start
        or end > len(canonical)
    ):
        raise SpanValidationError(
            f"span for source {span.source_id!r}: offsets "
            f"[{start}:{end}] are out of range for a {len(canonical)}-char "
            "source; refusing to grade"
        )
    if canonical[start:end] != span.quote:
        raise SpanValidationError(
            f"span for source {span.source_id!r}: the indexed text "
            f"{canonical[start:end]!r} does not match the span quote "
            f"{span.quote!r}; refusing to grade"
        )


def validate_plan_spans(
    plan: LockedPlan,
    sources: Mapping[str, str],
) -> None:
    """Validate every plan field's span against the authoritative sources.

    ``sources`` maps source_id -> the exact source text as fetched from the
    authoritative system. Every field in ``plan.fields`` must carry a span;
    every span's source_id must be present in ``sources``; every span must
    validate against its source bytes. Any gap raises SpanValidationError:
    a plan with missing or misbound provenance is ungradable, never clean.
    """
    if not isinstance(sources, Mapping):
        raise SpanValidationError(
            f"sources must be a mapping, got {type(sources).__name__}"
        )
    spans = plan.field_spans or {}
    for name in plan.fields:
        span = spans.get(name)
        if span is None:
            raise SpanValidationError(
                f"field {name!r} has no evidence span; refusing to grade"
            )
        source_id = span.source_id
        if source_id not in sources:
            raise SpanValidationError(
                f"field {name!r} cites source {source_id!r}, which was not "
                "supplied; refusing to grade"
            )
        validate_span(span, sources[source_id])


@dataclass(frozen=True)
class FieldCheck:
    field: str
    expected: Any
    actual: Any
    matched: bool
    note: str = ""
    pending_settle: bool = False
    span: EvidenceSpan | None = None


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
    sources: Mapping[str, str] | None = None,
) -> EvidenceResult:
    """Compare a locked plan against freshly fetched evidence.

    ``observed`` maps field name -> value the destination actually shows.
    A field absent from ``observed`` (or None) means the destination did not
    show it: within the settle window that is PENDING_SETTLEMENT, after the
    window it is MISMATCH. Precedence: any MISMATCH wins, then
    PENDING_SETTLEMENT, otherwise MATCHED.

    Every FieldCheck carries the plan field's evidence span
    (``plan.field_spans``), so graders and renderers can show and audit the
    exact source text each expected value came from. When ``sources`` is
    given (source_id -> authoritative source text), every span is validated
    first and a missing or misbound span raises SpanValidationError --
    the comparison is refused, never graded against unproven bytes.
    """

    if sources is not None:
        # Fail closed: no provenance, no grade.
        validate_plan_spans(plan, sources)

    captured_at = now or utc_now_iso()
    locked_at = _parse_iso(plan.locked_at) if plan.locked_at else None
    captured_dt = _parse_iso(captured_at)
    if locked_at is not None and captured_dt is not None:
        elapsed = (captured_dt - locked_at).total_seconds()
    else:
        elapsed = float("inf")  # unknown lock time -> no settle grace

    checks: list[FieldCheck] = []
    for name, expected in plan.fields.items():
        span = (plan.field_spans or {}).get(name)
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
                        span=span,
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
                        span=span,
                        note="field not present in destination evidence after settle delay",
                    )
                )
        elif values_equal(expected, actual):
            checks.append(
                FieldCheck(field=name, expected=expected, actual=actual, matched=True, span=span)
            )
        else:
            checks.append(
                FieldCheck(
                    field=name,
                    expected=expected,
                    actual=actual,
                    matched=False,
                    span=span,
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
