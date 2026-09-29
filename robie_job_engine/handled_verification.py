"""Verify whether flagged client email was actually handled (reply / forward),
and summarize genuinely untouched mail so a human can judge it.

Adapted 2026-09-29 from Jake Ferrara's ``verify_handled.py`` to the repo's
keyless delegated-service pattern (see ``gmail_accountability``). Read-only:
uses ``gmail.readonly`` and never writes, labels, or deletes.

Why this exists: ``gmail_accountability.summarize_mailbox_threads`` is
metadata-only and same-thread-only, so a forward (which starts a NEW Gmail
thread) leaves the client's message last and the thread is counted
"awaiting employee" - reps get graded on work they did. For each inbound
client message this module checks, in order:

1. **reply** - an internal message in the same thread, sent *after* the
   inbound (date-anchored; earlier internal messages in old threads do not
   count as replies to this inbound).
2. **forward** - a message in the employee's Sent, or in a shared mailbox
   they send from (``hello@``, ``certificates@``, ...), that references the
   original (``References``/``In-Reply-To`` contains the inbound
   ``Message-ID``) or carries the same normalized subject (``Re:``/``Fwd:``
   stripped, minimum length to avoid junk matches).
3. **unhandled** - neither found. The body is summarized extractively
   (quotes/signatures/disclaimers stripped) with an action heuristic
   (``likely yes`` / ``no clear action (looks FYI)`` / ``unknown``). FYI
   items are marked so report builders can exclude them from reply-rate
   denominators: ``handled / (received - fyi_no_action)``.

The metadata-only collection path in ``gmail_accountability`` is unchanged;
this module is an opt-in enrichment (manifest
``collection.gmail_accountability.verify_handled.enabled``) because it reads
message bodies of unhandled candidates in employee mailboxes.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Any, Callable, Iterable, Mapping, Sequence

from .gmail_accountability import (
    GMAIL_READONLY_SCOPE,
    build_keyless_delegated_service,
    verify_delegated_mailbox,
)


VERIFICATION_SCOPES = (GMAIL_READONLY_SCOPE,)
INTERNAL_DOMAINS = ("streetsmart.insurance",)
DEFAULT_SHARED_MAILBOXES = ("hello@streetsmart.insurance",)


class HandledVerificationError(RuntimeError):
    """Raised when handled verification cannot fail closed safely."""


# ---------------------------------------------------------------- body text

def _walk_parts(payload: Mapping[str, Any]):
    mime = str(payload.get("mimeType") or "")
    data = (payload.get("body") or {}).get("data")
    if data and mime.startswith("text/plain"):
        yield base64.urlsafe_b64decode(data).decode("utf-8", "replace")
    for part in payload.get("parts") or []:
        yield from _walk_parts(part)


def body_text(msg: Mapping[str, Any]) -> str:
    texts = list(_walk_parts(msg.get("payload") or {}))
    return "\n".join(texts)


def clean_body(text: str) -> str:
    """Strip quoted replies, signatures, disclaimers; keep the new content."""
    kept = []
    for ln in str(text or "").splitlines():
        s = ln.strip()
        if s.startswith(">"):
            continue
        if re.search(r"On .*wrote:", s):
            break
        if s in ("--", "-- "):
            break
        if re.match(r"(?i)^(sent from|confidentiality|coverage cannot be bound)", s):
            break
        kept.append(ln)
    text = "\n".join(kept)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


ACTION_WORDS = re.compile(
    r"\b(please|need|request|send me|provide|quote|cancel|add|remove|update|"
    r"urgent|asap|deadline|by (today|tomorrow|friday|monday)|follow up|"
    r"let me know|confirm|sign|attach|forward me)\b", re.I)

# Positive FYI signals ONLY - ambiguous or no-keyword mail is "unknown" and
# stays in the reply-rate denominator so real action items can't hide.
FYI_WORDS = re.compile(
    r"\b(unsubscribe|newsletter|digest|mailing list|do[- ]?not[- ]?reply|"
    r"this is an automated|auto-generated|no action (is )?required|for your "
    r"information only)\b", re.I)

FYI_ACTION_FLAG = "no clear action (positive FYI signal)"
UNKNOWN_ACTION_FLAG = "unknown"


def summarize(text: str, from_header: str = "") -> dict[str, str]:
    """Extractive summary: first substantive sentences + action heuristic.

    action_needed is "likely yes" on request language, the FYI flag only on
    positive FYI signals (bulk/auto mail patterns or a no-reply sender), and
    "unknown" otherwise - unknown items remain accountable."""
    text = clean_body(text)
    if not text:
        return {"summary": "(no readable body)", "action_needed": UNKNOWN_ACTION_FLAG}
    sentences = re.split(r"(?<=[.!?])\s+", text.replace("\n", " "))
    sentences = [s.strip() for s in sentences if len(s.strip()) > 15]
    summary = " ".join(sentences[:2])[:280]
    sender = _address_of(from_header)
    if ACTION_WORDS.search(text):
        action_needed = "likely yes"
    elif FYI_WORDS.search(text) or sender.startswith(("no-reply@", "noreply@", "donotreply@")):
        action_needed = FYI_ACTION_FLAG
    else:
        action_needed = UNKNOWN_ACTION_FLAG
    return {"summary": summary or text[:280], "action_needed": action_needed}


# ---------------------------------------------------------------- matching

def headers_of(msg: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(h.get("name") or "").lower(): str(h.get("value") or "")
        for h in ((msg.get("payload") or {}).get("headers") or [])
    }


def _address_of(header: str) -> str:
    text = str(header or "").strip().lower()
    if "<" in text and ">" in text:
        text = text.rsplit("<", 1)[-1].split(">", 1)[0]
    return text.strip()


def _addresses_of(header: str) -> list[str]:
    return [_address_of(part) for part in str(header or "").split(",") if _address_of(part)]


def is_internal(addr: str) -> bool:
    """Exact-domain match on the parsed address - lookalike domains
    (notstreetsmart.insurance.evil.com) must NOT count as internal."""
    address = _address_of(addr)
    if "@" not in address:
        return False
    domain = address.rsplit("@", 1)[-1]
    return any(domain == dom for dom in INTERNAL_DOMAINS)


def norm_subject(subj: str) -> str:
    s = (subj or "").lower()
    previous = None
    while previous != s:  # strip stacked prefixes ("Re: Fwd: ...")
        previous = s
        s = re.sub(r"^(re|fw|fwd):\s*", "", s).strip()
    s = re.sub(r"\s+", " ", s)
    return s


def thread_has_internal_reply(
    thread: Mapping[str, Any], employee: str, inbound_id: str
) -> dict[str, str] | None:
    """Any message in the thread from the employee or internal domain,
    sent AFTER the inbound message. (Old threads can contain earlier
    internal messages that are not replies to this inbound.)"""
    msgs = thread.get("messages") or []
    inbound_ts = None
    for m in msgs:
        if m.get("id") == inbound_id:
            inbound_ts = int(m.get("internalDate", "0") or "0")
            break
    if inbound_ts is None:
        return None
    for m in msgs:
        if m.get("id") == inbound_id:
            continue
        if int(m.get("internalDate", "0") or "0") <= inbound_ts:
            continue
        hs = headers_of(m)
        frm = hs.get("from", "")
        if employee.lower() in frm.lower() or is_internal(frm):
            return {
                "via": "reply",
                "detail": f"in-thread reply {hs.get('date', '')} from {frm}",
            }
    return None


def is_related_send(
    sent_headers: Mapping[str, str],
    inbound_subject: str,
    inbound_msgid: str,
    inbound_from: str = "",
) -> str | None:
    """Pure matching logic: is this sent message a forward / related send
    of the inbound? Returns a detail string or None. Unit-testable.

    Identity, per review: a bare subject match is NOT enough (any sent item
    in the window could collide). Require either a References/In-Reply-To
    hit on the inbound Message-ID, or a normalized-subject match PLUS the
    inbound client appearing among the send's To/Cc recipients."""
    refs = (sent_headers.get("references", "") + " " +
            sent_headers.get("in-reply-to", ""))
    if inbound_msgid and inbound_msgid in refs:
        return (f"references original message "
                f"({sent_headers.get('subject', '')[:60]})")
    target_subj = norm_subject(inbound_subject)
    sent_subj = norm_subject(sent_headers.get("subject", ""))
    if target_subj and len(target_subj) > 8 and target_subj in sent_subj:
        client = _address_of(inbound_from)
        recipients = set(
            _addresses_of(sent_headers.get("to", ""))
            + _addresses_of(sent_headers.get("cc", ""))
        )
        if client and client in recipients:
            return (f"matching subject and client recipient "
                    f"({sent_headers.get('subject', '')[:60]})")
    return None


# ---------------------------------------------------------------- Gmail I/O

def _list_messages(
    service: Any, query: str, max_results: int = 100
) -> tuple[list[dict[str, Any]], bool]:
    """Returns (messages, capped). capped=True means the result MAY be
    truncated at max_results (another page existed), so callers must flag
    the output as partial instead of silently truncating."""
    # Fetch one extra so "capped" is unambiguous: only a (max_results + 1)-th
    # hit proves the window overflowed the cap.
    found: list[dict[str, Any]] = []
    token = None
    while len(found) < max_results + 1:
        params: dict[str, Any] = {
            "userId": "me",
            "q": query,
            "maxResults": min(100, max_results + 1 - len(found)),
        }
        if token:
            params["pageToken"] = token
        response = service.users().messages().list(**params).execute()
        found.extend(response.get("messages") or [])
        token = response.get("nextPageToken")
        if not token:
            break
    return found[:max_results], len(found) > max_results


def _get_message(
    service: Any,
    message_id: str,
    *,
    fmt: str = "full",
    metadata_headers: Sequence[str] | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {"userId": "me", "id": message_id, "format": fmt}
    if metadata_headers:
        params["metadataHeaders"] = list(metadata_headers)
    return service.users().messages().get(**params).execute()


def _get_thread_metadata(service: Any, thread_id: str) -> dict[str, Any]:
    return service.users().threads().get(
        userId="me",
        id=thread_id,
        format="metadata",
        metadataHeaders=["From", "Date"],
    ).execute()


def find_inbound_client_messages(
    service: Any,
    employee: str,
    *,
    after: str,
    before: str,
    max_results: int = 30,
) -> list[dict[str, Any]]:
    """Inbound external (client) mail to the employee's mailbox in window.

    Internal senders (same agency domain) are excluded so coworker mail is
    never graded as client work."""
    query = (f"to:{employee} -from:{employee} -in:sent "
             f"after:{after} before:{before}")
    stubs, capped = _list_messages(service, query, max_results=max_results)
    messages: list[dict[str, Any]] = []
    for stub in stubs:
        msg = _get_message(
            service,
            stub["id"],
            fmt="metadata",
            metadata_headers=["From", "Subject", "Date", "Message-ID"],
        )
        headers = headers_of(msg)
        if is_internal(headers.get("from", "")):
            continue
        messages.append(msg)
    return messages, capped


def find_forward(
    services_by_mailbox: Mapping[str, Any],
    sent_mailboxes: Iterable[str],
    inbound_subject: str,
    inbound_msgid: str,
    inbound_from: str,
    inbound_ts: int,
    *,
    after: str,
    before: str,
) -> tuple[dict[str, str] | None, bool]:
    """Search each Sent mailbox (with its own delegated service) for a
    forward / related send of the inbound message. Only sends dated AFTER
    the inbound qualify. Returns (match, sent_scan_capped)."""
    sent_capped = False
    for box in sent_mailboxes:
        service = services_by_mailbox.get(str(box).casefold())
        if service is None:
            continue
        query = f"in:sent after:{after} before:{before}"
        stubs, capped = _list_messages(service, query, max_results=100)
        sent_capped = sent_capped or capped
        for stub in stubs:
            sent = _get_message(
                service,
                stub["id"],
                fmt="metadata",
                metadata_headers=["Subject", "References", "In-Reply-To", "From", "To", "Cc", "Date"],
            )
            # Date anchor: a send that predates the inbound can never be
            # its forward, however similar the subject.
            if int(sent.get("internalDate", "0") or "0") <= inbound_ts:
                continue
            match = is_related_send(
                headers_of(sent), inbound_subject, inbound_msgid, inbound_from
            )
            if match:
                return {"via": "forward", "detail": f"{box} sent {match}"}, sent_capped
    return None, sent_capped


def verify_inbound_message(
    service: Any,
    services_by_mailbox: Mapping[str, Any],
    employee: str,
    message: Mapping[str, Any],
    sent_mailboxes: Iterable[str],
    *,
    after: str,
    before: str,
) -> dict[str, Any]:
    """Classify one inbound client message: reply / forward / unhandled."""
    headers = headers_of(message)
    result: dict[str, Any] = {
        "message_id": str(message.get("id") or ""),
        "thread_id": str(message.get("threadId") or ""),
        "evidence_id": hashlib.sha256(
            f"{employee.casefold()}:{message.get('id', '')}".encode()
        ).hexdigest()[:16],
        "from": headers.get("from", ""),
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "handled_via": "unhandled",
        "detail": "",
        "summary": "",
        "action_needed": "",
    }

    inbound_ts = int(message.get("internalDate", "0") or "0")

    # 1. in-thread reply, date-anchored?
    thread = _get_thread_metadata(service, result["thread_id"])
    hit = thread_has_internal_reply(thread, employee, result["message_id"])
    if hit:
        result.update(handled_via=hit["via"], detail=hit["detail"])
        return result, False

    # 2. forward / related send from the employee or a shared mailbox?
    hit, sent_capped = find_forward(
        services_by_mailbox,
        sent_mailboxes,
        headers.get("subject", ""),
        headers.get("message-id", ""),
        headers.get("from", ""),
        inbound_ts,
        after=after,
        before=before,
    )
    if hit:
        result.update(handled_via=hit["via"], detail=hit["detail"])
        return result, sent_capped

    # 3. genuinely untouched: summarize the body for human judgment.
    full = _get_message(service, result["message_id"], fmt="full")
    summary = summarize(body_text(full), headers.get("from", ""))
    result.update(
        summary=summary["summary"],
        action_needed=summary["action_needed"],
        detail="no reply in thread, no forward/send found",
    )
    return result, sent_capped


def verify_mailbox_handled(
    employee: str,
    services_by_mailbox: Mapping[str, Any],
    *,
    sent_mailboxes: Sequence[str],
    after: str,
    before: str,
    max_messages: int = 30,
) -> dict[str, Any]:
    """Verify every inbound client message in one mailbox's window."""
    employee = employee.casefold().strip()
    service = services_by_mailbox[employee]
    inbound, inbound_capped = find_inbound_client_messages(
        service, employee, after=after, before=before, max_results=max_messages
    )
    items: list[dict[str, Any]] = []
    sent_capped = False
    for message in inbound:
        try:
            item, item_sent_capped = verify_inbound_message(
                service,
                services_by_mailbox,
                employee,
                message,
                sent_mailboxes,
                after=after,
                before=before,
            )
            sent_capped = sent_capped or item_sent_capped
            items.append(item)
        except Exception as exc:  # noqa: BLE001 - keep going, report the error
            items.append({
                "message_id": str(message.get("id") or ""),
                "handled_via": "check-failed",
                "detail": f"{type(exc).__name__}: {exc}"[:200],
                "summary": "",
                "action_needed": "",
            })
    received = len(items)
    via_reply = sum(item["handled_via"] == "reply" for item in items)
    via_forward = sum(item["handled_via"] == "forward" for item in items)
    fyi = sum(
        item["handled_via"] == "unhandled" and item["action_needed"] == FYI_ACTION_FLAG
        for item in items
    )
    unhandled_action = sum(
        item["handled_via"] == "unhandled" and item["action_needed"] == "likely yes"
        for item in items
    )
    unhandled_unknown = sum(
        item["handled_via"] == "unhandled"
        and item["action_needed"] not in (FYI_ACTION_FLAG, "likely yes")
        for item in items
    )
    check_failed = sum(item["handled_via"] == "check-failed" for item in items)
    # Unknown/FYI bucketing: only positive-FYI items leave the denominator.
    # Any failed check makes the mailbox's rate unevaluable - never grade
    # a mailbox on a partially-verified population.
    denominator = received - fyi - check_failed
    if check_failed:
        reply_rate = None
        rate_status = "unevaluable: check-failed item(s) present"
    elif denominator > 0:
        reply_rate = round((via_reply + via_forward) / denominator, 3)
        rate_status = "ok"
    else:
        reply_rate = None
        rate_status = "no accountable mail in window"
    partial = inbound_capped or sent_capped
    return {
        "mailbox": employee,
        "window": {"after": after, "before": before},
        "received": received,
        "handled_via_reply": via_reply,
        "handled_via_forward": via_forward,
        "unhandled_action": unhandled_action,
        "unhandled_unknown": unhandled_unknown,
        "unhandled_fyi": fyi,
        "check_failed": check_failed,
        "reply_rate": reply_rate,
        "rate_status": rate_status,
        "partial": partial,
        "inbound_capped": inbound_capped,
        "sent_scan_capped": sent_capped,
        "items": items,
    }


def verification_window(as_of: datetime, lookback_days: int) -> tuple[str, str]:
    """Gmail query window: after (inclusive) / before (exclusive), YYYY/MM/DD."""
    end = as_of.astimezone(timezone.utc).date() + timedelta(days=1)
    start = end - timedelta(days=max(1, lookback_days))
    return start.strftime("%Y/%m/%d"), end.strftime("%Y/%m/%d")


def collect_handled_verification(
    *,
    environment: Mapping[str, str] | None = None,
    service_factory: Callable[[str, str], Any] | None = None,
    as_of: datetime | None = None,
    approved_users: Iterable[str] | None = None,
    shared_mailboxes: Sequence[str] = DEFAULT_SHARED_MAILBOXES,
    lookback_days: int = 7,
    max_messages: int = 30,
    verify_mailbox: Callable[[Any, str], None] = verify_delegated_mailbox,
) -> dict[str, Any]:
    """Verify handled-status across the approved roster (opt-in, readonly).

    One delegated service per mailbox is built so each Sent search (including
    shared mailboxes such as ``hello@``) runs against that mailbox itself -
    a forward visible only in another mailbox's Sent cannot be found from
    the employee's own view. Every delegated profile is verified before use,
    same fail-closed posture as ``gmail_accountability.collect_agency_summary``.
    """
    environment = environment or os.environ
    service_account = environment.get("ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT", "").strip()
    users = tuple(
        dict.fromkeys(str(item).strip().casefold() for item in (approved_users or ()) if str(item).strip())
    )
    if not service_account or not users:
        return {"source_status": "missing delegated service account or mailbox allowlist"}
    if service_factory is None:
        service_factory = partial(build_keyless_delegated_service, scopes=VERIFICATION_SCOPES)
    now = as_of or datetime.now(timezone.utc)
    after, before = verification_window(now, lookback_days)
    shared = tuple(
        dict.fromkeys(str(item).strip().casefold() for item in shared_mailboxes if str(item).strip())
    )
    by_employee: dict[str, dict[str, Any]] = {}
    for user in users:
        services: dict[str, Any] = {user: service_factory(service_account, user)}
        verify_mailbox(services[user], user)
        sent_mailboxes = [user]
        for box in shared:
            if box == user:
                continue
            services[box] = service_factory(service_account, box)
            verify_mailbox(services[box], box)
            sent_mailboxes.append(box)
        by_employee[user] = verify_mailbox_handled(
            user,
            services,
            sent_mailboxes=sent_mailboxes,
            after=after,
            before=before,
            max_messages=max_messages,
        )
    totals = {
        "received": sum(item["received"] for item in by_employee.values()),
        "handled_via_reply": sum(item["handled_via_reply"] for item in by_employee.values()),
        "handled_via_forward": sum(item["handled_via_forward"] for item in by_employee.values()),
        "unhandled_action": sum(item["unhandled_action"] for item in by_employee.values()),
        "unhandled_unknown": sum(item["unhandled_unknown"] for item in by_employee.values()),
        "unhandled_fyi": sum(item["unhandled_fyi"] for item in by_employee.values()),
        "check_failed": sum(item["check_failed"] for item in by_employee.values()),
    }
    denominator = totals["received"] - totals["unhandled_fyi"] - totals["check_failed"]
    result = {
        "source_status": "available",
        "scope": GMAIL_READONLY_SCOPE,
        "body_access": True,
        "window": {"after": after, "before": before},
        "lookback_days": max(1, int(lookback_days)),
        "mailboxes": len(by_employee),
        "partial": any(item["partial"] for item in by_employee.values()),
        **totals,
        "reply_rate": (
            round((totals["handled_via_reply"] + totals["handled_via_forward"]) / denominator, 3)
            if denominator > 0 and not totals["check_failed"]
            else None
        ),
        "rate_status": (
            "unevaluable: check-failed item(s) present"
            if totals["check_failed"]
            else ("ok" if denominator > 0 else "no accountable mail in window")
        ),
        "by_employee": by_employee,
    }
    return result
