"""Decide which robie@ emails become jobs.

Carlo 2026-10-08: "ignore reactions, reports and FYI forwards, and only
send 'did not finish' for real requests".

Since the Playground drop-in came off the email watcher (Oct 5), every
staff email that reached robie@ became a job, and every job that did not
finish mailed "You asked Robie to <subject>, and Robie did not finish it."
to the sender and everyone cc'd. Most of those emails were never asks:
Gmail emoji reactions, forwarded Zapier alerts, the Manual Renewal Report,
out-of-office replies, newsletter test sends, Google Chat notices, threads
where Robie was only cc'd, and replies addressed to someone else.

The rule here is conservative. A real request is a direct ask from staff
where robie@ is in To (or Robie is named with an instruction), or a reply
that continues a job Robie is waiting on. Anything else is skipped and
logged with a reason. Nothing here sends mail or touches a client.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from email.utils import getaddresses
from pathlib import Path
from typing import Any, Iterable, Mapping

MAILBOX_ADDRESS = "robie@streetsmart.insurance"

KEEP_REQUEST = "request"
KEEP_CONTINUATION = "continuation"
SKIP = "skip"

# Statuses where Robie is waiting on the human: a reply on that thread is
# the answer, even when it has no ask in it.
_WAITING_STATUSES = {"AWAITING_HUMAN_INPUT", "NEEDS_CLARIFICATION"}

GMAIL_REACTION_MIME = "text/vnd.google.email-reaction+json"
_REACTION_TEXT = re.compile(r"\breacted via Gmail\b", re.IGNORECASE)

_GOOGLE_CHAT_NOTICE = re.compile(
    r"started a conversation about this email in Google Chat"
    r"|sent to you on behalf of .{1,80}? by Google Chat",
    re.IGNORECASE | re.DOTALL,
)

_AUTO_SUBJECT = re.compile(
    r"^\s*(?:out of (?:the )?office|automatic reply|auto(?:matic)?[- ]?reply|autoreply"
    r"|auto:|away from (?:the )?office|undeliverable|undelivered mail"
    r"|delivery status notification|mail delivery (?:failed|subsystem)"
    r"|read:|accepted:|declined:|tentative:|invitation:|updated invitation)",
    re.IGNORECASE,
)

# Reports, alerts and test sends that land in robie@ but are not asks.
_REPORT_SUBJECTS = (
    re.compile(r"\bmanual renewal report\b", re.IGNORECASE),
    re.compile(r"\btask check-?in\b", re.IGNORECASE),
    re.compile(r"\b(?:daily|weekly|monthly) (?:digest|summary|report)\b", re.IGNORECASE),
    re.compile(r"\breport\s*[\u2014\u2013-]\s*\d{4}-\d{2}-\d{2}\b", re.IGNORECASE),
    re.compile(r"^\s*(?:(?:fwd?|fw)\s*:\s*)*\[(?:robie|test|alert)\b", re.IGNORECASE),
    re.compile(r"^\s*(?:(?:re|fwd?|fw)\s*:\s*)*test\s*\(step\s*\d+\)", re.IGNORECASE),
    re.compile(r"\bzaps?\b.*\b(?:paused|error|turned off)\b", re.IGNORECASE),
    re.compile(r"\b(?:possible error on your|held tasks?|out of tasks)\b", re.IGNORECASE),
)

_AUTOMATED_LOCAL = re.compile(
    r"^(?:no-?reply|do-?not-?reply|donotreply|notifications?|notify|alerts?|"
    r"mailer-daemon|postmaster|bounces?|system|robot|automation|calendar-notification)\b",
    re.IGNORECASE,
)
_AUTOMATED_DOMAINS = (
    "zapier.com",
    "mail.zapier.com",
    "appliedsystems.com",
    "accounts.google.com",
    "calendar.google.com",
    "chat.google.com",
)

_FORWARD_MARKER = re.compile(
    r"-{5,}\s*Forwarded message\s*-{5,}|Begin forwarded message:|-{3,}\s*Original Message\s*-{3,}",
    re.IGNORECASE,
)
_FORWARD_FROM = re.compile(
    r"(?:-{5,}\s*Forwarded message\s*-{5,}|Begin forwarded message:|-{3,}\s*Original Message\s*-{3,})"
    r"\s*(?:\*\s*)?From:\s*\*?\s*([^\n]+)",
    re.IGNORECASE,
)
_REPLY_HEADER = re.compile(
    r"\bOn\s+(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s.{4,160}?\bwrote:",
    re.IGNORECASE | re.DOTALL,
)
_OUTLOOK_HEADER = re.compile(r"^\s*From:\s.+\n\s*(?:Sent|Date):\s", re.IGNORECASE | re.MULTILINE)
_SIGNATURE = re.compile(
    r"(?:^|\n|\s)(?:\[image:[^\]]*\]\s*)?(?:best regards|kind regards|warm regards|regards,|thanks,|thank you,"
    r"|thanks!|cheers,|sent from my (?:iphone|android|ipad)|get outlook for)",
    re.IGNORECASE,
)
_URL = re.compile(r"(?:https?://|www\.)\S+|<[^>\s]+>", re.IGNORECASE)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

_GREETING = re.compile(
    r"^\s*(?:hi|hello|hey|dear|good (?:morning|afternoon|evening))\b[\s,]*([A-Za-z][A-Za-z.'-]*)?",
    re.IGNORECASE,
)
# Robie spoken to, not spoken about: "Robie, ...", "@Robie", "Robie please ...".
_ROBIE_ADDRESSED = re.compile(
    r"(?:^|[\s(])@?robie\b\s*(?:[,:\-\u2014!]|(?=(?:please|pls|kindly|can|could|would|will|go|file|send|"
    r"update|add|create|check|fix|handle|make|start|run|pull|find|review|upload|attach)\b))",
    re.IGNORECASE,
)

_ASK_PHRASES = re.compile(
    r"\b(?:please|pls|plz|kindly)\b(?!\s+(?:note|see below|be advised|disregard|ignore))"
    r"|\b(?:can|could|would|will) you\b"
    r"|\b(?:i|we) need (?:you|robie)\b|\bneed (?:you|robie) to\b"
    r"|\byou (?:need|have) to\b"
    r"|\bmake sure\b|\btake care of\b|\bgo ahead\b|\bfollow up\b|\blook into\b"
    r"|\b(?:is|are|needs?|has|have) to be (?:done|filed|sent|updated|completed|fixed|changed|added|removed|processed|run)\b"
    r"|\bwork (?:up|on)\b|\bfigure out\b|\bget (?:this|it|them) (?:done|filed|sent|over)\b",
    re.IGNORECASE,
)
_IMPERATIVE_VERBS = (
    "add", "attach", "book", "build", "call", "cancel", "change", "check", "complete",
    "confirm", "correct", "create", "draft", "email", "endorse", "file", "find", "fix",
    "forward", "generate", "get", "handle", "issue", "map", "prepare",
    "process", "pull", "quote", "remove", "renew", "reply", "request", "resend", "review",
    "run", "save", "schedule", "send", "set up", "start", "submit", "update", "upload",
    "verify", "write",
)
_IMPERATIVE = re.compile(
    r"(?:^|[.!\n;:]\s*|\bthen\s+|\band\s+)(?:" + "|".join(_IMPERATIVE_VERBS) + r")\b(?!\w)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class InboundEmail:
    gmail_message_id: str
    thread_id: str
    sender: str
    subject: str
    body: str
    headers: Mapping[str, str] = field(default_factory=dict)
    mime_types: tuple[str, ...] = ()
    attachment_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class IntakeDecision:
    keep: bool
    kind: str
    reason: str
    detail: str = ""
    fingerprint: str = ""
    rfc822_message_id: str = ""

    def as_payload(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in ("", None)}

    def log_line(self, email: InboundEmail) -> str:
        subject = re.sub(r"\s+", " ", str(email.subject or ""))[:140]
        detail = f" detail={self.detail}" if self.detail else ""
        return (
            f"email intake {'KEEP' if self.keep else 'SKIP'} kind={self.kind} "
            f"reason={self.reason} msg={email.gmail_message_id} thread={email.thread_id} "
            f"from={email.sender} subject={subject!r}{detail}"
        )


# --------------------------------------------------------------------------
# Text helpers


def header(headers: Mapping[str, str], name: str) -> str:
    wanted = name.lower()
    for key, value in (headers or {}).items():
        if str(key).lower() == wanted:
            return str(value or "")
    return ""


def addresses(value: str) -> list[str]:
    return [addr.strip().lower() for _, addr in getaddresses([value or ""]) if addr.strip()]


def strip_subject_prefixes(subject: str) -> str:
    text = str(subject or "").strip()
    while True:
        new = re.sub(r"^\s*(?:re|fwd?|fw|aw|wg)\s*:\s*", "", text, flags=re.IGNORECASE)
        if new == text:
            return new.strip()
        text = new


def is_forward_subject(subject: str) -> bool:
    return bool(re.match(r"^\s*(?:(?:re)\s*:\s*)*(?:fwd?|fw)\s*:", str(subject or ""), re.IGNORECASE))


def new_text(body: str) -> str:
    """The sender's own words: above any forward, quote, or signature."""
    text = str(body or "").replace("\r\n", "\n")
    cut = len(text)
    for pattern in (_FORWARD_MARKER, _REPLY_HEADER, _OUTLOOK_HEADER):
        match = pattern.search(text)
        if match:
            cut = min(cut, match.start())
    quoted = re.search(r"^\s*>", text, re.MULTILINE)
    if quoted:
        cut = min(cut, quoted.start())
    text = text[:cut]
    sig = _SIGNATURE.search(text)
    if sig:
        text = text[: sig.start()]
    return re.sub(r"[ \t]+", " ", text).strip()


def forwarded_sender(body: str) -> str:
    match = _FORWARD_FROM.search(str(body or ""))
    if not match:
        return ""
    found = addresses(match.group(1).replace("&lt;", "<").replace("&gt;", ">"))
    return found[0] if found else match.group(1).strip().lower()


def is_automated_address(address: str) -> bool:
    addr = str(address or "").strip().lower()
    if not addr:
        return False
    local, _, domain = addr.partition("@")
    if _AUTOMATED_LOCAL.match(local):
        return True
    return any(domain == d or domain.endswith("." + d) for d in _AUTOMATED_DOMAINS)


def has_ask(text: str) -> bool:
    cleaned = _EMAIL.sub(" ", _URL.sub(" ", str(text or "")))
    cleaned = cleaned.strip()
    if not cleaned:
        return False
    if "?" in cleaned:
        return True
    if _ASK_PHRASES.search(cleaned):
        return True
    return bool(_IMPERATIVE.search(cleaned))


def robie_addressed(text: str) -> bool:
    raw = str(text or "")
    return greeting_name(raw) == "robie" or bool(_ROBIE_ADDRESSED.search(raw))


def robie_named_with_ask(text: str) -> bool:
    return robie_addressed(text) and has_ask(text)


def greeting_name(text: str) -> str:
    match = _GREETING.match(str(text or ""))
    if not match or not match.group(1):
        return ""
    name = match.group(1).strip(".,'-").lower()
    if name in {"all", "team", "everyone", "there", "guys", "folks", "please", "can",
                "could", "i", "we", "so", "just", "and", "the", "this", "you"}:
        return ""
    return name


def fingerprint(subject: str, body: str) -> str:
    """Same subject words + same content = same request, even if re-sent."""
    norm_subject = strip_subject_prefixes(subject).casefold()
    text = str(body or "")
    text = re.sub(r"(?im)^\s*(?:date|sent|to|cc):.*$", " ", text)
    text = re.sub(r"\s+", " ", text).strip().casefold()[:4000]
    return hashlib.sha256(f"{norm_subject}\n{text}".encode("utf-8")).hexdigest()[:32]


def fingerprint_from_request_text(request_text: str) -> str:
    raw = str(request_text or "")
    subject, _, body = raw.partition("\n\n")
    subject = re.sub(r"^\s*Subject:\s*", "", subject, flags=re.IGNORECASE)
    return fingerprint(subject, body)


# --------------------------------------------------------------------------
# Job DB lookups (read-only)


def _connect_ro(db_path: str | Path | None) -> sqlite3.Connection | None:
    if not db_path:
        return None
    path = Path(db_path)
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    return conn


def email_jobs_on_thread(db_path: str | Path | None, thread_id: str) -> list[dict[str, Any]]:
    thread = str(thread_id or "").strip()
    conn = _connect_ro(db_path)
    if conn is None or not thread:
        return []
    try:
        rows = conn.execute(
            "SELECT id, idempotency_key, status, payload_json FROM jobs "
            "WHERE action_type='hermes.email_task' AND payload_json LIKE ?",
            (f'%"gmail_thread_id":"{thread}"%',),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    out = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            payload = {}
        if str(payload.get("gmail_thread_id") or "").strip() != thread:
            continue
        out.append({
            "id": row["id"],
            "idempotency_key": row["idempotency_key"],
            "status": str(row["status"] or ""),
            "payload": payload,
        })
    return out


def email_jobs_with_rfc822_id(db_path: str | Path | None, rfc822_id: str) -> list[dict[str, Any]]:
    wanted = str(rfc822_id or "").strip()
    conn = _connect_ro(db_path)
    if conn is None or not wanted:
        return []
    try:
        rows = conn.execute(
            "SELECT id, idempotency_key, status, payload_json FROM jobs "
            "WHERE action_type='hermes.email_task' AND payload_json LIKE ?",
            (f'%"rfc822_message_id":{json.dumps(wanted)}%',),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    out = []
    for row in rows:
        try:
            intake = (json.loads(row["payload_json"]) or {}).get("email_intake") or {}
        except (TypeError, ValueError, AttributeError):
            intake = {}
        if str(intake.get("rfc822_message_id") or "").strip() == wanted:
            out.append({"id": row["id"], "idempotency_key": row["idempotency_key"], "status": row["status"]})
    return out


# --------------------------------------------------------------------------
# Classification


def _skip(reason: str, detail: str = "", **kw: str) -> IntakeDecision:
    return IntakeDecision(keep=False, kind=SKIP, reason=reason, detail=detail, **kw)


def junk_reason(email: InboundEmail) -> tuple[str, str] | None:
    """Reasons that hold no matter who sent it or which thread it is on."""
    headers = email.headers or {}
    body = str(email.body or "")
    top = new_text(body)
    if GMAIL_REACTION_MIME in {m.lower() for m in email.mime_types}:
        return "gmail_reaction", "reaction MIME part"
    if _REACTION_TEXT.search(top[:400] or body[:400]):
        return "gmail_reaction", "'reacted via Gmail'"
    auto = header(headers, "auto-submitted").strip().lower()
    if auto and auto != "no":
        return "auto_submitted", f"Auto-Submitted: {auto}"
    precedence = header(headers, "precedence").strip().lower()
    if precedence in {"bulk", "list", "junk", "auto_reply"}:
        return "bulk_mail", f"Precedence: {precedence}"
    if header(headers, "x-autoreply") or header(headers, "x-autorespond"):
        return "auto_reply", "auto-reply header"
    if _AUTO_SUBJECT.match(str(email.subject or "")):
        return "auto_reply", "out-of-office or system subject"
    if is_automated_address(email.sender) or str(email.sender or "").lower() == MAILBOX_ADDRESS:
        return "automated_sender", email.sender
    hitl_reply = bool(re.search(r"\[ROBIE HITL\]", f"{email.subject}\n{body[:4000]}", re.IGNORECASE))
    for pattern in _REPORT_SUBJECTS:
        if not hitl_reply and pattern.search(str(email.subject or "")):
            return "report_or_alert", "report, alert or test-send subject"
    if _GOOGLE_CHAT_NOTICE.search(body[:2000]):
        return "google_chat_notice", "Gmail 'started a conversation in Google Chat' notice"
    fwd_from = forwarded_sender(body)
    if fwd_from and is_automated_address(fwd_from) and not has_ask(top):
        return "forwarded_automated_alert", f"forward of {fwd_from} with no ask"
    if (header(headers, "list-id") or header(headers, "list-unsubscribe")) and (
        MAILBOX_ADDRESS not in addresses(header(headers, "to"))
    ):
        return "mailing_list", "list mail not addressed to Robie"
    return None


def continuation_reason(
    email: InboundEmail,
    *,
    db_path: str | Path | None = None,
    ascend_sessions: Mapping[str, Any] | None = None,
) -> str:
    session = (ascend_sessions or {}).get(email.thread_id) if email.thread_id else None
    if isinstance(session, Mapping) and not session.get("closed"):
        return "open Ascend clarification on this thread"
    try:
        from .email_hitl import parse_hitl_job_token, reply_leads_with_retry
    except Exception:  # pragma: no cover - import guard for slim test envs
        parse_hitl_job_token = None
        reply_leads_with_retry = None
    if parse_hitl_job_token and parse_hitl_job_token(email.subject, email.body):
        return "reply to a Robie HITL job"
    for job in email_jobs_on_thread(db_path, email.thread_id):
        if job["status"] in _WAITING_STATUSES:
            return f"Robie is waiting on this thread (job {job['id'][:8]} {job['status']})"
    if reply_leads_with_retry and reply_leads_with_retry(email.body) and email_jobs_on_thread(
        db_path, email.thread_id
    ):
        return "RETRY reply on a Robie job thread"
    return ""


def duplicate_reason(email: InboundEmail, fp: str, rfc822_id: str, db_path: str | Path | None) -> str:
    own_key = f"gmail:{email.gmail_message_id}"
    for job in email_jobs_with_rfc822_id(db_path, rfc822_id):
        if job.get("idempotency_key") != own_key:
            return f"same Message-ID already has job {str(job['id'])[:8]}"
    for job in email_jobs_on_thread(db_path, email.thread_id):
        if job["idempotency_key"] == own_key:
            # Same Gmail message: the existing job resumes; not a new job.
            continue
        payload = job["payload"]
        intake = payload.get("email_intake") if isinstance(payload.get("email_intake"), dict) else {}
        other = str(intake.get("fingerprint") or "") or fingerprint_from_request_text(
            str(payload.get("request_text") or "")
        )
        if other and other == fp:
            return f"same request already has job {str(job['id'])[:8]} ({job['status']})"
    return ""


def classify_inbound(
    email: InboundEmail,
    *,
    db_path: str | Path | None = None,
    ascend_sessions: Mapping[str, Any] | None = None,
    mailbox: str = MAILBOX_ADDRESS,
) -> IntakeDecision:
    """KEEP only real requests and replies Robie is waiting on."""
    headers = email.headers or {}
    rfc822_id = header(headers, "message-id").strip()
    fp = fingerprint(email.subject, email.body)
    stamp = {"fingerprint": fp, "rfc822_message_id": rfc822_id}

    junk = junk_reason(email)
    if junk:
        return _skip(junk[0], junk[1], **stamp)

    dup = duplicate_reason(email, fp, rfc822_id, db_path)
    if dup:
        return _skip("duplicate", dup, **stamp)

    cont = continuation_reason(email, db_path=db_path, ascend_sessions=ascend_sessions)
    if cont:
        return IntakeDecision(True, KEEP_CONTINUATION, "continuation", cont, **stamp)

    to_list = addresses(header(headers, "to"))
    in_to = mailbox.lower() in to_list
    top = new_text(email.body)

    # Carlo 2026-10-08 6:45 AM: a forward only becomes a job when the
    # forwarder wrote their own note above it ("file this"). A bare forward
    # is skipped, even when it was sent to Robie alone.
    if not top and (is_forward_subject(email.subject) or _FORWARD_MARKER.search(str(email.body or ""))):
        fwd_from = forwarded_sender(email.body)
        return _skip("forward_no_note", f"forward of {fwd_from or 'unknown sender'} with no note", **stamp)

    if not in_to:
        if robie_named_with_ask(top):
            return IntakeDecision(True, KEEP_REQUEST, "robie_named_with_ask", "Robie cc'd but asked by name", **stamp)
        where = "cc" if mailbox.lower() in addresses(header(headers, "cc")) else "group/bcc"
        return _skip("robie_not_in_to", f"Robie only on {where} (FYI)", **stamp)

    name = greeting_name(top)
    if name and name != "robie" and not robie_named_with_ask(top):
        return _skip("addressed_to_someone_else", f"greets {name.title()}, not Robie", **stamp)

    if has_ask(top):
        return IntakeDecision(True, KEEP_REQUEST, "direct_ask", "", **stamp)

    return _skip("no_ask", "no ask for Robie in the new text", **stamp)


# --------------------------------------------------------------------------
# Gmail payload helpers


def payload_mime_types(payload: Mapping[str, Any] | None) -> tuple[str, ...]:
    found: list[str] = []

    def walk(part: Mapping[str, Any]) -> None:
        mime = str(part.get("mimeType") or "")
        if mime:
            found.append(mime.lower())
        for child in part.get("parts") or []:
            if isinstance(child, Mapping):
                walk(child)

    if isinstance(payload, Mapping):
        walk(payload)
    return tuple(found)


def payload_attachment_names(payload: Mapping[str, Any] | None) -> tuple[str, ...]:
    names: list[str] = []

    def walk(part: Mapping[str, Any]) -> None:
        if part.get("filename"):
            names.append(str(part["filename"]))
        for child in part.get("parts") or []:
            if isinstance(child, Mapping):
                walk(child)

    if isinstance(payload, Mapping):
        walk(payload)
    return tuple(names)


def is_unfinished_reply(text: str) -> bool:
    lowered = str(text or "").casefold()
    return "did not finish" in lowered or "could not confirm how it ended" in lowered


def reply_cc_for(job_status: str | None, reply_text: str, cc: Iterable[str]) -> list[str]:
    """CC only on a finished job. 'Did not finish' goes to the requester only."""
    if str(job_status or "").upper() != "COMPLETE" or is_unfinished_reply(reply_text):
        return []
    return list(cc)


def job_status_for_message(db_path: str | Path | None, gmail_message_id: str) -> str:
    conn = _connect_ro(db_path)
    if conn is None or not gmail_message_id:
        return ""
    try:
        row = conn.execute(
            "SELECT status FROM jobs WHERE idempotency_key=?", (f"gmail:{gmail_message_id}",)
        ).fetchone()
    except sqlite3.Error:
        return ""
    finally:
        conn.close()
    return str(row["status"]) if row else ""
