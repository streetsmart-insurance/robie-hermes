"""HITL retry loop: Gemini first, then Carlo, then kill the job.

Generic mechanism for ANY job, email or chat origin:

  1. The job reports a failure with evidence.
  2. Ask Gemini for a runtime fix -> apply it -> retry
     (up to max_gemini_attempts; default 2).
  3. Still failing -> ping Carlo in Chat with the evidence and wait up to
     carlo_timeout_seconds (default 1800 = 30 minutes) for his reply.
  4. Reply arrives -> apply his guidance, one final retry.
  5. No reply in time -> kill the job: fail closed with a final summary.

The loop never invents success: a step counts as recovered only when the
job's own retry reports success. Nothing continues on words alone.

Carlo's standing rule for this loop (2026-09-13): Gemini gets asked first
and the job keeps going on Gemini's fix; Carlo is looped in only if it
keeps failing; 30 minutes of silence kills the job.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass
class HitlLoopConfig:
    """Tunable bounds for the loop. Defaults are Carlo's stated policy."""

    max_gemini_attempts: int = 2  # Gemini tries, then Carlo gets looped in
    carlo_timeout_seconds: int = 1800  # 30 minutes of silence, then kill
    poll_interval_seconds: int = 60  # how often to check for Carlo's reply


@dataclass
class RetryOutcome:
    """What one retry attempt did. Built by the job, judged by the job."""

    success: bool
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


def _log(log: list[str], line: str) -> None:
    log.append(line)


def _human_duration(seconds: int) -> str:
    minutes = int(seconds // 60)
    if minutes >= 1:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{int(seconds)} second{'s' if int(seconds) != 1 else ''}"


def build_loop_carlo_notice(
    *,
    job_id: str,
    phase: str,
    first_error: str,
    attempted: list[str],
    gemini_log: list[str],
    timeout_seconds: int,
) -> dict[str, str]:
    """Honest Carlo-facing wording. Never claims resolved or continuing."""
    duration = _human_duration(timeout_seconds)
    tried = "\n".join(f"  - {a}" for a in attempted) if attempted else "  (none)"
    gemini_lines = "\n".join(f"  - {g}" for g in gemini_log) if gemini_log else "  (none)"
    subject = f"[ROBIE HITL] Job {job_id} needs you at {phase} ({duration} to respond)"
    body = (
        "Robie is stuck and Gemini could not get it unstuck.\n\n"
        f"Job ID: {job_id}\n"
        f"Phase: {phase}\n"
        f"Failure: {first_error}\n\n"
        "What was tried:\n"
        f"{tried}\n\n"
        "What Gemini tried:\n"
        f"{gemini_lines}\n\n"
        f"Reply in this Chat thread within {duration} with what to do "
        "(e.g. the correct value, or KILL to stop now). "
        f"If I don't hear back in {duration}, the job is killed automatically.\n"
    )
    chat = (
        f"ROBIE HITL: job {job_id} stuck at {phase}: {first_error[:140]} "
        f"Gemini tried and failed. Reply here within {duration} or the job is killed."
    )
    return {"subject": subject, "body": body, "chat": chat}


def build_loop_kill_notice(*, job_id: str, phase: str, last_error: str, timeout_seconds: int = 1800) -> str:
    return (
        f"ROBIE HITL: job {job_id} KILLED at {phase} "
        f"after {_human_duration(timeout_seconds)} with no response. Last error: {last_error[:160]}"
    )


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value):
        return await value
    return value


async def _wait_for_carlo_reply(
    *,
    job_id: str,
    deps: dict[str, Any],
    config: HitlLoopConfig,
    log: list[str],
) -> dict[str, Any] | None:
    """Poll Chat (then email) for Carlo's reply. None on timeout.

    deps may carry:
      chat_reply_checker: (job_id) -> {"body": str} | None  (sync or async)
      email_checker: object with .check_for_reply(job_id) -> {"body": str} | None
    """
    chat_checker = (deps or {}).get("chat_reply_checker")
    email_checker = (deps or {}).get("email_checker")
    deadline = time.time() + config.carlo_timeout_seconds
    while time.time() < deadline:
        reply = None
        if chat_checker is not None:
            try:
                reply = await _maybe_await(chat_checker(job_id))
            except Exception as exc:  # noqa: BLE001 - a broken watcher must not kill the wait
                _log(log, f"Chat reply watcher errored ({type(exc).__name__}); continuing to wait.")
                chat_checker = None
        if reply is None and email_checker is not None:
            try:
                check = getattr(email_checker, "check_for_reply", None)
                if callable(check):
                    reply = await _maybe_await(check(job_id))
            except Exception as exc:  # noqa: BLE001
                _log(log, f"Email reply watcher errored ({type(exc).__name__}); continuing to wait.")
                email_checker = None
        body = str((reply or {}).get("body") or "").strip()
        if body:
            return {"body": body, "raw": reply}
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        await asyncio.sleep(min(config.poll_interval_seconds, max(1, remaining)))
    return None


async def hitl_retry_loop(
    *,
    job_id: str,
    phase: str,
    first_error: str,
    first_evidence: dict[str, Any],
    consult_gemini: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]],
    retry_with_fix: Callable[[dict[str, Any]], Awaitable[RetryOutcome]],
    applicant_id: str = "",
    policy_id: str | None = None,  # noqa: ARG001 - kept for signature stability across callers
    attempted: list[str] | None = None,
    screenshot_path: str | None = None,
    deps: dict[str, Any] | None = None,
    config: HitlLoopConfig | None = None,
) -> dict[str, Any]:
    """Run the Gemini-first HITL loop for one failure. See module docstring."""
    cfg = config or HitlLoopConfig()
    deps = deps or {}
    attempted = list(attempted or [])
    log: list[str] = []
    gemini_log: list[str] = []
    evidence = dict(first_evidence or {})

    _log(log, f"Job failed at {phase}: {first_error}. Asking Gemini first.")
    gemini_attempts = 0
    fixes_applied = 0

    for attempt_no in range(1, cfg.max_gemini_attempts + 1):
        try:
            fix = await consult_gemini(evidence)
        except Exception as exc:  # noqa: BLE001 - Gemini failing must not break the loop
            fix = None
            _log(log, f"Gemini consult {attempt_no} raised {type(exc).__name__}: {exc}.")
        gemini_attempts += 1
        if not fix:
            _log(log, f"Gemini consult {attempt_no}: no usable fix.")
            gemini_log.append(f"attempt {attempt_no}: no usable fix")
            continue
        fixes_applied += 1
        summary = str(fix.get("reason") or fix.get("action") or "fix")[:160]
        _log(log, f"Gemini consult {attempt_no}: applying fix ({summary}) and retrying.")
        try:
            outcome = await retry_with_fix(fix)
        except Exception as exc:  # noqa: BLE001
            outcome = RetryOutcome(
                success=False,
                detail=f"retry raised {type(exc).__name__}: {exc}",
                evidence={},
            )
        if outcome.success:
            _log(log, f"Retry {attempt_no} worked: {outcome.detail[:160]}")
            return {
                "recovered": True,
                "killed": False,
                "carlo_looped_in": False,
                "gemini_attempts": gemini_attempts,
                "gemini_fixes_applied": fixes_applied,
                "final_error": None,
                "final_evidence": outcome.evidence or evidence,
                "hitl_posted": False,
                "carlo_replied": False,
                "loop_log": log,
            }
        evidence = outcome.evidence or evidence
        _log(log, f"Retry {attempt_no} still failing: {outcome.detail[:200]}")
        gemini_log.append(f"attempt {attempt_no}: applied fix, still failing ({outcome.detail[:120]})")

    # Gemini is out of ideas. Loop Carlo in.
    _log(log, "Gemini could not fix it. Looping Carlo in.")
    notice = build_loop_carlo_notice(
        job_id=job_id,
        phase=phase,
        first_error=first_error,
        attempted=attempted,
        gemini_log=gemini_log,
        timeout_seconds=cfg.carlo_timeout_seconds,
    )
    chat_sender = deps.get("chat_sender")
    hitl_posted = False
    if chat_sender is not None:
        try:
            hitl_posted = bool(await _maybe_await(chat_sender(notice["chat"])))
        except Exception as exc:  # noqa: BLE001
            _log(log, f"Carlo Chat ping failed ({type(exc).__name__}: {exc}).")
    else:
        _log(log, "No chat_sender in deps; Carlo ping not sent.")
    if screenshot_path:
        _log(log, f"Screenshot of the stuck state: {screenshot_path}")

    reply = await _wait_for_carlo_reply(
        job_id=job_id, deps=deps, config=cfg, log=log
    )
    if reply is None:
        duration = _human_duration(cfg.carlo_timeout_seconds)
        kill_error = (
            f"HITL: Carlo did not respond within {duration}; job killed at {phase}. "
            f"Last error: {first_error}"
        )
        _log(log, kill_error)
        if chat_sender is not None:
            try:
                await _maybe_await(
                    chat_sender(build_loop_kill_notice(job_id=job_id, phase=phase, last_error=first_error, timeout_seconds=cfg.carlo_timeout_seconds))
                )
            except Exception:  # noqa: BLE001 - kill notice is best-effort
                pass
        return {
            "recovered": False,
            "killed": True,
            "carlo_looped_in": True,
            "gemini_attempts": gemini_attempts,
            "gemini_fixes_applied": fixes_applied,
            "final_error": kill_error,
            "final_evidence": evidence,
            "hitl_posted": hitl_posted,
            "carlo_replied": False,
            "loop_log": log,
        }

    body = str(reply.get("body") or "").strip()
    _log(log, f"Carlo replied ({len(body)} chars). Applying his guidance, one final retry.")
    if body.lower() in ("kill", "stop", "abort", "cancel"):
        kill_error = f"HITL: Carlo said {body.upper()}; job killed at {phase}."
        _log(log, kill_error)
        return {
            "recovered": False,
            "killed": True,
            "carlo_looped_in": True,
            "gemini_attempts": gemini_attempts,
            "gemini_fixes_applied": fixes_applied,
            "final_error": kill_error,
            "final_evidence": evidence,
            "hitl_posted": hitl_posted,
            "carlo_replied": True,
            "loop_log": log,
        }
    try:
        outcome = await retry_with_fix(
            {"action": "carlo_guidance", "guidance": body, "reason": "Carlo's instruction"}
        )
    except Exception as exc:  # noqa: BLE001
        outcome = RetryOutcome(
            success=False,
            detail=f"retry raised {type(exc).__name__}: {exc}",
            evidence={},
        )
    if outcome.success:
        _log(log, "Carlo's guidance worked.")
        final_error = None
    else:
        final_error = (
            f"HITL: Carlo replied but the final retry still failed at {phase}: "
            f"{outcome.detail[:200]}"
        )
        _log(log, final_error)
    return {
        "recovered": bool(outcome.success),
        "killed": False,
        "carlo_looped_in": True,
        "gemini_attempts": gemini_attempts,
        "gemini_fixes_applied": fixes_applied,
        "final_error": final_error,
        "final_evidence": outcome.evidence or evidence,
        "hitl_posted": hitl_posted,
        "carlo_replied": True,
        "loop_log": log,
    }
