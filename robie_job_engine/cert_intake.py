"""Certificates inbox intake: discovery, dedupe, fact extraction.

Read-only. This module never writes to EZLynx, never sends mail, never
creates tasks. It turns raw Gmail API payloads into structured
``CertEmail`` records, decides whether each record was already processed,
and extracts the request facts a human (or the filing stage) needs to
verify the client.

Pluggable boundaries (all duck-typed, all fakeable in tests):

- ``gmail_client``: ``list_message_ids(query, page_token)`` ->
  ``(ids, next_page_token)``; ``get_message(gmail_id)`` -> Gmail API
  ``users.messages.get`` ``format=full`` dict;
  ``get_attachment(gmail_id, attachment_id)`` -> ``bytes``.
- ``dedupe_store``: ``seen(key) -> bool`` / ``mark(key, meta)``.
- ``pdf_text_extractor``: ``(bytes) -> str | None``; ``None`` means the
  PDF could not be read as text and the item must be held for human
  review rather than guessed at.
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass
class CertAttachment:
    filename: str
    mime_type: str
    content: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    @property
    def size(self) -> int:
        return len(self.content)


@dataclass
class CertEmail:
    gmail_id: str
    thread_id: str
    rfc_message_id: str
    from_header: str
    subject: str
    date: str
    body_text: str
    attachments: list[CertAttachment] = field(default_factory=list)

    @classmethod
    def from_gmail_api(
        cls, payload: dict[str, Any], attachment_fetcher: Any = None
    ) -> "CertEmail":
        """Parse a ``users.messages.get`` ``format=full`` payload.

        ``attachment_fetcher(gmail_id, attachment_id) -> bytes`` is called
        for every part that carries an ``attachmentId``; parts without a
        fetcher are recorded with empty content and flagged by callers.
        """
        gmail_id = str(payload.get("id") or "")
        thread_id = str(payload.get("threadId") or "")
        headers = {
            h.get("name", "").lower(): h.get("value", "")
            for h in (payload.get("payload", {}).get("headers", []) or [])
            if isinstance(h, dict)
        }
        body_text, atts = _walk_parts(
            payload.get("payload", {}), gmail_id, attachment_fetcher
        )
        return cls(
            gmail_id=gmail_id,
            thread_id=thread_id,
            rfc_message_id=headers.get("message-id", "").strip(),
            from_header=headers.get("from", ""),
            subject=headers.get("subject", ""),
            date=headers.get("date", ""),
            body_text=body_text,
            attachments=atts,
        )


def _b64decode(data: str) -> bytes:
    data = (data or "").replace("-", "+").replace("_", "/")
    return base64.b64decode(data + "=" * (-len(data) % 4))


def _walk_parts(
    part: dict[str, Any], gmail_id: str, attachment_fetcher: Any
) -> tuple[str, list[CertAttachment]]:
    """Return (plain-text body, attachments) from a message payload part."""
    texts: list[str] = []
    atts: list[CertAttachment] = []
    mime = str(part.get("mimeType") or "")
    filename = str(part.get("filename") or "")
    body = part.get("body", {}) or {}
    if filename and body.get("attachmentId") and attachment_fetcher is not None:
        content = attachment_fetcher(gmail_id, body["attachmentId"]) or b""
        atts.append(
            CertAttachment(filename=filename, mime_type=mime, content=content)
        )
    elif mime == "text/plain" and body.get("data"):
        try:
            texts.append(_b64decode(body["data"]).decode("utf-8", "replace"))
        except Exception:
            pass
    for sub in part.get("parts", []) or []:
        t, a = _walk_parts(sub, gmail_id, attachment_fetcher)
        texts.append(t)
        atts.extend(a)
    return "\n".join(t for t in texts if t).strip(), atts


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_messages(
    gmail_client: Any, query: str, *, page_size: int = 50
) -> list[str]:
    """Return every message id matching ``query``, paging to the end.

    The unread flag is never used as the processing ledger: a person can
    mark a message read before automation sees it. Callers persist their
    own checkpoint (e.g. the newest internal date seen) separately.
    """
    ids: list[str] = []
    page_token: str | None = None
    while True:
        batch, page_token = gmail_client.list_message_ids(
            query, page_token, page_size=page_size
        )
        ids.extend(batch or [])
        if not page_token:
            break
    return ids


# ---------------------------------------------------------------------------
# Dedupe
# ---------------------------------------------------------------------------


class MemoryDedupeStore:
    """Process-local dedupe store. The server uses a durable implementation."""

    def __init__(self) -> None:
        self._seen: dict[str, dict[str, Any]] = {}

    def seen(self, key: str) -> bool:
        return key in self._seen

    def mark(self, key: str, meta: dict[str, Any] | None = None) -> None:
        self._seen[key] = meta or {}


def _normalize_body(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"\s+", " ", text)
    # Forwarded copies add headers; the request body stays identical.
    text = re.sub(r"(-{2,}forwarded message-{2,}|_{2,}forwarded message_{2,})", "", text)
    return text.strip()


def dedupe_keys(email: CertEmail) -> dict[str, Any]:
    """Stable identity keys for one intake email.

    - ``provider_id``: Gmail's immutable message id (one delivery).
    - ``rfc_id``: RFC Message-ID (same logical email across deliveries).
    - ``body_hash``: sha256 of normalized subject+body (catches forwards
      that mint a new provider id with identical content).
    - ``attachment_hashes``: sha256 per attachment, in order.
    """
    body_hash = hashlib.sha256(
        _normalize_body(email.subject + "\n" + email.body_text).encode("utf-8")
    ).hexdigest()
    return {
        "provider_id": f"gmail:{email.gmail_id}",
        "rfc_id": f"rfc:{email.rfc_message_id}" if email.rfc_message_id else None,
        "body_hash": f"body:{body_hash}",
        "attachment_hashes": [f"att:{a.sha256}" for a in email.attachments],
    }


def is_duplicate(email: CertEmail, store: Any) -> tuple[bool, str]:
    """True when this email was already processed, with the reason.

    Only identity keys gate: the Gmail message id, then the RFC
    Message-ID. Two distinct messages can legitimately share identical
    body bytes (thread replies quoting prior content, recurring
    auto-notifications) — skipping on body_hash drops real mail, which
    violates the one-flag-per-message rule. Body/attachment hashes are
    still marked for forensics but never suppress a distinct message.
    """
    keys = dedupe_keys(email)
    if store.seen(keys["provider_id"]):
        return True, "same Gmail message id already processed"
    if keys["rfc_id"] and store.seen(keys["rfc_id"]):
        return True, "same RFC Message-ID already processed"
    return False, ""


def mark_processed(
    email: CertEmail, store: Any, meta: dict[str, Any] | None = None
) -> None:
    """Record every dedupe key for a processed email."""
    keys = dedupe_keys(email)
    meta = meta or {}
    store.mark(keys["provider_id"], meta)
    if keys["rfc_id"]:
        store.mark(keys["rfc_id"], meta)
    store.mark(keys["body_hash"], meta)
    for h in keys["attachment_hashes"]:
        store.mark(h, meta)


# ---------------------------------------------------------------------------
# Fact extraction
# ---------------------------------------------------------------------------

# Certificate requests name the insured in a handful of shapes. Every
# pattern below is fail-soft: no match means None, never a guess.
_INSURED_PATTERNS = (
    re.compile(r"(?i)named insured\s*[:\-]\s*(.+?)(?:\n|$)"),
    re.compile(r"(?i)insured\s*[:\-]\s*(.+?)(?:\n|$)"),
    re.compile(r"(?i)certificate (?:of insurance )?for\s+(.+?)(?:\s+(?:pol#|policy\b)|\n|$)"),
    re.compile(r"(?i)loss runs?\s+for\s+(.+?)\s+(?:pol#|policy\b)"),
    re.compile(r"(?i)^(.+?)\s+(?:homeowners|dwelling fire|auto|general liability|gl|wc|workers comp)\s+policy\b"),
    re.compile(r"(?i)on behalf of\s+(.+?)(?:,|\n|$)"),
)

_POLICY_RE = re.compile(
    r"(?i)pol(?:icy)?(?:\s*(?:no|number|#))?\s*[:#]?\s*"
    r"(?!number\b|no\b)(?=[A-Z0-9\-\./]*\d)"
    r"([A-Z0-9][A-Z0-9\-\./]{4,30})(?![A-Z0-9\-\./])"
)

_HOLDER_RE = re.compile(r"(?i)certificate holder\s*[:\-]\s*(.+?)(?:\n|$)")
_DBA_RE = re.compile(r"(?i)\bdba\b\s+(.+?)(?:,|\n|$)")
_REQUESTER_RE = re.compile(r"(?i)(?:requested by|requester)\s*[:\-]\s*(.+?)(?:\n|$)")

# ---------------------------------------------------------------------------
# Subject-line insured extraction (moved here from cert_verification so the
# intake extractor and the verifier share one implementation — the intake
# used to miss names the verifier later found, e.g. "Renewal Certificate
# Request- Abg Transportation MC1121844").
# ---------------------------------------------------------------------------

_SUBJECT_PREFIXES = re.compile(r"^(?:\s*(?:re|fwd?)\s*:\s*)+", re.IGNORECASE)
_MC_SUFFIX = re.compile(r"\s+(?:MC|DOT|USDOT)\s*\d+\s*$", re.IGNORECASE)
_POLICY_TAIL = re.compile(r"\s+[A-Z0-9][A-Z0-9/\-]{3,}\s*$")

_SUBJECT_PATTERNS = [
    # "Certificate of Insurance for Homegrown Moving Company"
    re.compile(r"certificate of insurance for\s+(.+?)(?:\s+to\s+|\s*$)", re.IGNORECASE),
    # "Request for COI for Ameritesting LLC Covering SilverLini"
    re.compile(r"request for coi for\s+(.+?)(?:\s+covering\s+|\s*$)", re.IGNORECASE),
    # "Certificate of Insurance LA Burger LLC to Anderson Marke"
    re.compile(r"certificate of insurance\s+(.+?)\s+to\s+", re.IGNORECASE),
    # "Renewal Certificate Request- Abg Transportation MC112184"
    re.compile(r"certificate request\s*[-:]\s*(.+?)\s*$", re.IGNORECASE),
    # "COI - Fonseca General Contractor LLC"
    re.compile(r"\bcoi\s*[-:]\s*(.+?)\s*$", re.IGNORECASE),
]

_NAME_LIKE = re.compile(r"[A-Za-z]{2,}")


def _strip_subject_prefixes(subject: str) -> str:
    return _SUBJECT_PREFIXES.sub("", subject or "").strip()


def extract_subject_insured(subject: str) -> str | None:
    """Best-effort insured name from the email subject line.

    Returns None when nothing name-like is found — the caller holds
    instead of guessing.
    """
    clean = _strip_subject_prefixes(subject)
    for pat in _SUBJECT_PATTERNS:
        m = pat.search(clean)
        if m:
            name = _MC_SUFFIX.sub("", m.group(1)).strip(" -:,")
            if _NAME_LIKE.search(name):
                return name
    # "Haris Uddin 008265/15/00": leading name before a policy-like tail.
    m = _POLICY_TAIL.search(clean)
    if m:
        name = _MC_SUFFIX.sub("", clean[: m.start()]).strip(" -:,")
        if _NAME_LIKE.search(name) and len(name.split()) <= 6:
            return name
    return None


# ---------------------------------------------------------------------------
# Sentence-runoff truncation: body-text captures sometimes glue a trailing
# sentence onto the name ("LA Burger LLC. This certificate confirms that
# the listed insurance"). Truncate at the first period+space that ends a
# complete-looking name, while keeping legitimate internal periods
# ("St. Mary Hospital", "J. P. Morgan", "MAR Engineering, P.C.").
# ---------------------------------------------------------------------------

_ENTITY_SUFFIXES = (
    "llc", "inc", "corp", "ltd", "co", "company", "pllc",
    "pa", "pc", "lp", "llp", "jr", "sr", "ii", "iii", "iv",
)

# Words that end with a period but do not end a name: street abbreviations,
# titles, etc. ("Main St. Pizza Shop" must not truncate to "Main St").
_ABBREVIATIONS = {
    "st", "ave", "avenue", "blvd", "rd", "road", "dr", "drive", "ln",
    "lane", "ct", "court", "cir", "circle", "pl", "place", "pkwy",
    "parkway", "mt", "ft", "ste", "suite", "apt", "bldg", "dept",
    "mr", "mrs", "ms", "prof", "rev", "hon", "esq", "vs",
}


def _truncate_sentence_runoff(name: str) -> str:
    text = name or ""
    start = 0
    while True:
        idx = text.find(". ", start)
        if idx == -1:
            return text
        pre, post = text[:idx], text[idx + 2:]
        pre_words = pre.split()
        post_words = post.split()
        if len(pre_words) >= 2 and len(pre_words[-1]) > 1:
            last = pre_words[-1].rstrip(".").lower()
            if last in _ENTITY_SUFFIXES:
                return pre
            if last not in _ABBREVIATIONS and len(post_words) >= 3:
                return pre
        start = idx + 1


def _clean_name(value: str) -> str:
    value = re.sub(r"\s+", " ", (value or "").strip())
    value = _truncate_sentence_runoff(value)
    # A trailing period is sentence punctuation, not part of the name —
    # unless the name ends in an abbreviation/entity suffix ("P.C.").
    if value.endswith("."):
        last = value[:-1].split()[-1].rstrip(".").lower() if value[:-1].split() else ""
        if last not in _ABBREVIATIONS and last not in _ENTITY_SUFFIXES:
            value = value[:-1]
    return value[:120]


@dataclass
class RequestFacts:
    """Everything the verifier needs. Every field may be None: the
    verifier holds the item instead of guessing."""

    insured_name: str | None = None
    dba: str | None = None
    policy_numbers: list[str] = field(default_factory=list)
    requester_name: str | None = None
    requester_email: str | None = None
    holder_names: list[str] = field(default_factory=list)
    requested_action: str | None = None
    pdf_texts: list[str] = field(default_factory=list)
    pdf_unreadable: bool = False
    raw_subject: str = ""
    raw_from: str = ""

    @property
    def requester_is_third_party(self) -> bool:
        """True when the sender is clearly not the insured.

        A certificate holder, GC, town, or vendor asking *about* the
        insured is a third party. Someone writing from the insured's own
        domain (taylor@lawnbuddies.example for Lawn Buddies LLC) is not.
        Unknown stays False — the verifier holds unknowns, it does not
        route on them.
        """
        if not self.insured_name or not self.requester_name:
            return False
        a = re.sub(r"[^a-z0-9]", "", self.insured_name.lower())
        b = re.sub(r"[^a-z0-9]", "", self.requester_name.lower())
        if not a or not b:
            return False
        if a == b or b in a or a in b:
            return False
        domain = ""
        if self.requester_email and "@" in self.requester_email:
            host = self.requester_email.split("@", 1)[1]
            domain = re.sub(r"[^a-z0-9]", "", host.split(".")[0])
        insured_core = a
        for suffix in ("llc", "inc", "corp", "ltd", "co", "company", "pllc"):
            if insured_core.endswith(suffix) and len(insured_core) > len(suffix):
                insured_core = insured_core[: -len(suffix)]
                break
        if domain and insured_core and (
            domain in insured_core or insured_core in domain
        ):
            return False
        return True


def _from_name_email(from_header: str) -> tuple[str | None, str | None]:
    m = re.match(r'\s*(?:"?([^"<]+)"?\s*)?<([^>]+)>', from_header or "")
    if m:
        return _clean_name(m.group(1)) or None, m.group(2).strip().lower() or None
    m = re.match(r"\s*([^@\s]+@[^@\s]+)\s*", from_header or "")
    if m:
        return None, m.group(1).lower()
    name = _clean_name(from_header)
    return (name or None), None


def extract_request_facts(
    email: CertEmail, pdf_text_extractor: Any = None
) -> RequestFacts:
    """Parse the email (and its PDFs) into verifiable facts.

    ``pdf_text_extractor(bytes) -> str | None``; when a PDF cannot be
    read as text, ``pdf_unreadable`` is set and the item must be held
    for human review — the insured on the PDF is exactly what proves
    the request belongs to our client.
    """
    facts = RequestFacts(raw_subject=email.subject, raw_from=email.from_header)
    text = f"{email.subject}\n{email.body_text}"

    for pat in _INSURED_PATTERNS:
        m = pat.search(text)
        if m and _clean_name(m.group(1)):
            facts.insured_name = _clean_name(m.group(1))
            break
    if not facts.insured_name:
        # Subject lines often carry the cleanest name
        # ("Renewal Certificate Request- Abg Transportation MC1121844").
        # Shares the verifier's subject parser so intake and verify agree.
        subj_name = extract_subject_insured(email.subject)
        if subj_name and _clean_name(subj_name):
            facts.insured_name = _clean_name(subj_name)
    dba_m = _DBA_RE.search(text)
    if dba_m:
        facts.dba = _clean_name(dba_m.group(1))
    seen: set[str] = set()
    for m in _POLICY_RE.finditer(text):
        num = m.group(1).strip().upper()
        if num not in seen:
            seen.add(num)
            facts.policy_numbers.append(num)
    facts.holder_names = [
        _clean_name(m.group(1))
        for m in _HOLDER_RE.finditer(text)
        if _clean_name(m.group(1))
    ]
    req_m = _REQUESTER_RE.search(text)
    if req_m:
        facts.requester_name = _clean_name(req_m.group(1))
    from_name, from_email = _from_name_email(email.from_header)
    facts.requester_email = from_email
    if not facts.requester_name:
        facts.requester_name = from_name
    if re.search(r"(?i)\bcertificate\b", text):
        facts.requested_action = "certificate_request"
    elif re.search(r"(?i)\bloss runs?\b", text):
        facts.requested_action = "loss_runs_request"

    if pdf_text_extractor is not None:
        for att in email.attachments:
            if att.filename.lower().endswith(".pdf"):
                try:
                    pdf_text = pdf_text_extractor(att.content)
                except Exception:
                    pdf_text = None
                if pdf_text:
                    facts.pdf_texts.append(pdf_text)
                    # PDFs often carry the cleanest insured/policy lines.
                    if not facts.insured_name:
                        for pat in _INSURED_PATTERNS:
                            m = pat.search(pdf_text)
                            if m and _clean_name(m.group(1)):
                                facts.insured_name = _clean_name(m.group(1))
                                break
                    for m in _POLICY_RE.finditer(pdf_text):
                        num = m.group(1).strip().upper()
                        if num not in seen:
                            seen.add(num)
                            facts.policy_numbers.append(num)
                else:
                    facts.pdf_unreadable = True
    return facts


def _mask_policy_for_note(policy: str) -> str:
    """Mask a policy number for EZLynx note text.

    Digit runs of 3+ look phone-like to the agency's call automation,
    which dials numbers it finds in discussion text. The full number is
    used for matching and lives in the filed email document; the note
    keeps the alpha prefix and last two digits for human reference.
    """
    def _mask(m: "re.Match[str]") -> str:
        digits = m.group(0)
        return "\u2022" * (len(digits) - 2) + digits[-2:]

    return re.sub(r"\d{3,}", _mask, policy or "")


def render_email_source(email: CertEmail) -> str:
    """Render the email as plain text for filing to the client's file.

    This is the durable "email saved to the account" artifact: headers,
    body, and an attachment manifest (names + sha256, never the bytes
    twice).
    """
    lines = [
        f"From: {email.from_header}",
        f"Date: {email.date}",
        f"Subject: {email.subject}",
        f"Gmail-ID: {email.gmail_id}",
        f"Message-ID: {email.rfc_message_id}",
        "",
        email.body_text or "(no text body)",
        "",
        "Attachments:",
    ]
    if email.attachments:
        for att in email.attachments:
            lines.append(f"- {att.filename} ({att.size} bytes, sha256 {att.sha256})")
    else:
        lines.append("(none)")
    return "\n".join(lines).strip() + "\n"


def summarize_for_note(
    email: CertEmail, facts: RequestFacts, filed_documents: list[str]
) -> str:
    """Draft the plain-English EZLynx note. Concise, nontechnical.

    Never a placeholder: when facts are missing the note says what is
    missing so the human reviewer sees it. Policy digits are masked so
    the note can never trip the call automation's phone-number guard.
    """
    insured = facts.insured_name or "insured not identified in email"
    policy = (
        _mask_policy_for_note(facts.policy_numbers[0])
        if facts.policy_numbers
        else "policy number not shown"
    )
    requester = facts.requester_name or facts.requester_email or "sender not identified"
    docs = (
        f"{len(filed_documents)} attachment(s) saved to the file"
        if filed_documents
        else "no attachments filed"
    )
    third = ""
    if facts.requester_is_third_party:
        third = f" Request came from a third party ({requester}), not the insured."
    missing = ""
    if facts.pdf_unreadable:
        missing = " A PDF attachment could not be read as text — human to verify its contents."
    return (
        f"Certificate request {email.date or 'date not shown'}: {requester} "
        f"asked for a certificate for {insured} (policy {policy}). "
        f"{docs}.{third} Steffany to review and issue.{missing}"
    ).strip()
