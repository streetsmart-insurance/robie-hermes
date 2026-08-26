"""Durable human-in-the-loop contracts for bounded browser work."""

from __future__ import annotations

import re
from typing import Any

from .secrets import redact_text


_MISSING_FIELD = re.compile(
    r"(?:MISSING_REQUIRED_FIELD|missing required field)\s*[:=]?\s*([^;,.]+)",
    re.IGNORECASE,
)
_STRUCTURED_BLOCKER = re.compile(
    r"^\s*ROBIE_BLOCKED:\s*"
    r"((?:MISSING_REQUIRED_FIELD|PLAYWRIGHT_BLOCKED)\s*:\s*[^\r\n]+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_SENSITIVE_FIELD = re.compile(
    r"\b(SSN|tax id|password|MFA|one[- ]time code|payment|card number|bank account|routing number)\b",
    re.IGNORECASE,
)

_FIELD_LABELS = {
    "fein": "Federal Employer Identification Number (FEIN)",
    "naics": "Industry Code (NAICS)",
    "naics code": "Industry Code (NAICS)",
    "effective_date": "effective date",
    "effective date": "effective date",
}

_NEW_INTENT_PREFIXES = (
    "what ",
    "when ",
    "where ",
    "who ",
    "why ",
    "how ",
    "show ",
    "show me ",
    "list ",
    "find ",
    "search ",
    "run ",
    "start ",
    "create ",
    "schedule ",
    "cancel ",
    "update ",
    "send ",
    "check ",
    "audit ",
    "can you ",
    "could you ",
    "would you ",
    "please ",
)
_FEIN_REPLY = re.compile(r"^(?:FEIN\s*[:#-]?\s*)?\d{2}-?\d{7}$", re.IGNORECASE)
_NAICS_REPLY = re.compile(
    r"^(?:NAICS(?:\s+code)?\s*[:#-]?\s*)?\d{2,6}$",
    re.IGNORECASE,
)
_DATE_REPLY = re.compile(
    r"^(?:effective(?:\s+date)?\s*[:#-]?\s*)?"
    r"(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/\d{2,4})$",
    re.IGNORECASE,
)


def _friendly_field_label(field_name: str) -> str:
    normalized = re.sub(r"\s+", " ", field_name.strip()).casefold()
    return _FIELD_LABELS.get(normalized, field_name.replace("_", " ").strip())


def _friendly_greeting(requester_name: str | None) -> str:
    clean = redact_text(str(requester_name or "")).strip()
    return f"Hey {clean}, I need a quick hand! 👋" if clean else "Hey, I need a quick hand! 👋"


def _job_note(job_id: str | None) -> str:
    clean = str(job_id or "").strip()
    return f"\n\nJob ID: `{clean[:8]}`" if clean else ""


def structured_blocker_reason(text: str) -> str | None:
    """Return only an explicit machine-readable worker blocker."""
    safe = redact_text(str(text or ""))
    match = _STRUCTURED_BLOCKER.search(safe)
    return match.group(1).strip()[:1_000] if match else None


def interaction_for_blocker(
    error: str,
    *,
    action_type: str,
    requester_name: str | None = None,
    job_id: str | None = None,
    subject_name: str | None = None,
) -> dict[str, Any]:
    """Convert a bounded Playwright blocker into a resumable public prompt."""
    safe = redact_text(str(error or "PLAYWRIGHT_BLOCKED"))[:1_000]
    match = _MISSING_FIELD.search(safe)
    if match:
        field_name = match.group(1).strip()
        field_label = _friendly_field_label(field_name)
        accepts_value = _SENSITIVE_FIELD.search(field_name) is None
        subject = redact_text(str(subject_name or "")).strip()
        work_context = (
            f"the application for {subject}"
            if subject
            else "this application"
        )
        if accepts_value:
            prompt = (
                f"{_friendly_greeting(requester_name)}\n\n"
                f"I'm working on {work_context}, but I'm missing the {field_label}.\n\n"
                f"Please reply in this thread with the {field_label} so I can continue."
                f"{_job_note(job_id)}"
            )
        else:
            prompt = (
                f"{_friendly_greeting(requester_name)}\n\n"
                f"I'm working on {work_context}, but a sensitive required field is missing: "
                f"{field_label}.\n\nFor security, enter it directly in EZLynx, then reply "
                f"RETRY here so I can continue. Do not send the value in Chat."
                f"{_job_note(job_id)}"
            )
        checkpoint = f"required_field:{field_name}"
    else:
        detail = safe.split(":", 1)[-1].strip() or "browser step blocked"
        field_name = "operator_response"
        accepts_value = True
        prompt = (
            f"{_friendly_greeting(requester_name)}\n\n"
            "I paused at an EZLynx browser step because "
            f"{detail}. Please reply with the needed correction, or reply RETRY after "
            f"you have corrected the page, so I can continue."
            f"{_job_note(job_id)}"
        )
        checkpoint = f"playwright_blocked:{detail}"
    return {
        "awaiting": "human_input",
        "action_type": action_type,
        "field_name": field_name,
        "field_label": _friendly_field_label(field_name),
        "checkpoint": checkpoint,
        "blocked_reason": safe,
        "prompt": prompt,
        "accepts_value": accepts_value,
    }


def human_reply_value(text: str) -> str:
    """Return a non-empty reply. Never log or include the result in prompts."""
    value = str(text or "").strip()
    if not value:
        raise ValueError("a non-empty human reply is required")
    return value


def classify_human_reply(text: str, interaction_state: dict[str, Any]) -> str:
    """Classify a DM received while a Job is awaiting human input.

    ``ANSWER`` is deliberately narrow: known fields must match their expected
    shape, and unknown fields accept only short data-like values. Questions,
    slash commands, and action requests are ``NEW_INTENT`` so they can never
    be written into the previous Job payload as a missing-field value.
    ``INVALID`` keeps the Job parked and asks the user for a usable value.
    """

    value = human_reply_value(text)
    normalized = " ".join(value.casefold().split())
    if normalized in {"retry", "/retry"}:
        return "ANSWER"

    is_new_intent = (
        value.startswith("/")
        or "?" in value
        or any(normalized.startswith(prefix) for prefix in _NEW_INTENT_PREFIXES)
    )
    if is_new_intent:
        return "NEW_INTENT"

    if not bool(interaction_state.get("accepts_value", True)):
        return "INVALID"

    field_name = str(interaction_state.get("field_name") or "").casefold()
    if "fein" in field_name:
        return "ANSWER" if _FEIN_REPLY.fullmatch(value) else "INVALID"
    if "naics" in field_name:
        return "ANSWER" if _NAICS_REPLY.fullmatch(value) else "INVALID"
    if "effective" in field_name and "date" in field_name:
        return "ANSWER" if _DATE_REPLY.fullmatch(value) else "INVALID"

    # Generic missing fields may legitimately be a name, address, carrier,
    # or another short value. Multi-sentence prose is treated as a new intent
    # instead of being silently injected into a parked executable Job.
    if len(value) <= 160 and len(value.split()) <= 12 and not re.search(r"[.!]\s+\S", value):
        return "ANSWER"
    return "NEW_INTENT"
