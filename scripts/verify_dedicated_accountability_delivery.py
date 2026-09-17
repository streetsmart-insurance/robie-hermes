#!/usr/bin/env python3
"""Preflight or verify exact Gmail delivery for the dedicated accountability run."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from email.utils import getaddresses
from pathlib import Path
from zoneinfo import ZoneInfo


GMAIL_METADATA_SCOPE = "https://www.googleapis.com/auth/gmail.metadata"


def _expected(root: Path) -> tuple[str, set[str], object, str]:
    sys.path.insert(0, str(root))
    from googleapiclient.discovery import build
    from src.engine.date_utils import get_previous_business_day
    from src.google_auth import delegated_credentials
    from src.production_config import REPORTING_MAILBOX
    from src.reporters.email_delivery import RECIPIENTS

    today = datetime.now(ZoneInfo("America/New_York")).date()
    target = get_previous_business_day(today).isoformat()
    subject = f"StreetSmart Yesterday Accountability — {target}"
    credential_path = root / "data" / "credentials" / "service_account.json"
    from google.oauth2 import service_account

    if credential_path.is_file():
        credentials = service_account.Credentials.from_service_account_file(
            str(credential_path),
            scopes=[GMAIL_METADATA_SCOPE],
            subject=REPORTING_MAILBOX,
        )
    else:
        try:
            credentials = delegated_credentials([GMAIL_METADATA_SCOPE], REPORTING_MAILBOX)
        except RuntimeError as exc:
            if str(exc) != "Google delegation credential is unavailable":
                raise
            service_account_email = os.environ.get(
                "ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT", ""
            ).strip()
            if not service_account_email:
                raise
            import google.auth
            from google.auth import iam
            from google.auth.transport.requests import Request

            source, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            credentials = service_account.Credentials(
                signer=iam.Signer(Request(), source, service_account_email),
                service_account_email=service_account_email,
                token_uri="https://oauth2.googleapis.com/token",
                scopes=[GMAIL_METADATA_SCOPE],
                subject=REPORTING_MAILBOX,
            )
    gmail = build("gmail", "v1", credentials=credentials, cache_discovery=False)
    return subject, {value.casefold() for value in RECIPIENTS}, gmail, target


def _keyless_expected(
    *,
    target: str,
    mailbox: str,
    recipients: list[str],
    service_account_email: str,
) -> tuple[str, set[str], object, str]:
    from googleapiclient.discovery import build
    import google.auth
    from google.auth import iam
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    source, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    credentials = service_account.Credentials(
        signer=iam.Signer(Request(), source, service_account_email),
        service_account_email=service_account_email,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=[GMAIL_METADATA_SCOPE],
        subject=mailbox,
    )
    gmail = build("gmail", "v1", credentials=credentials, cache_discovery=False)
    subject = f"StreetSmart Yesterday Accountability — {target}"
    return subject, {value.casefold() for value in recipients}, gmail, target


def _find_exact(gmail, subject: str, expected_recipients: set[str]) -> bool:
    response = gmail.users().messages().list(
        userId="me",
        labelIds=["SENT"],
        maxResults=100,
        includeSpamTrash=False,
    ).execute()
    for item in response.get("messages", []):
        message = gmail.users().messages().get(
            userId="me",
            id=item["id"],
            format="metadata",
            metadataHeaders=["Subject", "To"],
        ).execute()
        headers = {
            entry.get("name", "").casefold(): entry.get("value", "")
            for entry in message.get("payload", {}).get("headers", [])
        }
        if headers.get("subject") != subject:
            continue
        recipients = {
            address.casefold()
            for _, address in getaddresses([headers.get("to", "")])
            if address
        }
        if recipients != expected_recipients:
            raise RuntimeError(
                "an exact-subject Sent message exists but its recipients do not match the approved set"
            )
        return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-root")
    parser.add_argument("--mode", choices=("preflight", "verify"), required=True)
    parser.add_argument("--target-date")
    parser.add_argument("--mailbox")
    parser.add_argument("--recipient", action="append", default=[])
    parser.add_argument("--service-account-email")
    args = parser.parse_args()

    standalone = all((
        args.target_date,
        args.mailbox,
        args.recipient,
        args.service_account_email,
    ))
    if standalone:
        subject, recipients, gmail, target = _keyless_expected(
            target=args.target_date,
            mailbox=args.mailbox,
            recipients=args.recipient,
            service_account_email=args.service_account_email,
        )
    elif args.app_root and not any((
        args.target_date,
        args.mailbox,
        args.recipient,
        args.service_account_email,
    )):
        root = Path(args.app_root).expanduser().resolve()
        subject, recipients, gmail, target = _expected(root)
    else:
        parser.error(
            "provide --app-root alone, or provide target-date, mailbox, "
            "at least one recipient, and service-account-email"
        )

    attempts = 1 if args.mode == "preflight" else 7
    found = False
    for attempt in range(attempts):
        found = _find_exact(gmail, subject, recipients)
        if found or attempt + 1 == attempts:
            break
        time.sleep(5)

    result = {
        "mode": args.mode,
        "target_date": target,
        "exact_delivery_found": found,
        "recipient_count": len(recipients),
    }
    print(json.dumps(result, sort_keys=True))

    if args.mode == "preflight" and found:
        return 20
    if args.mode == "verify" and not found:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
