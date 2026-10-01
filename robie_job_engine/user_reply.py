"""One formatter for text a person sees in Chat or email.

Job ids, audit notes, and verifier words stay in logs and the health
channel. This function never adds them back.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger("robie.health")

STUCK_LINE = "I got stuck on that; a CSR should take a look."
_INTERNAL_CODE = re.compile(
    r"\b(?:ROBIE_[A-Z0-9_]+|PLAYWRIGHT_[A-Z0-9_]+|EZLYNX_[A-Z0-9_]*REFUSED"
    r"|MISSING_REQUIRED_FIELD)\b",
    re.IGNORECASE,
)
_SIGN_IN = re.compile(r"\b(?:sign[\s-]?in|log[\s-]?in)\b", re.IGNORECASE)
_ASKS = re.compile(r"^(?:which|what|who|where|when|how)\b", re.IGNORECASE)
_MISSING_FIELD = re.compile(
    r"MISSING_REQUIRED_FIELD\s*:\s*([^\n]+)",
    re.IGNORECASE,
)
SIGN_IN_QUESTION = "Please sign in to EZLynx, then tell me to continue?"
_SELECTOR = re.compile(
    r"(?:input#[A-Za-z_][\w-]*|#[A-Za-z_][\w-]*|>>|xpath=|css=|get_by_\w+)",
    re.IGNORECASE,
)
_FIELD_NAME = re.compile(
    r"\b(?:applicant_id|account_id|note_id|document_id|discussion_id|"
    r"thread_id|mostRecentNoteId|noteCount|NoteId)\b"
)

_JOB_REF = re.compile(r"\bref:\s*job\b[^\n]*", re.IGNORECASE)
_UUID = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_BANNED = re.compile(
    r"\b(?:UNVERIFIED|read-?backs?|audit|heartbeat|Jev|checkpoints?|"
    r"ROBIE_BLOCKED|PLAYWRIGHT_BLOCKED|Discussion\s*API|DiscussionApi)\b",
    re.IGNORECASE,
)
_LABEL_LINE = re.compile(
    r"^(?:what happened|anything needed|status|details|end state|field|"
    r"client|old value|new value|before|after|subject|technical detail|"
    r"worker report(?: \(not proof\))?)\b",
    re.IGNORECASE,
)
_MENU_START = "Here's what I can"
_HITL_KEEP = (
    "Reply in this Chat thread",
    "I got stuck",
    "I need a quick hand",
    "I will not guess",
)


def format_user_reply(text: str, *, collapse: bool = True) -> str:
    """One outbound formatter. Their scrubber runs inside this function.

    Fixture policy numbers, worker markers, and missing-field prompts are
    rewritten first. Multi-line status reports then collapse to the first
    plain line. A help menu is kept so the staff line is not dropped.
    Transport that already formatted the text passes collapse=False so a
    chunk is not rewritten a second time.
    """
    from .answer_only import scrub_user_reply

    asked = plain_clarify_or_sign_in(text)
    if asked:
        return asked
    raw = _strip_markers(scrub_user_reply(text).replace("\r\n", "\n"))
    if not raw or not collapse:
        return _release_internal(text, raw)
    if raw.startswith(_MENU_START) or "I can't make these yet" in raw:
        return _release_internal(text, _clean_menu(raw))
    if any(marker in raw for marker in _HITL_KEEP):
        return _release_internal(text, _clean_menu(raw))
    kept: list[str] = []
    for line in raw.splitlines():
        cleaned = _clean_line(line)
        if cleaned:
            kept.append(cleaned)
    if not kept:
        return _release_internal(text, "I couldn't finish that.")
    questions = [line for line in kept if line.endswith("?")]
    statements = [line for line in kept if not line.endswith("?")]
    chosen = statements[0] if statements else questions[0]
    if chosen[-1] not in ".!?":
        chosen += "."
    if len(chosen) > 400:
        trimmed = chosen[:397].rsplit(" ", 1)[0].rstrip(".,;:")
        chosen = trimmed + "."
    return _release_internal(text, chosen)


def plain_clarify_or_sign_in(text: str) -> str | None:
    """One plain question when a clarify or sign-in ask still carries codes.

    A clean sentence is left alone. A field marker that is already a
    question ("which of the 62 Renewal discussions") stays that question.
    """
    raw = str(text or "")
    if not _has_internal_detail(raw):
        return None
    logger.info(
        "outbound reply held internal detail: %s",
        " ".join(raw.split())[:2000],
    )
    if _SIGN_IN.search(raw):
        return SIGN_IN_QUESTION
    match = _MISSING_FIELD.search(raw)
    if not match:
        return None
    field = _INTERNAL_CODE.sub("", match.group(1))
    field = " ".join(field.split()).strip(" .:")
    if not field or _has_internal_detail(field):
        return None
    if _ASKS.match(field) or field.endswith("?"):
        asked = field[0].upper() + field[1:]
        asked = asked.rstrip("?").rstrip()
        return asked + "?"
    return None


def _has_internal_detail(text: str) -> bool:
    raw = str(text or "")
    return bool(
        _INTERNAL_CODE.search(raw) or _SELECTOR.search(raw) or _FIELD_NAME.search(raw)
    )


def _release_internal(original: str, shown: str) -> str:
    """Last step before Chat or email. Codes stay in the health log."""
    visible = str(shown or "").strip()
    if visible and not _has_internal_detail(visible):
        return visible
    source = str(original or "")
    if not _has_internal_detail(source) and not _has_internal_detail(visible):
        return visible
    logger.info(
        "outbound reply held internal detail: %s",
        " ".join(source.split())[:2000],
    )
    question = _plain_piece(source, question=True) or _plain_piece(visible, question=True)
    if question:
        return question
    sentence = _plain_piece(source, question=False) or _plain_piece(visible, question=False)
    if sentence:
        return sentence
    return STUCK_LINE


def _plain_piece(text: str, *, question: bool) -> str:
    raw = str(text or "").replace("\r\n", "\n")
    pieces: list[str] = []
    for line in raw.splitlines():
        for part in re.split(r"(?<=[.!?])\s+", line.strip()):
            cleaned = " ".join(part.split()).strip()
            if cleaned:
                pieces.append(cleaned)
    for piece in pieces:
        if _has_internal_detail(piece):
            continue
        is_question = piece.endswith("?")
        if question and is_question and len(piece) > 1:
            return piece
        if not question and not is_question and piece[-1:] in ".!":
            return piece
        if not question and not is_question and len(piece.split()) >= 2:
            if piece[-1] not in ".!?":
                piece += "."
            return piece
    return ""


def _clean_menu(raw: str) -> str:
    lines: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or _JOB_REF.search(stripped):
            continue
        cleaned = _UUID.sub("", stripped)
        cleaned = _BANNED.sub("", cleaned)
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
        if cleaned:
            lines.append(cleaned)
    return "\n".join(lines).strip()


def _strip_markers(text: str) -> str:
    """Drop job ids and verifier words. Keep the sentence around them."""
    cleaned = _JOB_REF.sub("", str(text or ""))
    cleaned = _UUID.sub("", cleaned)
    cleaned = _BANNED.sub("", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r" *\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip(" \n-:")


def _clean_line(line: str) -> str:
    stripped = _strip_markers(line)
    if not stripped:
        return ""
    if _LABEL_LINE.match(stripped):
        return ""
    return stripped.strip(" -:")
