#!/usr/bin/env python3
"""Send the approved link-only StreetSmart daily digest to Google Chat."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request


ENV_NAME = "STREETSMART_DAILY_REPORT_WEBHOOK_URL"
DEFAULT_KEYCHAIN_SERVICE = "streetsmart-daily-accountability-google-chat"
DEFAULT_KEYCHAIN_ACCOUNT = "carlo@streetsmart.insurance"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-date", required=True, help="Prior-business-day date")
    parser.add_argument("--document-url", required=True)
    parser.add_argument("--workbook-url", required=True)
    parser.add_argument("--dashboard-url", required=True)
    parser.add_argument("--runbook-url", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--configuration-test", action="store_true")
    parser.add_argument("--keychain-service", default=DEFAULT_KEYCHAIN_SERVICE)
    parser.add_argument("--keychain-account", default=DEFAULT_KEYCHAIN_ACCOUNT)
    return parser


def _validate_drive_link(value: str, label: str) -> str:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "https" or parsed.hostname not in {
        "docs.google.com",
        "drive.google.com",
    }:
        raise ValueError(f"{label} must be a secured Google Drive or Docs HTTPS link")
    return value


def _read_webhook_from_keychain(service: str, account: str) -> str:
    if sys.platform != "darwin":
        return ""
    result = subprocess.run(
        [
            "security",
            "find-generic-password",
            "-w",
            "-s",
            service,
            "-a",
            account,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _resolve_webhook(service: str, account: str) -> str:
    value = os.environ.get(ENV_NAME, "").strip()
    if not value:
        value = _read_webhook_from_keychain(service, account)
    if not value:
        raise RuntimeError(
            f"Webhook unavailable: inject {ENV_NAME} from a secret manager or configure the approved macOS Keychain item"
        )
    parsed = urllib.parse.urlparse(value)
    query = urllib.parse.parse_qs(parsed.query)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "chat.googleapis.com"
        or not parsed.path.startswith("/v1/spaces/")
        or not query.get("key")
        or not query.get("token")
    ):
        raise RuntimeError("Secret does not contain an approved Google Chat incoming-webhook URL")
    return value


def _payload(args: argparse.Namespace) -> dict[str, str]:
    document = _validate_drive_link(args.document_url, "document-url")
    workbook = _validate_drive_link(args.workbook_url, "workbook-url")
    dashboard = _validate_drive_link(args.dashboard_url, "dashboard-url")
    runbook = _validate_drive_link(args.runbook_url, "runbook-url")
    lead = (
        "StreetSmart daily accountability webhook configuration verified"
        if args.configuration_test
        else "StreetSmart Daily Accountability Report"
    )
    text = (
        f"{lead} — {args.report_date}\n\n"
        f"Complete team-lead report: {document}\n"
        f"All-data workbook: {workbook}\n"
        f"Visual dashboard: {dashboard}\n"
        f"Runbook: {runbook}\n\n"
        "Please scrutinize callbacks and queues, overdue tasks, policy changes, "
        "COIs, Sales Center, submissions, and Magellan, then return corrections or remarks.\n\n"
        "Client-identifying details remain in the secured report and workbook, not in Chat."
    )
    return {"text": text}


def main() -> int:
    args = _parser().parse_args()
    payload = _payload(args)
    if args.dry_run:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    webhook = _resolve_webhook(args.keychain_service, args.keychain_account)
    request = urllib.request.Request(
        webhook,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = json.loads(response.read().decode("utf-8"))
            if response.status != 200 or not body.get("name"):
                raise RuntimeError("Google Chat did not return a verified message receipt")
            print(json.dumps({"delivered": True, "message_name": body["name"]}))
            return 0
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Google Chat delivery failed with HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Google Chat delivery failed: {exc.reason}") from None


if __name__ == "__main__":
    raise SystemExit(main())
