#!/usr/bin/env python3
"""Collect, publish, and deliver the weekday department-tab Google Doc.

Collect 6:45 AM ET. Publish and deliver 9:00 AM ET. Late-check 9:20 AM ET.
This is not a Hermes markdown duplicate and does not send from this script
unless --deliver is explicit. Tests and agents must not pass --deliver
against Production.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from robie_job_engine.accountability_pipeline import needs_gmail_service, run_stages
from robie_job_engine.scheduled_report_ingest import MISSING_SUBJECTS_EXIT


def _gmail_service():
    import google.auth
    from google.auth import iam
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    sender = os.environ.get("ACCOUNTABILITY_REPORT_MAILBOX", "robie@streetsmart.insurance").strip()
    account = os.environ.get("ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT", "").strip()
    if not account:
        raise SystemExit("ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT is required for collect")
    source, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    delegated = service_account.Credentials(
        signer=iam.Signer(Request(), source, account),
        service_account_email=account,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        subject=sender,
    )
    return build("gmail", "v1", credentials=delegated, cache_discovery=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="StreetSmart department-tab accountability pipeline")
    parser.add_argument("--collect", action="store_true", help="6:45 AM ET mailbox ingest")
    parser.add_argument("--publish", action="store_true", help="9:00 AM ET Doc publish marker")
    parser.add_argument("--deliver", action="store_true", help="9:00 AM ET team-lead email")
    parser.add_argument("--late-check", action="store_true", help="9:20 AM ET success-marker check")
    parser.add_argument(
        "--state-root",
        default=os.environ.get(
            "ACCOUNTABILITY_STATE_ROOT",
            "/opt/streetsmart-hermes/accountability/department-doc",
        ),
    )
    args = parser.parse_args(argv)
    if not any((args.collect, args.publish, args.deliver, args.late_check)):
        parser.error("choose --collect, --publish, --deliver, and/or --late-check")
    now = datetime.now(timezone.utc)
    service = None
    if needs_gmail_service(
        collect_enabled=args.collect,
        publish_enabled=args.publish,
        state_root=Path(args.state_root),
        now=now,
    ):
        service = _gmail_service()
    try:
        result = run_stages(
            collect_enabled=args.collect,
            publish_enabled=args.publish,
            deliver_enabled=args.deliver,
            late_check_enabled=args.late_check,
            service=service,
            state_root=Path(args.state_root),
            now=now,
        )
    except SystemExit as exc:
        return int(exc.code or MISSING_SUBJECTS_EXIT)
    print(json.dumps(result, indent=2, default=str))
    deliver = result.get("deliver") or {}
    late = result.get("late_check") or {}
    if deliver and not deliver.get("delivered"):
        return 1
    if late and late.get("late"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
