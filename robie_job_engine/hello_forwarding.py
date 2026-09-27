"""Forward-envelope unwrapping + dedupe for the hello@ intake worker.

53% of hello@ volume arrives as forwards — mostly Carlo bare-forwarding
carrier/wholesaler mail into hello@ with zero instructions. Two problems
follow:

1. The classifier sees the FORWARD (Carlo's address, "Fwd:" subject) and
   never the original carrier notice. The worker must unwrap to the
   original sender/subject/body before classifying.
2. Carriers routinely CC both carlo@ and hello@; Carlo then forwards his
   copy into hello@. Without dedupe the same notice is worked twice.

Unwrap first, then classify the inner content, then dedupe on the
(original sender, normalized subject, date) key before anything is
queued or filed.

Read-only: pure text handling. No EZLynx, no Zapier, no email.
"""

from __future__ import annotations

import hashlib
import re
from email.utils import parsedate_to_datetime

# ---------------------------------------------------------------------------
# Forward detection
# ---------------------------------------------------------------------------

# Gmail's forward block and Outlook's "Original Message" block.
_FORWARD_MARKERS = (
    "---------- Forwarded message ---------",
    "-----Original Message-----",
    "Begin forwarded message:",
)

_SUBJECT_FORWARD_RE = re.compile(r"(?i)^\s*(fwd?|fw)\s*:")

# Header lines inside a forwarded block: "From: ...", "Date: ...",
# "Subject: ...", "To: ...", "Sent: ..." (Outlook style).
_FORWARDED_HEADER_RE = re.compile(
    r"(?im)^\s*(from|date|subject|to|sent|cc)\s*:\s*(.+?)\s*$")


def is_forward_subject(subject: str | None) -> bool:
    """True when the subject line is a forward ("Fwd: ...")."""
    return bool(_SUBJECT_FORWARD_RE.match(subject or ""))


def has_forward_block(body: str | None) -> bool:
    """True when the body carries a forwarded-message header block."""
    text = body or ""
    return any(marker.lower() in text.lower() for marker in _FORWARD_MARKERS)


def detect_forward(subject: str | None, body: str | None) -> bool:
    """True when the message is (or contains) a forward envelope."""
    return is_forward_subject(subject) or has_forward_block(body)


# ---------------------------------------------------------------------------
# Unwrapping
# ---------------------------------------------------------------------------

def _split_forward_block(body: str) -> tuple[str, str] | None:
    """Split body into (header_block, original_body) at a forward marker.

    Returns None when no marker is present.
    """
    text = body or ""
    lowered = text.lower()
    for marker in _FORWARD_MARKERS:
        idx = lowered.find(marker.lower())
        if idx >= 0:
            rest = text[idx + len(marker):]
            return marker, rest
    return None


def _parse_forward_headers(header_region: str) -> dict:
    """Pull From/Date/Subject/To out of a forwarded header region."""
    found: dict[str, str] = {}
    for match in _FORWARDED_HEADER_RE.finditer(header_region or ""):
        key = match.group(1).strip().lower()
        value = " ".join(match.group(2).split())
        # First occurrence wins; forwarded blocks list each header once.
        found.setdefault(key, value)
    return found


def _email_of(header_value: str | None) -> str | None:
    if not header_value:
        return None
    m = re.search(r"<([^<>@\s]+@[^<>@\s]+)>", header_value)
    if m:
        return m.group(1).strip().lower()
    m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", header_value)
    return m.group(0).strip().lower() if m else None


def _strip_forward_prefix(subject: str | None) -> str:
    return _SUBJECT_FORWARD_RE.sub("", subject or "").strip()


def unwrap_forward(subject: str | None, body: str | None) -> dict | None:
    """Unwrap one forward envelope.

    Returns None when the message is not a forward. Otherwise returns
    {"original_sender", "original_sender_name", "original_subject",
    "original_date", "original_body", "has_nested_forward"} — the content
    the worker should classify, match, and file. Missing pieces stay
    None; the caller fails closed on them, never guesses.
    """
    if not detect_forward(subject, body):
        return None
    split = _split_forward_block(body or "")
    if split is None:
        # Forward-shaped subject but no parseable block: the inner
        # content is just the body with the Fwd: prefix stripped off the
        # subject. Sender stays unknown (never guessed).
        return {
            "original_sender": None,
            "original_sender_name": None,
            "original_subject": _strip_forward_prefix(subject),
            "original_date": None,
            "original_body": (body or "").strip() or None,
            "has_nested_forward": False,
        }
    _, rest = split
    # The header block runs until the first blank line; everything after
    # is the original body. Strip leading blank lines first — the marker
    # line itself is usually followed by one.
    lines = rest.lstrip("\n").splitlines()
    header_lines: list[str] = []
    body_lines: list[str] = []
    in_headers = True
    for line in lines:
        if in_headers and not line.strip():
            in_headers = False
            continue
        (header_lines if in_headers else body_lines).append(line)
    headers = _parse_forward_headers("\n".join(header_lines))
    from_header = headers.get("from")
    original_body = "\n".join(body_lines).strip() or None
    return {
        "original_sender": _email_of(from_header),
        "original_sender_name": (
            re.sub(r"\s*<[^<>]*>\s*", "", from_header or "").strip()
            or None),
        "original_subject": (
            headers.get("subject") or _strip_forward_prefix(subject) or None),
        "original_date": headers.get("date") or headers.get("sent"),
        "original_body": original_body,
        "has_nested_forward": detect_forward(None, original_body),
    }


# ---------------------------------------------------------------------------
# Dedupe
# ---------------------------------------------------------------------------

def normalize_subject(subject: str | None) -> str:
    """Canonical subject: strip Re:/Fwd: prefixes, lowercase, collapse space."""
    text = (subject or "").strip()
    while True:
        stripped = re.sub(r"(?i)^\s*(re|fwd?|fw)\s*:\s*", "", text).strip()
        if stripped == text:
            break
        text = stripped
    return " ".join(text.lower().split())


def _date_day(value: str | None) -> str:
    """Calendar day (YYYY-MM-DD) for a date header, or "" when unparseable."""
    if not value:
        return ""
    try:
        return parsedate_to_datetime(value).date().isoformat()
    except Exception:  # noqa: BLE001 - unparseable date is not a dedupe signal
        return ""


def content_hash(text: str | None) -> str:
    """Stable hash of normalized body text (whitespace-collapsed)."""
    normalized = " ".join((text or "").split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def dedupe_key(message: dict) -> tuple:
    """Dedupe key for one message dict.

    The message is unwrapped first, so a carrier notice that arrived
    directly AND via Carlo's forward collapses to one key:
    (original sender email, normalized original subject, calendar day).
    When the sender is unknown (bare Fwd: with no parseable block) the
    body hash joins the key so two different forwards never collapse.
    message_id, when present, is checked for exact equality separately
    in dedupe_messages.
    """
    sender = str(message.get("sender") or "")
    subject = message.get("subject")
    body = message.get("body")
    date = message.get("date")
    unwrapped = unwrap_forward(subject, body)
    if unwrapped:
        inner_sender = unwrapped.get("original_sender") or sender
        inner_subject = (unwrapped.get("original_subject")
                         or _strip_forward_prefix(subject))
        inner_date = unwrapped.get("original_date") or date
        inner_body = unwrapped.get("original_body") or body
    else:
        inner_sender, inner_subject, inner_date, inner_body = (
            sender, subject, date, body)
    key = (
        (inner_sender or "").strip().lower(),
        normalize_subject(inner_subject),
        _date_day(inner_date),
    )
    if not key[0]:
        # Unknown sender: only collapse on identical body content.
        return key + (content_hash(inner_body),)
    return key


def dedupe_messages(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """Collapse duplicate hello@ messages.

    messages: [{message_id, sender, subject, date, body, ...}].
    Returns (kept, collapsed): the first occurrence of each dedupe key
    is kept; later duplicates (or exact message_id repeats) are
    returned in `collapsed` with a "duplicate_of" annotation. Order is
    preserved; nothing is deleted anywhere — the caller decides what to
    do with collapsed entries (skip quietly, they are the same notice).
    """
    kept: list[dict] = []
    collapsed: list[dict] = []
    seen_ids: set[str] = set()
    seen_keys: dict[tuple, str] = {}
    for msg in messages or []:
        mid = str(msg.get("message_id") or "")
        if mid and mid in seen_ids:
            dup = dict(msg)
            dup["duplicate_of"] = mid
            dup["dedupe_reason"] = "same message_id seen twice"
            collapsed.append(dup)
            continue
        key = dedupe_key(msg)
        if key in seen_keys:
            dup = dict(msg)
            dup["duplicate_of"] = seen_keys[key]
            dup["dedupe_reason"] = (
                "same original sender + subject + date as an earlier message")
            collapsed.append(dup)
            continue
        if mid:
            seen_ids.add(mid)
        seen_keys[key] = mid or f"key:{len(seen_keys)}"
        kept.append(msg)
    return kept, collapsed


# ---------------------------------------------------------------------------
# One-call worker entry: envelope + classification of the inner content
# ---------------------------------------------------------------------------

def classify_with_envelope(subject: str | None, body: str | None,
                           sender: str | None) -> dict:
    """Detect the envelope, unwrap forwards, classify the inner content.

    Returns {"envelope": "internal_forward" | "internal_discussion" |
    "external_forward" | "external_direct", "forwarder": sender|None,
    "original": unwrap dict|None, "action", "request_type", "reason"}.
    The classification always runs against the ORIGINAL sender/subject/
    body when the message is a forward — the forwarder's address never
    decides the request type.
    """
    # Local import: the classifier must not import this module (cycle).
    from .hello_classifier import _sender_is_internal, classify_hello

    forwarded = detect_forward(subject, body)
    internal_forwarder = _sender_is_internal(sender)
    if forwarded:
        unwrapped = unwrap_forward(subject, body)
        inner_sender = (unwrapped or {}).get("original_sender") or sender
        inner_subject = ((unwrapped or {}).get("original_subject")
                         or _strip_forward_prefix(subject))
        inner_body = ((unwrapped or {}).get("original_body") or body)
        action, request_type, reason = classify_hello(
            inner_subject, inner_body, inner_sender)
        envelope = ("internal_forward" if internal_forwarder
                    else "external_forward")
    else:
        unwrapped = None
        action, request_type, reason = classify_hello(subject, body, sender)
        envelope = ("internal_discussion" if internal_forwarder
                    else "external_direct")
    return {
        "envelope": envelope,
        "forwarder": sender if forwarded else None,
        "original": unwrapped,
        "action": action,
        "request_type": request_type,
        "reason": reason,
    }
