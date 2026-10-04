"""Scheduled driver: Ascend notice emails -> EZLynx discussion note + Zapier task.

This is the trigger PR #430 was missing. PR #430 put the engine on the box
(read-only ``triage_notice()``, Discussion API v8 append to an EXISTING
discussion, Zapier task firing with a required due date) but nothing ever
called it. This driver watches the agency mailboxes for unread Ascend
notice emails and runs the chain once per email.

Pipeline per email::

    Gmail (default staff mailboxes, unread, from the Ascend notice senders)
      -> triage_notice()                       (read-only classification)
      -> resolve applicant_id                  (EZLynx PolicyApi, normalized policy number)
      -> resolve CSR login                    (cancellation only; Ascend producer)
      -> one titled discussion per category    (find, or POST v8/discussions/with-note)
      -> file the note on that discussion
      -> Zapier task                           (non-pay cancellation CSR, or
                                               disputed-charge accounting login)

Safety (non-negotiable):

- DRY_RUN defaults ON. Live mode only with ``--live`` or
  ``ASCEND_DRIVER_LIVE=1``. Dry-run logs exactly what it would do
  (subject, applicant, CSR, note text, task payload) and writes nothing.
- Each run appends a counts-only record to
  ``/var/lib/robie-ascend-notice-driver/runs.jsonl`` (directory 0755,
  file 0644). The hourly health check reads that file and does not read
  the journal. The record has no subject, body, or applicant id.
  Dry-run requests ``gmail.readonly`` only: no mark-read, no notes, no
  labels, no Zapier post, and no ``driver_gate_for_write`` call.
- Fail closed per email: triage ``needs_human_review``, unresolved
  applicant, missing/invalid CSR username on a cancellation, any API
  error, phone numbers in the note text, or a write-scope refusal -> that
  email is skipped, logged, and the driver continues with the rest.
  Informational mail is ``ignored``, not a human-review skip.
- Write-scope eligibility is checked before filing. Dry-run reports
  matches blocked only by that allowlist as
  ``would_file_if_write_scope_allowed``, with applicant ids. This driver's
  own unit and workflow set ``ROBIE_EZLYNX_WRITE_SCOPE=all`` (honored with
  ``ROBIE_PLAYGROUND=1``). The compiled default in ``ezlynx_write_scope``
  stays the test account. The guard has no per-action switch, so this
  driver allows exactly three writes: a note on an existing category
  discussion, creating that category discussion with its first note, and
  a task (non-pay cancellation CSR, or disputed-charge accounting).
  Everything else is refused. Dry-run does not create discussions or tasks.
- Cancellations are notes only. The driver does not list or apply the
  Ascend NOC label. That label sends client email and text, and Robie
  does not send those. The result says ``label_skipped_by_policy``.
- One run files one note per applicant, policy, notice type, and due or
  cancel date (or the same normalized email body). Later copies in that
  run are ``duplicate_in_run`` and are not filed. That collapse does not
  read EZLynx. A separate read of the existing discussion note still
  runs, including for applicants the write allowlist blocks, and a note
  already on the discussion is not filed again.
- Never deletes anything. Never invents an applicant_id or a CSR username.
- Live notes on an existing category discussion go through
  ``file_note_to_existing_discussion``. A missing category discussion is
  created with ``create_discussion_with_note`` (``POST v8/discussions/with-note``).
  Both enforce the EZLynx write-scope allowlist and the driver lease.
  Dry-run checks the same allowlist, validates the payload, and does not
  take the lease or POST. Anything outside the allowlist is logged as
  ``write_scope_refused`` and skipped.
- Gmail is read-only except in LIVE mode, where a fully processed email is
  marked read (UNREAD label removed) so the next run does not re-file the
  same note. Dry-run never touches labels.
- Delegated Gmail scopes: dry-run requests ``gmail.readonly`` only
  (unread search + body). ``gmail.metadata`` cannot use ``messages.list?q=``.
  Live mark-read also requests ``gmail.modify``. Dry-run never requests
  modify, even if ``ASCEND_DRIVER_GMAIL_MODIFY`` is set.
  Workspace Admin DWD client ``112650695780807418521`` must authorize
  those scopes for the delegation SA.

Known wiring gaps (documented, not silently worked around):

- Applicant lookup tries the billable policy number, then a normalized
  insured name, then email, then phone. A match is one unique applicant.
  An ambiguous policy result does not fall through. Zero policy hits may.
  An incomplete search is retried once. Still-unmatched notices are one
  ask list for a person; they are not filed and they are not guessed.
- CSR login username: PolicyApi rows carry no CSR field. A CSR is
  required only for cancellation (the Zapier task). It comes from the
  Ascend program producer (account_manager only when the producer is
  missing), mapped through ``confirmation_notify.requester_login``.
  Shared mailboxes (hello@, accounting@, robie@) and unmapped names fail
  closed with ``csr_unresolved``. A username is never invented. Late
  payment, intent-to-cancel, and return premium file the note with
  ``applicant_id`` alone.
"""

from __future__ import annotations

import argparse
import base64
import contextvars
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Protocol

from . import ascend_notice_triage as triage
from . import ezlynx_discussions as discussions
from . import zapier_tasks
from .ascend_api import AscendApiClient, configured_client as configured_ascend_client
from .ascend_driver_stall import annotate_summary, append_run_record
from .ezlynx_api import EzlynxApiClient, load_ezlynx_api_config
from .ezlynx_discussions import DiscussionApiClient, DiscussionApiConfig
from .ezlynx_write_scope import EzlynxWriteScopeError

ROBIE_WAS_HERE = "Robie was here"

logger = logging.getLogger(__name__)

DEFAULT_MAILBOX = "hello@streetsmart.insurance"
# Exact From addresses on the redacted notice fixtures (56 files). A query
# that does not already limit From to these addresses, or to useascend.com,
# has this clause appended so GitHub and other non-Ascend mail is never pulled.
ASCEND_NOTICE_SENDERS: tuple[str, ...] = (
    "no-reply@useascend.com",
    "accounting@useascend.com",
    "support@useascend.com",
)
DEFAULT_DUE_DAYS = 2

# Scanned when neither --mailbox nor ASCEND_DRIVER_MAILBOX / ASCEND_DRIVER_MAILBOXES
# is set. Also the hard allowlist: any other mailbox is refused unless
# ASCEND_DRIVER_ALLOW_EXTRA_MAILBOXES=1.
# Prod's live driver reads these 25 mailboxes (Carlo, 2026-10-04).
DEFAULT_MAILBOXES: tuple[str, ...] = (
    "carlo@streetsmart.insurance",
    "jake@streetsmart.insurance",
    "robie@streetsmart.insurance",
    "hello@streetsmart.insurance",
    "accounting@streetsmart.insurance",
    "sandy@streetsmart.insurance",
    "zeus@streetsmart.insurance",
    "certificates@streetsmart.insurance",
    "daniela@streetsmart.insurance",
    "angie@streetsmart.insurance",
    "ana@streetsmart.insurance",
    "jazmin@streetsmart.insurance",
    "jackie@streetsmart.insurance",
    "taylor@streetsmart.insurance",
    "steffany@streetsmart.insurance",
    "amber@streetsmart.insurance",
    "ashley@streetsmart.insurance",
    "jimmy@streetsmart.insurance",
    "matthew@streetsmart.insurance",
    "karla@streetsmart.insurance",
    "mitchell@streetsmart.insurance",
    "eimy@streetsmart.insurance",
    "alejandro@streetsmart.insurance",
    "mike@streetsmart.insurance",
    "andrea@streetsmart.insurance",
)
ALLOWED_MAILBOXES = frozenset(mailbox.casefold() for mailbox in DEFAULT_MAILBOXES)

# A login username looks like KarlaSS / Carlo1: no whitespace, ever.
# Display names ("Karla Brown") must never reach the Zap's assignee field.
_LOGIN_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{2,64}$")

# Ascend producer / account_manager identities that are shared inboxes, not
# a person. requester_login("robie") is a real login; using it would invent
# an assignee. These fail closed.
_SHARED_CSR_LOCAL_PARTS = frozenset({"hello", "accounting", "robie"})
_SHARED_CSR_NAMES = frozenset({"hello", "accounting", "robie", "robie ai"})

# Trailing term suffix on a policy number: -00, -01, -1.
_TERM_SUFFIX_RE = re.compile(r"-\d{1,2}$")

# Trailing EZLynx line-of-business code, separated by whitespace: "DSLA123 APD".
# 2-4 uppercase letters only. Applied to the EZLynx row, not the Ascend number.
_LOB_SUFFIX_RE = re.compile(r"\s+[A-Z]{2,4}\s*$")

_NOTICE_DATE_RE = re.compile(r"(\d{2}/\d{2}/\d{4})")
_EFFECTIVE_DATE_PREFIX_RE = re.compile(r"effective\s*date\s*$", re.IGNORECASE)
_ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\ufeff]")

# Policy-row fields that may carry the bound applicant id.
_APPLICANT_ID_KEYS = (
    "ApplicantId",
    "applicantId",
    "applicant_id",
    "AccountId",
    "accountId",
)


def _truthy(value: Any) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes"}


class MailboxAllowlistError(ValueError):
    """Raised when a mailbox is outside the hard staff allowlist."""


class NonNoteWriteRefused(RuntimeError):
    """Raised when this driver is asked for a write it does not own.

    ``ROBIE_EZLYNX_WRITE_SCOPE=all`` is applicant-wide. The write-scope
    guard has no per-action switch, so the driver itself allows only a
    note on an existing category discussion, creating that category
    discussion with the note, and a cancellation or disputed-charge task.
    Document uploads, untitled discussions, labels, policy writes, bind,
    and delete stay refused.
    """


def parse_mailbox_list(raw: str) -> list[str]:
    return [part.strip() for part in str(raw or "").split(",") if part.strip()]


def enforce_mailbox_allowlist(mailboxes: list[str]) -> list[str]:
    """Refuse mailboxes outside the staff allowlist unless explicitly allowed.

    The override is ``ASCEND_DRIVER_ALLOW_EXTRA_MAILBOXES=1``.
    """
    cleaned: list[str] = []
    refused: list[str] = []
    allow_extra = _truthy(os.environ.get("ASCEND_DRIVER_ALLOW_EXTRA_MAILBOXES"))
    for raw in mailboxes:
        mailbox = str(raw or "").strip()
        if not mailbox:
            continue
        if mailbox.casefold() not in ALLOWED_MAILBOXES and not allow_extra:
            refused.append(mailbox)
        cleaned.append(mailbox)
    if refused:
        raise MailboxAllowlistError(
            "mailbox not on the Ascend driver allowlist: "
            + ", ".join(refused)
            + "; set ASCEND_DRIVER_ALLOW_EXTRA_MAILBOXES=1 to override"
        )
    if not cleaned:
        raise MailboxAllowlistError("no mailboxes to scan")
    return cleaned


def resolve_mailboxes(
    *,
    mailbox: str | None = None,
    mailboxes: str | None = None,
) -> list[str]:
    """Choose scan targets.

    Precedence: ``--mailboxes``, then ``ASCEND_DRIVER_MAILBOXES``, then
    ``--mailbox``, then ``ASCEND_DRIVER_MAILBOX``, then the default staff
    mailboxes.
    ``None`` means the flag was omitted. An explicit empty string falls
    through the same way.
    """
    if mailboxes is not None and str(mailboxes).strip():
        chosen = parse_mailbox_list(mailboxes)
    else:
        env_list = os.environ.get("ASCEND_DRIVER_MAILBOXES")
        if mailboxes is None and env_list and str(env_list).strip():
            chosen = parse_mailbox_list(env_list)
        elif mailbox is not None and str(mailbox).strip():
            chosen = parse_mailbox_list(mailbox)
        else:
            env_one = os.environ.get("ASCEND_DRIVER_MAILBOX")
            if mailbox is None and env_one and str(env_one).strip():
                chosen = [str(env_one).strip()]
            else:
                chosen = list(DEFAULT_MAILBOXES)
    return enforce_mailbox_allowlist(chosen)


def ascend_sender_filter() -> str:
    """Gmail ``from:`` clause limited to the fixture-proven Ascend notice senders."""
    return "from:(" + " OR ".join(ASCEND_NOTICE_SENDERS) + ")"


DEFAULT_QUERY = "is:unread newer_than:2d " + ascend_sender_filter()

_FROM_CLAUSE_RE = re.compile(r"from:\(([^)]*)\)|from:(\S+)", re.IGNORECASE)
_ASCEND_DOMAIN = "useascend.com"


def _token_is_ascend_sender(token: str) -> bool:
    text = token.strip().strip("\"'").casefold()
    if not text:
        return False
    if text in {sender.casefold() for sender in ASCEND_NOTICE_SENDERS}:
        return True
    if text in {_ASCEND_DOMAIN, f"@{_ASCEND_DOMAIN}"}:
        return True
    return text.endswith(f"@{_ASCEND_DOMAIN}")


def _from_targets(query: str) -> list[str] | None:
    """Return From targets, or None when the query has no ``from:`` operator."""
    matches = list(_FROM_CLAUSE_RE.finditer(query))
    if not matches:
        return None
    targets: list[str] = []
    for match in matches:
        grouped = match.group(1)
        if grouped is not None:
            targets.extend(
                part.strip()
                for part in re.split(r"(?i)\s+OR\s+", grouped)
                if part.strip()
            )
        elif match.group(2):
            targets.append(match.group(2))
    return targets


def _has_or_outside_parens(query: str) -> bool:
    depth = 0
    index = 0
    while index < len(query):
        char = query[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and query[index : index + 2].casefold() == "or":
            before_ok = index == 0 or not query[index - 1].isalnum()
            after_at = index + 2
            after_ok = after_at >= len(query) or not query[after_at].isalnum()
            if before_ok and after_ok:
                return True
        index += 1
    return False


def ensure_ascend_sender_filter(query: str) -> str:
    """Keep a query from matching mail outside Ascend's notice senders.

    An empty query becomes the default. A query whose every ``from:`` token
    is an allowlisted sender or ``@useascend.com`` is left alone, including
    a domain-wide ``from:useascend.com``. Anything else gets the sender
    clause AND-ed on. A top-level ``OR`` is parenthesized first so a
    non-Ascend alternative cannot survive the AND.
    """
    text = str(query or "").strip()
    clause = ascend_sender_filter()
    if not text:
        return DEFAULT_QUERY
    if clause.casefold() in text.casefold():
        return text
    targets = _from_targets(text)
    if targets and all(_token_is_ascend_sender(target) for target in targets):
        return text
    if _has_or_outside_parens(text):
        return f"({text}) {clause}"
    return f"{text} {clause}"


def configured_query(cli_value: str | None = None) -> str:
    """Gmail query. An explicit CLI value wins; otherwise the env, then the default.

    The Ascend sender clause is applied unless the chosen query already
    limits From to those addresses or to ``useascend.com``.
    """
    if cli_value is not None and str(cli_value).strip():
        raw = str(cli_value).strip()
    else:
        raw = str(os.environ.get("ASCEND_DRIVER_QUERY") or "").strip() or DEFAULT_QUERY
    return ensure_ascend_sender_filter(raw)


# ---------------------------------------------------------------------------
# Email source
# ---------------------------------------------------------------------------


@dataclass
class EmailNotice:
    message_id: str
    subject: str
    body: str
    internal_date: str = ""
    mailbox: str = ""
    gmail_message_id: str = ""


class NoticeSource(Protocol):
    def fetch_notices(self) -> list[EmailNotice]: ...
    def mark_processed(self, message_id: str) -> None: ...


def _gmail_text_from_payload(payload: dict[str, Any]) -> str:
    """Best-effort plain-text extraction from a Gmail message payload."""
    texts: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        mime = str(part.get("mimeType") or "")
        body = part.get("body") or {}
        data = body.get("data")
        if data and mime.startswith("text/plain"):
            try:
                padded = data + "=" * (-len(data) % 4)
                texts.append(
                    base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
                )
            except (ValueError, TypeError, base64.binascii.Error):
                pass
        for sub in part.get("parts") or []:
            if isinstance(sub, dict):
                walk(sub)

    if isinstance(payload, dict):
        walk(payload)
    return "\n".join(texts).strip()


class MultiMailboxNoticeSource:
    """Scan multiple employee mailboxes via domain-wide delegation.

    Iterates over each mailbox, fetching unread Ascend notices. Each
    notice tracks which mailbox it came from. Marking processed only
    affects the source mailbox.
    """

    def __init__(
        self,
        *,
        mailboxes: list[str],
        query: str = DEFAULT_QUERY,
        service_account_email: str = "",
        service_factory: Callable[[str, str], Any] | None = None,
        max_results: int = 25,
        allow_modify: bool = False,
    ) -> None:
        self.mailboxes = [m.strip() for m in mailboxes if m.strip()]
        self.query = query
        self.service_account_email = service_account_email
        self._service_factory = service_factory
        self.max_results = max_results
        self.allow_modify = allow_modify
        self._services: dict[str, Any] = {}

    def _service_for(self, mailbox: str) -> Any:
        if mailbox not in self._services:
            factory = self._service_factory
            if factory is None:
                from .gmail_accountability import build_notice_gmail_service

                allow_modify = self.allow_modify

                def factory(service_account_email: str, mailbox: str) -> Any:
                    return build_notice_gmail_service(
                        service_account_email, mailbox, modify=allow_modify
                    )
            if not self.service_account_email:
                raise RuntimeError(
                    "ROBIE_GMAIL_DELEGATION_SA is not configured; "
                    "cannot open the delegated Gmail service"
                )
            self._services[mailbox] = factory(self.service_account_email, mailbox)
        return self._services[mailbox]

    def fetch_notices(self) -> list[EmailNotice]:
        notices: list[EmailNotice] = []
        for mailbox in self.mailboxes:
            try:
                service = self._service_for(mailbox)
            except Exception as exc:
                logger.warning("Skipping mailbox %s: %s", mailbox, type(exc).__name__)
                continue
            try:
                listed = (
                    service.users()
                    .messages()
                    .list(userId="me", q=self.query, maxResults=self.max_results)
                    .execute()
                    .get("messages", [])
                )
            except Exception as exc:
                logger.warning(
                    "Mailbox %s list failed: %s", mailbox, type(exc).__name__
                )
                continue
            for item in listed:
                message_id = str(item.get("id") or "")
                if not message_id:
                    continue
                try:
                    full = (
                        service.users()
                        .messages()
                        .get(userId="me", id=message_id, format="full")
                        .execute()
                    )
                except Exception:
                    continue
                headers = {
                    h.get("name", "").lower(): h.get("value", "")
                    for h in (full.get("payload", {}).get("headers") or [])
                    if isinstance(h, dict)
                }
                notices.append(
                    EmailNotice(
                        message_id=f"{mailbox}:{message_id}",
                        subject=headers.get("subject", ""),
                        body=_gmail_text_from_payload(full.get("payload", {})),
                        internal_date=str(full.get("internalDate") or ""),
                        mailbox=mailbox,
                        gmail_message_id=message_id,
                    )
                )
        return notices

    def mark_processed(self, message_id: str) -> None:
        # message_id is "mailbox:actual_id"
        if ":" in message_id:
            mailbox, actual_id = message_id.split(":", 1)
            service = self._service_for(mailbox)
            service.users().messages().modify(
                userId="me",
                id=actual_id,
                body={"removeLabelIds": ["UNREAD"]},
            ).execute()


class NullNoticeSource:
    """No-op source for callers that already hold the email (mailbox watcher)."""

    def fetch_notices(self) -> list[EmailNotice]:
        return []

    def mark_processed(self, message_id: str) -> None:
        return None


class GmailNoticeSource:
    """Unread-notice source backed by a delegated Gmail service.

    ``service_factory`` defaults to the notice-specific keyless domain-wide
    delegation builder, which requests ``gmail.readonly`` (search + body).
    Accountability's metadata-only factory cannot use ``messages.list?q=``.
    ``mark_processed`` removes the UNREAD label and is only called in live
    mode; that path requests ``gmail.modify`` in addition to readonly.
    """

    def __init__(
        self,
        *,
        mailbox: str = DEFAULT_MAILBOX,
        query: str = DEFAULT_QUERY,
        service_account_email: str = "",
        service_factory: Callable[[str, str], Any] | None = None,
        max_results: int = 25,
        allow_modify: bool = False,
    ) -> None:
        self.mailbox = mailbox
        self.query = query
        self.service_account_email = service_account_email
        self._service_factory = service_factory
        self.max_results = max_results
        self.allow_modify = allow_modify
        self._service: Any = None

    @property
    def requested_scopes(self) -> tuple[str, ...]:
        from .gmail_accountability import notice_gmail_scopes

        return notice_gmail_scopes(modify=self.allow_modify)

    def _service_client(self) -> Any:
        if self._service is None:
            factory = self._service_factory
            if factory is None:
                from .gmail_accountability import build_notice_gmail_service

                allow_modify = self.allow_modify

                def factory(service_account_email: str, mailbox: str) -> Any:
                    return build_notice_gmail_service(
                        service_account_email, mailbox, modify=allow_modify
                    )
            if not self.service_account_email:
                raise RuntimeError(
                    "ROBIE_GMAIL_DELEGATION_SA is not configured; "
                    "cannot open the delegated Gmail service"
                )
            self._service = factory(self.service_account_email, self.mailbox)
        return self._service

    def fetch_notices(self) -> list[EmailNotice]:
        service = self._service_client()
        listed = (
            service.users()
            .messages()
            .list(userId="me", q=self.query, maxResults=self.max_results)
            .execute()
            .get("messages", [])
        )
        notices: list[EmailNotice] = []
        for item in listed:
            message_id = str(item.get("id") or "")
            if not message_id:
                continue
            full = (
                service.users()
                .messages()
                .get(userId="me", id=message_id, format="full")
                .execute()
            )
            headers = {
                h.get("name", "").lower(): h.get("value", "")
                for h in (full.get("payload", {}).get("headers") or [])
                if isinstance(h, dict)
            }
            notices.append(
                EmailNotice(
                    message_id=message_id,
                    subject=headers.get("subject", ""),
                    body=_gmail_text_from_payload(full.get("payload", {})),
                    internal_date=str(full.get("internalDate") or ""),
                    mailbox=self.mailbox,
                    gmail_message_id=message_id,
                )
            )
        return notices

    def mark_processed(self, message_id: str) -> None:
        self._service_client().users().messages().modify(
            userId="me",
            id=message_id,
            body={"removeLabelIds": ["UNREAD"]},
        ).execute()


# ---------------------------------------------------------------------------
# Applicant + CSR resolution (read-only, fail-closed)
# ---------------------------------------------------------------------------


@dataclass
class ApplicantResolution:
    applicant_id: str
    csr_username: str
    via: str
    policy_number: str = ""


def _first_present(row: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def _policy_rows(search_result: Any) -> list[dict[str, Any]]:
    """Pull policy rows out of the PolicyApi search envelope (shape-tolerant)."""
    data = search_result.get("data") if isinstance(search_result, dict) else search_result
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for key in ("Policies", "policies", "Results", "results", "Items", "items", "Data"):
            candidate = data.get(key)
            if isinstance(candidate, list):
                return [r for r in candidate if isinstance(r, dict)]
        return [data]
    return []


def _row_policy_number(row: dict[str, Any]) -> str:
    return _first_present(row, ("PolicyNumber", "policyNumber", "policy_number"))


def normalize_policy_number(value: str) -> str:
    """Uppercase and drop spaces. Hyphens and term suffixes stay."""
    return re.sub(r"\s+", "", str(value or "")).upper()


def strip_ezlynx_lob_suffix(value: str) -> str:
    """Drop one trailing LOB code (`` APD``) from an EZLynx policy number.

    The suffix is 2-4 uppercase letters after whitespace. Lowercase, a
    single letter, and five-or-more letters stay, so a real policy token
    is not trimmed. The Ascend notice number is not passed through here.
    """
    return _LOB_SUFFIX_RE.sub("", str(value or ""))


def normalize_ezlynx_policy_number(value: str) -> str:
    """Normalize an EZLynx policy number after dropping a trailing LOB suffix."""
    return normalize_policy_number(strip_ezlynx_lob_suffix(value))


def strip_policy_term_suffix(value: str) -> str:
    """Drop a trailing term suffix (``-00``, ``-01``, ``-1``) after normalization."""
    return _TERM_SUFFIX_RE.sub("", normalize_policy_number(value))


def _rows_matching_policy(rows: list[dict[str, Any]], policy_number: str) -> list[dict[str, Any]]:
    """Exact normalized match, else a term-suffix match on both sides.

    EZLynx rows may carry a trailing line-of-business code (`` APD``). That
    suffix is stripped on the row only, then compared to the bare Ascend
    number. An exact hit wins and stops the suffix pass, so ``ABC123-00``
    does not also collect a different ``ABC123`` row when the exact row is
    present.
    """
    target = normalize_policy_number(policy_number)
    if not target:
        return []
    exact = [
        row
        for row in rows
        if normalize_ezlynx_policy_number(_row_policy_number(row)) == target
    ]
    if exact:
        return exact
    target_base = strip_policy_term_suffix(target)
    if not target_base:
        return []
    return [
        row
        for row in rows
        if normalize_ezlynx_policy_number(_row_policy_number(row))
        and strip_policy_term_suffix(
            normalize_ezlynx_policy_number(_row_policy_number(row))
        )
        == target_base
    ]


_IDENTITY_NAME_KEYS = (
    "ApplicantName",
    "applicantName",
    "BusinessName",
    "businessName",
    "InsuredName",
    "insuredName",
    "NamedInsured",
    "Name",
    "name",
)
_IDENTITY_EMAIL_KEYS = (
    "Email",
    "email",
    "BusinessEmail",
    "businessEmail",
    "InsuredEmail",
    "insuredEmail",
)
_NOTICE_EMAIL_RE = re.compile(r"(?im)^Email\s+(\S+@\S+)\s*$")
_NOTICE_PHONE_RE = re.compile(r"(?im)^Phone\s+([+0-9().\-\s]{7,20})\s*$")
_IDENTITY_PHONE_KEYS = (
    "PhoneNumber",
    "phoneNumber",
    "Phone",
    "phone",
    "BusinessPhone",
    "businessPhone",
    "MobilePhone",
    "mobilePhone",
)
_LEGAL_SUFFIX_RE = re.compile(
    r"\b(?:incorporated|limited|company|corporation|llc|inc|corp|ltd|co)\b",
    re.IGNORECASE,
)


def notice_insured_email(body: str) -> str:
    """The ``Email`` line on an API-synthesized notice. Empty otherwise."""
    match = _NOTICE_EMAIL_RE.search(str(body or ""))
    if not match:
        return ""
    return match.group(1).strip().strip("<>")


def notice_insured_phone(body: str) -> str:
    """The ``Phone`` line on an API-synthesized notice. Empty otherwise.

    Real past-due mail has no such line. The note text never includes it:
    discussion notes refuse dialable numbers.
    """
    match = _NOTICE_PHONE_RE.search(str(body or ""))
    if not match:
        return ""
    return " ".join(match.group(1).split())


def _phone_digits(value: str) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 11 and digits.startswith("1"):
        return digits[1:]
    return digits


def insured_name_cores(value: str) -> set[str]:
    """Comparable name cores: case, punctuation, legal suffix, and DBA.

    ``Fixture Hauling, LLC`` and ``Fixture Hauling`` share a core.
    ``Smith & Sons`` matches ``Smith and Sons``. A DBA splits into cores
    so either side can match.
    """
    text = str(value or "").casefold().replace("&", " and ")
    text = re.sub(r"d\s*/\s*b\s*/\s*a\.?", " dba ", text)
    text = re.sub(r"(?<=[a-z])\.(?=[a-z])", "", text)
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    cores: set[str] = set()
    for part in re.split(r"\bdbas?\b", text):
        cleaned = " ".join(_LEGAL_SUFFIX_RE.sub(" ", part).split())
        if cleaned:
            cores.add(cleaned)
    return cores


def insured_names_match(left: str, right: str) -> bool:
    left_cores = insured_name_cores(left)
    right_cores = insured_name_cores(right)
    return bool(left_cores and right_cores and (left_cores & right_cores))


def _page_total(search_result: Any) -> int | None:
    containers = [search_result]
    if isinstance(search_result, dict):
        containers.append(search_result.get("data"))
    for container in containers:
        if not isinstance(container, dict) or container.get("totalSize") in (None, ""):
            continue
        try:
            return int(container["totalSize"])
        except (TypeError, ValueError):
            return None
    return None


def _page_is_incomplete(search_result: Any) -> bool:
    total = _page_total(search_result)
    if total is None:
        return False
    return total != len(_policy_rows(search_result))


def _search_retry(call: Callable[[], Any]) -> Any:
    """Run a PolicyApi search. An incomplete page is tried once more."""
    result = call()
    if _page_is_incomplete(result):
        result = call()
    return result


def _unique_identity(
    result: Any,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    via: str,
    label: str,
) -> tuple[ApplicantResolution | None, str]:
    """One applicant, and every returned row must be that applicant."""
    rows = _policy_rows(result)
    if not rows:
        return None, "applicant_unresolved: 0 candidate rows"
    matched = [row for row in rows if predicate(row)]
    if len(matched) != len(rows):
        return None, f"applicant_unresolved: {label} search ambiguous"
    ids: list[str] = []
    for row in matched:
        account_id = _first_present(row, _APPLICANT_ID_KEYS)
        if account_id:
            ids.append(account_id)
    unique = list(dict.fromkeys(ids))
    if len(unique) != 1:
        count = len(unique) if unique else 0
        return None, f"applicant_unresolved: {count} candidate rows"
    return (
        ApplicantResolution(
            applicant_id=unique[0],
            csr_username="",
            via=via,
            policy_number="",
        ),
        "",
    )


def _best_unresolved(reasons: list[str]) -> str:
    for reason in reasons:
        if "incomplete" in reason or "ambiguous" in reason:
            return reason
    for reason in reasons:
        if "candidate" in reason and "0 candidate" not in reason:
            return reason
    for reason in reasons:
        if reason:
            return reason
    return "applicant_unresolved: 0 candidate rows"


def _resolve_by_name_and_email(
    ezlynx_client: Any,
    insured_name: str | None,
    insured_email: str | None,
) -> tuple[ApplicantResolution | None, str]:
    """Unique name-and-email match. Name alone is never enough.

    Accept only when the search page is complete and every returned row
    matches both the insured name and the email, and those rows share one
    applicant id. An unfiltered page, a partial page, or two applicants
    is no match.
    """
    name = str(insured_name or "").strip()
    email = str(insured_email or "").strip()
    if not insured_name_cores(name) or "@" not in email:
        return None, "applicant_unresolved: 0 candidate rows"
    search = getattr(ezlynx_client, "search_applicants_by_name_and_email", None)
    if search is None:
        return None, "applicant_unresolved: 0 candidate rows"
    try:
        result = _search_retry(lambda: search(name, email))
    except Exception as exc:  # noqa: BLE001 - fail closed, keep the class
        return None, f"applicant_unresolved: name and email search failed: {type(exc).__name__}"
    if _page_is_incomplete(result):
        return None, "applicant_unresolved: name and email search incomplete"

    def _both(row: dict[str, Any]) -> bool:
        row_email = _first_present(row, _IDENTITY_EMAIL_KEYS).casefold()
        return insured_names_match(name, _first_present(row, _IDENTITY_NAME_KEYS)) and row_email == email.casefold()

    return _unique_identity(result, _both, via="name_and_email", label="name and email")


# Plain reasons for the accounting list. Filing does not use these.
# A near match is never an applicant resolution.
POLICY_OUTCOME_NO_NUMBER = "no policy number"
POLICY_OUTCOME_NOT_IN_EZLYNX = "policy not in EZLynx"
POLICY_OUTCOME_MULTIPLE = "multiple candidates"
POLICY_OUTCOME_INCOMPLETE = "incomplete search"
_POLICY_SEARCH_OUTCOME: contextvars.ContextVar[str] = contextvars.ContextVar(
    "ascend_policy_search_outcome",
    default=POLICY_OUTCOME_NO_NUMBER,
)


def current_policy_search_outcome() -> str:
    """Why the exact policy search did not choose one client. Empty if it did."""
    return _POLICY_SEARCH_OUTCOME.get()


def _resolve_by_policy_numbers(
    ezlynx_client: Any, numbers: list[str]
) -> tuple[ApplicantResolution | None, str, bool]:
    """Policy numbers first. Ambiguous results do not fall through.

    Returns ``(resolution, reason, fall_through)``. ``fall_through`` is
    true only when every number produced zero rows or stayed incomplete
    after one retry. Several rows, or applicant ids that disagree, stop
    the cascade.

    The context value records the policy step for the accounting list.
    It does not change which applicant is filed.
    """
    found: list[tuple[str, str]] = []
    saw_incomplete = False
    for number in numbers:
        try:
            result = _search_retry(
                lambda number=number: ezlynx_client.search_policy_by_number(number)
            )
        except Exception as exc:  # noqa: BLE001 - fail closed, keep the class
            _POLICY_SEARCH_OUTCOME.set(POLICY_OUTCOME_INCOMPLETE)
            return None, f"policy_search_failed: {type(exc).__name__}", False
        if _page_is_incomplete(result):
            saw_incomplete = True
            continue
        matched = _rows_matching_policy(_policy_rows(result), number)
        if len(matched) == 0:
            continue
        if len(matched) != 1:
            _POLICY_SEARCH_OUTCOME.set(POLICY_OUTCOME_MULTIPLE)
            return None, f"applicant_unresolved: {len(matched)} candidate rows", False
        account_id = _first_present(matched[0], _APPLICANT_ID_KEYS)
        if not account_id:
            _POLICY_SEARCH_OUTCOME.set(POLICY_OUTCOME_INCOMPLETE)
            return None, (
                f"applicant_unresolved: {len(matched)} candidate row lacks accountId"
            ), False
        found.append((account_id, number))
    unique = {account_id for account_id, _number in found}
    if len(unique) > 1:
        _POLICY_SEARCH_OUTCOME.set(POLICY_OUTCOME_MULTIPLE)
        return None, f"applicant_unresolved: {len(unique)} candidate rows", False
    if len(unique) == 1:
        account_id, number = found[0]
        _POLICY_SEARCH_OUTCOME.set("")
        return (
            ApplicantResolution(
                applicant_id=account_id,
                csr_username="",
                via="policy_number",
                policy_number=number,
            ),
            "",
            False,
        )
    if saw_incomplete:
        _POLICY_SEARCH_OUTCOME.set(POLICY_OUTCOME_INCOMPLETE)
    else:
        _POLICY_SEARCH_OUTCOME.set(POLICY_OUTCOME_NOT_IN_EZLYNX)
    return None, "applicant_unresolved: 0 candidate rows", True


def _resolve_named_search(
    ezlynx_client: Any,
    method_name: str,
    argument: str,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    via: str,
    label: str,
) -> tuple[ApplicantResolution | None, str]:
    search = getattr(ezlynx_client, method_name, None)
    if search is None:
        return None, "applicant_unresolved: 0 candidate rows"
    try:
        result = _search_retry(lambda: search(argument))
    except Exception as exc:  # noqa: BLE001 - this step misses; the cascade continues
        return None, f"applicant_unresolved: {label} search failed: {type(exc).__name__}"
    if _page_is_incomplete(result):
        return None, f"applicant_unresolved: {label} search incomplete"
    return _unique_identity(result, predicate, via=via, label=label)


def resolve_applicant(
    ezlynx_client: Any,
    policy_numbers: list[str],
    insured_name: str | None,
    insured_email: str | None = None,
    insured_phone: str | None = None,
) -> tuple[ApplicantResolution | None, str]:
    """Resolve ``applicant_id`` from PolicyApi rows. Never guess.

    Policy numbers come first. Compare them normalized (uppercase, spaces
    removed). An EZLynx row may also drop one trailing LOB code (`` APD``,
    2-4 uppercase letters). When that misses, strip a trailing term suffix
    (``-00`` / ``-01`` / ``-1``) on both the notice and the row. Accept
    only when exactly one row matches and it carries ``accountId`` (or
    ``ApplicantId``). Several policy numbers must agree on that one
    applicant. An ambiguous policy result does not fall through.

    Zero policy hits, or a page that is still incomplete after one retry,
    continue to the insured name (normalized: legal suffix, punctuation,
    ``&`` versus ``and``, DBA), then email, then phone. Each of those needs
    one unique applicant and a complete page whose every row matches.
    Name plus email together remains a last step for callers that only
    implement that search.

    PolicyApi rows have no CSR field. This function does not return one.
    """
    numbers = [str(item or "").strip() for item in policy_numbers if str(item or "").strip()]
    _POLICY_SEARCH_OUTCOME.set(POLICY_OUTCOME_NO_NUMBER)
    if numbers:
        resolution, reason, fall_through = _resolve_by_policy_numbers(ezlynx_client, numbers)
        if resolution is not None or not fall_through:
            return resolution, reason
    reasons: list[str] = []
    name = str(insured_name or "").strip()
    email = str(insured_email or "").strip()
    phone = str(insured_phone or "").strip()
    steps: list[Callable[[], tuple[ApplicantResolution | None, str]]] = []
    if insured_name_cores(name):
        steps.append(
            lambda: _resolve_named_search(
                ezlynx_client,
                "search_applicants_by_name",
                name,
                lambda row: insured_names_match(name, _first_present(row, _IDENTITY_NAME_KEYS)),
                via="insured_name",
                label="name",
            )
        )
    if "@" in email:
        steps.append(
            lambda: _resolve_named_search(
                ezlynx_client,
                "search_applicants_by_email",
                email,
                lambda row: _first_present(row, _IDENTITY_EMAIL_KEYS).casefold() == email.casefold(),
                via="email",
                label="email",
            )
        )
    if len(_phone_digits(phone)) >= 10:
        wanted = _phone_digits(phone)
        steps.append(
            lambda: _resolve_named_search(
                ezlynx_client,
                "search_applicants_by_phone",
                phone,
                lambda row, wanted=wanted: any(
                    _phone_digits(str(row.get(key) or "")) == wanted
                    for key in _IDENTITY_PHONE_KEYS
                ),
                via="phone",
                label="phone",
            )
        )
    if insured_name_cores(name) and "@" in email:
        steps.append(lambda: _resolve_by_name_and_email(ezlynx_client, name, email))
    for step in steps:
        resolution, reason = step()
        if resolution is not None:
            return resolution, ""
        if reason:
            reasons.append(reason)
    return None, _best_unresolved(reasons)


def _person_record(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _person_email(person: dict[str, Any]) -> str:
    raw = str(person.get("email") or "").strip()
    match = re.search(r"<([^>]+)>", raw)
    if match:
        raw = match.group(1).strip()
    return raw


def _email_local_part(email: str) -> str:
    if "@" not in email:
        return ""
    return email.split("@", 1)[0].strip().casefold()


def _person_is_missing(person: dict[str, Any] | None) -> bool:
    if not person:
        return True
    return not (
        _person_email(person)
        or str(person.get("first_name") or "").strip()
        or str(person.get("last_name") or "").strip()
    )


def _normalized_name(value: str) -> str:
    return " ".join(str(value or "").casefold().split())


def resolve_cancellation_csr(program: dict[str, Any] | None) -> tuple[str, str]:
    """EZLynx login for a cancellation task. Fail closed. Never invent one.

    CSR is the Ascend program producer. When the producer record is
    missing, fall back to ``account_manager``. Map ``email`` /
    ``first_name`` / ``last_name`` through ``requester_login``. Shared
    mailboxes (hello@, accounting@, robie@) and unmapped names return
    ``csr_unresolved``.
    """
    from .confirmation_notify import requester_login

    record = program if isinstance(program, dict) else {}
    producer = _person_record(record.get("producer"))
    manager = _person_record(record.get("account_manager"))
    if _person_is_missing(producer):
        person = manager
        source = "account_manager"
    else:
        person = producer
        source = "producer"
    if _person_is_missing(person) or person is None:
        return "", "csr_unresolved: program has no producer or account_manager"
    email = _person_email(person)
    local = _email_local_part(email)
    if local in _SHARED_CSR_LOCAL_PARTS:
        return "", f"csr_unresolved: shared mailbox {email}"
    first = str(person.get("first_name") or "").strip()
    last = str(person.get("last_name") or "").strip()
    candidates: list[str] = []
    if first and last:
        candidates.append(f"{first} {last}")
    if first:
        candidates.append(first)
    if local:
        spaced = local.replace(".", " ")
        candidates.append(spaced)
        if spaced != local:
            candidates.append(local)
    non_shared = False
    for name in candidates:
        if _normalized_name(name) in _SHARED_CSR_NAMES:
            continue
        non_shared = True
        login = requester_login(name)
        if login and _LOGIN_USERNAME_RE.fullmatch(str(login)):
            return str(login), ""
    if not non_shared:
        return "", "csr_unresolved: shared mailbox"
    return "", f"csr_unresolved: unmapped {source}"


# ---------------------------------------------------------------------------
# Per-notice pipeline
# ---------------------------------------------------------------------------


@dataclass
class DriverContext:
    ascend_client: AscendApiClient
    ezlynx_client: Any  # EzlynxApiClient (PolicyApi reads)
    discussion_client: DiscussionApiClient
    source: NoticeSource
    dry_run: bool = True
    due_days: int = DEFAULT_DUE_DAYS
    today: date = field(default_factory=date.today)
    # In-run collapse. Cleared at the start of each run_driver call.
    seen_notice_events: list[dict[str, Any]] = field(default_factory=list)
    # (applicant id, canonical category title) -> discussion id, or a
    # planned-create token when this run has not created it yet.
    planned_category_discussions: dict[tuple[str, str], str] = field(default_factory=dict)


@dataclass
class NoticeResult:
    message_id: str
    subject: str
    status: str  # done | dry_run | skipped | ignored
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


def _due_date(today: date, due_days: int) -> str:
    return (today + timedelta(days=max(int(due_days), 0))).isoformat()


# One discussion per applicant per category. Titles are exact when created.
# An existing row matches after strip and case-fold.
CATEGORY_PAYMENTS = "payments"
CATEGORY_CANCELLATION_NOTICES = "cancellation_notices"
CATEGORY_RETURN_PREMIUM = "return_premium"
CATEGORY_UNDERWRITING = "underwriting"

CATEGORY_TITLES: dict[str, str] = {
    CATEGORY_PAYMENTS: "Ascend - Payments",
    CATEGORY_CANCELLATION_NOTICES: "Ascend - Cancellation Notices",
    CATEGORY_RETURN_PREMIUM: "Ascend - Return Premium",
    CATEGORY_UNDERWRITING: "Ascend - Underwriting",
}
_CATEGORY_TITLE_KEYS = {title.casefold(): title for title in CATEGORY_TITLES.values()}

DISCUSSION_PLAN_USE_EXISTING = "use_existing"
DISCUSSION_PLAN_CREATE = "create"
# A later notice in this run, when the discussion was planned and not read
# back from EZLynx. Dry-run must not call that a use of an existing row.
DISCUSSION_PLAN_CREATE_PLANNED = "create (planned earlier this run)"

NOTICE_CATEGORY: dict[str, str] = {
    triage.LATE_PAYMENT: CATEGORY_PAYMENTS,
    triage.PAYMENT_CONFIRMATION: CATEGORY_PAYMENTS,
    triage.PROCESSING_PAYMENT: CATEGORY_PAYMENTS,
    triage.PAID_OFF: CATEGORY_PAYMENTS,
    triage.DISPUTED_CHARGE: CATEGORY_PAYMENTS,
    triage.INTENT_TO_CANCEL: CATEGORY_CANCELLATION_NOTICES,
    triage.CANCELLATION: CATEGORY_CANCELLATION_NOTICES,
    triage.REINSTATEMENT: CATEGORY_CANCELLATION_NOTICES,
    triage.RETURN_PREMIUM: CATEGORY_RETURN_PREMIUM,
    triage.UNDERWRITING: CATEGORY_UNDERWRITING,
}

TASK_KIND_CANCELLATION = "cancellation"
TASK_KIND_DISPUTED_CHARGE = "disputed_charge"
TASK_KIND_INTENT_TO_CANCEL = "intent_to_cancel"
_ALLOWED_TASK_KINDS = frozenset(
    {TASK_KIND_CANCELLATION, TASK_KIND_DISPUTED_CHARGE, TASK_KIND_INTENT_TO_CANCEL}
)

ACCOUNTING_ASSIGNEE_UNKNOWN = "accounting assignee id unknown"
NO_MATCHING_CATEGORY = "no matching category"
INTENT_CSR_TASK_ENV = "ROBIE_ASCEND_INTENT_TO_CANCEL_CSR_TASK"

# Markley1 is copied from streetsmart-insurance/streetsmart-phone-watchdog
# src/ezlynx/ezlynx_users.py at main eca8cba27574021b7b0e924b9cf389d720cce745
# (Accounting Team / accounting@streetsmart.insurance; that directory entry
# had no ezlynx_user_id). The id is from live Prod evidence:
# Discussion API task notes use assignedUserId 263046 — discussion 849801097
# (Jake created an "Accounting Agency Bill Verification" task assigned to
# 263046) and discussion 849685947 (created by and assigned to 263046).
# The 2026-10-02 EZLynx open-task list shows 38 tasks with
# taskAssignment {userId: 263046, name: "Accounting Team"}. Markley1 is
# Accounting Team in both repos. A ROBIE_ACCOUNTING_ASSIGNEE login with no
# known id still fails closed (accounting assignee id unknown).
ACCOUNTING_EZLYNX_USER: dict[str, Any] = {
    "user_name": "Markley1",
    "first_name": "Accounting",
    "last_name": "Team",
    "email": "accounting@streetsmart.insurance",
    "role": "Accounting / Financial Operations",
    "ezlynx_user_id": 263046,
    "source": (
        "streetsmart-insurance/streetsmart-phone-watchdog "
        "src/ezlynx/ezlynx_users.py eca8cba27574021b7b0e924b9cf389d720cce745; "
        "ezlynx_user_id 263046 from Prod Discussion API discussions "
        "849801097 and 849685947 and the 2026-10-02 EZLynx open-task list "
        "(38 tasks, taskAssignment userId 263046, name Accounting Team)"
    ),
}


def category_for(notice_type: str) -> str:
    """Category key for a notice type. Empty when this driver does not file it."""
    return NOTICE_CATEGORY.get(str(notice_type or "").strip(), "")


def category_title(category: str) -> str:
    """Canonical discussion title for a category. Empty when unknown."""
    return CATEGORY_TITLES.get(str(category or "").strip(), "")


def canonical_category_title(title: str) -> str:
    """Canonical title when ``title`` is one of the four, ignoring case and ends."""
    return _CATEGORY_TITLE_KEYS.get(str(title or "").strip().casefold(), "")


def _planned_discussion_token(applicant_id: str, title: str) -> str:
    return f"planned:{applicant_id}:{title}"


def intent_to_cancel_csr_task_enabled() -> bool:
    """Off unless ``ROBIE_ASCEND_INTENT_TO_CANCEL_CSR_TASK`` is truthy.

    Off: intent-to-cancel is a note only. On: also create the CSR task.
    """
    return _truthy(os.environ.get(INTENT_CSR_TASK_ENV))


def ezlynx_user_id_for_login(username: str) -> int | None:
    """Confirmed numeric id, same rule as phone-watchdog ``ezlynx_user_id_for``.

    Match is the accounting login only. A different login, or a missing
    or non-positive ``ezlynx_user_id``, returns None. Markley1's id is
    263046 from the Prod task evidence cited on ``ACCOUNTING_EZLYNX_USER``.
    """
    key = str(username or "").strip().casefold()
    stored = str(ACCOUNTING_EZLYNX_USER.get("user_name") or "").strip().casefold()
    if not key or key != stored:
        return None
    raw = ACCOUNTING_EZLYNX_USER.get("ezlynx_user_id")
    if isinstance(raw, bool) or raw is None or raw == "":
        return None
    try:
        user_id = int(raw)
    except (TypeError, ValueError):
        return None
    if user_id <= 0:
        return None
    return user_id


def accounting_assigned_user_id() -> tuple[int | None, str]:
    """Numeric EZLynx user id for the disputed-charge task.

    The login comes from ``ROBIE_ACCOUNTING_ASSIGNEE`` (default Markley1,
    the same default ``ascend_sync`` uses; Production sets that variable).
    ``requester_login`` maps "accounting team" and "markley" to Markley1.
    The id is looked up the way the phone watchdog looks up
    ``ezlynx_user_id``. No id means ``accounting assignee id unknown``.
    """
    from .confirmation_notify import requester_login

    raw = os.environ.get("ROBIE_ACCOUNTING_ASSIGNEE", "Markley1")
    text = " ".join(str(raw or "").split())
    if not text:
        return None, ""
    mapped = requester_login(text)
    login = mapped or (text if _LOGIN_USERNAME_RE.fullmatch(text) else "")
    if not login or not _LOGIN_USERNAME_RE.fullmatch(str(login)):
        return None, ""
    return ezlynx_user_id_for_login(str(login)), str(login)


def build_disputed_task_note(
    *,
    assigned_user_id: int,
    title: str,
    description: str,
    due: str,
) -> dict[str, Any]:
    """TaskCreationNote body from the phone watchdog's direct task API.

    Same shape as ``build_task_note`` in streetsmart-phone-watchdog
    ``src/ezlynx/direct_task_api.py`` at eca8cba: ``assignedUserId`` is an
    integer inside ``task``. A ``/notes`` body does not send applicantId.
    ``due`` is the ISO date the cancellation task already uses; the phone
    watchdog converts that date to 10:00 PM America/New_York as UTC Z.
    """
    from datetime import timezone
    from zoneinfo import ZoneInfo

    user_id = int(assigned_user_id)
    if user_id <= 0:
        raise ValueError("assigned_user_id must be a positive integer")
    day = zapier_tasks.validate_due_date(due)
    local = datetime.strptime(day, "%Y-%m-%d").replace(
        hour=22, minute=0, second=0, microsecond=0, tzinfo=ZoneInfo("America/New_York")
    )
    due_utc = local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    text_parts = [part.strip() for part in (title, description) if str(part or "").strip()]
    return {
        "type": "TaskCreationNote",
        "body": "\n\n".join(text_parts),
        "task": {
            "due": due_utc,
            "assignedUserId": user_id,
            "reminders": [
                {
                    "scheduled": due_utc,
                    "remindees": {"myself": False, "assignee": True, "followers": False},
                    "types": {"email": False, "text": False, "notification": True},
                }
            ],
        },
    }


def due_or_cancel_dates(body: str) -> frozenset[str]:
    """Due and cancel dates in an Ascend notice. Policy effective dates stay out.

    ``Effective date 08/21/2026`` is the policy term, not the event. A
    payment due date and a cancel effective date are the event.
    """
    text = str(body or "")
    found: set[str] = set()
    for match in _NOTICE_DATE_RE.finditer(text):
        prefix = text[max(0, match.start() - 40) : match.start()]
        if _EFFECTIVE_DATE_PREFIX_RE.search(prefix):
            continue
        found.add(match.group(1))
    return frozenset(found)


def normalize_notice_body(body: str) -> str:
    """Case-folded email body with collapsed whitespace. Not an EZLynx read."""
    cleaned = _ZERO_WIDTH_RE.sub("", str(body or ""))
    return " ".join(cleaned.casefold().split())


def notice_event_key(
    *,
    applicant_id: str,
    policy_number: str,
    notice_type: str,
    body: str,
    note_text: str = "",
) -> dict[str, Any]:
    """Identity for collapsing repeated notices inside one driver run."""
    return {
        "applicant_id": str(applicant_id or "").strip(),
        "policy": strip_policy_term_suffix(policy_number),
        "notice_type": str(notice_type or "").strip(),
        "dates": tuple(sorted(due_or_cancel_dates(body))),
        "body": normalize_notice_body(body),
        "note_text": str(note_text or "").strip(),
        "discussion_id": "",
    }


def same_notice_event(kept: dict[str, Any], candidate: dict[str, Any]) -> bool:
    """True when two notices are one filing event.

    Same applicant, policy, and notice type, plus the same due/cancel
    date or the same normalized body. A date match requires both sides
    to have extracted a date, so two undated different bodies stay apart.
    The same rendered note on one discussion for one applicant is also
    one event, even when the emails have no dates.
    """
    if str(kept.get("applicant_id") or "") != str(candidate.get("applicant_id") or ""):
        return False
    kept_discussion = str(kept.get("discussion_id") or "")
    candidate_discussion = str(candidate.get("discussion_id") or "")
    kept_note = str(kept.get("note_text") or "")
    candidate_note = str(candidate.get("note_text") or "")
    if (
        kept_discussion
        and kept_discussion == candidate_discussion
        and kept_note
        and kept_note == candidate_note
    ):
        return True
    if str(kept.get("policy") or "") != str(candidate.get("policy") or ""):
        return False
    if str(kept.get("notice_type") or "") != str(candidate.get("notice_type") or ""):
        return False
    kept_dates = tuple(kept.get("dates") or ())
    candidate_dates = tuple(candidate.get("dates") or ())
    if kept_dates and candidate_dates and kept_dates == candidate_dates:
        return True
    kept_body = str(kept.get("body") or "")
    candidate_body = str(candidate.get("body") or "")
    return bool(kept_body) and kept_body == candidate_body


def _note_id_from(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    for key in ("noteId", "NoteId", "id", "Id"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


_DISCUSSION_APPLICANT_KEYS = (
    "applicantId",
    "ApplicantId",
    "ApplicantID",
    "applicant_id",
)


def _row_applicant_id(row: dict[str, Any]) -> str:
    for key in _DISCUSSION_APPLICANT_KEYS:
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def discussions_for_applicant(
    rows: list[dict[str, Any]] | None, applicant_id: str
) -> list[dict[str, Any]]:
    """Drop rows stamped with a different applicant id.

    The list request is already ``v8/discussions/by-applicant?applicantId=``,
    the same query the certificate, evidence, and policy-change readers use.
    None of those readers send a page index. Rows with no applicant id stay,
    because that payload shape is already the applicant's list. A row stamped
    with another applicant is not eligible.
    """
    wanted = str(applicant_id or "").strip()
    records = [row for row in (rows or []) if isinstance(row, dict)]
    if not wanted:
        return records
    return [
        row
        for row in records
        if not _row_applicant_id(row) or _row_applicant_id(row) == wanted
    ]


def _discussion_recency(row: dict[str, Any]) -> tuple[int, float, str]:
    """Newest updated discussion sorts last. Undated rows sort first."""
    raw = discussions._discussion_stamp(row)
    parsed: datetime | None = None
    if raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            for fmt in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y"):
                try:
                    parsed = datetime.strptime(raw, fmt)
                    break
                except ValueError:
                    continue
    discussion_id = discussions.discussion_id_of(row)
    if parsed is not None:
        return (2, parsed.timestamp(), discussion_id)
    if raw:
        return (1, 0.0, raw + discussion_id)
    return (0, 0.0, discussion_id)


def choose_category_discussion(
    rows: list[dict[str, Any]] | None,
    title: str,
) -> tuple[dict[str, Any] | None, int]:
    """The newest discussion whose title is exactly this category.

    Comparison is strip plus case-fold. Untitled rows never match. Several
    rows with the same title: the newest, and the count is greater than one
    so the caller can log it. No match returns ``(None, 0)``.
    """
    wanted = str(title or "").strip().casefold()
    if not wanted or wanted == "untitled":
        return None, 0
    hits = [
        row
        for row in (rows or [])
        if isinstance(row, dict)
        and discussions.discussion_id_of(row)
        and not discussions.is_untitled_discussion(row)
        and discussions.discussion_title_of(row).strip().casefold() == wanted
    ]
    if not hits:
        return None, 0
    return max(hits, key=_discussion_recency), len(hits)


def _list_category_discussion(
    client: Any,
    applicant_id: str,
    title: str,
) -> tuple[dict[str, Any] | None, int]:
    """Read this applicant's discussions and pick the exact category title.

    Several rows with that title log a warning and keep the newest. A
    transport or API error propagates so the caller can fail closed
    instead of creating another discussion.
    """
    listed = client.get_discussions(applicant_id)
    chosen, match_count = choose_category_discussion(
        discussions_for_applicant(listed, applicant_id),
        title,
    )
    if chosen is not None and match_count > 1:
        logger.warning(
            "applicant %s has %d discussions titled %r; using the newest %s",
            applicant_id,
            match_count,
            discussions.discussion_title_of(chosen) or title,
            discussions.discussion_id_of(chosen),
        )
    return chosen, match_count


def _read_existing_note(
    client: Any,
    applicant_id: str,
    note_body: str,
    *,
    discussion_id: str,
) -> dict[str, Any]:
    """Read the chosen discussion and compare note text. No write, no driver gate.

    Write-scope is not checked here. Dry-run uses this for applicants the
    allowlist blocks so a reviewer can see an existing duplicate.
    """
    del applicant_id
    outcome = {"read": False, "duplicate": False, "note_id": "", "reason": ""}
    record = {"discussionId": discussion_id}
    if not discussion_id:
        outcome["reason"] = "selected discussion has no id"
        return outcome
    detail: Any = None
    getter = getattr(client, "get_discussion", None)
    if callable(getter):
        try:
            detail = getter(discussion_id)
            outcome["read"] = True
        except Exception as exc:  # noqa: BLE001 - report the miss, do not write
            outcome["reason"] = f"{type(exc).__name__}: {exc}"
    else:
        detail = record
        outcome["read"] = True
    matched = discussions.find_identical_note(detail, note_body) if detail is not None else None
    if matched is None:
        matched = discussions.find_identical_note(record, note_body)
        if matched is not None:
            outcome["read"] = True
    if matched is None:
        return outcome
    outcome["duplicate"] = True
    outcome["note_id"] = _note_id_from(matched)
    outcome["read"] = True
    return outcome


def signed_notice_note(note_text: str) -> str:
    """Append the agency signature without inventing LOB or bind steps."""
    text = str(note_text or "").strip()
    if not text:
        return ""
    if ROBIE_WAS_HERE.casefold() in text.casefold():
        return text
    return f"{text}\n\n{ROBIE_WAS_HERE}"


def _prepare_dry_run_note(
    client: DiscussionApiClient,
    applicant_id: str,
    note_body: str,
    *,
    discussion_id: str,
) -> dict[str, Any]:
    """Validate the note the live path would file. No driver gate, no POST.

    Write-scope is checked with ``applicant_is_write_allowed`` so a
    disallowed applicant still fails closed. ``require_allowed_ezlynx_write_applicant``
    is not used: that helper always calls ``driver_gate_for_write``.
    """
    from .chat_write_boundary import assert_chat_applicant
    from .ezlynx_write_scope import normalize_applicant_id

    applicant = normalize_applicant_id(applicant_id)
    refusal = _write_scope_refusal_reason(applicant)
    if refusal:
        raise EzlynxWriteScopeError(refusal.removeprefix("write_scope_refused: "))
    assert_chat_applicant(applicant)
    text = discussions.reject_phone_numbers(note_body).strip()
    if not text:
        raise discussions.DiscussionApiError(None, "note body is required")
    rows = discussions_for_applicant(client.get_discussions(applicant), applicant)
    pinned = str(discussion_id or "").strip()
    titled = [
        row
        for row in rows
        if discussions.discussion_id_of(row) == pinned
        and not discussions.is_untitled_discussion(row)
    ]
    if len(titled) != 1:
        return {
            "status": "pending",
            "reason_code": "no matching discussion",
            "reason": "no matching discussion",
            "applicant_id": applicant,
            "discussion_id": None,
            "note_id": None,
        }
    record = titled[0]
    discussion_id = discussions.discussion_id_of(record)
    if not discussion_id:
        return {
            "status": "pending",
            "reason_code": discussions.AMBIGUOUS_DISCUSSIONS,
            "reason": "selected discussion has no usable id; refusing to write",
            "applicant_id": applicant,
            "discussion_id": None,
            "note_id": None,
        }
    return {
        "status": "dry_run",
        "reason_code": None,
        "reason": "dry run: note validated, nothing written",
        "applicant_id": applicant,
        "discussion_id": discussion_id,
        "discussion_title": discussions.discussion_title_of(record),
        "note_id": None,
    }


def _prepare_dry_run_create(
    applicant_id: str,
    title: str,
    note_body: str,
) -> dict[str, Any]:
    """Validate a category discussion create. No POST and no driver gate.

    ``create_discussion_with_note`` always takes the write gate, including
    its own dry-run branch. Dry-run uses ``build_with_note_payload`` only.
    """
    from .chat_write_boundary import assert_chat_applicant
    from .ezlynx_write_scope import normalize_applicant_id

    applicant = normalize_applicant_id(applicant_id)
    refusal = _write_scope_refusal_reason(applicant)
    if refusal:
        raise EzlynxWriteScopeError(refusal.removeprefix("write_scope_refused: "))
    assert_chat_applicant(applicant)
    payload = discussions.build_with_note_payload(
        applicant,
        title,
        discussions.reject_phone_numbers(note_body),
    )
    return {
        "status": "dry_run",
        "reason_code": None,
        "reason": "dry run: create payload validated, nothing written",
        "applicant_id": applicant,
        "discussion_id": None,
        "discussion_title": payload["title"],
        "note_id": None,
    }


def authorize_notice_write(
    action: str,
    *,
    discussion_id: str = "",
    discussion_title: str = "",
    task_kind: str = "",
) -> None:
    """Allow a category note, a category create-with-note, or one task kind.

    The EZLynx write-scope guard decides which applicant may be written. It
    does not accept an action. This check is the driver's own limit.
    """
    name = str(action or "").strip()
    title = str(discussion_title or "").strip()
    if name == "discussion_note":
        if not str(discussion_id or "").strip():
            raise NonNoteWriteRefused(
                "non_note_write_refused: discussion_note requires an existing discussion"
            )
        if not canonical_category_title(title):
            raise NonNoteWriteRefused(
                "non_note_write_refused: discussion_note requires a category discussion"
            )
        return
    if name == "discussion_create_with_note":
        if title not in CATEGORY_TITLES.values():
            raise NonNoteWriteRefused(
                "non_note_write_refused: discussion_create_with_note requires a category title"
            )
        return
    if name == "task_create":
        kind = str(task_kind or "").strip()
        if kind not in _ALLOWED_TASK_KINDS:
            raise NonNoteWriteRefused(
                f"non_note_write_refused: task_create {kind or 'missing'}"
            )
        return
    raise NonNoteWriteRefused(f"non_note_write_refused: {name or 'missing'}")


def _write_scope_refusal_reason(applicant_id: str) -> str:
    """Empty when the applicant may be written. Does not take the driver lease.

    Same refusal text as ``require_allowed_ezlynx_write_applicant``, without
    ``driver_gate_for_write``. Dry-run and the pre-label check both use this.
    """
    from .ezlynx_write_scope import (
        EZLYNX_WRITE_SCOPE_REFUSED,
        applicant_is_write_allowed,
        normalize_applicant_id,
    )

    applicant = normalize_applicant_id(applicant_id)
    if applicant_is_write_allowed(applicant):
        return ""
    display = applicant or "<missing>"
    return (
        f"write_scope_refused: {EZLYNX_WRITE_SCOPE_REFUSED}: applicant {display} "
        "is not on the EZLynx business-write allowlist"
    )


def process_notice(notice: EmailNotice, ctx: DriverContext) -> NoticeResult:
    """Run one email through triage -> note -> task. Never raises."""
    try:
        return _process_notice(notice, ctx)
    except Exception as exc:  # noqa: BLE001 - fail closed per email
        logger.warning("notice %s failed closed: %s", notice.message_id, type(exc).__name__)
        return NoticeResult(
            message_id=notice.message_id,
            subject=notice.subject,
            status="skipped",
            reason=f"error: {type(exc).__name__}: {exc}",
        )


def _process_notice(notice: EmailNotice, ctx: DriverContext) -> NoticeResult:
    result = NoticeResult(message_id=notice.message_id, subject=notice.subject, status="skipped")

    triaged = triage.triage_notice(ctx.ascend_client, notice.subject, notice.body)
    notice_type = str(triaged.get("notice_type") or "")
    result.detail["notice_type"] = notice_type
    # Program id stays out of the EZLynx note. It is logged and kept on the
    # job summary so a reviewer can still trace the Ascend program.
    program_uuid = str(triaged.get("program_uuid") or "").strip()
    if program_uuid:
        result.detail["program_uuid"] = program_uuid
        logger.info(
            "ascend notice %s type %s program %s",
            notice.message_id,
            notice_type or "unclassified",
            program_uuid,
        )
    if notice_type in triage.IGNORE_TYPES or triaged.get("ignored"):
        result.status = "ignored"
        result.reason = "ignored"
        return result
    if triaged.get("needs_human_review"):
        result.reason = (
            "needs_human_review: " + str(triaged.get("review_reason") or "triage flagged")
        )
        return result
    if notice_type == triage.UNKNOWN:
        result.reason = "unknown_notice_type"
        return result

    # The lease gate runs before the API-store lookup. A held lease refuses
    # the notice even when the live store already has this key, and a missing
    # store must not hide the refusal. Dry-run does not call the gate.
    if not ctx.dry_run:
        gate_reason = _driver_gate_refusal()
        if gate_reason:
            result.reason = gate_reason
            return result

    # Skip only when the live store has this key filed. A missing, empty,
    # or unreadable store means email files. The poller's own api: notice
    # is the writer and does not consult this hook.
    covered_key = _api_source_already_filed(notice, notice_type, program_uuid)
    if covered_key:
        result.reason = "api_already_filed"
        result.detail["event_key"] = covered_key
        result.detail["duplicate_source"] = "ascend_api"
        return result

    category = category_for(notice_type)
    canonical = category_title(category)
    if category:
        result.detail["category"] = category
        result.detail["discussion_title"] = canonical

    if not category:
        result.reason = NO_MATCHING_CATEGORY
        result.detail["needs_human_review"] = True
        return result
    else:
        resolution, reason = resolve_applicant(
            ctx.ezlynx_client,
            [str(p) for p in (triaged.get("policy_numbers") or [])],
            triaged.get("insured_name"),
            notice_insured_email(notice.body),
            notice_insured_phone(notice.body),
        )
        if resolution is None:
            result.reason = reason
            outcome = current_policy_search_outcome()
            if outcome:
                result.detail["unmatched_reason"] = outcome
            result.detail["needs_human_review"] = True
            return result
    needs_csr_task = notice_type == triage.CANCELLATION or (
        notice_type == triage.INTENT_TO_CANCEL and intent_to_cancel_csr_task_enabled()
    )
    if needs_csr_task:
        csr_login, csr_reason = resolve_cancellation_csr(triaged.get("program"))
        if not csr_login:
            result.reason = csr_reason
            result.detail["applicant_id"] = resolution.applicant_id
            result.detail["policy_number"] = resolution.policy_number
            return result
        resolution = ApplicantResolution(
            applicant_id=resolution.applicant_id,
            csr_username=csr_login,
            via=resolution.via,
            policy_number=resolution.policy_number,
        )
    result.detail["applicant_id"] = resolution.applicant_id
    result.detail["policy_number"] = resolution.policy_number
    if resolution.csr_username:
        result.detail["csr_username"] = resolution.csr_username
    result.detail["notice_type"] = notice_type

    if notice_type == triage.CANCELLATION:
        result.detail["task"] = {
            "type": TASK_KIND_CANCELLATION,
            "assignee": resolution.csr_username,
        }
    elif (
        notice_type == triage.INTENT_TO_CANCEL and intent_to_cancel_csr_task_enabled()
    ):
        result.detail["task"] = {
            "type": TASK_KIND_INTENT_TO_CANCEL,
            "assignee": resolution.csr_username,
        }
    elif notice_type == triage.DISPUTED_CHARGE:
        user_id, login = accounting_assigned_user_id()
        if user_id is None:
            result.reason = ACCOUNTING_ASSIGNEE_UNKNOWN
            result.detail["needs_human_review"] = True
            if login:
                result.detail["accounting_login"] = login
            return result
        result.detail["task"] = {
            "type": TASK_KIND_DISPUTED_CHARGE,
            "assignee": login,
            "assigned_user_id": user_id,
        }

    note_text = signed_notice_note(str(triaged.get("note_text") or "").strip())
    if not note_text:
        result.reason = "empty_note_text"
        return result
    try:
        discussions.reject_phone_numbers(note_text)
    except discussions.DiscussionApiError as exc:
        result.reason = f"discussion_error: {exc}"
        return result

    # In-run collapse uses the mail we already have. It does not read EZLynx.
    event = notice_event_key(
        applicant_id=resolution.applicant_id,
        policy_number=resolution.policy_number,
        notice_type=notice_type,
        body=notice.body,
        note_text=note_text,
    )
    if any(same_notice_event(kept, event) for kept in ctx.seen_notice_events):
        result.reason = "duplicate_in_run"
        result.detail["duplicate_in_run"] = True
        return result
    ctx.seen_notice_events.append(event)

    # One category discussion per applicant. Reuse it when the title matches
    # (trim, case-insensitive). Otherwise this run creates that title once.
    # A token from an earlier plan is not an EZLynx discussion: dry-run says
    # so, and live mode re-reads by title before it creates a second one.
    plan_key = (resolution.applicant_id, canonical)
    remembered = ctx.planned_category_discussions.get(plan_key)
    chosen_id = ""
    display_title = canonical
    if remembered is not None and str(remembered).startswith("planned:"):
        discussion_plan = DISCUSSION_PLAN_CREATE_PLANNED
        if not ctx.dry_run:
            try:
                chosen, _match_count = _list_category_discussion(
                    ctx.discussion_client, resolution.applicant_id, canonical
                )
            except Exception as exc:  # noqa: BLE001 - fail closed, do not write
                result.reason = f"discussion_error: {type(exc).__name__}: {exc}"
                return result
            if chosen is not None:
                discussion_plan = DISCUSSION_PLAN_USE_EXISTING
                chosen_id = discussions.discussion_id_of(chosen)
                display_title = discussions.discussion_title_of(chosen) or canonical
                ctx.planned_category_discussions[plan_key] = chosen_id
    elif remembered is not None:
        discussion_plan = DISCUSSION_PLAN_USE_EXISTING
        chosen_id = str(remembered)
    else:
        try:
            chosen, _match_count = _list_category_discussion(
                ctx.discussion_client, resolution.applicant_id, canonical
            )
        except Exception as exc:  # noqa: BLE001 - fail closed, do not write
            result.reason = f"discussion_error: {type(exc).__name__}: {exc}"
            return result
        if chosen is not None:
            discussion_plan = DISCUSSION_PLAN_USE_EXISTING
            chosen_id = discussions.discussion_id_of(chosen)
            display_title = discussions.discussion_title_of(chosen) or canonical
            ctx.planned_category_discussions[plan_key] = chosen_id
        else:
            discussion_plan = DISCUSSION_PLAN_CREATE
            ctx.planned_category_discussions[plan_key] = _planned_discussion_token(
                resolution.applicant_id, canonical
            )
    result.detail["discussion_plan"] = discussion_plan
    result.detail["discussion_title"] = display_title
    if chosen_id:
        result.detail["discussion_id"] = chosen_id
    event["discussion_id"] = chosen_id or ctx.planned_category_discussions[plan_key]
    # Same rendered note, same applicant, same discussion: one filing.
    # Undated emails with different bodies still collapse here.
    if any(
        kept is not event and same_notice_event(kept, event)
        for kept in ctx.seen_notice_events
    ):
        result.reason = "duplicate_in_run"
        result.detail["duplicate_in_run"] = True
        return result

    # Existing-note check is a read inside the category discussion, including
    # when write scope will refuse. A create has no notes to compare.
    if chosen_id:
        note_match = _read_existing_note(
            ctx.discussion_client,
            resolution.applicant_id,
            note_text,
            discussion_id=chosen_id,
        )
    else:
        note_match = {"read": False, "duplicate": False, "note_id": "", "reason": ""}
    result.detail["existing_note_read"] = bool(note_match.get("read"))
    result.detail["existing_note_duplicate"] = bool(note_match.get("duplicate"))
    if note_match.get("note_id"):
        result.detail["existing_note_id"] = note_match["note_id"]
    if note_match.get("reason") and not note_match.get("read"):
        result.detail["existing_note_read_reason"] = note_match["reason"]

    # Write scope before filing. A narrow allowlist must not hide a real match.
    scope_refusal = _write_scope_refusal_reason(resolution.applicant_id)
    if scope_refusal:
        result.reason = scope_refusal
        return result
    if note_match.get("duplicate"):
        note_id = str(note_match.get("note_id") or "").strip()
        result.reason = (
            f"existing_note_duplicate: {note_id}" if note_id else "existing_note_duplicate"
        )
        return result

    # Cancellations are notes only. Do not list or apply Ascend NOC.
    # That label sends client email and text, and Robie does not send those.
    # Intent-to-cancel and reinstatement are notes, never a cancellation task.
    # Dry-run does not POST and does not call driver_gate_for_write.
    try:
        if chosen_id:
            authorize_notice_write(
                "discussion_note",
                discussion_id=chosen_id,
                discussion_title=display_title,
            )
            if ctx.dry_run:
                filed = _prepare_dry_run_note(
                    ctx.discussion_client,
                    resolution.applicant_id,
                    note_text,
                    discussion_id=chosen_id,
                )
            else:
                from .ezlynx_driver_gate import EzlynxDriverGateRefused
                from .safety_seal import driver_gate_for_write

                try:
                    driver_gate_for_write()
                except EzlynxDriverGateRefused as exc:
                    result.reason = f"driver_gate_refused: {exc}"
                    return result
                filed = discussions.file_note_to_existing_discussion(
                    ctx.discussion_client,
                    resolution.applicant_id,
                    note_text,
                    discussion_id=chosen_id,
                    dry_run=False,
                )
        elif ctx.dry_run:
            authorize_notice_write(
                "discussion_create_with_note",
                discussion_title=canonical,
            )
            filed = _prepare_dry_run_create(
                resolution.applicant_id,
                canonical,
                note_text,
            )
        else:
            from .ezlynx_driver_gate import EzlynxDriverGateRefused
            from .safety_seal import driver_gate_for_write

            authorize_notice_write(
                "discussion_create_with_note",
                discussion_title=canonical,
            )
            try:
                driver_gate_for_write()
            except EzlynxDriverGateRefused as exc:
                result.reason = f"driver_gate_refused: {exc}"
                return result
            try:
                filed = discussions.create_discussion_with_note(
                    ctx.discussion_client,
                    resolution.applicant_id,
                    canonical,
                    note_text,
                    dry_run=False,
                )
            except (discussions.DiscussionApiError, TimeoutError, OSError) as exc:
                # The POST may have landed before the client saw the error.
                # Re-read by exact title and use that discussion. Do not
                # POST with-note again.
                try:
                    recovered, _match_count = _list_category_discussion(
                        ctx.discussion_client, resolution.applicant_id, canonical
                    )
                except Exception as read_exc:  # noqa: BLE001 - fail closed
                    result.reason = (
                        f"discussion_error: {exc}; reread failed: "
                        f"{type(read_exc).__name__}: {read_exc}"
                    )
                    return result
                if recovered is None:
                    result.reason = f"discussion_error: {exc}"
                    return result
                chosen_id = discussions.discussion_id_of(recovered)
                display_title = discussions.discussion_title_of(recovered) or canonical
                ctx.planned_category_discussions[plan_key] = chosen_id
                result.detail["discussion_plan"] = DISCUSSION_PLAN_USE_EXISTING
                result.detail["discussion_title"] = display_title
                result.detail["discussion_id"] = chosen_id
                result.detail["discussion_recovered_after_create_error"] = True
                logger.warning(
                    "with-note create for applicant %s title %r failed (%s); "
                    "using existing discussion %s",
                    resolution.applicant_id,
                    canonical,
                    exc,
                    chosen_id,
                )
                authorize_notice_write(
                    "discussion_note",
                    discussion_id=chosen_id,
                    discussion_title=display_title,
                )
                recovered_note = _read_existing_note(
                    ctx.discussion_client,
                    resolution.applicant_id,
                    note_text,
                    discussion_id=chosen_id,
                )
                if recovered_note.get("duplicate"):
                    filed = {
                        "status": "filed",
                        "discussion_id": chosen_id,
                        "discussion_title": display_title,
                        "note_id": recovered_note.get("note_id") or None,
                        "reason": "recovered category discussion already has this note",
                    }
                else:
                    filed = discussions.file_note_to_existing_discussion(
                        ctx.discussion_client,
                        resolution.applicant_id,
                        note_text,
                        discussion_id=chosen_id,
                        dry_run=False,
                    )
            else:
                created_id = str(filed.get("discussion_id") or "").strip()
                if created_id:
                    ctx.planned_category_discussions[plan_key] = created_id
    except NonNoteWriteRefused as exc:
        result.reason = str(exc)
        return result
    except EzlynxWriteScopeError as exc:
        result.reason = f"write_scope_refused: {exc}"
        return result
    except discussions.DiscussionApiError as exc:
        result.reason = f"discussion_error: {exc}"
        return result
    if filed.get("status") not in {"filed", "dry_run", "created"}:
        if str(filed.get("reason") or "") == "no matching discussion":
            result.reason = "no matching discussion"
            result.detail["needs_human_review"] = True
            return result
        result.reason = (
            f"note_not_filed: {filed.get('reason_code')}: {filed.get('reason')}"
        )
        return result
    result.detail["discussion_id"] = filed.get("discussion_id")
    result.detail["discussion_title"] = filed.get("discussion_title")
    result.detail["note_id"] = filed.get("note_id")
    result.detail["note_text"] = note_text

    # CSR tasks (non-pay cancellation, and intent-to-cancel when the flag
    # is on) go through Zapier with the login. A disputed charge uses the
    # phone-watchdog TaskCreationNote and needs a numeric assignedUserId.
    # Dry-run validates and does not fire or POST.
    task = result.detail.get("task") if isinstance(result.detail.get("task"), dict) else None
    if task:
        from .ezlynx_driver_gate import EzlynxDriverGateRefused

        task_kind = str(task.get("type") or "")
        try:
            authorize_notice_write("task_create", task_kind=task_kind)
            due = _due_date(ctx.today, ctx.due_days)
            if task_kind == TASK_KIND_CANCELLATION:
                payload = triage.build_cancellation_task_payload(
                    triaged,
                    applicant_id=resolution.applicant_id,
                    account_csr=str(task.get("assignee") or ""),
                    due_date=due,
                )
            elif task_kind == TASK_KIND_INTENT_TO_CANCEL:
                payload = triage.build_intent_to_cancel_task_payload(
                    triaged,
                    applicant_id=resolution.applicant_id,
                    account_csr=str(task.get("assignee") or ""),
                    due_date=due,
                )
            else:
                insured = triaged.get("insured_name") or "unknown insured"
                payload = build_disputed_task_note(
                    assigned_user_id=int(task.get("assigned_user_id") or 0),
                    title=f"Ascend disputed charge - {insured}",
                    description=str(triaged.get("note_text") or ""),
                    due=due,
                )
        except NonNoteWriteRefused as exc:
            result.reason = str(exc)
            return result
        except (ValueError, TypeError) as exc:
            result.reason = f"task_not_built: {exc}"
            return result
        result.detail["task_payload"] = payload
        try:
            if ctx.dry_run:
                if task_kind == TASK_KIND_DISPUTED_CHARGE:
                    if payload.get("task", {}).get("assignedUserId") != int(
                        task.get("assigned_user_id") or 0
                    ):
                        raise ValueError("disputed task note is missing assignedUserId")
                else:
                    checked = dict(payload)
                    zapier_tasks.validate_task_payload(checked)
            else:
                from .safety_seal import driver_gate_for_write

                driver_gate_for_write()
                if task_kind == TASK_KIND_DISPUTED_CHARGE:
                    discussion_id = str(result.detail.get("discussion_id") or "").strip()
                    if not discussion_id:
                        result.reason = "task_not_built: disputed task has no discussion"
                        return result
                    posted = ctx.discussion_client._post(
                        f"v8/discussions/{discussion_id}/notes",
                        payload,
                    )
                    result.detail["task_result"] = {"ok": True, "response": posted}
                else:
                    result.detail["zapier_result"] = zapier_tasks.fire_task(
                        dict(payload), dry_run=False
                    )
        except EzlynxDriverGateRefused as exc:
            result.reason = f"driver_gate_refused: {exc}"
            return result
        except (ValueError, RuntimeError) as exc:
            result.reason = f"task_not_built: {exc}"
            return result
        if ctx.dry_run and task_kind != TASK_KIND_DISPUTED_CHARGE:
            result.detail["zapier_result"] = {"ok": True, "dry_run": True}
        elif ctx.dry_run:
            result.detail["task_result"] = {"ok": True, "dry_run": True}
    else:
        result.detail["task_skipped"] = f"no task for notice type {notice_type!r}"

    result.status = "dry_run" if ctx.dry_run else "done"
    if result.status == "done" and not str(notice.message_id or "").startswith("api:"):
        _resolve_unmatched_after_email_file(
            notice_type=notice_type,
            program_id=program_uuid,
            policy_numbers=[str(item) for item in (triaged.get("policy_numbers") or [])],
            insured_name=str(triaged.get("insured_name") or ""),
        )
    if notice_type == triage.CANCELLATION:
        result.detail["label"] = {
            "status": "label_skipped_by_policy",
            "reason": (
                "Ascend NOC is not applied. That label sends client email "
                "and text, and Robie does not send those."
            ),
        }
        result.reason = "label_skipped_by_policy"
    else:
        result.reason = "ok"
    if not ctx.dry_run:
        try:
            ctx.source.mark_processed(notice.message_id)
            result.detail["marked_read"] = True
        except Exception as exc:  # noqa: BLE001 - note/task already done; log it
            logger.warning("mark_processed failed for %s: %s", notice.message_id, exc)
            result.detail["marked_read"] = False
    return result


# ---------------------------------------------------------------------------
# Run + CLI
# ---------------------------------------------------------------------------


def _driver_gate_refusal() -> str:
    """Empty when this host may write. Otherwise the lease refusal reason."""
    from .ezlynx_driver_gate import EzlynxDriverGateRefused
    from .safety_seal import driver_gate_for_write

    try:
        driver_gate_for_write()
    except EzlynxDriverGateRefused as exc:
        return f"driver_gate_refused: {exc}"
    return ""


def _resolve_unmatched_after_email_file(
    *,
    notice_type: str,
    program_id: str,
    policy_numbers: list[str],
    insured_name: str,
) -> None:
    """Drop the accounting row once this email is filed. A store error stays a log line."""
    try:
        from .ascend_api_notice_source import _iso, _now
        from .ascend_unmatched_digest import resolve_unmatched_filed_by_email

        resolve_unmatched_filed_by_email(
            notice_type=notice_type,
            program_id=program_id,
            policy_numbers=policy_numbers,
            insured_name=insured_name,
            seen_at=_iso(_now()),
        )
    except Exception as exc:  # noqa: BLE001 - the note is already filed
        logger.warning(
            "unmatched resolve after email file failed: %s", type(exc).__name__
        )


def _api_source_already_filed(
    notice: EmailNotice, notice_type: str, program_uuid: str
) -> str:
    """Live event key when that filing already happened. Empty means file.

    A missing, empty, or unreadable live store is empty. Email is the only
    source until the API poller is live, so the driver files the notice.
    A message id of ``api:<event key>`` is the poller's own synthetic
    notice. That path is the writer; its dedupe is the event-key store.
    """
    if str(notice.message_id or "").startswith("api:"):
        return ""
    program_id = str(program_uuid or "").strip()
    if not program_id and notice_type != triage.LATE_PAYMENT:
        return ""
    try:
        from .ascend_api_notice_source import email_covered_by_api

        found = email_covered_by_api(
            program_id=program_id,
            notice_type=notice_type,
            subject=notice.subject,
            body=notice.body,
            internal_date=notice.internal_date,
        )
    except Exception as exc:  # noqa: BLE001 - a bad store must not block the email
        logger.warning(
            "ascend api dedupe lookup failed (%s); filing %s",
            type(exc).__name__,
            notice.message_id,
        )
        return ""
    return str(found or "")


def _reason_prefix(reason: str) -> str:
    """``applicant_unresolved: 2 candidate rows`` -> ``applicant_unresolved``."""
    text = str(reason or "").strip()
    if not text:
        return ""
    return text.split(":", 1)[0].strip()


def _gmail_identity(notice: EmailNotice) -> tuple[str, str]:
    """Return (mailbox, gmail message id) for a dry-run spot check."""
    mailbox = str(notice.mailbox or "").strip()
    gmail_id = str(notice.gmail_message_id or "").strip()
    message_id = str(notice.message_id or "")
    if not gmail_id and mailbox and message_id.startswith(mailbox + ":"):
        gmail_id = message_id.split(":", 1)[1]
    if (not gmail_id or not mailbox) and ":" in message_id:
        left, right = message_id.split(":", 1)
        if "@" in left:
            mailbox = mailbox or left
            gmail_id = gmail_id or right
    if not gmail_id:
        gmail_id = message_id
    return mailbox, gmail_id


def _would_file_entry(notice: EmailNotice, result: NoticeResult) -> dict[str, Any]:
    mailbox, gmail_id = _gmail_identity(notice)
    detail = result.detail or {}
    entry = {
        "gmail_message_id": gmail_id,
        "mailbox": mailbox,
        "notice_type": str(detail.get("notice_type") or ""),
        "policy_number": str(detail.get("policy_number") or ""),
        "applicant_id": str(detail.get("applicant_id") or ""),
    }
    if entry["notice_type"] == triage.CANCELLATION:
        entry["csr_login"] = str(detail.get("csr_username") or "")
    if detail.get("program_uuid"):
        entry["program_uuid"] = str(detail["program_uuid"])
    if detail.get("category"):
        entry["category"] = str(detail["category"])
    if detail.get("discussion_title"):
        entry["discussion_title"] = str(detail["discussion_title"])
    if detail.get("discussion_plan"):
        entry["discussion_plan"] = str(detail["discussion_plan"])
    task = detail.get("task")
    if isinstance(task, dict) and task.get("type"):
        entry["task"] = {
            "type": str(task.get("type") or ""),
            "assignee": str(task.get("assignee") or ""),
        }
    if "existing_note_duplicate" in detail:
        entry["existing_note_duplicate"] = bool(detail.get("existing_note_duplicate"))
    if "existing_note_read" in detail:
        entry["existing_note_read"] = bool(detail.get("existing_note_read"))
    if detail.get("existing_note_id"):
        entry["existing_note_id"] = str(detail["existing_note_id"])
    return entry


def run_driver(ctx: DriverContext) -> dict[str, Any]:
    """Process every unread notice; return a JSON-serializable summary."""
    ctx.seen_notice_events.clear()
    ctx.planned_category_discussions.clear()
    notices = ctx.source.fetch_notices()
    paired: list[tuple[EmailNotice, NoticeResult]] = []
    for notice in notices:
        paired.append((notice, process_notice(notice, ctx)))
    results = [item for _, item in paired]
    by_notice_type: dict[str, int] = {}
    skipped_by_reason: dict[str, int] = {}
    for item in results:
        notice_type = str((item.detail or {}).get("notice_type") or "unclassified")
        by_notice_type[notice_type] = by_notice_type.get(notice_type, 0) + 1
        if item.status == "skipped":
            prefix = _reason_prefix(item.reason)
            skipped_by_reason[prefix] = skipped_by_reason.get(prefix, 0) + 1
    would_file = [
        _would_file_entry(notice, item)
        for notice, item in paired
        if ctx.dry_run and item.status == "dry_run"
    ]
    # Real matches the narrow allowlist blocked. Still skipped; not filed.
    would_file_if_write_scope_allowed = [
        _would_file_entry(notice, item)
        for notice, item in paired
        if ctx.dry_run
        and item.status == "skipped"
        and _reason_prefix(item.reason) == "write_scope_refused"
        and str((item.detail or {}).get("applicant_id") or "")
    ]
    duplicate_in_run = [
        _would_file_entry(notice, item)
        for notice, item in paired
        if _reason_prefix(item.reason) == "duplicate_in_run"
    ]
    ignored = sum(1 for item in results if item.status == "ignored")
    summary = {
        "dry_run": ctx.dry_run,
        "notices_seen": len(notices),
        "done": sum(1 for item in results if item.status == "done"),
        "dry_runs": sum(1 for item in results if item.status == "dry_run"),
        "skipped": sum(1 for item in results if item.status == "skipped"),
        "ignored": ignored,
        "by_notice_type": by_notice_type,
        "would_file_count": len(would_file),
        "would_file": would_file,
        "would_file_if_write_scope_allowed_count": len(would_file_if_write_scope_allowed),
        "would_file_if_write_scope_allowed": would_file_if_write_scope_allowed,
        "duplicate_in_run": len(duplicate_in_run),
        "duplicate_in_run_notices": duplicate_in_run,
        "skipped_by_reason": skipped_by_reason,
        "breakdown": {
            "by_notice_type": dict(by_notice_type),
            "would_file": len(would_file),
            "would_file_if_write_scope_allowed": len(would_file_if_write_scope_allowed),
            "duplicate_in_run": len(duplicate_in_run),
            "ignored": ignored,
            "skipped_by_reason": dict(skipped_by_reason),
        },
        "results": [
            {
                "message_id": item.message_id,
                "subject": item.subject,
                "status": item.status,
                "reason": item.reason,
                "detail": item.detail,
            }
            for item in results
        ],
    }
    return annotate_summary(summary)


def _notice_allow_modify(*, dry_run: bool) -> bool:
    """Live mark-read needs gmail.modify. Dry-run requests readonly only."""
    return not dry_run


def discussion_config_from_api_config(api_config: Any) -> DiscussionApiConfig:
    """Build the Discussion API config from the shared EZLynx API secret payload.

    The host-only root comes from the certificate sweep helper. Appending
    ``DiscussionApi`` onto ``document_base_url`` produces
    ``.../DocumentApi/DiscussionApi/`` and every read 404s.
    """
    from .cert_sweep import _discussion_client_for

    return _discussion_client_for(api_config)._config


def build_processing_context(
    *,
    dry_run: bool,
    due_days: int = DEFAULT_DUE_DAYS,
    source: NoticeSource | None = None,
) -> DriverContext:
    """Assemble Ascend + EZLynx clients. No Gmail delegation required.

    The mailbox watcher already holds the email via OAuth; it passes a
    :class:`NullNoticeSource` (or omits ``source``) and marks mail itself.
    """
    api_config = load_ezlynx_api_config()
    ezlynx_client = EzlynxApiClient(api_config)
    discussion_client = DiscussionApiClient(discussion_config_from_api_config(api_config))
    ascend_client = configured_ascend_client()
    return DriverContext(
        ascend_client=ascend_client,
        ezlynx_client=ezlynx_client,
        discussion_client=discussion_client,
        source=source if source is not None else NullNoticeSource(),
        dry_run=dry_run,
        due_days=due_days,
    )


def build_live_context(
    *,
    mailbox: str,
    query: str,
    dry_run: bool,
    due_days: int,
    mailboxes: list[str] | None = None,
) -> DriverContext:
    """Assemble real clients from Secret Manager / env. Raises with a clear
    message when required configuration is missing (fail closed)."""
    service_account = str(os.environ.get("ROBIE_GMAIL_DELEGATION_SA") or "").strip()
    allow_modify = _notice_allow_modify(dry_run=dry_run)
    # Multi-mailbox mode: scan the allowlisted staff mailboxes via domain-wide
    # delegation. A single mailbox stays on GmailNoticeSource.
    if mailboxes:
        targets = enforce_mailbox_allowlist(list(mailboxes))
        source: NoticeSource = MultiMailboxNoticeSource(
            mailboxes=targets,
            query=query,
            service_account_email=service_account,
            allow_modify=allow_modify,
        )
    else:
        targets = enforce_mailbox_allowlist([mailbox])
        source = GmailNoticeSource(
            mailbox=targets[0],
            query=query,
            service_account_email=service_account,
            allow_modify=allow_modify,
        )
    return build_processing_context(dry_run=dry_run, due_days=due_days, source=source)


def main(argv: list[str] | None = None) -> int:
    # The GitHub workflow captures stdout and parses it as the JSON run
    # summary, so every human-readable log line must go to stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, force=True)
    parser = argparse.ArgumentParser(
        description="Drive Ascend notice emails to EZLynx discussion notes + Zapier tasks."
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Actually write notes and fire tasks. Without this (or "
        "ASCEND_DRIVER_LIVE=1) the driver runs in dry-run mode and writes nothing.",
    )
    parser.add_argument(
        "--mailbox",
        default=None,
        help="Scan one mailbox. Used when --mailboxes and ASCEND_DRIVER_MAILBOXES are unset.",
    )
    parser.add_argument(
        "--mailboxes",
        default=None,
        help="Comma-separated mailboxes. Overrides --mailbox. "
        "When neither this nor --mailbox nor the mailbox env vars are set, "
        "the driver scans the default staff mailboxes.",
    )
    parser.add_argument(
        "--query",
        default=None,
        help="Gmail search. Defaults to ASCEND_DRIVER_QUERY or the built-in "
        "Ascend sender filter. A query that does not already limit From to "
        "Ascend notice addresses has that filter added.",
    )
    parser.add_argument(
        "--due-days",
        type=int,
        default=int(os.environ.get("ASCEND_DRIVER_DUE_DAYS", str(DEFAULT_DUE_DAYS))),
        help="Days from today for the Zapier task due date (default 2).",
    )
    args = parser.parse_args(argv)

    dry_run = not (args.live or _truthy(os.environ.get("ASCEND_DRIVER_LIVE")))
    if dry_run:
        logger.warning("DRY RUN: nothing will be written (use --live to write)")
    else:
        logger.warning("LIVE MODE: notes will be filed and Zapier tasks fired")

    try:
        mailboxes = resolve_mailboxes(mailbox=args.mailbox, mailboxes=args.mailboxes)
        ctx = build_live_context(
            mailbox=mailboxes[0],
            query=configured_query(args.query),
            dry_run=dry_run,
            due_days=args.due_days,
            mailboxes=mailboxes,
        )
        summary = run_driver(ctx)
    except Exception as exc:  # noqa: BLE001 - top-level fail closed
        logger.error("driver failed closed: %s: %s", type(exc).__name__, exc)
        summary = {"dry_run": dry_run, "fatal": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(summary))
        if append_run_record(summary) is None:
            logger.warning("could not write ascend driver run log")
        return 1
    print(json.dumps(summary, indent=2, default=str))
    if append_run_record(summary) is None:
        logger.warning("could not write ascend driver run log")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
