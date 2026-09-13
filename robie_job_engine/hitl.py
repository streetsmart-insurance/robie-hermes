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
    # The worker contract asks for the ROBIE_BLOCKED: prefix, but workers
    # often paste the raw guard error instead (e.g. "PLAYWRIGHT_BLOCKED:
    # write target matched 3 fields"). The prefix is therefore optional:
    # a bare PLAYWRIGHT_BLOCKED / MISSING_REQUIRED_FIELD line is the same
    # machine signal and must also park for human input. This was the
    # root cause of HITL never firing on Google Chat (2026-09-10).
    r"^\s*(?:ROBIE_BLOCKED:\s*)?"
    r"((?:MISSING_REQUIRED_FIELD|PLAYWRIGHT_BLOCKED)\s*:\s*[^\r\n]+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_POLICY_SETUP_FAIL_CLOSED = re.compile(
    r"(?:ROBIE_OUTCOME_UNKNOWN:\s*)?"
    r"(ezlynx_policy_setup is not registered; failing closed[^\r\n]*)",
    re.IGNORECASE,
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
HITL_SLANG_MARKERS = (
    "listen up",
    "listen here",
    "ain't",
    "aint my fault",
    "it ain't my fault",
    "it aint my fault",
    "not my fault",
    "ain't my fault",
    "howdy",
    "y'all",
    "ya'll",
    "partner,",
    "cowboy",
    "shucks",
    "blame me",
    "don't blame",
    "dont blame",
    "it aint",
)
_NICKNAME_VOICE = re.compile(
    r"\b(?:listen up,?\s+)?(?:jake|pal|buddy|chief|sport|boss)!",
    re.IGNORECASE,
)
HITL_TONE_SCENARIO_ID = "hitl-tone:dry-playwright-blocked"
DRY_HITL_ASK = (
    "Reply RETRY after the page is corrected, or reply with the needed correction."
)
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


def hitl_text_is_slang_or_blame(text: str) -> bool:
    """True for cowboy / slang / blame / nickname-voice HITL ad-libs."""
    folded = " ".join(str(text or "").casefold().split())
    if not folded:
        return False
    if any(marker in folded for marker in HITL_SLANG_MARKERS):
        return True
    return _NICKNAME_VOICE.search(str(text or "")) is not None


def extract_artifact_path(text: str) -> str:
    for token in str(text or "").replace("`", " ").split():
        cleaned = token.strip(".,;:\"')")
        if "/artifacts/" in cleaned:
            return cleaned
    return ""


def extract_playwright_reason(text: str) -> str:
    safe = redact_text(str(text or ""))
    match = re.search(
        r"PLAYWRIGHT_BLOCKED\s*:\s*([^\r\n]+)",
        safe,
        re.IGNORECASE,
    )
    if match:
        return match.group(1).strip()[:500]
    path = extract_artifact_path(safe)
    if path:
        return f"cannot open artifact at {path}"
    return ""


def dry_playwright_hitl_text(
    *,
    reason: str,
    artifact_path: str = "",
    job_id: str = "",
    robie_blocked: bool = False,
) -> str:
    detail = str(reason or "browser step blocked").strip() or "browser step blocked"
    lines = [f"PLAYWRIGHT_BLOCKED: {detail}"]
    if artifact_path:
        lines.append(f"Path: {artifact_path}")
    if job_id:
        lines.append(f"Job ID: {job_id}")
    lines.append(DRY_HITL_ASK)
    body = "\n".join(lines)
    if robie_blocked:
        return f"ROBIE_BLOCKED: {body}"
    return body


def sanitize_hitl_chat_text(
    text: str,
    *,
    artifact_path: str = "",
    job_id: str = "",
) -> str:
    """Rewrite cowboy/slang HITL to dry PLAYWRIGHT_BLOCKED + path + ask.

    Templates stay plain English (HITL Carlo). Model ad-libs such as
    "Listen up, Jake!" / "it ain't my fault" are rewritten before send.
    """
    raw = str(text or "")
    if not hitl_text_is_slang_or_blame(raw):
        return raw
    reason = extract_playwright_reason(raw) or "browser step blocked"
    path = artifact_path or extract_artifact_path(raw)
    return dry_playwright_hitl_text(
        reason=reason,
        artifact_path=path,
        job_id=str(job_id or ""),
        robie_blocked="ROBIE_BLOCKED" in raw,
    )


def structured_blocker_reason(text: str) -> str | None:
    """Return only an explicit machine-readable worker blocker."""
    safe = redact_text(str(text or ""))
    fail_closed = policy_setup_fail_closed_reason(safe)
    if fail_closed:
        return fail_closed
    match = _STRUCTURED_BLOCKER.search(safe)
    return match.group(1).strip()[:1_000] if match else None


def policy_setup_fail_closed_reason(text: str) -> str | None:
    """Return the fail-closed line from job c282de98 / FAIL_CLOSED_MESSAGE."""
    from .policy_setup_dispatch import FAIL_CLOSED_MESSAGE, is_policy_setup_fail_closed

    safe = redact_text(str(text or ""))
    if not is_policy_setup_fail_closed(safe):
        return None
    match = _POLICY_SETUP_FAIL_CLOSED.search(safe)
    if match:
        return match.group(1).strip()[:1_000]
    return FAIL_CLOSED_MESSAGE


def policy_setup_fail_closed_hitl_text(*, job_id: str = "", detail: str = "") -> str:
    """Honest HITL for a missing/unregistered ezlynx_policy_setup handler."""
    from .policy_setup_dispatch import FAIL_CLOSED_MESSAGE

    reason = str(detail or "").strip() or FAIL_CLOSED_MESSAGE
    lines = [
        f"ROBIE HITL: STOP AND ASK. {reason}",
        "This Chat job is not still working. The ezlynx_policy_setup tool "
        "was not callable, so ROBIE failed closed and will not use "
        "playwright_exec for policy setup.",
        DRY_HITL_ASK,
    ]
    if job_id:
        lines.append(f"Job ID: {job_id}")
    return "\n".join(lines)


def interaction_for_blocker(
    error: str,
    *,
    action_type: str,
    requester_name: str | None = None,
    job_id: str | None = None,
    subject_name: str | None = None,
    artifact_path: str | None = None,
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
        path = str(artifact_path or "").strip() or extract_artifact_path(safe)
        prompt = dry_playwright_hitl_text(
            reason=detail,
            artifact_path=path,
            job_id=str(job_id or "").strip(),
        )
        checkpoint = f"playwright_blocked:{detail}"
    prompt = sanitize_hitl_chat_text(
        prompt, artifact_path=str(artifact_path or ""), job_id=str(job_id or "")
    )
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
