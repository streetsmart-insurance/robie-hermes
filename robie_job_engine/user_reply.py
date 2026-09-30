"""One formatter for text a person sees in Chat or email.

Job ids, audit notes, and verifier words stay in logs and the health
channel. This function never adds them back.
"""

from __future__ import annotations

import re

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


def format_user_reply(text: str, *, collapse: bool = True) -> str:
    """One outbound formatter. Their scrubber runs inside this function.

    Fixture policy numbers, worker markers, and missing-field prompts are
    rewritten first. Multi-line status reports then collapse to the first
    plain line. A help menu is kept so the staff line is not dropped.
    Transport that already formatted the text passes collapse=False so a
    chunk is not rewritten a second time.
    """
    from .answer_only import scrub_user_reply

    raw = scrub_user_reply(text).replace("\r\n", "\n").strip()
    if not raw or not collapse:
        return raw
    if raw.startswith(_MENU_START) or "I can't make these yet" in raw:
        return _clean_menu(raw)
    kept: list[str] = []
    for line in raw.splitlines():
        cleaned = _clean_line(line)
        if cleaned:
            kept.append(cleaned)
    if not kept:
        return "I couldn't finish that."
    questions = [line for line in kept if line.endswith("?")]
    statements = [line for line in kept if not line.endswith("?")]
    chosen = statements[0] if statements else questions[0]
    if chosen[-1] not in ".!?":
        chosen += "."
    if len(chosen) > 400:
        trimmed = chosen[:397].rsplit(" ", 1)[0].rstrip(".,;:")
        chosen = trimmed + "."
    return chosen


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


def _clean_line(line: str) -> str:
    stripped = line.strip()
    if not stripped:
        return ""
    if _JOB_REF.search(stripped):
        return ""
    if _BANNED.search(stripped):
        return ""
    if _LABEL_LINE.match(stripped):
        return ""
    cleaned = _UUID.sub("", stripped)
    cleaned = _BANNED.sub("", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip(" -:")
    return cleaned
