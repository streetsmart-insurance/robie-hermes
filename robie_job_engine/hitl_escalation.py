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
    formentry_exists: bool = False
    job_still_running: bool = False
    save_skipped: bool = False
    script_or_job_stopped: bool = True


@dataclass
class HitlResponse:
    """Response from Gemini or Carlo."""
    source: str  # "gemini" or "carlo"
    suggestion: str  # What to try next (structured diff format when from Gemini)
    actionable: bool  # Can the job act on this?
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
REASON: Dropdown contains "Direct" not "Direct Bill"; exact match fails, alias needed.

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


def gemini_resolved_and_job_continuing(request: HitlRequest) -> bool:
    """True only when FormEntry exists and a Job Engine job is still running."""
    return bool(
        request.gemini_applied
        and request.formentry_exists
        and request.job_still_running
        and not request.script_or_job_stopped
    )


def build_hitl_notice(
    request: HitlRequest,
    gemini_response: HitlResponse | None = None,
) -> dict[str, str]:
    """Carlo-facing HITL text. Never claim resolved/continuing without proof."""
    gemini_text = (gemini_response.suggestion if gemini_response else "") or ""
    gemini_answered = bool(
        gemini_response
        and gemini_response.source == "gemini"
        and gemini_text
        and "not available" not in gemini_text.casefold()
        and "not configured" not in gemini_text.casefold()
        and "could not provide" not in gemini_text.casefold()
    )
    if gemini_resolved_and_job_continuing(request):
        subject = f"[ROBIE HITL] Job {request.job_id}: Gemini applied {request.phase}"
        body = (
            f"Gemini named a live option and it was applied.\n"
            f"FormEntry exists. A Job Engine job is still running.\n\n"
            f"Job ID: {request.job_id}\n"
            f"Phase: {request.phase}\n"
            f"Error: {request.error}\n"
            f"Applicant: {request.applicant_id}\n"
            f"Policy: {request.policy_id or 'unknown'}\n"
        )
        chat = (
            f"ROBIE HITL: Job {request.job_id} at {request.phase}. "
            f"Gemini applied a live option. FormEntry exists. Job still running."
        )
        return {"subject": subject, "body": body, "chat": chat}

    facts: list[str] = []
    if gemini_answered and not request.gemini_applied:
        facts.append("Gemini answered. Nothing was applied.")
    elif gemini_answered and request.gemini_applied:
        facts.append("Gemini answered and a fill was applied.")
    elif gemini_response and gemini_response.source == "gemini":
        facts.append("Gemini was asked and did not name a usable live option.")
    else:
        facts.append("Gemini was not able to help.")
    if request.save_skipped:
        facts.append("The fill still failed. Save was skipped.")
    if request.script_or_job_stopped or not request.job_still_running:
        facts.append("The script/job stopped.")
    if not request.formentry_exists:
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
        f"Gemini suggestion (not applied unless stated above):\n{gemini_text or '(none)'}\n\n"
        f"Applicant: {request.applicant_id}\n"
        f"Policy: {request.policy_id or 'unknown'}\n"
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
        except Exception as exc:
            chat_error = f"{type(exc).__name__}: {exc}"
    return {
        "email_sent": email_sent,
        "chat_sent": chat_sent,
        "email_error": email_error,
        "chat_error": chat_error,
        "sent": email_sent or chat_sent,
    }


def notify_carlo_gemini_success(
    request: HitlRequest,
    gemini_response: HitlResponse,
    deps: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Inform Carlo only when Gemini was applied and the job is still running.

    A Gemini suggestion that was not applied is not a resolution. Email
    signBlob can fail; this function does not claim email worked.
    """
    if not gemini_resolved_and_job_continuing(request):
        delivery = _deliver_hitl(request, build_hitl_notice(request, gemini_response), deps)
        error = ""
        if not delivery["sent"]:
            parts = []
            if delivery["email_error"]:
                parts.append(f"email: {delivery['email_error']}")
            if delivery["chat_error"]:
                parts.append(f"chat: {delivery['chat_error']}")
            error = "; ".join(parts) or "unknown failure"
        return (bool(delivery["sent"]), error)

    notice = build_hitl_notice(request, gemini_response)
    delivery = _deliver_hitl(request, notice, deps)
    error = ""
    if not delivery["sent"]:
        parts = []
        if delivery["email_error"]:
            parts.append(f"email: {delivery['email_error']}")
        if delivery["chat_error"]:
            parts.append(f"chat: {delivery['chat_error']}")
        error = "; ".join(parts) or "unknown failure"
    return (bool(delivery["sent"]), error)


def ping_carlo(
    request: HitlRequest,
    deps: dict[str, Any] | None = None,
    gemini_response: HitlResponse | None = None,
) -> tuple[bool, str]:
    """Send HITL notifications via Email and Google Chat.

    Honest about Gemini and job state. Does not claim email worked unless
    send succeeded.
    """
    notice = build_hitl_notice(request, gemini_response)
    delivery = _deliver_hitl(request, notice, deps)
    parts = []
    if not delivery["email_sent"] and delivery["email_error"]:
        parts.append(f"email: {delivery['email_error']}")
    if not delivery["chat_sent"] and delivery["chat_error"]:
        parts.append(f"chat: {delivery['chat_error']}")
    return (bool(delivery["sent"]), "; ".join(parts))


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
    """Gemini first, then Carlo. Never claim resolved unless FormEntry exists
    and a Job Engine job is still running.
    """
    deps = deps or {}
    gemini_response = ask_gemini(request, deps.get("gemini_client"))
    resolved = gemini_resolved_and_job_continuing(request) and gemini_response.actionable

    if request.notify_carlo:
        if resolved:
            notify_carlo_gemini_success(request, gemini_response, deps)
            return gemini_response
        email_sent, email_error = ping_carlo(request, deps, gemini_response)
        if not email_sent and not request.job_still_running:
            notice = build_hitl_notice(request, gemini_response)
            return HitlResponse(
                source="system",
                suggestion=notice["body"],
                actionable=False,
                raw={"email_error": email_error, "gemini": gemini_response.suggestion},
            )
        if not email_sent:
            return HitlResponse(
                source="system",
                suggestion=f"Could not send HITL notification to Carlo ({email_error})",
                actionable=False,
            )

    if resolved:
        return gemini_response

    if request.job_still_running and not request.script_or_job_stopped:
        carlo_response = wait_for_carlo_response(
            request.job_id,
            timeout_seconds=deps.get("hitl_timeout", 1800),
            email_checker=deps.get("email_checker"),
        )
        if carlo_response and carlo_response.actionable:
            return carlo_response

    notice = build_hitl_notice(request, gemini_response)
    return HitlResponse(
        source="system",
        suggestion=notice["body"],
        actionable=False,
        raw={"gemini": gemini_response.suggestion},
    )
