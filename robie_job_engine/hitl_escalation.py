"""HITL escalation: Gemini first, then Carlo.

When the job gets stuck, this module implements the escalation path:
1. Ask Gemini for a suggestion (fast, automated)
2. If Gemini can't help, ping Carlo via email (slower, human)
3. Wait for Carlo's response with timeout
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


@dataclass
class HitlResponse:
    """Response from Gemini or Carlo."""
    source: str  # "gemini" or "carlo"
    suggestion: str  # What to try next
    actionable: bool  # Can the job act on this?
    raw: dict[str, Any] | None = None


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

Provide ONE specific actionable suggestion to get unstuck. For example:
- "Try selector X"
- "The button is in an iframe, switch to it first"
- "Click the 'Actions' dropdown first, then the button appears"
- "The page needs a longer wait, wait 10 seconds then retry"

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
        
        return HitlResponse(
            source="gemini",
            suggestion=suggestion,
            actionable=True,
            raw={"response": suggestion},
        )
    except Exception as exc:
        return HitlResponse(
            source="gemini",
            suggestion=f"Gemini request failed: {type(exc).__name__}: {exc}",
            actionable=False,
        )


def ping_carlo(request: HitlRequest, deps: dict[str, Any] | None = None) -> bool:
    """Send HITL notifications via Email and Google Chat.
    
    Notifies Carlo and optionally the original requester.
    Returns True if at least one notification was sent successfully.
    """
    deps = deps or {}
    email_sender = deps.get("email_sender")
    chat_sender = deps.get("chat_sender")  # Function that sends Google Chat messages
    
    # Build the message
    subject = f"[ROBIE HITL] Job {request.job_id} stuck at {request.phase}"
    
    body = f"""Robie needs your help.

Job ID: {request.job_id}
Phase: {request.phase}
Error: {request.error}

What was tried:
{chr(10).join(f"  - {a}" for a in request.attempted)}

Page state:
- URL: {request.page_state.get('url', 'unknown')}
- Title: {request.page_state.get('title', 'unknown')}
- Buttons: {', '.join(request.page_state.get('buttons', [])[:15])}

Applicant: {request.applicant_id}
Policy: {request.policy_id or 'unknown'}

Gemini was asked first but could not resolve this.

Reply with guidance, or the job will fail closed after 30 minutes.

To continue the job, reply with one of:
- A specific selector or button name to try
- "SKIP" to skip this phase and continue
- "ABORT" to stop the job
"""
    
    sent = False
    
    # Determine recipients
    recipients = []
    if request.notify_carlo:
        recipients.append("carlo@streetsmart.insurance")
    if request.notify_requester and request.original_requester:
        if request.original_requester not in recipients:
            recipients.append(request.original_requester)
    
    # Send via email (handle both callable functions and objects with .send())
    if email_sender and recipients:
        for recipient in recipients:
            try:
                if hasattr(email_sender, "send") and callable(email_sender.send):
                    email_sender.send(to=recipient, subject=subject, body=body)
                elif callable(email_sender):
                    email_sender(to=recipient, subject=subject, body=body)
                else:
                    continue
                sent = True
            except Exception:
                pass
    
    # Send via Google Chat (shorter format)
    if chat_sender:
        chat_msg = (
            f"🚨 ROBIE HITL: Job {request.job_id} stuck at {request.phase}\n"
            f"Error: {request.error[:200]}\n"
            f"Tried: {', '.join(request.attempted[:3])}\n"
            f"Reply with guidance or job fails in 30 min."
        )
        try:
            if chat_sender(chat_msg):
                sent = True
        except Exception:
            pass
    
    return sent


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
    """Full escalation: Gemini first, then Carlo.
    
    Returns a HitlResponse with actionable guidance, or actionable=False
    if neither Gemini nor Carlo could help.
    """
    deps = deps or {}
    
    # Step 1: Ask Gemini
    gemini_response = ask_gemini(request, deps.get("gemini_client"))
    if gemini_response.actionable:
        return gemini_response
    
    # Step 2: Ping Carlo (and original requester) via Email + Google Chat
    email_sent = ping_carlo(request, deps)
    if not email_sent:
        return HitlResponse(
            source="system",
            suggestion="Could not send HITL email to Carlo",
            actionable=False,
        )
    
    # Step 3: Wait for Carlo
    carlo_response = wait_for_carlo_response(
        request.job_id,
        timeout_seconds=deps.get("hitl_timeout", 1800),
        email_checker=deps.get("email_checker"),
    )
    
    if carlo_response and carlo_response.actionable:
        return carlo_response
    
    # Step 4: Fail closed
    return HitlResponse(
        source="system",
        suggestion="HITL timeout: neither Gemini nor Carlo provided actionable guidance",
        actionable=False,
    )
