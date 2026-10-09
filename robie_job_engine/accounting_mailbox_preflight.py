"""Bounded read-only delegation probe; never a complete accounting collector.

Run explicitly on an authorized runtime identity. No credential values, message
IDs, subjects, bodies, headers, attachments, or provider error strings are emitted.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from .gmail_accountability import GMAIL_READONLY_SCOPE, build_keyless_delegated_service


def _mailboxes(values: Sequence[str], domain: str) -> tuple[str, ...]:
    domain = domain.strip().casefold()
    if not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*\.[a-z]{2,}", domain):
        raise ValueError("an explicit valid approved domain is required")
    result = []
    for value in values:
        mailbox = value.strip().casefold()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._+-]*@" + re.escape(domain), mailbox):
            raise ValueError("every mailbox must be an exact address in the approved domain")
        if mailbox not in result:
            result.append(mailbox)
    if not result:
        raise ValueError("at least one explicitly approved mailbox is required")
    return tuple(result)


def probe_mailboxes(
    service_account: str,
    mailboxes: Sequence[str],
    *,
    approved_domain: str,
    service_factory: Callable[..., Any] = build_keyless_delegated_service,
) -> dict[str, Any]:
    """Check profile, search permission and one full-message read per mailbox.

    The caller supplies the reviewed mailbox population; no directory expansion
    or automatic fallback occurs. An empty inbox cannot prove message readback.
    API calls use zero retries so an uncertain read is reported for the operator.
    """
    approved = _mailboxes(mailboxes, approved_domain)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*@[a-z0-9][a-z0-9-]*\.iam\.gserviceaccount\.com", service_account):
        raise ValueError("a service-account reference is required; do not supply credentials")
    rows = []
    for index, mailbox in enumerate(approved, 1):
        row = {
            "mailbox_index": index,
            "identity_verified": False,
            "search_verified": False,
            "message_read_verified": False,
            "status": "UNVERIFIED",
            "reason": "ACCESS_CHECK_FAILED",
        }
        try:
            service = service_factory(service_account, mailbox, scopes=(GMAIL_READONLY_SCOPE,))
            users = service.users()
            profile = users.getProfile(userId="me").execute(num_retries=0)
            if str(profile.get("emailAddress") or "").strip().casefold() != mailbox:
                row["reason"] = "MAILBOX_IDENTITY_MISMATCH"
                rows.append(row)
                continue
            row["identity_verified"] = True
            # q requires more than gmail.metadata. This is a bounded capability
            # check, not an inbox inventory, aging report, or source watermark.
            listing = users.messages().list(userId="me", q="in:anywhere", maxResults=1).execute(num_retries=0)
            if not isinstance(listing, dict):
                row["reason"] = "INVALID_SEARCH_RESPONSE"
                rows.append(row)
                continue
            row["search_verified"] = True
            messages = listing.get("messages", [])
            if not isinstance(messages, list) or len(messages) > 1:
                row["reason"] = "INVALID_SEARCH_RESPONSE"
            elif not messages:
                row["reason"] = "NO_MESSAGE_TO_VERIFY"
            elif not isinstance(messages[0], dict) or not messages[0].get("id"):
                row["reason"] = "INVALID_MESSAGE_REFERENCE"
            else:
                message_id = messages[0]["id"]
                message = users.messages().get(userId="me", id=message_id, format="full").execute(num_retries=0)
                if not isinstance(message, dict) or message.get("id") != message_id or not isinstance(message.get("payload"), dict):
                    row["reason"] = "INVALID_MESSAGE_READBACK"
                else:
                    row.update(message_read_verified=True, status="VERIFIED", reason="READ_ACCESS_VERIFIED")
        except Exception:
            # Exception messages may contain tokens, queries or client data.
            row["reason"] = "ACCESS_CHECK_FAILED"
        rows.append(row)
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "scope": GMAIL_READONLY_SCOPE,
        "probe_only": True,
        "complete_source_inventory": False,
        "send_access_checked": False,
        "status": "VERIFIED" if all(row["status"] == "VERIFIED" for row in rows) else "UNVERIFIED",
        "mailboxes": rows,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service-account", required=True, help="existing delegated service-account reference")
    parser.add_argument("--approved-domain", required=True)
    parser.add_argument("--mailbox", action="append", required=True, help="explicitly approved mailbox; repeat for each")
    parser.add_argument("--live-read", action="store_true", help="explicit opt-in to bounded live reads")
    args = parser.parse_args(argv)
    if not args.live_read:
        parser.error("no access check ran; --live-read is required")
    try:
        report = probe_mailboxes(args.service_account, args.mailbox, approved_domain=args.approved_domain)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "VERIFIED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
