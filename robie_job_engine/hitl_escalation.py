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
    screenshot_path: str | None = None  # Path to screenshot of the stuck state


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


_PHASE_GOALS = {
    "formentry_mint": (
        "open the coverage form on this EZLynx policy so I can fill "
        "limits and deductibles"
    ),
    "hitl_isolation_test": "confirm I can reach you when a job is stuck",
}

_ATTEMPT_PLAIN = {
    "field_fill": "filling the required policy fields",
    "save_and_continue_edit": "clicking Save & Continue Edit",
    "formentry_url_poll_30s": "waiting for the coverage form to open",
    "css_selector": "finding the button by CSS",
    "xpath": "finding the button by XPath",
    "text_content": "finding the button by its text",
    "role_button": "finding the button by its role",
    "all_elements": "scanning the page for the button",
}

_SELECTOR_STRATEGY_KEYS = frozenset(
    {
        "css_selector",
        "xpath",
        "text_content",
        "role_button",
        "all_elements",
        "css",
        "id",
        "name",
    }
)


def _what_robie_is_trying_to_do(request: HitlRequest) -> str:
    return _PHASE_GOALS.get(request.phase, "finish this EZLynx policy step")


def _one_sentence_error(error: str) -> str:
    text = " ".join((error or "").split())
    if not text:
        return "I got stuck and could not finish."
    for sep in (". ", ".\n"):
        if sep in text:
            text = text.split(sep, 1)[0]
            break
    if len(text) > 240:
        text = text[:237].rstrip() + "..."
    return text


def _humanize_attempt(item: str) -> str:
    raw = (item or "").strip()
    if not raw:
        return ""
    mapped = _ATTEMPT_PLAIN.get(raw.casefold())
    if mapped:
        return mapped
    return raw


def _already_tried_lines(attempted: list[str]) -> list[str]:
    cleaned = [_humanize_attempt(item) for item in attempted]
    cleaned = [item for item in cleaned if item]
    if not cleaned:
        return ["I have not gotten a clean retry yet."]
    keys = [item.strip().casefold() for item in attempted if item.strip()]
    if keys and all(key in _SELECTOR_STRATEGY_KEYS for key in keys):
        return ["several ways to find the control on the page"]
    return cleaned


def _applicant_policy_line(request: HitlRequest) -> str:
    policy = request.policy_id or "unknown"
    return f"Applicant {request.applicant_id}. Policy {policy}."


def build_hitl_email(request: HitlRequest) -> tuple[str, str]:
    """Plain-English HITL email. Coworker tone. No phase-name subject."""
    subject = f"Robie needs a hand — applicant {request.applicant_id}"
    tried = _already_tried_lines(request.attempted)
    if len(tried) == 1:
        tried_block = tried[0]
    else:
        tried_block = "\n".join(f"{i}. {item}" for i, item in enumerate(tried, start=1))
    extras: list[str] = []
    title = (request.page_state or {}).get("title")
    url = (request.page_state or {}).get("url")
    if title or url:
        extras.append(f"I was on {title or 'the page'}" + (f" ({url})" if url else "") + ".")
    if request.screenshot_path:
        extras.append(f"I saved a screenshot: {request.screenshot_path}")
    extra_block = ("\n".join(extras) + "\n\n") if extras else ""
    body = f"""Hey Carlo — Robie needs a hand.

I'm trying to {_what_robie_is_trying_to_do(request)}.

What went wrong: {_one_sentence_error(request.error)}

I already tried:
{tried_block}

{_applicant_policy_line(request)}

Reply with what I should try next, SKIP to skip this step, or ABORT to stop the job. I'll wait 30 minutes, then stop.

{extra_block}Job {request.job_id}
"""
    return subject, body


def build_hitl_chat(request: HitlRequest) -> str:
    """Plain-English Google Chat HITL. Coworker tone. Job id is not the lead."""
    tried = _already_tried_lines(request.attempted)
    if len(tried) == 1:
        tried_line = f"I already tried {tried[0]}."
    else:
        tried_line = "I already tried: " + "; ".join(tried) + "."
    return (
        "Hey Carlo — Robie needs a hand.\n"
        f"\nI'm trying to {_what_robie_is_trying_to_do(request)}.\n"
        f"\nWhat went wrong: {_one_sentence_error(request.error)}\n"
        f"\n{tried_line}\n"
        f"\n{_applicant_policy_line(request)}\n"
        "\nReply with what I should try, SKIP, or ABORT. "
        "I'll wait 30 minutes, then stop.\n"
        f"\nJob {request.job_id}"
    )


def ping_carlo(request: HitlRequest, deps: dict[str, Any] | None = None) -> tuple[bool, str]:
    """Send HITL notifications via Email and Google Chat.
    
    Notifies Carlo and optionally the original requester.
    Returns (sent, error_reason): sent=True if at least one notification
    was sent successfully, error_reason describes the failure if not.
    """
    deps = deps or {}
    email_sender = deps.get("email_sender")
    chat_sender = deps.get("chat_sender")  # Function that sends Google Chat messages
    
    subject, body = build_hitl_email(request)
    
    sent = False
    
    # Determine recipients
    recipients = []
    if request.notify_carlo:
        recipients.append("carlo@streetsmart.insurance")
    if request.notify_requester and request.original_requester:
        if request.original_requester not in recipients:
            recipients.append(request.original_requester)
    
    # Send via email (handle both callable functions and objects with .send())
    email_error = ""
    if email_sender and recipients:
        for recipient in recipients:
            try:
                if hasattr(email_sender, "send") and callable(email_sender.send):
                    email_sender.send(to=recipient, subject=subject, body=body)
                elif callable(email_sender):
                    email_sender(to=recipient, subject=subject, body=body)
                else:
                    email_error = "email_sender is neither callable nor has .send"
                    continue
                sent = True
            except Exception as e:
                email_error = f"{type(e).__name__}: {e}"
    elif not email_sender:
        email_error = "no email_sender in deps"
    elif not recipients:
        email_error = "no recipients"
    
    # Send via Google Chat (plain-English coworker copy)
    chat_error = ""
    if chat_sender:
        chat_msg = build_hitl_chat(request)
        try:
            if chat_sender(chat_msg):
                sent = True
        except Exception as e:
            chat_error = f"{type(e).__name__}: {e}"
    
    error_reason = ""
    if not sent:
        parts = []
        if email_error:
            parts.append(f"email: {email_error}")
        if chat_error:
            parts.append(f"chat: {chat_error}")
        error_reason = "; ".join(parts) or "unknown failure"
    return (sent, error_reason)


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
    email_sent, email_error = ping_carlo(request, deps)
    if not email_sent:
        return HitlResponse(
            source="system",
            suggestion=f"Could not send HITL notification to Carlo ({email_error})",
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
