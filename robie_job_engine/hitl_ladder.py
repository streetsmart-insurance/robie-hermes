"""Global HITL ladder for every Hermes job (email or Chat).

Carlo 2026-09-13:

1. When stuck: ask Gemini first. Apply the answer. Keep going.
   Do not fail-closed after one miss.
2. After Gemini answers, go back to the originating job and continue.
   Do not ignore Gemini. Do not sit on the suggestion.
3. Loop Carlo only if it still fails after Gemini + at least one applied retry.
4. If Carlo does not reply to that HITL in 30 minutes, KILL the job
   (honest terminal FAILED, not fake RUNNING).
5. Same rules for email and Chat.

Unguessable facts (coverage amounts missing from the job) skip Gemini —
Gemini must not invent dollars — and go straight to Carlo.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .models import JobStatus


HITL_NO_REPLY_SECONDS = 1800
HITL_NO_REPLY_ERROR = (
    "HITL_NO_REPLY: Carlo did not reply within 30 minutes; job killed."
)
HITL_POSTED_AT_KEY = "hitl_posted_at"

ACTION_ASK_GEMINI = "ask_gemini"
ACTION_CONTINUE = "continue"
ACTION_AWAIT_HUMAN = "await_human"
ACTION_KILL = "kill"


def utc_now_dt() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso_datetime(value: str | None) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(frozen=True)
class HitlLadderState:
    """Facts the ladder needs. Channel is 'email' or 'chat' (or either)."""

    gemini_asked: bool = False
    gemini_actionable: bool = False
    gemini_applied: bool = False
    applied_retry_attempted: bool = False
    applied_retry_failed: bool = False
    unguessable: bool = False
    carlo_hitl_posted_at: datetime | None = None
    carlo_replied: bool = False
    channel: str = "chat"


@dataclass(frozen=True)
class HitlLadderDecision:
    action: str
    status: str
    reason: str
    loop_carlo: bool = False
    apply_gemini: bool = False
    channel: str = "chat"

    def is_continue(self) -> bool:
        return self.action == ACTION_CONTINUE

    def is_await_human(self) -> bool:
        return self.action == ACTION_AWAIT_HUMAN

    def is_kill(self) -> bool:
        return self.action == ACTION_KILL


def decide_hitl_ladder(
    state: HitlLadderState,
    *,
    now: datetime | None = None,
) -> HitlLadderDecision:
    """Pure HITL ladder. Same table for email and Chat."""
    now = now or utc_now_dt()
    channel = str(state.channel or "chat").strip().casefold() or "chat"

    if state.carlo_replied:
        return HitlLadderDecision(
            action=ACTION_CONTINUE,
            status=JobStatus.RUNNING.value,
            reason="Carlo replied; resume the originating job.",
            channel=channel,
        )

    if state.carlo_hitl_posted_at is not None:
        posted = state.carlo_hitl_posted_at
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        age = now - posted
        if age >= timedelta(seconds=HITL_NO_REPLY_SECONDS):
            return HitlLadderDecision(
                action=ACTION_KILL,
                status=JobStatus.FAILED.value,
                reason=HITL_NO_REPLY_ERROR,
                channel=channel,
            )
        return HitlLadderDecision(
            action=ACTION_AWAIT_HUMAN,
            status=JobStatus.AWAITING_HUMAN_INPUT.value,
            reason="HITL posted; waiting for Carlo (30-minute kill clock).",
            loop_carlo=True,
            channel=channel,
        )

    if state.unguessable:
        return HitlLadderDecision(
            action=ACTION_AWAIT_HUMAN,
            status=JobStatus.AWAITING_HUMAN_INPUT.value,
            reason=(
                "STOP AND ASK. A required fact is not on the job and must "
                "not be guessed (Gemini will not invent it)."
            ),
            loop_carlo=True,
            channel=channel,
        )

    if not state.gemini_asked:
        return HitlLadderDecision(
            action=ACTION_ASK_GEMINI,
            status=JobStatus.RUNNING.value,
            reason="Stuck: ask Gemini first. Do not fail-closed after one miss.",
            apply_gemini=True,
            channel=channel,
        )

    if state.gemini_actionable and not state.gemini_applied:
        return HitlLadderDecision(
            action=ACTION_CONTINUE,
            status=JobStatus.RUNNING.value,
            reason="Gemini answered. Apply the suggestion and continue.",
            apply_gemini=True,
            channel=channel,
        )

    if state.gemini_applied and not state.applied_retry_attempted:
        return HitlLadderDecision(
            action=ACTION_CONTINUE,
            status=JobStatus.RUNNING.value,
            reason="Gemini was applied. Retry the originating step once.",
            apply_gemini=True,
            channel=channel,
        )

    if (
        state.gemini_applied
        and state.applied_retry_attempted
        and not state.applied_retry_failed
    ):
        return HitlLadderDecision(
            action=ACTION_CONTINUE,
            status=JobStatus.RUNNING.value,
            reason="Gemini apply + retry succeeded; keep going.",
            channel=channel,
        )

    return HitlLadderDecision(
        action=ACTION_AWAIT_HUMAN,
        status=JobStatus.AWAITING_HUMAN_INPUT.value,
        reason=(
            "Gemini was asked and applied (or missed). The retry still failed. "
            "Loop Carlo. Do not fake RUNNING."
        ),
        loop_carlo=True,
        channel=channel,
    )


def hitl_posted_at_from_job(job: dict[str, Any] | None) -> datetime | None:
    job = dict(job or {})
    payload = dict(job.get("payload") or {})
    return parse_iso_datetime(
        payload.get(HITL_POSTED_AT_KEY) or job.get("updated_at")
    )


def unanswered_hitl_kill_reason(
    job: dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> str | None:
    """FAILED reason when AWAITING_HUMAN_INPUT HITL is older than 30 minutes."""
    job = dict(job or {})
    try:
        status = JobStatus(job.get("status"))
    except (TypeError, ValueError):
        return None
    if status != JobStatus.AWAITING_HUMAN_INPUT:
        return None
    posted = hitl_posted_at_from_job(job)
    if posted is None:
        return None
    now = now or utc_now_dt()
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)
    if now - posted < timedelta(seconds=HITL_NO_REPLY_SECONDS):
        return None
    return HITL_NO_REPLY_ERROR


def stamp_hitl_posted_at(payload: dict[str, Any] | None, *, now: str | None = None) -> dict[str, Any]:
    """Idempotent stamp. Do not reset an existing posted-at clock."""
    from .store import utc_now

    out = dict(payload or {})
    out.setdefault(HITL_POSTED_AT_KEY, now or utc_now())
    return out


def expire_unanswered_hitl_jobs(
    store: Any,
    *,
    now: datetime | None = None,
) -> list[str]:
    """Kill every unanswered HITL past 30 minutes. Honest FAILED, not RUNNING."""
    now = now or utc_now_dt()
    killed: list[str] = []
    try:
        jobs = store.list_jobs_by_status({JobStatus.AWAITING_HUMAN_INPUT})
    except Exception:
        return killed
    for job in jobs:
        reason = unanswered_hitl_kill_reason(job, now=now)
        if not reason:
            continue
        job_id = str(job.get("id") or "").strip()
        if not job_id:
            continue
        try:
            store.transition(
                job_id,
                JobStatus.FAILED,
                expected={JobStatus.AWAITING_HUMAN_INPUT},
                error=reason,
                release_lease=True,
            )
        except Exception:
            continue
        killed.append(job_id)
    return killed
