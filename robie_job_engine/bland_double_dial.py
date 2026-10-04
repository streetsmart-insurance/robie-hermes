"""Double-dial policy for Bland outbound carrier calls (pure; no I/O).

No network, no secrets, no clock of its own: the durable worker
(``double_dial_worker``) supplies call details, timestamps, and dispatch.
Nothing here places a call.

Policy ``double-dial-v1``. The defaults below are draft working constraints
# only; the owner has NOT ruled on target audience, retry scope, timing, or
# any live calling. Nothing here places a call, and no merge, deploy, or live
# call is authorized.

- Exactly ONE redial per target, only when the first call is conclusively
  classified ``VOICEMAIL_NO_MESSAGE`` from call-detail evidence. A queued or
  completed API status is never, on its own, evidence of the call's outcome.
- The redial uses the same pinned caller ID, fires ``redial_delay_seconds``
  (default 10s) after the conclusive attempt-1 outcome, and must dispatch
  within ``redial_window_seconds`` of the FIRST dispatch (default 180s,
  outer bound). A missed window is recorded as ``MISSED_WINDOW``; a late
  dial is never placed.
- Voicemail behavior per attempt (production call config recapped by Jake
  on 2026-09-28): attempt 1 reaching voicemail (or screening with no
  pickup) hangs up and leaves NO message; attempt 2 reaching voicemail
  LEAVES the message (``build_attempt2_voicemail_script``).
- No redial after human contact or on any second attempt. No third call.
- ``NO_ANSWER`` and ``BUSY`` are terminal by default; their redial
  eligibility sits behind config flags pending the owner's ruling.
  ``SCREENER_DECLINED`` (call ended, screener engaged, no human pickup) is
  redial-ELIGIBLE - Jake's configured choice within the voice-settings
  lane Carlo delegated to him; the config flag flips it without rework.
- The name-and-reason call screener (iOS call screening, Google Call
  Screen) is a LIVE-WAIT state, never terminal: the agent answers the
  name/reason question immediately, then stays on the line silently until
  connected, told the person is unavailable, or the call rolls to
  voicemail. Screener engagement is checked BEFORE voicemail so it is
  never misclassified as a mailbox.
- Identity on every call (first breath, before anything else; screeners
  cut in after ~2 seconds): ``FIRST_BREATH_TEMPLATE`` - "Eva, an AI
  assistant calling on behalf of Jake from StreetSmart Insurance". AI
  disclosure stays in the first sentence. Pace: slow and measured, one or
  two sentences per response.
- Carriers only. Client, insured, prospect, or row-supplied numbers are
  refused; the target must come from the carrier directory and must not
  match any known client/insured phone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Mapping, Optional, Sequence, Tuple

POLICY_VERSION = "double-dial-v1"
PINNED_CALLER_ID = "+17322986745"

# Default: first dispatch to second dispatch under 180 seconds, zero
# intentional delay after a conclusive voicemail-no-message classification.
DEFAULT_REDIAL_WINDOW_SECONDS = 180


class CallOutcome(str, Enum):
    """Terminal and interim outcomes for one call attempt."""

    PENDING = "pending"  # call still queued/in progress; not conclusive
    HUMAN_REACHED = "human_reached"  # corroborated human conversation
    VOICEMAIL_NO_MESSAGE = "voicemail_no_message"  # redial-eligible
    VOICEMAIL_MESSAGE_LEFT = "voicemail_message_left"  # never redial
    SCREENER_WAITING = "screener_waiting"  # screener engaged mid-call; hold silently
    SCREENER_DECLINED = "screener_declined"  # screener engaged, no human
    NO_ANSWER = "no_answer"
    BUSY = "busy"
    FAILED_DISPATCH = "failed_dispatch"  # phone never rang; retryable
    UNKNOWN = "unknown"  # inconclusive evidence (incl. poll timeout)
    MISSED_WINDOW = "missed_window"  # redial window expired before dispatch


# Queue/transport statuses where the phone never rang. Mirrors the queue
# discipline in audit_verification_worker.classify_voice_result: a failed
# dispatch is retryable and must NOT consume the policy's single redial.
QUEUE_ERROR_STATUSES = frozenset({"pre_queue_error", "queue_error", "error"})
IN_PROGRESS_STATUSES = frozenset(
    {"queued", "started", "in_progress", "ringing", "initiated", "allocated"}
)
NO_ANSWER_STATUSES = frozenset({"no_answer", "no-answer", "noanswer"})
BUSY_STATUSES = frozenset({"busy", "user_busy"})
FAILED_STATUSES = frozenset({"failed", "canceled", "cancelled", "rejected"})

# Name-and-reason call screeners: Apple iOS call screening, Google Call
# Screen, carrier spam-filter assistants. Transcript-matched so the policy
# answers briefly and waits for a human instead of misreading the screener
# as a mailbox. Keep patterns conservative; ambiguity degrades to UNKNOWN.
_SCREENER_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"screening (service|tool|assistant)",
        r"using a screening",
        r"google (assistant|call screen)",
        r"may i ask who'?s calling",
        r"(who is|who'?s) calling",
        r"state your name and (the )?reason",
        r"name and (the )?reason (for|of) (your|the|this) call",
        r"purpose of (your|the|this) call",
        r"can i (tell|let) (them|him|her) know who'?s calling",
        r"reason (for|of) (your|the|this) call",
    )
)

# Markers that, after a screener exchange, indicate a human actually
# accepted and the conversation continued.
_HUMAN_ACCEPTANCE_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bhello\?",
        r"\bthis is [a-z]+\b",
        r"\bspeaking\b",
        r"\bhow can i help\b",
    )
)


class TargetRefused(ValueError):
    """Raised when a dial target violates the carriers-only boundary."""


@dataclass(frozen=True)
class DoubleDialConfig:
    """Owner-rulable knobs. Defaults encode working constraints, not rulings."""

    policy_version: str = POLICY_VERSION
    caller_id: str = PINNED_CALLER_ID
    redial_window_seconds: int = DEFAULT_REDIAL_WINDOW_SECONDS
    # Delay AFTER the conclusive attempt-1 outcome before the redial may
    # dispatch (Jake's 2026-09-28 config: 10 seconds, changed from 90).
    redial_delay_seconds: int = 10
    # Pending the owner's retry-scope ruling; default terminal:
    redial_on_no_answer: bool = False
    redial_on_busy: bool = False
    # Jake's configured choice (2026-09-28 iMessage), within the
    # voice-settings lane Carlo delegated to him the same day: a first
    # call that goes straight to voicemail OR a screen with no pickup
    # earns the automatic callback within 10 seconds. Flag stays so a
    # future change flips it without rework.
    redial_on_screener_declined: bool = True
    # Corroboration thresholds for HUMAN_REACHED and screener acceptance.
    min_human_call_minutes: float = 0.05  # ~3 seconds
    # Bounded polling for call detail (durable worker enforces).
    poll_intervals_seconds: Tuple[int, ...] = (15, 30, 60, 60, 120, 120)
    max_poll_seconds: int = 900
    # Optional marker proving our voicemail message was left (must be None
    # for double-dial calls, which never leave a message).
    voicemail_message_marker: Optional[str] = None


@dataclass(frozen=True)
class OutcomeClassification:
    outcome: CallOutcome
    conclusive: bool
    reason: str
    retryable_dispatch: bool = False  # phone never rang; redial not consumed


def _norm(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "_").replace("-", "_")


def _transcript(detail: Mapping[str, Any]) -> str:
    for key in ("concatenated_transcript", "transcript", "summary"):
        text = detail.get(key)
        if isinstance(text, str) and text.strip():
            return text
    return ""


def matches_screener(transcript: str) -> bool:
    return any(p.search(transcript) for p in _SCREENER_PATTERNS)


def classify_outcome(
    detail: Optional[Mapping[str, Any]], *, config: DoubleDialConfig = DoubleDialConfig()
) -> OutcomeClassification:
    """Classify one call-detail read (pure).

    ``detail`` is a Bland call-detail / post-call webhook shaped mapping
    (completed, queue_status, status, answered_by, call_length, price,
    error_message, concatenated_transcript, ...). Queue acceptance or a
    completed API status is never treated as a conversation on its own.
    """
    detail = detail or {}
    queue_status = _norm(detail.get("queue_status"))
    status = _norm(detail.get("status"))
    answered_by = _norm(detail.get("answered_by"))
    error_message = str(detail.get("error_message") or "").strip()
    transcript = _transcript(detail)

    # 1. Transport failure: phone never rang. Retryable; redial not consumed.
    if queue_status in QUEUE_ERROR_STATUSES or status in FAILED_STATUSES:
        return OutcomeClassification(
            CallOutcome.FAILED_DISPATCH, True,
            f"dispatch failed before ring (queue_status={queue_status or 'n/a'}, "
            f"status={status or 'n/a'}): {error_message[:120] or 'no error message'}",
            retryable_dispatch=True,
        )

    # 2. Still in flight: not conclusive, keep polling. A screener heard
    #    mid-call is a LIVE-WAIT state, never terminal: the agent has
    #    answered name/reason and now holds silently for a human.
    completed = detail.get("completed")
    if completed is not True and (
        queue_status in IN_PROGRESS_STATUSES
        or status in IN_PROGRESS_STATUSES
        or completed is False
    ):
        if matches_screener(transcript):
            return OutcomeClassification(
                CallOutcome.SCREENER_WAITING, False,
                "screener engaged mid-call - holding silently for human pickup",
            )
        return OutcomeClassification(
            CallOutcome.PENDING, False,
            f"call not finished (queue_status={queue_status or 'n/a'})",
        )
    if not queue_status and not status and not answered_by and not transcript:
        return OutcomeClassification(
            CallOutcome.UNKNOWN, False, "call detail empty - no evidence yet",
        )

    # 3. Terminal telephony outcomes without a conversation.
    if status in NO_ANSWER_STATUSES or answered_by in NO_ANSWER_STATUSES:
        return OutcomeClassification(
            CallOutcome.NO_ANSWER, True, "call ended unanswered",
        )
    if status in BUSY_STATUSES or answered_by in BUSY_STATUSES:
        return OutcomeClassification(CallOutcome.BUSY, True, "line busy")

    # 4. Screener detection happens BEFORE voicemail: a name-and-reason
    #    screener transcript is never a mailbox.
    screener = matches_screener(transcript)
    human_accepted = screener and any(
        p.search(transcript) for p in _HUMAN_ACCEPTANCE_PATTERNS
    )
    if screener and not human_accepted:
        return OutcomeClassification(
            CallOutcome.SCREENER_DECLINED, True,
            "name-and-reason screener engaged; no human acceptance observed",
        )

    # 5. Human: answered_by=human must be corroborated by transcript
    #    evidence of an actual exchange (or screener-then-human), never by
    #    the status field alone.
    if answered_by == "human" or human_accepted:
        length = detail.get("call_length")
        try:
            minutes = float(length) if length is not None else 0.0
        except (TypeError, ValueError):
            minutes = 0.0
        if transcript and minutes >= config.min_human_call_minutes:
            return OutcomeClassification(
                CallOutcome.HUMAN_REACHED, True,
                "answered_by=human corroborated by transcript and duration",
            )
        return OutcomeClassification(
            CallOutcome.UNKNOWN, False,
            "answered_by=human without corroborating transcript/duration",
        )

    # 6. Voicemail: distinguish message-left (never redial) from
    #    died-at-voicemail (the only default redial trigger).
    if answered_by == "voicemail":
        marker = config.voicemail_message_marker
        if marker and marker in transcript:
            return OutcomeClassification(
                CallOutcome.VOICEMAIL_MESSAGE_LEFT, True,
                "voicemail message marker present in transcript",
            )
        return OutcomeClassification(
            CallOutcome.VOICEMAIL_NO_MESSAGE, True,
            "answered_by=voicemail with no message-left evidence",
        )

    return OutcomeClassification(
        CallOutcome.UNKNOWN, False,
        f"inconclusive evidence (queue_status={queue_status or 'n/a'}, "
        f"answered_by={answered_by or 'n/a'})",
    )


def redial_eligible(
    classification: OutcomeClassification, *, config: DoubleDialConfig = DoubleDialConfig()
) -> bool:
    """Whether an attempt-1 outcome is eligible for the single redial."""
    if not classification.conclusive:
        return False
    outcome = classification.outcome
    if outcome is CallOutcome.VOICEMAIL_NO_MESSAGE:
        return True
    if outcome is CallOutcome.NO_ANSWER:
        return config.redial_on_no_answer
    if outcome is CallOutcome.BUSY:
        return config.redial_on_busy
    if outcome is CallOutcome.SCREENER_DECLINED:
        return config.redial_on_screener_declined
    return False


@dataclass(frozen=True)
class RedialDecision:
    redial: bool
    reason: str
    deadline: Optional[datetime] = None  # first dispatch + window (outer bound)
    not_before: Optional[datetime] = None  # outcome + redial_delay_seconds
    missed_window: bool = False


def decide_redial(
    *,
    attempt_outcomes: Sequence[CallOutcome],
    classification: OutcomeClassification,
    first_dispatch_at: datetime,
    now: datetime,
    caller_id: str,
    config: DoubleDialConfig = DoubleDialConfig(),
    first_outcome_at: Optional[datetime] = None,
) -> RedialDecision:
    """Decide whether the single redial may fire now (pure).

    ``attempt_outcomes`` holds terminal outcomes already recorded for this
    target under this policy version, oldest first (excluding the pending
    attempt-2 row). The redial guard: exactly one redial, only off attempt
    1, only on an eligible conclusive outcome, same pinned caller ID, and
    only inside the window anchored to the FIRST dispatch.
    """
    if caller_id != config.caller_id:
        return RedialDecision(
            False, f"caller ID {caller_id!r} is not the pinned {config.caller_id!r}",
        )
    if len(attempt_outcomes) >= 2 or any(
        o is CallOutcome.HUMAN_REACHED for o in attempt_outcomes
    ):
        return RedialDecision(False, "second attempt exists or human reached - no third call")
    if len(attempt_outcomes) != 1:
        return RedialDecision(False, "redial decision requires exactly one prior attempt")
    deadline = first_dispatch_at + timedelta(seconds=config.redial_window_seconds)
    if now > deadline:
        return RedialDecision(
            False,
            f"window closed at {deadline.isoformat()} - record MISSED_WINDOW, never dial late",
            deadline=deadline, missed_window=True,
        )
    if not redial_eligible(classification, config=config):
        return RedialDecision(
            False,
            f"outcome {classification.outcome.value} is not redial-eligible: "
            f"{classification.reason}",
            deadline=deadline,
        )
    not_before = None
    if first_outcome_at is not None:
        not_before = first_outcome_at + timedelta(seconds=config.redial_delay_seconds)
        if now < not_before:
            return RedialDecision(
                False,
                f"redial delay: wait until {not_before.isoformat()} "
                f"({config.redial_delay_seconds}s after the attempt-1 outcome)",
                deadline=deadline, not_before=not_before,
            )
    return RedialDecision(
        True,
        "conclusive voicemail-no-message, delay honored, within window - "
        "redial once, same caller ID",
        deadline=deadline, not_before=not_before,
    )


_E164 = re.compile(r"^\+[1-9]\d{7,14}$")

# Where a target number came from. Only the carrier directory is dialable.
ALLOWED_NUMBER_SOURCES = frozenset({"carrier_directory"})
REFUSED_NUMBER_SOURCES = frozenset(
    {"row", "client", "insured", "prospect", "applicant", "manual", "unknown"}
)


def validate_target(
    *,
    target_number: str,
    number_source: str,
    client_phones: Sequence[str] = (),
) -> str:
    """Enforce the carriers-only boundary. Returns the normalized number.

    Mirrors the audit worker's hard rule: directory-resolved carrier
    numbers only; row-supplied phones are never dialed; a directory number
    matching a client/insured phone on the row is refused.
    """
    number = (target_number or "").strip()
    source = (number_source or "unknown").strip().lower()
    if source not in ALLOWED_NUMBER_SOURCES:
        raise TargetRefused(
            f"number source {source!r} refused: only {sorted(ALLOWED_NUMBER_SOURCES)} "
            "is dialable (carriers only - never clients, prospects, or row values)"
        )
    if not _E164.fullmatch(number):
        raise TargetRefused(f"target {number!r} is not E.164")
    for client in client_phones:
        if client and client.strip() == number:
            raise TargetRefused(
                "directory number matches a client/insured phone on the row - refused"
            )
    return number


# --- Call scripts (Jake's 2026-09-28 production config recap) ------------
#
# Subject to the owner's scope/audience rulings. Identity: Eva, an AI
# assistant calling on behalf of Jake from StreetSmart Insurance. The first
# breath is the full identity as ONE self-contained statement before any
# question - phone screeners cut in after ~2 seconds, so nothing precedes
# it. AI disclosure stays in the first sentence on EVERY call. Pace: slow
# and measured throughout; every response one or two sentences.

AGENT_NAME = "Eva"
AGENT_IDENTITY = (
    "Eva, an AI assistant calling on behalf of Jake from StreetSmart Insurance"
)
CALLBACK_NUMBER_SPOKEN = "732-481-2520"
PACE_GUIDANCE = (
    "Speak slowly and evenly. Every response is one or two sentences. "
    "Never rush; pause between turns."
)

FIRST_BREATH_TEMPLATE = (
    "This is Eva, an AI assistant calling on behalf of Jake from "
    "StreetSmart Insurance, {reason}."
)

SCREENER_GUIDANCE = (
    "If a screening service asks who is calling and why, answer immediately "
    "and directly with your name and reason; repeat it if asked. Then stay "
    "on the line silently and wait to be connected. Never say goodbye or "
    "hang up unless you are told the person is unavailable or the call "
    "rolls to voicemail."
)


def build_first_breath(reason: str) -> str:
    """The mandatory opening statement; nothing may precede it."""
    if not (reason or "").strip():
        raise ValueError("first breath requires a call reason")
    return FIRST_BREATH_TEMPLATE.format(reason=reason.strip().rstrip("."))


def build_attempt2_voicemail_script(reason: str) -> str:
    """Attempt-2 voicemail: attempt 1 leaves NO message; attempt 2 leaves
    this one - slow and clear, Eva identity, callback number."""
    first = build_first_breath(reason)
    return (
        f"{first} {PACE_GUIDANCE} Please call us back at "
        f"{CALLBACK_NUMBER_SPOKEN}. Again, that is {CALLBACK_NUMBER_SPOKEN}. "
        "Thank you."
    )


def utc_from_epoch(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc)
