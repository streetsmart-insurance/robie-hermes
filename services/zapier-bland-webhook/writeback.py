"""EZLynx writeback for Bland calls (Carlo's standing rule).

Every Bland call — no matter what triggered it — must write back to EZLynx:
1. A concise Notes API entry on the applicant's discussion.
2. The MP3 recording via the Documents API.

Note text rules (house conventions):
- Plain English, first line states unambiguously whether the call was
  successful. Agency staff (non-technical) must understand it.
- No call IDs, no jargon ("double-dial", "voicemail_action", etc.).
- The Discussion API refuses bodies containing phone-number-like values,
  so NEVER put literal phone numbers in note text (transcript text is
  scrubbed). Reference "the callback number Eva left on the voicemail"
  instead of digits.
- Every note ends with "Robie was here".
"""
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger("bland_dispatcher.writeback")

SIGNATURE = "\n\nRobie was here"

# Campaign ID -> plain-English topic for the note's opening sentence.
# Includes the hyphenated variants the Zapier Zaps actually send.
TOPIC_LABELS = {
    "robie-lead-followup": "your recent quote request",
    "robie-lead-follow-up": "your recent quote request",
    "robie-client-outreach": "a check-in on your account",
    "robie-cancellation": "your policy cancellation request",
    "robie-audit": "an audit follow-up",
    "robie-returned-mail": "returned mail for your address on file",
    "robie-esign": "documents needing your electronic signature",
    "robie-e-sign": "documents needing your electronic signature",
    "robie-additional-info": "additional information needed for your request",
    "robie-recommendations": "coverage recommendations",
    "robie-unresponsive": "a follow-up (client hasn't responded)",
    "robie-renewal-reachout": "your upcoming policy renewal",
    "robie-renewal-reach-out": "your upcoming policy renewal",
    "robie-call": "your request",
}

# Same shape as the phone guard used in tests: scrub phone-like values
# out of free-text (transcript summaries) before they reach EZLynx.
_PHONE_LIKE_RE = re.compile(r"(\+?1?[\s\-.]?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4})")


def _scrub_phones(text: str) -> str:
    return _PHONE_LIKE_RE.sub("[phone number]", text or "")


def _topic_for(campaign_id: str, label_name: str) -> str:
    cid = (campaign_id or "").strip().lower()
    if cid in TOPIC_LABELS:
        return TOPIC_LABELS[cid]
    return (label_name or "").strip() or "your request"


def _attempt_outcome(attempt: Dict[str, Any]) -> str:
    """Classify one call attempt: 'connected' | 'failed' | 'unknown'.

    'connected' means Bland's final_status shows the call actually reached
    someone or something (human or voicemail). 'failed' means it definitively
    did not (carrier failure, no-answer, busy, canceled, or the POST itself
    failed). 'unknown' means we cannot tell — e.g. the POST succeeded but
    the status poll timed out. Callers must surface 'unknown' honestly;
    it must NEVER be reported as a success.
    """
    if not attempt.get("success"):
        # A POST that died on timeout/network may still have been accepted
        # by Bland — the call may have gone out. That is 'unknown', not
        # 'failed'. Definitive rejections (4xx, auth, validation) are failed.
        err = str(attempt.get("error") or "").lower()
        if any(h in err for h in ("timed out", "timeout", "connection reset",
                                  "network")):
            return "unknown"
        return "failed"
    final = attempt.get("final_status") or {}
    if final.get("poll_timed_out"):
        return "unknown"
    answered_by = str(final.get("answered_by") or "").lower()
    if answered_by in ("human", "voicemail"):
        return "connected"
    status = str(final.get("status") or "").lower()
    if status in ("completed", "ended"):
        # Terminal but answered_by missing — the call ran its course.
        # Treat as connected only if there is a duration; otherwise unknown.
        try:
            dur = float(final.get("duration") or final.get("call_duration") or 0)
        except (TypeError, ValueError):
            dur = 0
        return "connected" if dur > 0 else "unknown"
    if status in ("failed", "no-answer", "busy", "canceled"):
        return "failed"
    # No final_status at all (POST ok, never polled): unknown, not success.
    if not final:
        return "unknown"
    return "unknown"


def _call_succeeded(call_summary: Dict[str, Any]) -> bool:
    """True only when at least one attempt verifiably CONNECTED.

    A successful Bland POST alone is NOT success — the call may have died
    at the carrier. This was the September-incident class of bug (false
    "call dispatched" notes). Uncertain outcomes return False; the note
    text distinguishes 'failed' from 'unknown'.
    """
    if call_summary.get("mode") == "DRY_RUN":
        return False
    return any(
        _attempt_outcome(a) == "connected"
        for a in (call_summary.get("attempts") or [])
    )


def _call_outcome_unknown(call_summary: Dict[str, Any]) -> bool:
    """True when no attempt connected but at least one is 'unknown'.

    The call may have happened (e.g. POST timed out after Bland accepted
    it, or the status poll timed out). The note must say we couldn't
    confirm, never claim failure as fact.
    """
    if call_summary.get("mode") == "DRY_RUN":
        return False
    outcomes = [
        _attempt_outcome(a) for a in (call_summary.get("attempts") or [])
    ]
    return bool(outcomes) and "connected" not in outcomes and "unknown" in outcomes


def _duration_text(attempt: Dict[str, Any], call_summary: Dict[str, Any]) -> str:
    secs: Optional[int] = None
    final = attempt.get("final_status") or {}
    for key in ("duration", "duration_seconds", "call_duration"):
        try:
            if final.get(key) is not None:
                secs = int(float(final[key]))
                break
        except (TypeError, ValueError):
            continue
    if secs is None and call_summary.get("duration_seconds") is not None:
        try:
            secs = int(float(call_summary["duration_seconds"]))
        except (TypeError, ValueError):
            secs = None
    if secs is None:
        return "a few minutes"
    if secs < 60:
        return f"{secs} seconds"
    mins = secs // 60
    return f"{mins} minute" + ("s" if mins != 1 else "")


def _outcome_sentence(call_summary: Dict[str, Any], first_name: str) -> str:
    """Plain-English description of what happened on the call.

    Never overstates: an 'unknown' answered_by is reported as uncertainty,
    not as a human answer.
    """
    attempts = call_summary.get("attempts") or []
    if call_summary.get("halted_before_redial"):
        return ("The first attempt went to voicemail. The callback was not "
                "placed because the system was paused mid-call.")
    if call_summary.get("redialed"):
        last = attempts[-1] if attempts else {}
        if _attempt_outcome(last) == "connected":
            return "The first attempt went to voicemail, so we called back and left a message."
        return "The first attempt went to voicemail, and the callback attempt did not go through."
    if call_summary.get("voicemail_hit"):
        return "The call went to voicemail."
    # Look at the last successful POST for what actually happened.
    for attempt in reversed(attempts):
        if not attempt.get("success"):
            continue
        final = attempt.get("final_status") or {}
        answered_by = str(final.get("answered_by") or "").lower()
        if answered_by == "human":
            return f"Spoke with {first_name} for {_duration_text(attempt, call_summary)}."
        if answered_by == "voicemail":
            return "The call went to voicemail."
        if answered_by == "unknown" or not answered_by:
            return ("The call was placed but we couldn't confirm whether it "
                    "reached the person or voicemail.")
        return "The call was placed."
    return "The call was placed."


def _plain_error(err: str) -> str:
    """Map common technical errors to plain English."""
    low = err.lower()
    if "timed out" in low or "timeout" in low:
        return "the calling service timed out"
    if "connection" in low:
        return "could not reach the calling service"
    if "401" in low or "unauthorized" in low or "403" in low:
        return "the calling service rejected our credentials"
    if "429" in low or "rate limit" in low:
        return "the calling service rate-limited the request"
    if any(code in low for code in ("500", "502", "503", "504")):
        return "the calling service had an internal error"
    if "disconnected" in low:
        return "the number was disconnected"
    if "no answer" in low:
        return "there was no answer"
    return err


def _failure_reason(call_summary: Dict[str, Any]) -> str:
    reasons = []
    for a in call_summary.get("attempts") or []:
        if a.get("success"):
            continue
        err = str(a.get("error") or "").strip()
        if err:
            plain = _plain_error(err)
            if plain not in reasons:
                reasons.append(plain)
    if reasons:
        return "; ".join(reasons)
    return "the call did not connect"


def _recording_sentence(call_summary: Dict[str, Any], recording_status: str) -> str:
    if call_summary.get("recording_url"):
        return "The call recording is saved in the applicant's Documents tab."
    if recording_status in ("failed", "error"):
        return "Note: the call recording is not available — the upload failed."
    if recording_status == "pending":
        return "Note: the call recording is not yet available."
    return ""


def _phones_tried_sentence(call_summary: Dict[str, Any]) -> str:
    """Note when we fell over to a backup number (trust-building).

    call_summary may carry 'phones_tried': [{'result': ...}] — one entry
    per phone dialed, in order. Phone digits are never in notes, so entries
    use labels like 'the first number on file'.
    """
    tried = call_summary.get("phones_tried") or []
    if len(tried) < 2:
        return ""
    # Find the first non-connected entry (the one we fell over from).
    first_bad = next((t for t in tried if t.get("result") != "connected"), None)
    if not first_bad:
        return ""
    reason = str(first_bad.get("result") or "did not work").strip()
    # Map terse labels to natural phrasing.
    if reason == "service failure":
        return ""
    return (f"Note: the first number on file {reason}, "
            f"so we tried the next number on file.")


def _one_line(text: str, limit: int = 300) -> str:
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) > limit:
        return collapsed[:limit].rstrip() + "..."
    return collapsed


def format_call_note(
    client_name: str,
    campaign_id: str,
    label_name: str,
    call_summary: Dict[str, Any],
    recording_status: str = "",
) -> str:
    """Build the plain-English EZLynx note body for a completed dispatch.

    The first line states unambiguously whether the call was successful.
    No call IDs, no jargon. Phone numbers are scrubbed from free text
    (the Discussion API rejects them).

    call_summary keys: mode, attempts (list), voicemail_hit, redialed,
    duration_seconds, recording_url, transcript_summary.
    """
    topic = _topic_for(campaign_id, label_name)
    name = (client_name or "").strip() or "the client"
    first_name = name.split()[0]

    if call_summary.get("mode") == "DRY_RUN":
        body = (
            f"Called {name} about {topic}. "
            "Mode: DRY RUN \u2014 no real call was placed. "
            "Eva identified herself as an AI assistant calling for Jake "
            "from StreetSmart Insurance."
        )
        return body + SIGNATURE

    if _call_succeeded(call_summary):
        parts = [
            f"Called {name} about {topic}.",
            "The call was successful.",
            _outcome_sentence(call_summary, first_name),
        ]
        tsum = _one_line(call_summary.get("transcript_summary") or "")
        if tsum:
            parts.append(f"What was discussed: {_scrub_phones(tsum)}")
        rec = _recording_sentence(call_summary, recording_status)
        if rec:
            parts.append(rec)
        parts.append(
            "Eva identified herself as an AI assistant calling for Jake "
            "from StreetSmart Insurance."
        )
        phones_note = _phones_tried_sentence(call_summary)
        if phones_note:
            parts.append(phones_note)
        return " ".join(p for p in parts if p) + SIGNATURE

    if _call_outcome_unknown(call_summary):
        # The call may have happened (POST accepted but status unconfirmed).
        # Say so plainly — never claim failure as fact, never claim success.
        return (
            f"Attempted to call {name} about {topic}. "
            "We couldn't confirm whether the call went through — the phone "
            "system accepted the request but we lost track of the outcome. "
            "No message was confirmed left. If the client mentions receiving "
            "a call from Eva, it was this attempt."
        ) + SIGNATURE

    reason = _failure_reason(call_summary)
    return (
        f"Attempted to call {name} about {topic}. "
        f"The call was NOT successful: {reason}. "
        "No message was left."
    ) + SIGNATURE


def pick_writeback_discussion(
    discussions: List[Dict[str, Any]],
    trigger_discussion_id: Optional[str] = None,
) -> Optional[str]:
    """Choose where to append the call note.

    Prefer the discussion that carried the trigger (keeps the call
    next to its instruction); otherwise the most recently active discussion
    (v8 field: lastModified; legacy fallbacks kept for safety).
    """
    if trigger_discussion_id:
        return trigger_discussion_id
    best = None
    best_ts = ""
    for disc in discussions:
        did = str(disc.get("discussionId") or disc.get("id") or "")
        if not did:
            continue
        ts = str(
            disc.get("lastModified")
            or disc.get("lastActivityDate")
            or disc.get("modifiedDate")
            or ""
        )
        if ts >= best_ts:
            best, best_ts = did, ts
    return best
