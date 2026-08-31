"""Read-only, metadata-only Gmail accountability summaries.

No message body is requested or persisted. The production credential path uses
keyless IAM signing plus Google Workspace domain-wide delegation.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping


GMAIL_METADATA_SCOPE = "https://www.googleapis.com/auth/gmail.metadata"


def mailbox_allowlist(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item.strip().casefold() for item in value.split(",") if item.strip()))


def _address(header: str) -> str:
    text = str(header or "").casefold()
    if "<" in text and ">" in text:
        text = text.rsplit("<", 1)[-1].split(">", 1)[0]
    return text.strip()


def _domain(address: str) -> str:
    return address.rsplit("@", 1)[-1] if "@" in address else ""


def _is_automated(sender: str, headers: Mapping[str, str]) -> bool:
    auto_submitted = headers.get("auto-submitted", "").strip().casefold()
    precedence = headers.get("precedence", "").strip().casefold()
    local = sender.split("@", 1)[0]
    return (
        bool(auto_submitted and auto_submitted != "no")
        or precedence in {"bulk", "junk", "list"}
        or bool(re.search(r"(?:^|[._-])(no-?reply|mailer-daemon|postmaster)(?:$|[._-])", local))
    )


def summarize_mailbox_threads(
    mailbox: str,
    threads: Iterable[Mapping[str, Any]],
    *,
    as_of: datetime,
    stalled_hours: int = 24,
    agency_domains: Iterable[str] = ("streetsmart.insurance",),
    mailbox_aliases: Iterable[str] = (),
) -> dict[str, Any]:
    """Classify who owes the next reply using message metadata only."""

    mailbox = mailbox.casefold().strip()
    agency_domain_set = {str(value).strip().casefold().lstrip("@") for value in agency_domains if str(value).strip()}
    aliases = {mailbox, *(_address(value) for value in mailbox_aliases)}
    rows: list[dict[str, Any]] = []
    excluded = {"internal": 0, "automated": 0, "ambiguous": 0}
    for thread in threads:
        messages = list(thread.get("messages") or [])
        if not messages:
            continue
        last = max(messages, key=lambda item: int(item.get("internalDate") or 0))
        headers = {
            str(item.get("name") or "").casefold(): str(item.get("value") or "")
            for item in ((last.get("payload") or {}).get("headers") or [])
        }
        sender = _address(headers.get("from", ""))
        if not sender or "@" not in sender:
            excluded["ambiguous"] += 1
            continue
        if _is_automated(sender, headers):
            excluded["automated"] += 1
            continue
        if sender not in aliases and _domain(sender) in agency_domain_set:
            excluded["internal"] += 1
            continue
        sent_at = datetime.fromtimestamp(int(last.get("internalDate") or 0) / 1000, tz=timezone.utc)
        age_hours = max(0.0, (as_of.astimezone(timezone.utc) - sent_at).total_seconds() / 3600)
        awaiting = "customer" if sender in aliases else "employee"
        rows.append(
            {
                "evidence_id": hashlib.sha256(f"{mailbox}:{thread.get('id', '')}".encode()).hexdigest()[:16],
                "last_message_at": sent_at.isoformat(),
                "age_hours": round(age_hours, 1),
                "awaiting": awaiting,
                "stalled": awaiting == "employee" and age_hours > stalled_hours,
            }
        )
    return {
        "mailbox": mailbox,
        "threads_reviewed": len(rows),
        "threads_excluded": sum(excluded.values()),
        "exclusion_counts": excluded,
        "awaiting_employee": sum(item["awaiting"] == "employee" for item in rows),
        "awaiting_customer": sum(item["awaiting"] == "customer" for item in rows),
        "stalled_threads": sum(bool(item["stalled"]) for item in rows),
        "stalled": [item for item in rows if item["stalled"]],
    }


def build_keyless_delegated_service(service_account_email: str, user: str) -> Any:
    """Create a Gmail client with IAM-backed signing and delegated mailbox subject."""

    import google.auth
    from google.auth import iam
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    source, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    request = Request()
    signer = iam.Signer(request, source, service_account_email)
    delegated = service_account.Credentials(
        signer=signer,
        service_account_email=service_account_email,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=[GMAIL_METADATA_SCOPE],
        subject=user,
    )
    return build("gmail", "v1", credentials=delegated, cache_discovery=False)


def fetch_mailbox_threads(
    service: Any,
    *,
    max_threads: int = 500,
) -> list[dict[str, Any]]:
    """Fetch a bounded recent thread set without Gmail search or message bodies."""

    found: list[dict[str, Any]] = []
    token = None
    while len(found) < max_threads:
        response = service.users().threads().list(
            userId="me",
            maxResults=min(100, max_threads - len(found)),
            pageToken=token,
        ).execute()
        for item in response.get("threads", []):
            found.append(
                service.users().threads().get(
                    userId="me",
                    id=item["id"],
                    format="metadata",
                    metadataHeaders=["From", "To", "Date", "Auto-Submitted", "Precedence"],
                ).execute()
            )
        token = response.get("nextPageToken")
        if not token:
            break
    return found


def collect_agency_summary(
    *,
    environment: Mapping[str, str] | None = None,
    service_factory: Callable[[str, str], Any] = build_keyless_delegated_service,
    as_of: datetime | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    environment = environment or os.environ
    config = dict(config or {})
    service_account = environment.get("ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT", "").strip()
    users = mailbox_allowlist(environment.get("ACCOUNTABILITY_GMAIL_USERS", ""))
    if not service_account or not users:
        return {"source_status": "missing delegated service account or mailbox allowlist"}
    now = as_of or datetime.now(timezone.utc)
    by_employee: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    excluded_mailboxes = set(mailbox_allowlist(",".join(str(value) for value in config.get("excluded_mailboxes", []) or [])))
    aliases = dict(config.get("mailbox_aliases") or {})
    for user in users:
        if user in excluded_mailboxes:
            continue
        try:
            service = service_factory(service_account, user)
            by_employee[user] = summarize_mailbox_threads(
                user,
                fetch_mailbox_threads(service, max_threads=max(1, int(config.get("max_threads") or 500))),
                as_of=now,
                stalled_hours=max(1, int(config.get("stalled_hours") or 24)),
                agency_domains=config.get("agency_domains") or ("streetsmart.insurance",),
                mailbox_aliases=aliases.get(user, []) or [],
            )
        except Exception as exc:
            errors[user] = type(exc).__name__
    return {
        "source_status": "available" if by_employee and not errors else "partial or unavailable mailbox evidence",
        "scope": GMAIL_METADATA_SCOPE,
        "body_access": False,
        "mailboxes": len(by_employee),
        "mailboxes_failed": len(errors),
        "mailbox_error_types": errors,
        "stalled_threads": sum(item["stalled_threads"] for item in by_employee.values()),
        "by_employee": by_employee,
    }
