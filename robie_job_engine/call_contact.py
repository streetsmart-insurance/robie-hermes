"""Did a Robie Call reach a live person? (pure; no I/O)

Bland reports a call as "completed" whenever the line was answered, even
when only a recording, a phone menu, a hold message, a call screener or a
voicemail greeting picked up. ``answered_by`` is often "unknown" or empty,
for real conversations too, so the transcript decides.

Call d25f46d6 (task 63558413, Oct 8 2026) is the reference case: Bland said
completed, answered_by unknown, 40 seconds. The other side was "Please hold
while I try to connect you" and then a recorded life-insurance pitch ending
"Please continue to stay on the line. We will be with you shortly." Nobody
was reached, yet the call was counted as successful.

Contacts:
  person                  a live person spoke (successful)
  voicemail_message_left  voicemail, and Robie left the message (successful)
  voicemail_no_message    voicemail greeting, no message left
  recording               recording, robocall, phone menu, hold message or
                          call screener; no live person
  miss                    no answer, busy, failed, canceled, or zero length
  unconfirmed             the line connected but nothing shows a person
                          was reached (fail closed: not successful)
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

PERSON = "person"
VOICEMAIL_MESSAGE_LEFT = "voicemail_message_left"
VOICEMAIL_NO_MESSAGE = "voicemail_no_message"
RECORDING = "recording"
MISS = "miss"
UNCONFIRMED = "unconfirmed"

SUCCESSFUL_CONTACTS = frozenset({PERSON, VOICEMAIL_MESSAGE_LEFT})
# Best first. With two attempts the better one decides the outcome.
CONTACT_RANK = (PERSON, VOICEMAIL_MESSAGE_LEFT, UNCONFIRMED, RECORDING,
                VOICEMAIL_NO_MESSAGE, MISS)

_MISS_STATUSES = frozenset({
    "no-answer", "no_answer", "noanswer", "no answer", "busy", "user_busy",
    "failed", "canceled", "cancelled", "rejected",
})
_USER_ROLES = frozenset({"user", "human", "callee", "customer"})
_ASSISTANT_ROLES = frozenset({"assistant", "agent", "bot", "ai", "robie", "eva"})

# What a machine on the other end says. Kinds name what answered.
_MARKERS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = tuple(
    (kind, re.compile(pattern, re.IGNORECASE)) for kind, pattern in (
        ("voicemail_greeting", r"\bleave (?:a|your) (?:brief |short )?(?:message|name)"),
        ("voicemail_greeting", r"\b(?:after|at) the (?:tone|beep)\b"),
        ("voicemail_greeting", r"\bvoice\s*-?\s*mail\b|\bmailbox\b"),
        ("voicemail_greeting", r"\b(?:is )?not available to take (?:your|the) call\b"),
        ("voicemail_greeting", r"\brecord your message\b"),
        ("voicemail_greeting", r"\byou(?:'ve| have) reached\b"),
        ("phone_menu", r"\bpress (?:\d|one|two|three|four|five|six|seven|eight|nine|zero|pound|star)\b"),
        ("phone_menu", r"\bpara (?:espa[nñ]ol|continuar)\b|\bfor (?:english|spanish)\b"),
        ("phone_menu", r"\bmain menu\b|\bmenu options\b|\bplease listen carefully\b"),
        ("phone_menu", r"\b(?:please )?enter (?:your|the)\b|\bdial by name\b"),
        ("phone_menu", r"\bif you know your party'?s extension\b"),
        ("phone_menu", r"\bthank you for calling\b"),
        ("phone_menu", r"\byour call (?:may|will) be (?:monitored|recorded)\b"),
        ("phone_menu", r"\b(?:office|business) hours\b|\bwe are (?:currently )?closed\b"),
        ("hold_message", r"\bplease hold\b|\bwhile i (?:try to )?connect you\b"),
        ("hold_message", r"\bstay on the line\b|\bremain on the line\b"),
        ("hold_message", r"\b(?:will|'ll) be with you shortly\b"),
        ("hold_message", r"\byour call is important\b|\bnext available\b"),
        ("hold_message", r"\bdo not hang up\b"),
        ("recording", r"\bthe number you (?:have )?(?:dialed|called|reached)\b"),
        ("recording", r"\bnot in service\b|\bhas been disconnected\b"),
        ("recording", r"\bcall cannot be completed\b"),
        ("recording", r"\bthis is an automated\b|\bautomated (?:message|call|system)\b"),
        ("recording", r"\bpre-?recorded\b|\brecorded message\b"),
        ("recording", r"\blife insurance is\b|\bextended warranty\b"),
        ("recording", r"\byou(?:'ve| have) been (?:selected|pre-?approved|chosen)\b"),
        ("recording", r"\bto be removed from (?:our|this) (?:list|call)"),
        ("call_screener", r"\bscreening (?:service|tool|assistant)\b|\busing a screening\b"),
        ("call_screener", r"\bgoogle (?:assistant|call screen)\b"),
        ("call_screener", r"\bstate your name and (?:the )?reason\b"),
        ("call_screener", r"\b(?:name|purpose) (?:and )?(?:the )?(?:reason )?(?:of|for) (?:your|the|this) call\b"),
    )
)

# A short reply only a live person gives. Wins over a marker in the same turn.
_LIVE_TURN_RE = re.compile(
    r"(?i)\bhow (?:can|may) i help\b|\bspeaking\b|\bwho is this\b|\bwho'?s this\b"
    r"|^\s*(?:hello|hi|hey|yes|yeah|yep|sure|okay|ok|no|nope|uh-?huh|correct)\s*[?.!,]*\s*$"
)

# Bland's own summary, used only when there is no transcript.
_SUMMARY_MACHINE_RE = re.compile(
    r"(?i)\bautomated\b|\brecord(?:ed|ing)\b|\bvoice\s*-?\s*mail\b|\bphone menu\b"
    r"|\bIVR\b|\bhold message\b|\bon hold\b|\brobocall\b|\bno one answered\b"
    r"|\bdid not (?:reach|speak)\b"
)
_SUMMARY_VOICEMAIL_RE = re.compile(r"(?i)\bvoice\s*-?\s*mail\b|\bmailbox\b")

PLAIN_KIND = {
    "voicemail_greeting": "a voicemail greeting",
    "phone_menu": "an automated phone menu",
    "hold_message": "a hold message",
    "recording": "a recorded message",
    "call_screener": "an automated call screener",
}


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def transcript_turns(detail: Mapping[str, Any]) -> List[Tuple[str, str]]:
    """(role, text) turns from any Bland transcript shape.

    Accepts ``transcripts`` / ``transcript`` as a list of dicts
    ({"user": "user"|"assistant"|..., "text": ...}) or of "role: text"
    strings, or ``concatenated_transcript`` / ``transcript`` as one string
    with "role: text" lines. A line with no role keeps role "".
    """
    raw: Any = None
    for key in ("transcripts", "transcript", "concatenated_transcript"):
        value = detail.get(key)
        if value:
            raw = value
            break
    turns: List[Tuple[str, str]] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, Mapping):
                role = _norm(item.get("user") or item.get("role") or item.get("speaker"))
                text = str(item.get("text") or item.get("content") or "").strip()
                if text:
                    turns.append((role, text))
            elif isinstance(item, str):
                turns.extend(_split_lines(item))
    elif isinstance(raw, str):
        turns.extend(_split_lines(raw))
    return turns


_ROLE_PREFIX_RE = re.compile(r"^\s*([A-Za-z][A-Za-z -]{0,20}):\s*(.*)$", re.S)


def _split_lines(text: str) -> List[Tuple[str, str]]:
    turns: List[Tuple[str, str]] = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        match = _ROLE_PREFIX_RE.match(line)
        if match and (_norm(match.group(1)) in _USER_ROLES | _ASSISTANT_ROLES
                      or _norm(match.group(1)).startswith("agent")):
            turns.append((_norm(match.group(1)), match.group(2).strip()))
        elif turns and turns[-1][0]:
            role, prev = turns[-1]
            turns[-1] = (role, f"{prev} {line}")
        else:
            turns.append(("", line))
    return turns


def _other_side(turns: List[Tuple[str, str]]) -> List[str]:
    """What the called party said. Assistant turns and tool actions are not it."""
    out = []
    for role, text in turns:
        if role in _ASSISTANT_ROLES or role.startswith("agent"):
            continue
        if role and role not in _USER_ROLES:
            continue
        if text.strip():
            out.append(text.strip())
    return out


def machine_kind(text: str) -> Optional[str]:
    """The machine kind a turn sounds like, or None for a live reply."""
    if _LIVE_TURN_RE.search(text or ""):
        return None
    for kind, pattern in _MARKERS:
        if pattern.search(text or ""):
            return kind
    return None


def _duration(detail: Mapping[str, Any]) -> float:
    for key in ("duration_s", "duration", "call_length", "call_duration"):
        value = detail.get(key)
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def classify_contact(detail: Mapping[str, Any], *,
                     message_left: bool = False) -> Tuple[str, str]:
    """(contact, plain reason) for one terminal call.

    ``message_left`` is True when this call was the attempt that leaves the
    voicemail (Jake's second dial) or the caller already knows a message
    was left.
    """
    detail = detail or {}
    status = _norm(detail.get("status"))
    answered = _norm(detail.get("answered_by"))
    if status in _MISS_STATUSES or answered in _MISS_STATUSES:
        return MISS, f"Bland reported {status or answered}"
    if status and status != "completed":
        return MISS, f"Bland reported {status}"
    if _norm(detail.get("voicemail_action")) == "leave_message":
        message_left = True
    turns = transcript_turns(detail)
    said = _other_side(turns)
    kinds = [machine_kind(t) for t in said]
    live = [t for t, k in zip(said, kinds) if k is None]
    machine = [k for k in kinds if k]

    def _voicemail() -> Tuple[str, str]:
        if message_left:
            return VOICEMAIL_MESSAGE_LEFT, "voicemail; Robie left the message"
        return VOICEMAIL_NO_MESSAGE, "voicemail greeting; no message left"

    if answered == "voicemail":
        return _voicemail()
    if said and not live:
        # Only machines spoke: recording, menu, hold, screener or greeting.
        if "voicemail_greeting" in machine:
            return _voicemail()
        return RECORDING, f"only {PLAIN_KIND.get(machine[0], 'a recording')} answered"
    if live:
        return PERSON, "a live person spoke"
    if _duration(detail) <= 0:
        return MISS, "the call had no length"
    if answered == "human":
        # No transcript to check; Bland's own word.
        return PERSON, "Bland reported a person answered"
    summary = str(detail.get("summary") or "")
    if summary and _SUMMARY_MACHINE_RE.search(summary):
        if _SUMMARY_VOICEMAIL_RE.search(summary):
            return _voicemail()
        return RECORDING, "Bland's summary describes an automated answer"
    return UNCONFIRMED, "nothing shows a person was reached"


def best_contact(contacts: List[str]) -> str:
    """The best contact across attempts (person beats voicemail, and so on)."""
    for contact in CONTACT_RANK:
        if contact in contacts:
            return contact
    return MISS


def recording_kind(detail: Mapping[str, Any]) -> str:
    """Plain words for what answered a RECORDING call."""
    for text in _other_side(transcript_turns(detail or {})):
        kind = machine_kind(text)
        if kind and kind != "voicemail_greeting":
            return PLAIN_KIND[kind]
    return "a recording or automated phone system"
