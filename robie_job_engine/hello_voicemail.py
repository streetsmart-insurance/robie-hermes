"""RingCentral voicemail / SMS callback workflow for hello@ intake (Step 5).

The 6-month analysis found 14 voicemail/SMS notifications in hello@ —
every one with no visible reply, including a personal-lines lead that
sat unanswered. The old classifier treated these as noise; they are now
GENUINE (voicemail_text_notify).

Workflow per notification:
1. Parse: message type (voicemail/sms), caller name, caller number,
   transcription / message text, received time.
2. File: a plain-English summary note to the applicant/prospect's
   existing discussion — transcription included, phone digits NEVER in
   the note (agency rule: EZLynx notes carry no phone numbers).
3. Alert: the on-duty producer/CSR with the callback number and a
   one-business-hour callback SLA task.

Read-only until wired: filing runs through the injected interfaces in
hello_filing.execute_plan(). Phone numbers appear only in the alert
payload and in-memory parse results — never in filed notes, never in
logs the worker writes.
"""

from __future__ import annotations

import re
from typing import Any

from .hello_filing import TASK_CALLBACK, build_filing_plan

# ---------------------------------------------------------------------------
# Notification parsing
# ---------------------------------------------------------------------------

_VOICEMAIL_SUBJECT_RE = re.compile(
    r"(?i)(?:new\s+)?voice\s*-?\s*mail\s+from\s+(.+?)\s*(?:\(([^()]*)\))?\s*$")
_SMS_SUBJECT_RE = re.compile(
    r"(?i)^\s*(?:re:\s*)?(?:do not reply\s*-\s*)?(.+?)\s+has sent you a "
    r"text message\s*$")
_MISSED_CALL_RE = re.compile(
    r"(?i)\bmissed\s+call\b(?:\s+from\s+(.+?))?\s*(?:\(([^()]*)\))?\s*$")
_TRANSCRIPTION_RE = re.compile(
    r"(?im)^\s*transcription\s*:\s*(.+?)(?=^\s*\S+:\s*|\Z)", re.DOTALL)
_PHONE_RE = re.compile(r"\+?1?\s*(?:\(\s*)?(\d{3})(?:\s*\)|\s*)?[-.\s]*"
                       r"(\d{3})[-.\s]*(\d{4})")
_RECEIVED_RE = re.compile(
    r"(?im)^\s*(?:received|date|time)\s*:\s*(.+?)\s*$")


def _digits(text: str | None) -> str | None:
    """First plausible 10-digit caller number in text, or None."""
    match = _PHONE_RE.search(text or "")
    if not match:
        return None
    return "".join(match.groups())


def parse_voicemail_notification(subject: str | None, body: str | None,
                                 sender: str | None = None) -> dict[str, Any]:
    """Parse a RingCentral voicemail/SMS relay notification.

    Returns {"message_type": "voicemail"|"sms"|"missed_call"|"unknown",
      "caller_name", "caller_number", "transcription", "received_at",
      "is_lead"}. caller_number is the raw digits for the ALERT path
    only — callers must strip it before filing any note.
    is_lead is a heuristic: a voicemail/SMS from an unknown personal
    name with quote/cover language. Never authoritative on its own.
    """
    subject = subject or ""
    body = body or ""
    result: dict[str, Any] = {
        "message_type": "unknown",
        "caller_name": None,
        "caller_number": None,
        "transcription": None,
        "received_at": None,
        "is_lead": False,
    }
    voice_match = _VOICEMAIL_SUBJECT_RE.search(subject)
    sms_match = _SMS_SUBJECT_RE.search(subject)
    missed_match = _MISSED_CALL_RE.search(subject)
    if voice_match:
        result["message_type"] = "voicemail"
        result["caller_name"] = voice_match.group(1).strip() or None
        result["caller_number"] = (_digits(voice_match.group(2))
                                   or _digits(body))
        transcription = _TRANSCRIPTION_RE.search(body)
        result["transcription"] = (transcription.group(1).strip()
                                   if transcription else None)
    elif sms_match:
        result["message_type"] = "sms"
        result["caller_name"] = sms_match.group(1).strip() or None
        result["caller_number"] = _digits(body)
        # The SMS body IS the message; strip headers/footers.
        text = body.strip()
        text = re.sub(r"(?im)^\s*do not reply.*$", "", text).strip()
        result["transcription"] = text or None
    elif missed_match:
        result["message_type"] = "missed_call"
        result["caller_name"] = (missed_match.group(1) or "").strip() or None
        result["caller_number"] = (_digits(missed_match.group(2))
                                   or _digits(body))
    else:
        return result
    received = _RECEIVED_RE.search(body)
    result["received_at"] = received.group(1).strip() if received else None
    message_text = result["transcription"] or ""
    result["is_lead"] = bool(
        result["message_type"] in ("voicemail", "sms")
        and result["caller_name"]
        and re.search(r"(?i)\b(quote|coverage|new policy|insurance)\b",
                      message_text))
    return result


# ---------------------------------------------------------------------------
# Note sanitizing: phone digits never reach EZLynx notes
# ---------------------------------------------------------------------------

_PHONE_LIKE_RE = re.compile(
    r"\+?1?[-.\s]*(?:\(\s*\d{3}\s*\)|\d{3})[-.\s]*\d{3}[-.\s]*\d{4}")


def sanitize_note_body(text: str | None) -> str:
    """Strip phone-number-like values from a note body.

    The agency rule: EZLynx discussion text must not carry phone numbers
    (the call automation dials numbers found in discussion text). The
    note says "callback number on file" instead.
    """
    cleaned = _PHONE_LIKE_RE.sub("[number on file]", text or "")
    return " ".join(cleaned.split())


def build_voicemail_summary(parsed: dict[str, Any],
                            summary_hint: str | None = None) -> str:
    """Plain-English note body for the filed summary.

    Contains the transcription/message text and the caller name — never
    the digits.
    """
    who = parsed.get("caller_name") or "an unknown caller"
    kind = parsed.get("message_type") or "message"
    lines = [f"{kind.capitalize()} from {who}."]
    if parsed.get("received_at"):
        lines.append(f"Received {parsed['received_at']}.")
    transcription = parsed.get("transcription")
    if transcription:
        lines.append(f"Message: {transcription}")
    else:
        lines.append("No transcription was provided.")
    lines.append("Callback number on file — see the on-duty alert.")
    if summary_hint:
        lines.append(summary_hint)
    if parsed.get("is_lead"):
        lines.append("Possible new-business lead — route to a producer "
                     "with the callback.")
    return sanitize_note_body(" ".join(lines))


# ---------------------------------------------------------------------------
# Callback plan
# ---------------------------------------------------------------------------

def build_voicemail_plan(context: dict[str, Any],
                         on_duty_roster: dict[str, str] | None = None
                         ) -> dict[str, Any]:
    """Build the filing + callback plan for a voicemail/SMS notification.

    context: {"applicant_id", "subject", "body", "sender",
      "summary_hint", "identity": {...}}.
    The plan has two actions: the filed summary note (digits stripped)
    and the on-duty alert carrying the callback number with the
    one-business-hour callback SLA. on_duty_roster maps role names to
    contacts; when it names nobody the alert stays pending.
    """
    parsed = parse_voicemail_notification(
        context.get("subject"), context.get("body"), context.get("sender"))
    note_body = build_voicemail_summary(parsed,
                                        context.get("summary_hint"))
    who = parsed.get("caller_name") or "unknown caller"
    number = parsed.get("caller_number") or "number not parsed"
    alert_message = (f"Callback needed within 1 business hour: "
                     f"{parsed.get('message_type')} from {who}, "
                     f"{number}. Summary filed to applicant "
                     f"{context.get('applicant_id')}.")
    plan = build_filing_plan("voicemail_text_notify", {
        "applicant_id": context.get("applicant_id"),
        "summary": note_body,
        "documents": None,
        "task_title": f"Callback within 1 business hour — {who}",
        "task": {"task_kind": TASK_CALLBACK, "callback_number": number,
                 "stays_open": False},
        "alert_message": alert_message,
        "identity": context.get("identity"),
    })
    plan["parsed"] = parsed
    plan["on_duty_roster"] = on_duty_roster or {}
    return plan


__all__ = [
    "TASK_CALLBACK",
    "build_voicemail_plan",
    "build_voicemail_summary",
    "parse_voicemail_notification",
    "sanitize_note_body",
]
