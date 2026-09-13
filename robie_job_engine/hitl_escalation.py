"""HITL escalation: Gemini first, then Carlo.

When the job gets stuck, this module implements the escalation path:
1. Ask Gemini for a suggestion (fast, automated)
2. ALWAYS notify Carlo (informational if Gemini succeeded, blocking if not)
3. If Gemini couldn't help, wait for Carlo's response with timeout
4. Continue with guidance or fail closed

This is the REAL HITL — not the dry marker. It actually sends notifications.
"""

from __future__ import annotations

import json
import time
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class HitlRequest:
    """Context for a HITL escalation."""
    job_id: str
    phase: str  # e.g., "formentry_mint"
    error: str  # What went wrong
    page_state: dict[str, Any]  # What the worker sees
    attempted: list[str]  # What was already tried
    applicant_id: str
    policy_id: str | None = None
    original_requester: str | None = None  # Email of who triggered the job
    notify_carlo: bool = True  # Always notify Carlo
    notify_requester: bool = True  # Also notify the original requester
    screenshot_path: str | None = None  # Path to screenshot of the stuck state
    gemini_applied: bool = False
    gemini_named_option: str | None = None
    live_control_shows: str | None = None
    formentry_exists: bool = False
    job_still_running: bool = False
    save_skipped: bool = False
    script_or_job_stopped: bool = True
    unguessable: bool = False
    channel: str = "chat"
    gemini_asked: bool = False
    applied_retry_failed: bool = False


@dataclass
class HitlResponse:
    """Response from Gemini or Carlo."""
    source: str  # "gemini" or "carlo"
    suggestion: str  # What to try next (structured diff format when from Gemini)
    actionable: bool  # Can the job act on this? HITL is always False (STOP AND ASK).
    hitl_posted: bool = False
    raw: dict[str, Any] | None = None
    # Structured fix fields (populated when Gemini returns diff format):
    fix_file: str | None = None  # e.g. "robie_job_engine/ezlynx_policy_setup.py"
    fix_location: str | None = None  # e.g. "_fill_required_policy_fields, Billing Type"
    fix_before: str | None = None  # Code before
    fix_after: str | None = None  # Code after
    fix_reason: str | None = None  # Why this fixes it


def _parse_gemini_diff(suggestion: str) -> dict[str, str | None]:
    """Extract FILE/LOCATION/REASON/BEFORE/AFTER from Gemini's structured response."""
    out: dict[str, str | None] = {"file": None, "location": None, "reason": None, "before": None, "after": None}
    lines = suggestion.split("\n")
    current_key: str | None = None
    buf: list[str] = []
    # Map header names to dict keys
    header_map = {"FILE": "file", "LOCATION": "location", "REASON": "reason", "BEFORE": "before", "AFTER": "after"}

    def flush():
        if current_key and buf:
            # Strip leading blank lines, keep code indentation
            text = "\n".join(buf).strip("\n")
            # For BEFORE/AFTER, preserve as-is (trim trailing whitespace only)
            out[current_key] = text.strip() if current_key in ("before", "after") else text.strip()

    for line in lines:
        stripped = line.strip()
        matched = False
        for header, key in header_map.items():
            if stripped.upper().startswith(header + ":"):
                flush()
                current_key = key
                buf = []
                # Capture any content after the colon on the same line
                rest = stripped[len(header) + 1:].strip()
                if rest:
                    buf.append(rest)
                matched = True
                break
        if not matched and current_key:
            buf.append(line)
    flush()
    return out


def ask_gemini(request: HitlRequest, gemini_client: Any = None) -> HitlResponse:
    """Ask Gemini for help with a stuck job.
    
    Sends the page state and error, asks for a specific actionable suggestion.
    Returns actionable=False if Gemini is unsure or unavailable.
    """
    if gemini_client is None:
        # Try to get the default client from gemini_field_helper
        try:
            from .gemini_field_helper import default_gemini_field_client
            gemini_client = default_gemini_field_client()
        except Exception:
            return HitlResponse(
                source="gemini",
                suggestion="Gemini client not available",
                actionable=False,
            )
        # Client may be None if not configured (no env vars)
        if gemini_client is None:
            return HitlResponse(
                source="gemini",
                suggestion="Gemini not configured (missing project/location/model)",
                actionable=False,
            )
    
    # Build a focused prompt
    prompt = f"""The Robie job is stuck and needs help.

Job ID: {request.job_id}
Phase: {request.phase}
Error: {request.error}

What was tried: {', '.join(request.attempted)}

Current page state:
- URL: {request.page_state.get('url', 'unknown')}
- Title: {request.page_state.get('title', 'unknown')}
- Buttons on page: {json.dumps(request.page_state.get('buttons', [])[:20])}
- Headings: {json.dumps(request.page_state.get('headings', [])[:5])}

Applicant: {request.applicant_id}
Policy ID: {request.policy_id or 'unknown'}

Respond in this EXACT structured format so a human can turn it into a code fix in minutes:

FILE: <relative path to the file, e.g. robie_job_engine/ezlynx_policy_setup.py>
LOCATION: <function/method and area, e.g. _fill_required_policy_fields, Billing Type selection>
REASON: <one sentence: why the current code fails and why your fix works>

BEFORE:
<the exact current code that's wrong, as best you can reconstruct it>

AFTER:
<the corrected code>

If the fix is not a code change (e.g. "wait longer", "click a different button"), put the action in AFTER and leave BEFORE empty.

Example:
FILE: robie_job_engine/ezlynx_policy_setup.py
LOCATION: _fill_required_policy_fields, Billing Type dropdown
REASON: Wanted value is not an exact live option; ask Gemini which one live option to select.

BEFORE:
if text.lower() == "direct bill":
    target_value = val

AFTER:
if text.lower() in ("direct bill", "direct"):
    target_value = val

If you cannot provide a specific actionable suggestion, respond with exactly: UNSURE
"""
    
    try:
        result = gemini_client.generate_content(prompt)
        suggestion = result.strip() if isinstance(result, str) else str(result).strip()

        # Check if Gemini is unsure
        if suggestion.upper() in ("UNSURE", "HITL", "UNKNOWN", ""):
            return HitlResponse(
                source="gemini",
                suggestion="Gemini could not provide a suggestion",
                actionable=False,
                raw={"response": suggestion},
            )

        # Parse structured diff format
        fix = _parse_gemini_diff(suggestion)

        return HitlResponse(
            source="gemini",
            suggestion=suggestion,
            actionable=True,
            fix_file=fix.get("file"),
            fix_location=fix.get("location"),
            fix_before=fix.get("before"),
            fix_after=fix.get("after"),
            fix_reason=fix.get("reason"),
            raw={"response": suggestion},
        )
    except Exception as exc:
        return HitlResponse(
            source="gemini",
            suggestion=f"Gemini request failed: {type(exc).__name__}: {exc}",
            actionable=False,
        )


def live_control_shows_named_option(request: HitlRequest) -> bool:
    """True only when the live control actually shows Gemini's named option."""
    named = str(request.gemini_named_option or "").strip()
    shown = str(request.live_control_shows or "").strip()
    if not named or not shown:
        return False
    from .ezlynx_field_widgets import normalize_option_text

    return normalize_option_text(named) == normalize_option_text(shown)


def gemini_resolved_and_job_continuing(request: HitlRequest) -> bool:
    """True only when Gemini answered, was applied, and the job is continuing.

    Carlo HITL copy must never claim this. If the script stopped or Save was
    skipped, this is False even if a live control once matched.
    """
    if request.script_or_job_stopped or request.save_skipped:
        return False
    if not request.job_still_running:
        return False
    return bool(request.gemini_applied) and live_control_shows_named_option(request)


def build_hitl_notice(
    request: HitlRequest,
    gemini_response: HitlResponse | None = None,
) -> dict[str, str]:
    """Carlo-facing HITL text. Never claim resolved/continuing without proof."""
    gemini_text = (gemini_response.suggestion if gemini_response else "") or ""
    named = str(request.gemini_named_option or "").strip()
    shown = str(request.live_control_shows or "").strip()
    applied = bool(request.gemini_applied) and live_control_shows_named_option(request)
    facts: list[str] = []
    mint_miss = (
        request.phase == "formentry_mint"
        and not request.formentry_exists
        and (
            "no FormEntry URL after 30s" in (request.error or "")
            or "FormEntry was not minted" in (request.error or "")
        )
    )
    if applied:
        facts.append(
            f"Gemini named {named!r} and the live control shows {shown!r}."
        )
    elif named and not applied:
        facts.append(
            f"Gemini named a live option {named!r}. Nothing was applied. "
            f"Live control shows {(shown or '(empty)')!r}."
        )
    elif mint_miss:
        facts.append(
            "Save & Continue Edit did not mint FormEntry. "
            "The page stayed on Policy/Actions/Edit. "
            "Gemini did not handle this."
        )
    elif request.phase == "coverage_fill":
        facts.append(
            "FormEntry opened. Coverage labels were not filled from the job "
            "payload. Amounts that were not on the job were not guessed. "
            "Gemini did not handle this."
        )
    elif gemini_text and "unsure" in (request.error or "").casefold():
        facts.append("Gemini is still unsure after one retry. HITL, no select.")
    elif gemini_response and gemini_response.source == "gemini" and gemini_text:
        if request.gemini_applied:
            facts.append("Gemini answered and a fill was applied.")
        else:
            facts.append("Gemini answered. Nothing was applied.")
    elif gemini_response and gemini_response.source == "gemini":
        facts.append("Gemini was asked and did not name a usable live option. Nothing was applied.")
    else:
        facts.append("Gemini was not asked to mint FormEntry. Nothing was applied.")
    if request.save_skipped:
        facts.append("The fill still failed. Save was skipped.")
    facts.append("STOP AND ASK. The script/job stopped.")
    if not request.formentry_exists and request.phase != "coverage_fill":
        facts.append("FormEntry does not exist.")
    subject = f"[ROBIE HITL] Job {request.job_id} stuck at {request.phase}"
    body = (
        "Robie needs your help.\n\n"
        + " ".join(facts)
        + "\n\n"
        f"Job ID: {request.job_id}\n"
        f"Phase: {request.phase}\n"
        f"Error: {request.error}\n\n"
        "What was tried:\n"
        + "\n".join(f"  - {a}" for a in request.attempted)
        + "\n\n"
        f"Gemini suggestion (not applied unless the live control shows it):\n"
        f"{gemini_text or named or '(none)'}\n\n"
        f"Applicant: {request.applicant_id}\n"
        f"Policy: {request.policy_id or 'unknown'}\n"
        "Reply RETRY in this same Chat thread after the page is corrected.\n"
    )
    chat = (
        f"ROBIE HITL: Job {request.job_id} stuck at {request.phase}. "
        + " ".join(facts)
        + f" Error: {request.error[:160]}"
    )
    return {"subject": subject, "body": body, "chat": chat}


def _deliver_hitl(
    request: HitlRequest,
    notice: dict[str, str],
    deps: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Send email/chat. Never claim email worked unless send succeeded."""
    deps = deps or {}
    email_sender = deps.get("email_sender")
    chat_sender = deps.get("chat_sender")
    email_sent = False
    chat_sent = False
    email_error = ""
    chat_error = ""
    recipients: list[str] = []
    if request.notify_carlo:
        recipients.append("carlo@streetsmart.insurance")
    if request.notify_requester and request.original_requester:
        if request.original_requester not in recipients:
            recipients.append(request.original_requester)
    if email_sender and recipients:
        for recipient in recipients:
            try:
                if hasattr(email_sender, "send") and callable(email_sender.send):
                    email_sender.send(
                        to=recipient, subject=notice["subject"], body=notice["body"]
                    )
                elif callable(email_sender):
                    email_sender(to=recipient, subject=notice["subject"], body=notice["body"])
                else:
                    email_error = "email_sender is neither callable nor has .send"
                    continue
                email_sent = True
            except Exception as exc:
                email_error = f"{type(exc).__name__}: {exc}"
    elif not email_sender:
        email_error = "no email_sender in deps"
    elif not recipients:
        email_error = "no recipients"
    if chat_sender:
        try:
            if chat_sender(notice["chat"]):
                chat_sent = True
            else:
                chat_error = chat_error or "chat_sender returned false"
        except Exception as exc:
            chat_error = f"{type(exc).__name__}: {exc}"
    else:
        chat_error = chat_error or "no chat_sender in deps"
    channel = str(getattr(request, "channel", "chat") or "chat").strip().casefold()
    if channel == "email":
        posted = email_sent
    elif channel == "any":
        posted = chat_sent or email_sent
    else:
        posted = chat_sent
    return {
        "email_sent": email_sent,
        "chat_sent": chat_sent,
        "email_error": email_error,
        "chat_error": chat_error,
        "sent": posted,
        "hitl_posted": posted,
    }


def notify_carlo_gemini_success(
    request: HitlRequest,
    gemini_response: HitlResponse,
    deps: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Post honest HITL. Never treat this as a continue/resolved signal."""
    delivery = _deliver_hitl(request, build_hitl_notice(request, gemini_response), deps)
    error = ""
    if not delivery["hitl_posted"]:
        parts = []
        if delivery["email_error"]:
            parts.append(f"email: {delivery['email_error']}")
        if delivery["chat_error"]:
            parts.append(f"chat: {delivery['chat_error']}")
        error = "; ".join(parts) or "HITL posted=false"
    return (bool(delivery["hitl_posted"]), error)


def ping_carlo(
    request: HitlRequest,
    deps: dict[str, Any] | None = None,
    gemini_response: HitlResponse | None = None,
) -> tuple[bool, str]:
    """Send HITL into the originating Chat thread. Email is best-effort only.

    Honest about Gemini and job state. Chat failure is HITL posted=false.
    Email signBlob 403 is not a HITL post and does not authorize continue.
    """
    notice = build_hitl_notice(request, gemini_response)
    delivery = _deliver_hitl(request, notice, deps)
    parts = []
    if not delivery["email_sent"] and delivery["email_error"]:
        parts.append(f"email: {delivery['email_error']}")
    if not delivery["chat_sent"] and delivery["chat_error"]:
        parts.append(f"chat: {delivery['chat_error']}")
    if not delivery["chat_sent"] and not delivery["chat_error"]:
        parts.append("chat: HITL posted=false")
    return (bool(delivery["hitl_posted"]), "; ".join(parts))


def wait_for_carlo_response(
    job_id: str,
    timeout_seconds: int = 1800,  # 30 minutes
    email_checker: Any = None,
) -> Optional[HitlResponse]:
    """Wait for Carlo's email reply.
    
    Polls for a reply to the HITL email. Returns None on timeout.
    """
    if email_checker is None:
        return None
    
    start = time.time()
    while time.time() - start < timeout_seconds:
        try:
            reply = email_checker.check_for_reply(job_id)
            if reply:
                return HitlResponse(
                    source="carlo",
                    suggestion=reply.get("body", ""),
                    actionable=True,
                    raw=reply,
                )
        except Exception:
            pass
        time.sleep(60)  # Check every minute
    
    return None


def escalate(request: HitlRequest, deps: dict[str, Any] | None = None) -> HitlResponse:
    """Global HITL ladder: Gemini first, apply and continue, Carlo only after.

    1. Unguessable facts (missing coverage amounts) skip Gemini and HITL Carlo.
    2. Otherwise ask Gemini. If actionable, return continue — do not sit on it.
    3. Gemini miss, or Gemini + applied retry still failing, loops Carlo.
    4. Chat is the HITL channel for Chat jobs; email is the channel for email jobs.
    """
    from .hitl_ladder import (
        ACTION_AWAIT_HUMAN,
        ACTION_CONTINUE,
        HitlLadderState,
        decide_hitl_ladder,
    )

    deps = deps or {}
    coverage_unguessable = request.phase == "coverage_fill" and (
        "will not guess" in (request.error or "").casefold()
        or "coverage amounts not on the job" in (request.error or "").casefold()
    )
    gemini_asked = bool(request.gemini_asked) or bool(request.gemini_named_option)
    applied_failed = bool(request.applied_retry_failed) or (
        bool(request.gemini_applied) and bool(request.script_or_job_stopped)
    )
    named_not_applied = bool(request.gemini_named_option) and not request.gemini_applied
    if named_not_applied and request.script_or_job_stopped:
        applied_failed = True

    gemini_response: HitlResponse | None = None
    if (
        not request.unguessable
        and not coverage_unguessable
        and not gemini_asked
        and not applied_failed
    ):
        gemini_response = ask_gemini(request, deps.get("gemini_client"))
        gemini_asked = True

    state = HitlLadderState(
        gemini_asked=gemini_asked,
        gemini_actionable=bool(gemini_response and gemini_response.actionable)
        or (bool(request.gemini_named_option) and request.gemini_applied),
        gemini_applied=bool(request.gemini_applied),
        applied_retry_attempted=applied_failed or bool(request.applied_retry_failed),
        applied_retry_failed=applied_failed,
        unguessable=bool(request.unguessable) or coverage_unguessable,
        channel=request.channel or "chat",
    )
    decision = decide_hitl_ladder(state)

    if decision.action == ACTION_CONTINUE or (
        decision.action != ACTION_AWAIT_HUMAN
        and gemini_response
        and gemini_response.actionable
        and not applied_failed
    ):
        suggestion = (
            (gemini_response.suggestion if gemini_response else "")
            or request.gemini_named_option
            or "Gemini named a live option; apply it and continue."
        )
        raw = {
            "hitl_posted": False,
            "continue_after_hitl": True,
            "ladder": decision.action,
            "reason": decision.reason,
        }
        return HitlResponse(
            source="gemini",
            suggestion=suggestion,
            actionable=True,
            hitl_posted=False,
            fix_file=gemini_response.fix_file if gemini_response else None,
            fix_location=gemini_response.fix_location if gemini_response else None,
            fix_before=gemini_response.fix_before if gemini_response else None,
            fix_after=gemini_response.fix_after if gemini_response else None,
            fix_reason=gemini_response.fix_reason if gemini_response else None,
            raw=raw,
        )

    notice = build_hitl_notice(request, gemini_response)
    if request.notify_carlo:
        chat_posted, post_error = ping_carlo(request, deps, gemini_response)
    else:
        chat_posted, post_error = False, "notify_carlo=false"
    raw = {
        "hitl_posted": chat_posted,
        "continue_after_hitl": False,
        "error": post_error,
        "ladder": decision.action,
        "reason": decision.reason,
    }
    if not chat_posted:
        return HitlResponse(
            source="system",
            suggestion=(
                notice["body"]
                + f" HITL posted=false ({post_error or 'Chat ping failed'})."
            ),
            actionable=False,
            hitl_posted=False,
            raw=raw,
        )
    return HitlResponse(
        source="system",
        suggestion=notice["body"],
        actionable=False,
        hitl_posted=True,
        raw=raw,
    )
