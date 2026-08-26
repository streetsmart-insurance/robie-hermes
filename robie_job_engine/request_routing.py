from __future__ import annotations

import re
from dataclasses import dataclass


WORKER_FOR_ACTION = {
    "drive.skill_sync": "drive-skill-sync",
    "carrier.proposal": "carrier-proposal",
    "browser.read": "browser-read",
    "ezlynx.reassign": "hermes-cua",
    "ezlynx.move_document": "hermes-cua",
    "ezlynx.apply_label": "hermes-cua",
    "ezlynx.submission_audit": "submission-audit",
    "ezlynx.session_refresh": "session-refresh",
    "filesystem.skill_update": "hermes-cua",
    "hermes.plain_english": "hermes-cua",
    "hermes.google_chat_task": "hermes-cua",
    "hermes.needs_clarification": "hermes-cua",
}

BOUNDED_ENGINE_ACTIONS = frozenset(
    {
        "drive.skill_sync",
        "carrier.proposal",
        "browser.read",
        "ezlynx.reassign",
        "ezlynx.move_document",
        "ezlynx.apply_label",
        "ezlynx.submission_audit",
        "ezlynx.session_refresh",
    }
)

_PROPOSAL_PHRASES = (
    "carrier proposal",
    "create a proposal",
    "generate a proposal",
    "make a proposal",
    "write a proposal",
    "proposal pdf",
)

_BROWSER_READ_PHRASES = (
    "read only",
    "read-only",
    "just look",
    "look up the page",
    "what does the page say",
    "check the page",
    "read the page",
    "open the page and tell me",
    "browser-only read",
    "browser only read",
)

_MUTATION_WORDS = (
    "upload",
    "move",
    "delete",
    "apply",
    "send",
    "create",
    "generate",
    "reassign",
    "submit",
)

_VAGUE = frozenset(
    {
        "do it",
        "handle this",
        "fix it",
        "take care of this",
        "you know what to do",
        "please help",
        "go ahead",
    }
)


@dataclass(frozen=True)
class RequestClassification:
    action_type: str
    worker: str
    hold_status: str | None = None


def _normalized(text: str) -> str:
    return " ".join(str(text or "").casefold().split())


def classify_request(text: str, *, attachment_count: int = 0) -> RequestClassification:
    """Deterministically classify a staff request before any worker runs."""
    normalized = _normalized(text)
    if is_skill_sync_command(normalized):
        return RequestClassification(
            "drive.skill_sync", WORKER_FOR_ACTION["drive.skill_sync"]
        )
    if _is_carrier_proposal(normalized):
        return RequestClassification("carrier.proposal", WORKER_FOR_ACTION["carrier.proposal"])
    if "ezlynx" in normalized and "reassign" in normalized:
        return RequestClassification("ezlynx.reassign", WORKER_FOR_ACTION["ezlynx.reassign"])
    if "ezlynx" in normalized and "move" in normalized and "document" in normalized:
        return RequestClassification("ezlynx.move_document", WORKER_FOR_ACTION["ezlynx.move_document"])
    if "ezlynx" in normalized and "label" in normalized:
        return RequestClassification("ezlynx.apply_label", WORKER_FOR_ACTION["ezlynx.apply_label"])
    if _is_submission_audit(normalized):
        return RequestClassification(
            "ezlynx.submission_audit", WORKER_FOR_ACTION["ezlynx.submission_audit"]
        )
    if _is_skill_update(normalized):
        return RequestClassification(
            "filesystem.skill_update", WORKER_FOR_ACTION["filesystem.skill_update"]
        )
    if _is_browser_read(normalized):
        return RequestClassification("browser.read", WORKER_FOR_ACTION["browser.read"])
    if normalized in _VAGUE and attachment_count == 0:
        return RequestClassification(
            "hermes.needs_clarification",
            WORKER_FOR_ACTION["hermes.needs_clarification"],
            hold_status="NEEDS_CLARIFICATION",
        )
    if _is_plain_english(normalized, attachment_count):
        return RequestClassification("hermes.plain_english", WORKER_FOR_ACTION["hermes.plain_english"])
    return RequestClassification(
        "hermes.google_chat_task", WORKER_FOR_ACTION["hermes.google_chat_task"]
    )


def is_skill_sync_command(text: str) -> bool:
    """Recognize deterministic admin phrasing without catching Skill edits."""
    normalized = _normalized(text).strip(" .!?")
    return normalized in {
        "/sync-skills",
        "/sync skills",
        "sync skills",
        "update memory",
        "refresh skills",
        "refresh memory",
    }


def _is_carrier_proposal(text: str) -> bool:
    if any(phrase in text for phrase in _PROPOSAL_PHRASES):
        return True
    return "proposal" in text and ("fee" in text or "$350" in text or "350" in text)


def _is_browser_read(text: str) -> bool:
    if any(word in text for word in _MUTATION_WORDS):
        return False
    return any(phrase in text for phrase in _BROWSER_READ_PHRASES)


def _is_submission_audit(text: str) -> bool:
    if "submission center" not in text:
        return False
    if any(
        re.search(rf"\b{word}\b", _positive_request_text(text))
        for word in ("upload", "move", "delete", "send", "submit")
    ):
        return False
    return any(word in text for word in ("audit", "review", "check", "read", "overdue"))


def _positive_request_text(text: str) -> str:
    """Remove explicit safety prohibitions before mutation-word routing."""
    kept: list[str] = []
    for clause in re.split(r"(?<=[.!?;])\s+", text):
        normalized = clause.strip()
        if re.match(r"^(?:do not|don't|never)\b", normalized):
            contrast = re.search(r"\b(?:but|however)\b(.+)$", normalized)
            if contrast:
                kept.append(contrast.group(1))
            continue
        kept.append(normalized)
    return " ".join(kept)


def _is_skill_update(text: str) -> bool:
    has_target = "skill.md" in text or " skill file" in text or " skill-file" in text
    return has_target and any(word in text for word in ("update", "edit", "write", "create"))


def _is_plain_english(text: str, attachment_count: int) -> bool:
    if not text or attachment_count:
        return False
    return any(
        text.startswith(prefix)
        for prefix in (
            "please ",
            "can you ",
            "could you ",
            "i need ",
            "we need ",
        )
    )
