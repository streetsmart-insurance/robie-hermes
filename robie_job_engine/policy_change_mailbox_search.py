"""Cross-mailbox read-only Gmail search for the phase-2 carrier-contact worker.

Carlo authorized domain-wide delegation to speed up manual-route and
awaiting-directory changes: for each unconfirmed change this worker searches
the Gmail of the PEOPLE INVOLVED IN THAT CHANGE ONLY — robie@ plus the
assigned CSR plus the assigned producer (resolved from the same approved
roster phase 1 uses) — for carrier replies or endorsement documents, so it
finds evidence without waiting on human triage.

Hard boundaries (tested):

- READ-ONLY. The delegated service is built with the ``gmail.readonly``
  scope and the subject set to the mailbox being searched. This module has
  NO send path: it never invokes the Gmail send endpoint, and the
  default factory never requests a send scope. All outbound
  mail stays in
  :func:`policy_change_carrier_contact.default_carrier_mailer`, which
  hardcodes ``From: robie@streetsmart.insurance`` on a separate
  send-scoped credential.
- Only ``@streetsmart.insurance`` addresses that resolve from the change's
  own CSR / Assigned Producer via the approved roster are ever
  impersonated. A person who does not resolve to an agency address is
  skipped — never guessed, never searched.
- Every mailbox searched in a run is logged in the evidence file (who,
  query, hit/miss/error). No silent searching.
- A delegation error on one mailbox fails that mailbox closed; the run
  continues with the others.
"""

from __future__ import annotations

import base64
import logging
import os
import re
from collections.abc import Callable, Mapping
from datetime import date
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any

logger = logging.getLogger(__name__)

SENDER = "robie@streetsmart.insurance"
APPROVED_DOMAIN = "streetsmart.insurance"

#: Delegated service-account email (same domain-wide delegation that covers
#: robie@'s mailbox; read scopes for domain users are part of that grant).
SERVICE_ACCOUNT_ENV = "ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT"

#: How far back the Gmail query looks.
SEARCH_LOOKBACK_DAYS = 60
#: Cap on messages examined per mailbox per change (bounded reads).
MAX_MESSAGES_PER_MAILBOX = 25

#: Filename hints that mark an attachment as endorsement evidence. The
#: attachment must ALSO name the change's policy (digit run) to count.
ENDORSEMENT_FILENAME_HINTS = (
    "endorsement", "revised dec", "dec page", "declarations",
    "declaration page", "amendment", "policy change",
)

#: Stopwords excluded from insured-name token matching.
_NAME_STOPWORDS = frozenset({
    "the", "and", "for", "inc", "llc", "corp", "co", "ltd", "dba",
    "company", "group", "services", "insurance",
})


class MailboxSearchError(RuntimeError):
    """A single mailbox could not be searched (delegation/read failure)."""


def _name_key(name: Any) -> str:
    return " ".join(str(name or "").casefold().split())


def _significant_tokens(text: Any) -> list[str]:
    tokens = re.findall(r"[a-z0-9']+", str(text or "").casefold())
    return [t for t in tokens if len(t) >= 4 and t not in _NAME_STOPWORDS]


def resolve_change_mailboxes(
    item: Mapping[str, Any],
    roster_maps: Mapping[str, Any] | None,
    producer_fallbacks: Mapping[str, str] | None = None,
) -> list[str]:
    """Mailboxes this change's people own: robie@ + CSR + producer.

    Only @streetsmart.insurance addresses that resolve from the change's
    own "CSR" / "Assigned Producer" fields via the approved roster
    directory (plus producer fallbacks, same rule as phase 1) are
    returned. Nobody else's mailbox is ever included. When the roster is
    unavailable, only robie@ is searched — the worker cannot verify who
    is involved, so it impersonates no one unverified.
    """
    mailboxes = [SENDER]
    directory: Mapping[str, str] = {}
    if isinstance(roster_maps, Mapping):
        raw = roster_maps.get("directory")
        if isinstance(raw, Mapping):
            directory = raw
    fallbacks = producer_fallbacks or {}
    for field, use_fallbacks in (("CSR", False), ("Assigned Producer", True)):
        name = str(item.get(field) or "").strip()
        if not name:
            continue
        email = str(directory.get(_name_key(name)) or "").strip().casefold()
        if not email and use_fallbacks:
            email = str(fallbacks.get(_name_key(name)) or "").strip().casefold()
        if (
            email
            and email.endswith("@" + APPROVED_DOMAIN)
            and email not in mailboxes
        ):
            mailboxes.append(email)
    return mailboxes


def _from_domain(from_header: str) -> str:
    addrs = getaddresses([str(from_header or "")])
    if not addrs:
        return ""
    email = (addrs[0][1] or "").strip().casefold()
    return email.split("@")[-1] if "@" in email else ""


def _policy_digits_in_text(text: str, digits: str) -> bool:
    if not digits:
        return False
    return any(tok == digits for tok in re.findall(r"\d+", text or ""))


def _walk_parts(payload: Mapping[str, Any]):
    stack = [payload]
    while stack:
        part = stack.pop()
        if not isinstance(part, dict):
            continue
        yield part
        children = part.get("parts")
        if isinstance(children, list):
            stack.extend(children)


def extract_message_fields(message: Mapping[str, Any]) -> dict[str, Any]:
    """Subject/snippet/body-text/filenames/From/date from a full message."""
    payload = message.get("payload") if isinstance(message, dict) else {}
    headers: dict[str, str] = {}
    for header in (payload.get("headers") or []):
        if isinstance(header, dict):
            headers[str(header.get("name") or "").casefold()] = str(
                header.get("value") or ""
            )
    texts: list[str] = []
    filenames: list[str] = []
    if isinstance(payload, dict):
        for part in _walk_parts(payload):
            filename = str(part.get("filename") or "").strip()
            if filename:
                filenames.append(filename)
                continue
            mime = str(part.get("mimeType") or "")
            if mime.startswith("text/"):
                data = ((part.get("body") or {}).get("data")) if isinstance(
                    part.get("body"), dict
                ) else None
                if data:
                    try:
                        texts.append(base64.urlsafe_b64decode(str(data)).decode(
                            "utf-8", errors="replace"))
                    except Exception:  # noqa: BLE001
                        continue
    sent: date | None = None
    try:
        parsed = parsedate_to_datetime(headers.get("date", ""))
        sent = parsed.date() if parsed else None
    except Exception:  # noqa: BLE001
        sent = None
    return {
        "id": str(message.get("id") or ""),
        "subject": headers.get("subject", ""),
        "from": headers.get("from", ""),
        "date": sent,
        "snippet": str(message.get("snippet") or ""),
        "body": "\n".join(texts),
        "filenames": filenames,
    }


def classify_message(
    fields: Mapping[str, Any], *, policy_digits: str, insured_tokens: list[str]
) -> dict[str, Any] | None:
    """Decide whether a message is evidence for this change.

    Returns {"kind": "endorsement" | "carrier_reply", ...} or None.
    A message must name the change's policy (digit run) AND carry an
    insured-name token; unrelated mail (wrong policy number) is ignored.
    """
    haystack = " ".join([
        str(fields.get("subject") or ""),
        str(fields.get("snippet") or ""),
        str(fields.get("body") or ""),
        " ".join(str(f) for f in (fields.get("filenames") or [])),
    ])
    if not _policy_digits_in_text(haystack, policy_digits):
        return None
    if insured_tokens and not any(
        tok in haystack.casefold() for tok in insured_tokens
    ):
        return None
    for filename in (fields.get("filenames") or []):
        lowered = str(filename).casefold()
        if any(h in lowered for h in ENDORSEMENT_FILENAME_HINTS) and \
                _policy_digits_in_text(lowered, policy_digits):
            return {
                "kind": "endorsement",
                "message_id": fields.get("id"),
                "subject": fields.get("subject"),
                "date": fields.get("date"),
                "filename": str(filename),
            }
    if _from_domain(str(fields.get("from") or "")) not in ("", APPROVED_DOMAIN):
        return {
            "kind": "carrier_reply",
            "message_id": fields.get("id"),
            "subject": fields.get("subject"),
            "date": fields.get("date"),
            "from": str(fields.get("from") or ""),
        }
    return None


def default_search_service_factory(
    service_account_email: str,
) -> Callable[[str], Any]:
    """Read-only delegated Gmail factory: one service per impersonated user.

    Only the ``gmail.readonly`` scope is ever requested here — there is no
    code path in this module that can send mail from an impersonated
    account.
    """
    from .gmail_accountability import (
        GMAIL_READONLY_SCOPE,
        build_keyless_delegated_service,
        verify_delegated_mailbox,
    )

    def factory(user: str) -> Any:
        service = build_keyless_delegated_service(
            service_account_email, user, scopes=(GMAIL_READONLY_SCOPE,)
        )
        # Fail closed: prove delegation resolved to the intended mailbox.
        verify_delegated_mailbox(service, user)
        return service

    return factory


class PolicyChangeMailboxSearcher:
    """Read-only cross-mailbox search for carrier replies / endorsements.

    ``service_factory`` maps an impersonated mailbox address -> a Gmail
    service. The default factory builds keyless domain-wide-delegation
    services with ``gmail.readonly`` scope only. The searcher only ever
    calls ``messages().list``, ``messages().get`` and ``getProfile`` on
    those services — the send endpoint is never invoked from this
    module (asserted by tests with a send-raising fake service).
    """

    def __init__(
        self,
        *,
        service_factory: Callable[[str], Any] | None = None,
        lookback_days: int = SEARCH_LOOKBACK_DAYS,
    ) -> None:
        self._factory = service_factory
        self._lookback_days = max(1, int(lookback_days))

    @property
    def configured(self) -> bool:
        return self._factory is not None

    def _list_candidates(self, service: Any, query: str) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        token = None
        while len(found) < MAX_MESSAGES_PER_MAILBOX:
            response = service.users().messages().list(
                userId="me",
                q=query,
                maxResults=min(25, MAX_MESSAGES_PER_MAILBOX - len(found)),
                pageToken=token,
            ).execute()
            for item in response.get("messages", []) or []:
                found.append(item)
                if len(found) >= MAX_MESSAGES_PER_MAILBOX:
                    break
            token = response.get("nextPageToken")
            if not token:
                break
        return found

    def search_change(
        self,
        item: Mapping[str, Any],
        mailboxes: list[str],
        today: date,
    ) -> dict[str, Any]:
        """Search each mailbox for this change; log who/query/hit/miss.

        Returns {"mailboxes": [ {"mailbox", "query", "status",
        "hits", "error"} ], "endorsement": hit|None,
        "carrier_reply": hit|None}.
        """
        from .policy_change_carrier_contact import policy_digits

        digits = policy_digits(item.get("Policy Number"))
        insured_tokens = _significant_tokens(item.get("Account Name"))
        query = (
            f"{digits} newer_than:{self._lookback_days}d" if digits else ""
        )
        result: dict[str, Any] = {
            "mailboxes": [],
            "endorsement": None,
            "carrier_reply": None,
        }
        for mailbox in mailboxes:
            entry: dict[str, Any] = {
                "mailbox": mailbox,
                "query": query,
                "status": "miss",
                "hits": [],
            }
            if not self._factory:
                entry["status"] = "error"
                entry["error"] = "mailbox searcher is not configured"
            elif not digits:
                entry["status"] = "skipped"
                entry["reason"] = "no policy digits to search for"
            else:
                try:
                    service = self._factory(mailbox)
                    for candidate in self._list_candidates(service, query):
                        full = service.users().messages().get(
                            userId="me", id=candidate.get("id"),
                            format="full",
                        ).execute()
                        fields = extract_message_fields(full)
                        hit = classify_message(
                            fields, policy_digits=digits,
                            insured_tokens=insured_tokens,
                        )
                        if hit:
                            hit["mailbox"] = mailbox
                            entry["hits"].append(hit)
                            if hit["kind"] == "endorsement" and \
                                    result["endorsement"] is None:
                                result["endorsement"] = hit
                            if hit["kind"] == "carrier_reply" and \
                                    result["carrier_reply"] is None:
                                result["carrier_reply"] = hit
                    if entry["hits"]:
                        entry["status"] = "hit"
                except Exception as exc:  # noqa: BLE001
                    entry["status"] = "error"
                    entry["error"] = f"{type(exc).__name__}: {exc}"
                    logger.warning("mailbox search failed for %s: %s",
                                   mailbox, exc)
            result["mailboxes"].append(entry)
        return result


def build_default_mailbox_searcher() -> PolicyChangeMailboxSearcher | None:
    """Default searcher from the environment, or None when unconfigured.

    Returns None (search disabled, logged in evidence) when the delegated
    service-account env var is missing — the worker fails closed to
    robie@-only behavior rather than impersonating anyone unverified.
    """
    service_account = os.environ.get(SERVICE_ACCOUNT_ENV, "").strip()
    if not service_account:
        logger.info(
            "mailbox search disabled: %s is not configured", SERVICE_ACCOUNT_ENV
        )
        return None
    return PolicyChangeMailboxSearcher(
        service_factory=default_search_service_factory(service_account)
    )
