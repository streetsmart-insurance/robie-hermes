"""Read-only, metadata-only Gmail accountability summaries.

No message body is requested or persisted. The production credential path uses
keyless IAM signing plus Google Workspace domain-wide delegation.
"""

from __future__ import annotations

import hashlib
import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping


GMAIL_METADATA_SCOPE = "https://www.googleapis.com/auth/gmail.metadata"


class GmailAccountabilityError(RuntimeError):
    """Raised when the approved mailbox population cannot be verified in full."""


def mailbox_allowlist(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item.strip().casefold() for item in value.split(",") if item.strip()))


def approved_mailboxes_from_role_registry(
    registry: Mapping[str, Any],
    *,
    approved_domain: str = "streetsmart.insurance",
) -> tuple[str, ...]:
    """Derive the active employee mailbox allowlist from the approved roster.

    The role-registry builder already excludes inactive employees. Missing or
    non-agency work addresses are treated as a completeness failure so an
    accountability run cannot silently omit a person.
    """

    if str(registry.get("source_status") or "").casefold() != "available":
        raise GmailAccountabilityError("approved employee roster is unavailable or partial")
    employees = dict(registry.get("employees") or {})
    if not employees:
        raise GmailAccountabilityError("approved employee roster contains no active employees")
    domain = approved_domain.casefold().lstrip("@")
    mailboxes: list[str] = []
    missing: list[str] = []
    invalid: list[str] = []
    for name, raw in sorted(employees.items()):
        email = str((raw or {}).get("email") or "").strip().casefold()
        if not email:
            missing.append(str(name))
        elif email.rsplit("@", 1)[-1] != domain:
            invalid.append(f"{name} ({email})")
        else:
            mailboxes.append(email)
    if missing:
        raise GmailAccountabilityError(
            "approved employee roster is missing work email for: " + ", ".join(missing)
        )
    if invalid:
        raise GmailAccountabilityError(
            "approved employee roster contains non-agency mailbox values: " + ", ".join(invalid)
        )
    return tuple(dict.fromkeys(mailboxes))


def _address(header: str) -> str:
    text = str(header or "").casefold()
    if "<" in text and ">" in text:
        text = text.rsplit("<", 1)[-1].split(">", 1)[0]
    return text.strip()


def summarize_mailbox_threads(
    mailbox: str,
    threads: Iterable[Mapping[str, Any]],
    *,
    as_of: datetime,
    stalled_hours: int = 24,
) -> dict[str, Any]:
    """Classify who owes the next reply using message metadata only."""

    mailbox = mailbox.casefold().strip()
    rows: list[dict[str, Any]] = []
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
        sent_at = datetime.fromtimestamp(int(last.get("internalDate") or 0) / 1000, tz=timezone.utc)
        age_hours = max(0.0, (as_of.astimezone(timezone.utc) - sent_at).total_seconds() / 3600)
        awaiting = "customer" if sender == mailbox else "employee"
        rows.append(
            {
                "thread_id": str(thread.get("id") or ""),
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


def verify_delegated_mailbox(service: Any, expected_mailbox: str) -> None:
    """Prove domain-wide delegation resolves to the exact approved mailbox."""

    profile = service.users().getProfile(userId="me").execute()
    observed = str(profile.get("emailAddress") or "").strip().casefold()
    if observed != expected_mailbox.casefold():
        raise GmailAccountabilityError(
            f"delegated mailbox verification mismatch for {expected_mailbox}"
        )


def collect_agency_summary(
    *,
    environment: Mapping[str, str] | None = None,
    service_factory: Callable[[str, str], Any] = build_keyless_delegated_service,
    as_of: datetime | None = None,
    approved_users: Iterable[str] | None = None,
    verify_mailbox: Callable[[Any, str], None] = verify_delegated_mailbox,
) -> dict[str, Any]:
    environment = environment or os.environ
    service_account = environment.get("ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT", "").strip()
    users = (
        tuple(dict.fromkeys(str(item).strip().casefold() for item in approved_users if str(item).strip()))
        if approved_users is not None
        else mailbox_allowlist(environment.get("ACCOUNTABILITY_GMAIL_USERS", ""))
    )
    if not service_account or not users:
        return {"source_status": "missing delegated service account or mailbox allowlist"}
    now = as_of or datetime.now(timezone.utc)
    by_employee: dict[str, dict[str, Any]] = {}
    for user in users:
        service = service_factory(service_account, user)
        verify_mailbox(service, user)
        by_employee[user] = summarize_mailbox_threads(user, fetch_mailbox_threads(service), as_of=now)
    return {
        "source_status": "available",
        "scope": GMAIL_METADATA_SCOPE,
        "body_access": False,
        "mailboxes": len(by_employee),
        "mailboxes_verified": len(by_employee),
        "allowlist_source": "approved_active_employee_roster" if approved_users is not None else "environment",
        "stalled_threads": sum(item["stalled_threads"] for item in by_employee.values()),
        "by_employee": by_employee,
    }
