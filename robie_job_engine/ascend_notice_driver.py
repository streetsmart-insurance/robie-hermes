"""Scheduled driver: Ascend notice emails -> EZLynx discussion note + Zapier task.

This is the trigger PR #430 was missing. PR #430 put the engine on the box
(read-only ``triage_notice()``, Discussion API v8 append to an EXISTING
discussion, Zapier task firing with a required due date) but nothing ever
called it. This driver watches the agency mailboxes for unread Ascend
notice emails and runs the chain once per email.

Pipeline per email::

    Gmail (default staff mailboxes, unread, from no-reply@ or accounting@)
      -> triage_notice()                       (read-only classification)
      -> resolve applicant_id                  (EZLynx PolicyApi, normalized policy number)
      -> resolve CSR login                    (cancellation only; Ascend producer)
      -> file_note_to_existing_discussion()    (EXISTING discussion only)
      -> build_cancellation_task_payload()     (cancellation notices only)
      -> zapier_tasks.fire_task()              (Zapier catch-hook Zap)

Safety (non-negotiable):

- DRY_RUN defaults ON. Live mode only with ``--live`` or
  ``ASCEND_DRIVER_LIVE=1``. Dry-run logs exactly what it would do
  (subject, applicant, CSR, note text, task payload) and writes nothing.
  Dry-run requests ``gmail.readonly`` only: no mark-read, no notes, no
  labels, no Zapier post, and no ``driver_gate_for_write`` call.
- Fail closed per email: triage ``needs_human_review``, unresolved
  applicant, missing/invalid CSR username on a cancellation, any API
  error, phone numbers in the note text, or a write-scope refusal -> that
  email is skipped, logged, and the driver continues with the rest.
  Informational mail is ``ignored``, not a human-review skip.
- Write-scope eligibility is checked before filing. Dry-run reports
  matches blocked only by that allowlist as
  ``would_file_if_write_scope_allowed``, with applicant ids. The allowlist
  itself is unchanged.
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
- Live note writes go through ``file_note_to_existing_discussion``, which
  enforces the EZLynx write-scope allowlist and the driver lease. Dry-run
  checks the same allowlist and does not take the lease. Anything outside
  the allowlist is logged as ``write_scope_refused`` and skipped.
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

- Insured-name -> applicant lookup: no reliable API helper exists in the
  repo (PolicyApi has no applicant search; the legacy matcher in
  ``ascend_sync.py`` imports a client from outside this repo). When an email
  has no policy number, the driver fails closed with
  ``applicant_unresolved`` instead of guessing.
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
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable, Protocol

from . import ascend_notice_triage as triage
from . import ezlynx_discussions as discussions
from . import zapier_tasks
from .ascend_api import AscendApiClient, configured_client as configured_ascend_client
from .ezlynx_api import EzlynxApiClient, load_ezlynx_api_config
from .ezlynx_discussions import DiscussionApiClient, DiscussionApiConfig
from .ezlynx_write_scope import EzlynxWriteScopeError

ROBIE_WAS_HERE = "Robie was here"

logger = logging.getLogger(__name__)

DEFAULT_MAILBOX = "hello@streetsmart.insurance"
DEFAULT_QUERY = (
    "is:unread newer_than:2d from:(no-reply@useascend.com OR accounting@useascend.com)"
)
DEFAULT_DUE_DAYS = 2

# Scanned when neither --mailbox nor ASCEND_DRIVER_MAILBOX / ASCEND_DRIVER_MAILBOXES
# is set. Also the hard allowlist: any other mailbox is refused unless
# ASCEND_DRIVER_ALLOW_EXTRA_MAILBOXES=1.
DEFAULT_MAILBOXES: tuple[str, ...] = (
    "hello@streetsmart.insurance",
    "mike@streetsmart.insurance",
    "angie@streetsmart.insurance",
    "eimy@streetsmart.insurance",
    "sandy@streetsmart.insurance",
    "zeus@streetsmart.insurance",
    "taylor@streetsmart.insurance",
    "jake@streetsmart.insurance",
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
    """A requested mailbox is outside the Ascend driver allowlist."""


def parse_mailbox_list(raw: str) -> list[str]:
    return [part.strip() for part in str(raw or "").split(",") if part.strip()]


def enforce_mailbox_allowlist(mailboxes: list[str]) -> list[str]:
    """Refuse mailboxes outside the default staff set unless explicitly allowed.

    The override is ``ASCEND_DRIVER_ALLOW_EXTRA_MAILBOXES=1``. Shared or
    unknown mailboxes are not scanned by default.
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
    ``--mailbox``, then ``ASCEND_DRIVER_MAILBOX``, then the default eight.
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


def configured_query(cli_value: str | None = None) -> str:
    """Gmail query. An explicit CLI value wins; otherwise the env, then the default."""
    if cli_value is not None and str(cli_value).strip():
        return str(cli_value)
    return str(os.environ.get("ASCEND_DRIVER_QUERY") or DEFAULT_QUERY)


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


def resolve_applicant(
    ezlynx_client: Any,
    policy_numbers: list[str],
    insured_name: str | None,
) -> tuple[ApplicantResolution | None, str]:
    """Resolve ``applicant_id`` from PolicyApi rows. Fail closed.

    Compare policy numbers normalized (uppercase, spaces removed). An
    EZLynx row may also drop one trailing LOB code (`` APD``, 2-4 uppercase
    letters). When that misses, strip a trailing term suffix (``-00`` /
    ``-01`` / ``-1``) on both the notice and the row. Accept only when
    exactly one row matches and it carries ``accountId`` (or
    ``ApplicantId``). Otherwise ``applicant_unresolved`` and the candidate
    count.

    PolicyApi rows have no CSR field. This function does not return one.
    Insured-name lookup is not attempted.
    """
    del insured_name  # name lookup is intentionally unwired
    numbers = [str(item or "").strip() for item in policy_numbers if str(item or "").strip()]
    if not numbers:
        return None, "applicant_unresolved: 0 candidate rows"
    last_count = 0
    for number in numbers:
        try:
            result = ezlynx_client.search_policy_by_number(number)
        except Exception as exc:  # noqa: BLE001 - fail closed, keep the class
            return None, f"policy_search_failed: {type(exc).__name__}"
        matched = _rows_matching_policy(_policy_rows(result), number)
        last_count = len(matched)
        if last_count == 0:
            continue
        if last_count != 1:
            return None, f"applicant_unresolved: {last_count} candidate rows"
        account_id = _first_present(matched[0], _APPLICANT_ID_KEYS)
        if not account_id:
            return None, (
                f"applicant_unresolved: {last_count} candidate row lacks accountId"
            )
        return (
            ApplicantResolution(
                applicant_id=account_id,
                csr_username="",
                via="policy_number",
                policy_number=number,
            ),
            "",
        )
    return None, f"applicant_unresolved: {last_count} candidate rows"


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


@dataclass
class NoticeResult:
    message_id: str
    subject: str
    status: str  # done | dry_run | skipped | ignored
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


def _due_date(today: date, due_days: int) -> str:
    return (today + timedelta(days=max(int(due_days), 0))).isoformat()


def discussion_title_hint(notice_type: str) -> str | None:
    """Prefer the SOP-titled card for this notice; never Untitled.

    Substring match against existing titles. ``cancellation`` hits both
    ``Cancellation`` and ``Service-Cancellation``. ``noc`` hits ``Ascend NOC``.
    Other notice types leave the hint empty so a single titled discussion wins.
    """
    if notice_type == triage.CANCELLATION:
        return "cancellation"
    # Late payment and intent-to-cancel are notes on the Ascend NOC card.
    # Intent-to-cancel never uses the cancellation hint and never gets the label.
    if notice_type in {triage.LATE_PAYMENT, triage.INTENT_TO_CANCEL}:
        return "noc"
    return None


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
) -> dict[str, Any]:
    """Identity for collapsing repeated notices inside one driver run."""
    return {
        "applicant_id": str(applicant_id or "").strip(),
        "policy": strip_policy_term_suffix(policy_number),
        "notice_type": str(notice_type or "").strip(),
        "dates": tuple(sorted(due_or_cancel_dates(body))),
        "body": normalize_notice_body(body),
    }


def same_notice_event(kept: dict[str, Any], candidate: dict[str, Any]) -> bool:
    """True when two notices are one filing event.

    Same applicant, policy, and notice type, plus the same due/cancel
    date or the same normalized body. A date match requires both sides
    to have extracted a date, so two undated different bodies stay apart.
    """
    if str(kept.get("applicant_id") or "") != str(candidate.get("applicant_id") or ""):
        return False
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


def _read_existing_note(
    client: Any,
    applicant_id: str,
    note_body: str,
    *,
    title_hint: str | None,
) -> dict[str, Any]:
    """Read the discussion and compare note text. No write, no driver gate.

    Write-scope is not checked here. Dry-run uses this for applicants the
    allowlist blocks so a reviewer can see an existing duplicate.
    """
    outcome = {"read": False, "duplicate": False, "note_id": "", "reason": ""}
    try:
        rows = client.get_discussions(applicant_id)
        record = discussions.select_discussion_for_note(rows, title_hint=title_hint)
    except discussions.DiscussionSelectionError as exc:
        outcome["reason"] = f"{exc.code}: {exc}"
        return outcome
    except Exception as exc:  # noqa: BLE001 - read failed; caller decides
        outcome["reason"] = f"{type(exc).__name__}: {exc}"
        return outcome
    discussion_id = discussions.discussion_id_of(record)
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
    title_hint: str | None,
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
    rows = client.get_discussions(applicant)
    try:
        record = discussions.select_discussion_for_note(rows, title_hint=title_hint)
    except discussions.DiscussionSelectionError as exc:
        return {
            "status": "pending",
            "reason_code": exc.code,
            "reason": str(exc),
            "applicant_id": applicant,
            "discussion_id": None,
            "note_id": None,
            "matches": list(getattr(exc, "matches", []) or []),
        }
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

    resolution, reason = resolve_applicant(
        ctx.ezlynx_client,
        [str(p) for p in (triaged.get("policy_numbers") or [])],
        triaged.get("insured_name"),
    )
    if resolution is None:
        result.reason = reason
        return result
    if notice_type == triage.CANCELLATION:
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
    )
    if any(same_notice_event(kept, event) for kept in ctx.seen_notice_events):
        result.reason = "duplicate_in_run"
        result.detail["duplicate_in_run"] = True
        return result
    ctx.seen_notice_events.append(event)

    # Existing-note check is a read, including when write scope will refuse.
    # A matching note is not filed again. A failed read does not invent a match.
    title_hint = discussion_title_hint(notice_type)
    note_match = _read_existing_note(
        ctx.discussion_client,
        resolution.applicant_id,
        note_text,
        title_hint=title_hint,
    )
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
    # Intent-to-cancel is also a note, never a cancellation task.

    # Append to an EXISTING titled discussion. Untitled is refused inside
    # select_discussion_for_note. The write-scope guard refuses
    # non-allowlisted applicants; phone numbers in the body raise. Both are
    # caught below and become a skip, never a silent write.
    # The shared EZLynx seat is gated before a live note and again before
    # a live Zapier post. Dry-run does not call driver_gate_for_write.
    # title_hint was chosen above for the existing-note read.
    try:
        if ctx.dry_run:
            filed = _prepare_dry_run_note(
                ctx.discussion_client,
                resolution.applicant_id,
                note_text,
                title_hint=title_hint,
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
                title_hint=title_hint,
                dry_run=False,
            )
    except EzlynxWriteScopeError as exc:
        result.reason = f"write_scope_refused: {exc}"
        return result
    except discussions.DiscussionApiError as exc:
        result.reason = f"discussion_error: {exc}"
        return result
    if filed.get("status") not in {"filed", "dry_run"}:
        result.reason = (
            f"note_not_filed: {filed.get('reason_code')}: {filed.get('reason')}"
        )
        return result
    result.detail["discussion_id"] = filed.get("discussion_id")
    result.detail["discussion_title"] = filed.get("discussion_title")
    result.detail["note_id"] = filed.get("note_id")
    result.detail["note_text"] = note_text

    # Zapier task: only cancellation notices have a builder. Anything else
    # gets its note and a logged skip — never an invented payload.
    task_fired: dict[str, Any] | None = None
    if notice_type == triage.CANCELLATION:
        from .ezlynx_driver_gate import EzlynxDriverGateRefused

        payload = triage.build_cancellation_task_payload(
            triaged,
            applicant_id=resolution.applicant_id,
            account_csr=resolution.csr_username,
            due_date=_due_date(ctx.today, ctx.due_days),
        )
        result.detail["task_payload"] = payload
        try:
            if ctx.dry_run:
                # In-process validation only. Do not spawn zap-trigger and do
                # not call driver_gate_for_write.
                checked = dict(payload)
                zapier_tasks.validate_task_payload(checked)
                task_fired = {"ok": True, "dry_run": True}
            else:
                from .safety_seal import driver_gate_for_write

                driver_gate_for_write()
                task_fired = zapier_tasks.fire_task(dict(payload), dry_run=False)
        except EzlynxDriverGateRefused as exc:
            result.reason = f"driver_gate_refused: {exc}"
            return result
        except (ValueError, RuntimeError) as exc:
            result.reason = f"zapier_fire_failed: {exc}"
            return result
        result.detail["zapier_result"] = task_fired
    else:
        result.detail["task_skipped"] = f"no task builder for notice type {notice_type!r}"

    result.status = "dry_run" if ctx.dry_run else "done"
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
    return summary


def _notice_allow_modify(*, dry_run: bool) -> bool:
    """Live mark-read needs gmail.modify. Dry-run requests readonly only."""
    return not dry_run


def discussion_config_from_api_config(api_config: Any) -> DiscussionApiConfig:
    """Build the Discussion API config from the shared EZLynx API secret payload.

    The DiscussionApi root lives under the same host as the document API
    (https://app.ezlynx.com/DiscussionApi/). No new secrets are introduced.
    """
    base = str(api_config.document_base_url or "").rstrip("/") + "/DiscussionApi/"
    return DiscussionApiConfig(
        discussion_base_url=base,
        token_endpoint=str(api_config.token_endpoint),
        client_id=str(api_config.client_id),
        client_secret=str(api_config.client_secret),
        username=str(api_config.username),
        integration_group_id=str(api_config.integration_group_id),
        scope="DiscussionApi openid",
    )


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
        help="Gmail search. Defaults to ASCEND_DRIVER_QUERY or the Ascend sender filter.",
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
        print(json.dumps({"dry_run": dry_run, "fatal": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
