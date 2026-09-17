"""Scheduled driver: Ascend notice emails -> EZLynx discussion note + Zapier task.

This is the trigger PR #430 was missing. PR #430 put the engine on the box
(read-only ``triage_notice()``, Discussion API v8 append to an EXISTING
discussion, Zapier task firing with a required due date) but nothing ever
called it. This driver watches the hello@ mailbox for unread Ascend notice
emails and runs the chain once per email.

Pipeline per email::

    Gmail (hello@streetsmart.insurance, unread)
      -> triage_notice()                       (read-only classification)
      -> resolve applicant_id                  (EZLynx PolicyApi, by policy number)
      -> resolve CSR login username            (never a display name)
      -> file_note_to_existing_discussion()    (EXISTING discussion only)
      -> build_cancellation_task_payload()     (cancellation notices only)
      -> zapier_tasks.fire_task()              (Zapier catch-hook Zap)

Safety (non-negotiable):

- DRY_RUN defaults ON. Live mode only with ``--live`` or
  ``ASCEND_DRIVER_LIVE=1``. Dry-run logs exactly what it would do
  (subject, applicant, CSR, note text, task payload) and writes nothing.
- Fail closed per email: triage ``needs_human_review``, unresolved
  applicant, missing/invalid CSR username, any API error, phone numbers in
  the note text, or a write-scope refusal -> that email is skipped, logged,
  and the driver continues with the rest.
- Never deletes anything. Never invents an applicant_id or a CSR username.
- The repo's write-scope guard (``ezlynx_write_scope``) is enforced inside
  ``file_note_to_existing_discussion``; the driver does not bypass it. In
  practice that means live note writes land only where the guard allows
  (the Test allowlist, or the applicant bound to the active Production job).
  Anything else is logged as ``write_scope_refused`` and skipped.
- Gmail is read-only except in LIVE mode, where a fully processed email is
  marked read (UNREAD label removed) so the next run does not re-file the
  same note. Dry-run never touches labels.
- Delegated Gmail scopes: dry-run requests ``gmail.readonly`` (unread
  search + body). ``gmail.metadata`` cannot use ``messages.list?q=``.
  Live mark-read also requests ``gmail.modify``. Set
  ``ASCEND_DRIVER_GMAIL_MODIFY=1`` to request modify without ``--live``.
  Workspace Admin DWD client ``112650695780807418521`` must authorize
  those scopes for the delegation SA.

Known wiring gaps (documented, not silently worked around):

- Insured-name -> applicant lookup: no reliable API helper exists in the
  repo (PolicyApi has no applicant search; the legacy matcher in
  ``ascend_sync.py`` imports a client from outside this repo). When an email
  has no policy number, the driver fails closed with
  ``applicant_unresolved`` instead of guessing.
- CSR login username: the driver accepts only username-shaped values
  (``^[A-Za-z0-9._-]+$``, no whitespace) from username-like policy-row
  fields. Display names such as "Karla Brown" are rejected, never used.
  There is no user-directory API in the repo to map display name -> login,
  so if the PolicyApi row carries no username field the email fails closed
  with ``csr_unresolved``.
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

logger = logging.getLogger(__name__)

DEFAULT_MAILBOX = "hello@streetsmart.insurance"
DEFAULT_QUERY = "is:unread newer_than:2d"
DEFAULT_DUE_DAYS = 2

# A login username looks like KarlaSS / Carlo1: no whitespace, ever.
# Display names ("Karla Brown") must never reach the Zap's assignee field.
_LOGIN_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{2,64}$")

# Policy-row fields that may carry the assigned user's *login* username.
# Checked in order; the first username-shaped value wins. Display-name
# fields (AssignedUser, ProducerName, ...) are deliberately NOT listed.
_CSR_USERNAME_KEYS = (
    "AssignedUsername",
    "CSRUsername",
    "AssignedUserName",
    "assigned_username",
    "csr_username",
)

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


# ---------------------------------------------------------------------------
# Email source
# ---------------------------------------------------------------------------


@dataclass
class EmailNotice:
    message_id: str
    subject: str
    body: str
    internal_date: str = ""


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


def resolve_applicant(
    ezlynx_client: Any,
    policy_numbers: list[str],
    insured_name: str | None,
) -> tuple[ApplicantResolution | None, str]:
    """Resolve (applicant_id, CSR login username) from PolicyApi rows.

    Policy numbers are tried first, in order; the first row whose number
    matches exactly (case-insensitive) and which carries both an applicant id
    and a username-shaped CSR value wins. Insured-name lookup is NOT
    attempted: no reliable API helper exists in the repo, so a notice with no
    policy number fails closed with ``applicant_unresolved``.

    Returns (resolution, reason). ``reason`` is "" on success.
    """
    for policy_number in policy_numbers:
        number = str(policy_number or "").strip()
        if not number:
            continue
        try:
            result = ezlynx_client.search_policy_by_number(number)
        except Exception as exc:  # noqa: BLE001 - fail closed, keep the class
            return None, f"policy_search_failed: {type(exc).__name__}"
        for row in _policy_rows(result):
            if _row_policy_number(row).casefold() != number.casefold():
                continue
            applicant_id = _first_present(row, _APPLICANT_ID_KEYS)
            if not applicant_id:
                continue
            csr_username = ""
            for key in _CSR_USERNAME_KEYS:
                candidate = str(row.get(key) or "").strip()
                if candidate and _LOGIN_USERNAME_RE.fullmatch(candidate):
                    csr_username = candidate
                    break
            if not csr_username:
                return None, "csr_unresolved: policy row has no login-username-shaped CSR field"
            return (
                ApplicantResolution(
                    applicant_id=applicant_id,
                    csr_username=csr_username,
                    via="policy_number",
                    policy_number=number,
                ),
                "",
            )
    if insured_name:
        return None, "applicant_unresolved: no policy number matched; name lookup not wired"
    return None, "applicant_unresolved: notice carries no policy number"


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


@dataclass
class NoticeResult:
    message_id: str
    subject: str
    status: str  # done | dry_run | skipped
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


def _due_date(today: date, due_days: int) -> str:
    return (today + timedelta(days=max(int(due_days), 0))).isoformat()


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
    if triaged.get("needs_human_review"):
        result.reason = (
            "needs_human_review: " + str(triaged.get("review_reason") or "triage flagged")
        )
        return result
    notice_type = str(triaged.get("notice_type") or "")
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
    result.detail["applicant_id"] = resolution.applicant_id
    result.detail["csr_username"] = resolution.csr_username
    result.detail["notice_type"] = notice_type

    note_text = str(triaged.get("note_text") or "").strip()
    if not note_text:
        result.reason = "empty_note_text"
        return result

    # Append to the EXISTING discussion. The write-scope guard inside refuses
    # non-allowlisted applicants; phone numbers in the body raise. Both are
    # caught below and become a skip, never a silent write.
    try:
        filed = discussions.file_note_to_existing_discussion(
            ctx.discussion_client,
            resolution.applicant_id,
            note_text,
            dry_run=ctx.dry_run,
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
        payload = triage.build_cancellation_task_payload(
            triaged,
            applicant_id=resolution.applicant_id,
            account_csr=resolution.csr_username,
            due_date=_due_date(ctx.today, ctx.due_days),
        )
        result.detail["task_payload"] = payload
        try:
            task_fired = zapier_tasks.fire_task(dict(payload), dry_run=ctx.dry_run)
        except (ValueError, RuntimeError) as exc:
            result.reason = f"zapier_fire_failed: {exc}"
            return result
        result.detail["zapier_result"] = task_fired
    else:
        result.detail["task_skipped"] = f"no task builder for notice type {notice_type!r}"

    result.status = "dry_run" if ctx.dry_run else "done"
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


def run_driver(ctx: DriverContext) -> dict[str, Any]:
    """Process every unread notice; return a JSON-serializable summary."""
    notices = ctx.source.fetch_notices()
    results: list[NoticeResult] = []
    for notice in notices:
        results.append(process_notice(notice, ctx))
    summary = {
        "dry_run": ctx.dry_run,
        "notices_seen": len(notices),
        "done": sum(1 for r in results if r.status == "done"),
        "dry_runs": sum(1 for r in results if r.status == "dry_run"),
        "skipped": sum(1 for r in results if r.status == "skipped"),
        "results": [
            {
                "message_id": r.message_id,
                "subject": r.subject,
                "status": r.status,
                "reason": r.reason,
                "detail": r.detail,
            }
            for r in results
        ],
    }
    return summary


def _notice_allow_modify(*, dry_run: bool) -> bool:
    """Request gmail.modify for live mark-read, or when the env flag is set."""
    return (not dry_run) or _truthy(os.environ.get("ASCEND_DRIVER_GMAIL_MODIFY"))


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


def build_live_context(
    *,
    mailbox: str,
    query: str,
    dry_run: bool,
    due_days: int,
) -> DriverContext:
    """Assemble real clients from Secret Manager / env. Raises with a clear
    message when required configuration is missing (fail closed)."""
    api_config = load_ezlynx_api_config()
    ezlynx_client = EzlynxApiClient(api_config)
    discussion_client = DiscussionApiClient(discussion_config_from_api_config(api_config))
    ascend_client = configured_ascend_client()
    service_account = str(os.environ.get("ROBIE_GMAIL_DELEGATION_SA") or "").strip()
    source = GmailNoticeSource(
        mailbox=mailbox,
        query=query,
        service_account_email=service_account,
        allow_modify=_notice_allow_modify(dry_run=dry_run),
    )
    return DriverContext(
        ascend_client=ascend_client,
        ezlynx_client=ezlynx_client,
        discussion_client=discussion_client,
        source=source,
        dry_run=dry_run,
        due_days=due_days,
    )


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
    parser.add_argument("--mailbox", default=os.environ.get("ASCEND_DRIVER_MAILBOX", DEFAULT_MAILBOX))
    parser.add_argument("--query", default=os.environ.get("ASCEND_DRIVER_QUERY", DEFAULT_QUERY))
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
        ctx = build_live_context(
            mailbox=args.mailbox, query=args.query, dry_run=dry_run, due_days=args.due_days
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
