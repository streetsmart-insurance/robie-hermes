"""Question replies and short vague asks.

A question such as "Which carriers do we quote for NJ homeowners?" is not
an EZLynx write. Destination readback must not run, and the reply is the
answer. A short ask with no client, policy, carrier, or attachment is not
a job to investigate.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

_QUESTION_PREFIXES = (
    "which ",
    "what ",
    "who ",
    "why ",
    "when ",
    "where ",
    "how ",
    "do we ",
    "do you ",
    "is there ",
    "are there ",
    "tell me ",
)

# These are work requests, even when they end in a question mark.
_ACTION_REQUEST = re.compile(
    r"\b(?:certificate of insurance|certificate request|certificates?|cois?|"
    r"policy change|endorsements?|endorse|mailing address|"
    r"upload|bind|delete|draft|issue|create|file a note|add a note)\b"
    r"|\b(?:change|update)\b.{0,40}\b(?:policy|address|mailing)\b",
    re.IGNORECASE,
)

_POLITE_PREFIX = re.compile(
    r"^(?:@\s*robie\b[,\s]*|(?:please|can you|could you|would you)\b[\s,]*)",
    re.IGNORECASE,
)

_WORK_MARKERS = re.compile(
    r"\b(?:applicant|policy|policies|carrier|carriers|certificate|certificates|"
    r"cert|certs|coi|quote|quotes|endorsement|endorsements|mailing|address|"
    r"insured|client|homeowners|auto)\b",
    re.IGNORECASE,
)

_CLIENT_NAME = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b")

_VAGUE_WORD_LIMIT = 6

# A short polite ask that names a real task is still a job.
_TASK_VERBS = re.compile(
    r"\b(?:finish|file|update|change|issue|draft|create|send|upload|explain|"
    r"remind|check|read|open|run|set|add|write|move|delete|apply|quote|"
    r"complete|fill|bind|edit|reassign|audit|sync|generate|submit|perform|"
    r"verify|inspect|navigate|download|attach|endorse)\b",
    re.IGNORECASE,
)

CLARIFICATION_QUESTION = (
    "What should I do? Name the client, the policy, or the task you want finished."
)

CERT_ROUTE = (
    "Route: certificate. Use ezlynx_discussion_note and "
    "robie_job_engine.certificate_filing to draft and file the holder note "
    "on the existing discussion. Draft only. "
    "Do not bind, take payment, or email the client or the certificate holder. "
    "Do not browse EZLynx by hand. Do not read Robie's source, jobs.db, or token files."
)

POLICY_CHANGE_ROUTE = (
    "Route: policy change. File the note with ezlynx_discussion_note on the "
    "existing discussion title named in the request. For a mailing-address "
    "change, use that existing title (for example Policy Change Request "
    "Checkup - Mailing Address update). Do not create a discussion. "
    "The note must say the mailing-address change was requested until a "
    "readback shows the new address. Do not say the mailing address was "
    "updated before that readback. "
    "Do not use Playwright Add Note or Save Note. "
    "Do not bind, take payment, or email the client. "
    "Do not read Robie's source, jobs.db, or token files."
)

_VERIFIER_NOISE = re.compile(r"file-mutation verifier", re.IGNORECASE)

_ADDRESS_COMPLETION_CLAIMS = (
    (
        re.compile(r"\bupdated the mailing address\b", re.IGNORECASE),
        "requested a mailing address change",
    ),
    (
        re.compile(r"\bmailing address has been updated\b", re.IGNORECASE),
        "mailing address change was requested",
    ),
    (
        re.compile(r"\bmailing address was updated\b", re.IGNORECASE),
        "mailing address change was requested",
    ),
    (
        re.compile(r"\bmailing address updated\b", re.IGNORECASE),
        "mailing address change requested",
    ),
)


def strip_answer_verifier_noise(text: str) -> str:
    """Drop Hermes verifier warnings from an answer-only reply."""
    kept = [
        line
        for line in str(text or "").splitlines()
        if not _VERIFIER_NOISE.search(line)
    ]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def address_readback_proved(args: dict | None = None) -> bool:
    """True only when this call or a checkpoint says the new address was read back."""
    payload = dict(args or {})
    flag = payload.get("address_readback_proved")
    if flag is True or str(flag or "").strip().lower() in {"1", "true", "yes"}:
        return True
    import os

    job_id = str(
        payload.get("job_id")
        or os.environ.get("ROBIE_JOB_ID")
        or os.environ.get("JOB_ID")
        or ""
    ).strip()
    db_path = str(payload.get("db_path") or os.environ.get("ROBIE_JOB_DB") or "").strip()
    if not job_id or not db_path:
        return False
    try:
        from .store import JobStore

        note = JobStore(db_path).get_checkpoint(job_id, "address_readback")
    except Exception:
        return False
    if not note:
        return False
    if note.get("passed") is True or note.get("proved") is True:
        return True
    return str(note.get("status") or "").casefold() == "passed"


def rewrite_unproved_address_note(note_text: str, *, proved: bool = False) -> str:
    """Keep a completion claim only after the address readback proved it."""
    text = str(note_text or "")
    if proved:
        return text
    for pattern, replacement in _ADDRESS_COMPLETION_CLAIMS:
        text = pattern.sub(replacement, text)
    return text

FORBIDDEN_READ_RULE = (
    "Do not read Robie's own source, jobs.db, .hermes/google_token.json, "
    "or any token file during a job. Do not grep the server. "
    "Use the purpose-built tool named in the task."
)


def _normalized(text: str) -> str:
    return " ".join(str(text or "").casefold().split())


def _ask_body(text: str) -> str:
    raw = str(text or "").strip()
    if raw.lower().startswith("subject:") and "\n\n" in raw:
        raw = raw.split("\n\n", 1)[1]
    return raw.strip()


def core_request(text: str) -> str:
    """Drop @Robie and can you / please / could you, repeatedly."""
    normalized = _normalized(_ask_body(text))
    previous = None
    while previous != normalized:
        previous = normalized
        normalized = _POLITE_PREFIX.sub("", normalized).strip()
    return normalized.strip(" .!?")


def has_client_name(text: str) -> bool:
    """A two-word capitalized name, ignoring the @Robie mention."""
    cleaned = re.sub(r"@\s*Robie\b", "", str(text or ""), flags=re.IGNORECASE)
    return _CLIENT_NAME.search(cleaned) is not None


def is_vague_short_request(text: str, *, attachment_count: int = 0) -> bool:
    """Short polite ask with nothing to act on.

    The "can you / please" prefix is what used to turn this into a full
    plain-English job. A short operational phrase with no polite prefix
    stays a job.
    """
    if attachment_count:
        return False
    if not _POLITE_PREFIX.search(_normalized(_ask_body(text))):
        return False
    core = core_request(text)
    words = [word for word in core.split() if word]
    if not words or len(words) > _VAGUE_WORD_LIMIT:
        return False
    if _WORK_MARKERS.search(core) or _TASK_VERBS.search(core):
        return False
    if re.search(r"\b\d{5,}\b", core):
        return False
    if has_client_name(_ask_body(text)):
        return False
    return True


def is_certificate_or_policy_change(text: str) -> bool:
    from .request_routing import _is_certificate_request, _is_policy_change_request

    normalized = _normalized(_ask_body(text))
    if "mailing address" in normalized:
        return True
    return _is_certificate_request(normalized) or _is_policy_change_request(normalized)


def purpose_built_instructions(text: str) -> str:
    """Name the existing tool. Empty when this is not a cert or policy change."""
    normalized = _normalized(_ask_body(text))
    from .request_routing import _is_certificate_request, _is_policy_change_request

    if _is_certificate_request(normalized):
        return CERT_ROUTE
    if _is_policy_change_request(normalized) or "mailing address" in normalized:
        return POLICY_CHANGE_ROUTE
    return ""


def is_informational_ask(text: str) -> bool:
    """A question to answer, not an EZLynx write or a vague shrug.

    Prefixes are checked on the request after @Robie / please / can you
    are stripped, so "@Robie which..." and "Can you tell me which..."
    are questions. The word "quote" inside a question is not a quote job.
    """
    body = _ask_body(text)
    core = core_request(text)
    if is_vague_short_request(body, attachment_count=0) and not body.rstrip().endswith("?"):
        # "can you do a book for me" is vague, not a question to research.
        if not core.startswith(_QUESTION_PREFIXES):
            return False
    normalized = _normalized(body)
    if not core and not normalized:
        return False
    if re.search(
        r"\b(?:get a quote|need a quote|new quote|quote request|request a quote)\b",
        core,
    ):
        return False
    asks = core.startswith(_QUESTION_PREFIXES) or core.endswith("?") or normalized.endswith("?")
    work_verb = re.search(
        r"\b(?:change|update|issue|create|file|draft|bind|endorse|upload)\b",
        core,
    )
    if asks and not work_verb:
        return True
    if _ACTION_REQUEST.search(normalized) or "mailing address" in normalized:
        return False
    # "get a quote" is work. "which carriers do we quote" is a question.
    if re.search(r"\bquotes?\b", core) and not asks:
        return False
    if purpose_built_instructions(body):
        return False
    return asks


def is_answer_only_job(job: dict[str, Any] | None) -> bool:
    payload = dict((job or {}).get("payload") or {})
    if payload.get("answer_only"):
        return True
    if str((job or {}).get("action_type") or "").startswith("ezlynx."):
        return False
    text = payload.get("request_text") or payload.get("text") or payload.get("prompt") or ""
    return is_informational_ask(str(text))


def _outcome_verifier_type():
    from .message_verification import MessageOutcomeVerifier

    return MessageOutcomeVerifier


class SkipDestinationReadback(_outcome_verifier_type()):
    """Do not re-read EZLynx for an answer-only question.

    This stays a MessageOutcomeVerifier so the Chat wiring check still
    sees the destination reader. Answer-only jobs return before that read.
    """

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.reader = getattr(inner, "reader", inner)

    def verify(self, job: dict[str, Any], action: dict[str, Any]) -> Any:
        if is_answer_only_job(job):
            from .models import VerificationEvidence, VerificationResult

            return VerificationResult(
                False,
                VerificationEvidence(
                    method="answer_text",
                    source="question",
                    expected={"answer_only": True},
                    observed={"ezlynx_readback": "not_run"},
                    authoritative=False,
                    captured_at=datetime.now(timezone.utc).isoformat(),
                ),
                retryable=False,
                error="answer only; no EZLynx destination readback",
            )
        return self.inner.verify(job, action)
