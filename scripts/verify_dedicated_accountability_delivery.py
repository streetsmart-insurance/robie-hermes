#!/usr/bin/env python3
"""Preflight or verify exact Gmail delivery for the dedicated accountability run."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from email.utils import getaddresses
from pathlib import Path
from zoneinfo import ZoneInfo


GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


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
    credentials = delegated_credentials([GMAIL_READONLY_SCOPE], REPORTING_MAILBOX)
    gmail = build("gmail", "v1", credentials=credentials, cache_discovery=False)
    return subject, {value.casefold() for value in RECIPIENTS}, gmail, target


def _find_exact(gmail, subject: str, expected_recipients: set[str]) -> bool:
    response = gmail.users().messages().list(
        userId="me",
        q=f'in:sent newer_than:7d subject:"{subject}"',
        maxResults=20,
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
    parser.add_argument("--app-root", required=True)
    parser.add_argument("--mode", choices=("preflight", "verify"), required=True)
    args = parser.parse_args()

    root = Path(args.app_root).expanduser().resolve()
    subject, recipients, gmail, target = _expected(root)

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
