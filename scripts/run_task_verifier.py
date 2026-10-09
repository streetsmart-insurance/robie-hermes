#!/usr/bin/env python3
"""Scheduled EZLynx task verifier.

Runs every 15 minutes (systemd timer). For each PENDING task older than 40
minutes, pulls the EZLynx task report and checks the task actually exists.

  1. Ingest phone-watchdog handoffs (delivered or sent_to_relay) from its DB.
     sent_to_relay means the row should be verified; it is not EZLynx proof.
  2. Pull the latest task-report CSV from the report mailbox (Gmail).
  3. Match due PENDING tasks; resolve VERIFIED / MISSING / UNVERIFIED.
  4. Alert Carlo via Google Chat on MISSING or UNVERIFIED. Never Gmail.

Exit 0 = ran clean (even if tasks are MISSING — that's a finding, not a
crash). Exit 2 = the verifier itself errored.

Usage:
    run_task_verifier.py [--no-chat] [--db PATH] [--report-subject SUBJECT]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from robie_job_engine.task_verifier import (  # noqa: E402
    REPORT_MAX_AGE_HOURS,
    TASK_REPORT_ID,
    TASK_REPORT_SUBJECT,
    TaskVerificationStore,
    format_missing_alert,
    format_unverified_alert,
    ingest_phone_watchdog,
    parse_task_report_csv,
    verify_due_tasks,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("run_task_verifier")

CHAT_WEBHOOK_URL = os.environ.get("ROBIE_TASK_VERIFY_CHAT_WEBHOOK_URL", "") or os.environ.get(
    "ROBIE_GOOGLE_CHAT_WEBHOOK_URL", ""
)

# Mailbox receiving the hourly "ROBIE task report CSV" emails (Applied
# Reporting scheduled send, set up by Carlo). The CertGmailAdapter default
# is the certificates@ mailbox, which never receives this report.
REPORT_MAILBOX = os.environ.get(
    "ROBIE_TASK_REPORT_MAILBOX", "robie@streetsmart.insurance"
)


def send_chat_alert(text: str) -> bool:
    if not CHAT_WEBHOOK_URL:
        logger.warning("no chat webhook configured; alert NOT sent:\n%s", text)
        return False
    try:
        req = urllib.request.Request(
            CHAT_WEBHOOK_URL,
            data=json.dumps({"text": text}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30):
            pass
        return True
    except Exception as exc:
        logger.error("chat alert failed: %s", exc)
        return False


def _iter_csv_attachments(full_msg: dict) -> "list[tuple[str, str]]":
    """Yield (filename, attachment_id) for CSV attachments in a Gmail full message.

    Walks MIME parts recursively; the adapter only exposes get_full_message
    and get_attachment_bytes (there is no search()/attachments() method).
    """
    found: "list[tuple[str, str]]" = []
    stack = list((full_msg.get("payload", {}) or {}).get("parts", []) or [])
    while stack:
        part = stack.pop()
        filename = part.get("filename", "") or ""
        body = part.get("body", {}) or {}
        if filename.lower().endswith(".csv") and body.get("attachmentId"):
            found.append((filename, body["attachmentId"]))
        stack.extend(part.get("parts", []) or [])
    return found


# Gmail receive time of the report fetch_report_csv last returned. A task
# fired after this cannot be in that report (verify_due_tasks waits).
LAST_REPORT_RECEIVED_AT: datetime | None = None


def fetch_report_csv(subject: str) -> tuple[bytes | None, str]:
    """Fetch the latest task-report CSV attachment from the report mailbox.

    Returns (content, detail). Content is None when the report is missing or
    stale. Uses the same keyless-delegated Gmail pattern as the 4359 worker.

    NOTE (2026-10-01): CertGmailAdapter exposes list_message_ids /
    get_full_message / get_attachment_bytes -- it has no search() or
    attachments() method. A previous version called those and every
    verification run failed with
    "AttributeError: 'CertGmailAdapter' object has no attribute 'search'".
    """
    try:
        sys.path.insert(0, "/opt/streetsmart-hermes/releases/current")
        from robie_job_engine.cert_gmail_adapter import CertGmailAdapter

        adapter = CertGmailAdapter.with_dwd(REPORT_MAILBOX)
    except Exception as exc:
        return None, f"gmail adapter unavailable: {type(exc).__name__}: {exc}"

    try:
        # Find the latest email with the report subject; pull its CSV attachment.
        ids, _ = adapter.list_message_ids(
            f'subject:"{subject}" newer_than:1d', None, page_size=5
        )
        if not ids:
            return None, f"no email with subject {subject!r} in the last day"
        for gmail_id in ids:
            full = adapter.get_full_message(gmail_id)
            csv_atts = _iter_csv_attachments(full)
            if not csv_atts:
                continue
            name, attachment_id = csv_atts[0]
            # internalDate is millis since epoch as a string.
            internal_ms = int(full.get("internalDate", "0") or 0)
            age_h = (
                datetime.now(timezone.utc)
                - datetime.fromtimestamp(internal_ms / 1000, tz=timezone.utc)
            ).total_seconds() / 3600
            if age_h > REPORT_MAX_AGE_HOURS:
                return None, (
                    f"latest report {name!r} is {age_h:.1f}h old "
                    f"(limit {REPORT_MAX_AGE_HOURS}h)"
                )
            data = adapter.get_attachment_bytes(gmail_id, attachment_id)
            global LAST_REPORT_RECEIVED_AT
            LAST_REPORT_RECEIVED_AT = (
                datetime.fromtimestamp(internal_ms / 1000, tz=timezone.utc)
                if internal_ms
                else None
            )
            return data, f"{name} ({age_h:.1f}h old)"
        return None, "no CSV attachment found on recent report emails"
    except Exception as exc:
        return None, f"report fetch failed: {type(exc).__name__}: {exc}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-chat", action="store_true")
    parser.add_argument("--db", default=os.environ.get("ROBIE_TASK_VERIFY_DB", ""))
    parser.add_argument("--report-subject", default=TASK_REPORT_SUBJECT)
    args = parser.parse_args()

    store = TaskVerificationStore(args.db or None)

    # 1. Ingest phone-watchdog handoffs (delivered or sent_to_relay).
    #    sent_to_relay is queued for report verification, not treated as delivery.
    try:
        ingest_phone_watchdog(store)
    except Exception as exc:
        logger.error("ingest failed: %s", exc)

    # 2. Pull the task report — unless the report ID isn't configured yet.
    report_rows = None
    report_available = False
    report_detail = ""
    if not TASK_REPORT_ID:
        report_detail = (
            "ROBIE_TASK_REPORT_ID is not set — the EZLynx task report has not "
            "been created yet (browser recon required). Verification is "
            "INCONCLUSIVE until the report exists."
        )
        logger.warning(report_detail)
    else:
        content, report_detail = fetch_report_csv(args.report_subject)
        if content:
            report_rows = parse_task_report_csv(content)
            report_available = True
            logger.info("report OK: %s (%d rows)", report_detail, len(report_rows))
        else:
            logger.warning("report unavailable: %s", report_detail)

    # 3. Verify due tasks.
    outcome = verify_due_tasks(
        store,
        report_rows,
        report_available,
        report_received_at=LAST_REPORT_RECEIVED_AT if report_available else None,
    )
    counts = store.counts()
    logger.info(
        "verified=%d missing=%d unverified=%d queue=%s",
        len(outcome["verified"]),
        len(outcome["missing"]),
        len(outcome["unverified"]),
        counts,
    )

    # 4. Alert on MISSING or UNVERIFIED. Never silently pass.
    if outcome["missing"]:
        text = format_missing_alert(outcome["missing"])
        if not args.no_chat:
            send_chat_alert(text)
        else:
            logger.warning("--no-chat: alert suppressed:\n%s", text)
    if outcome["unverified"]:
        text = format_unverified_alert(outcome["unverified"], report_detail)
        if not args.no_chat:
            send_chat_alert(text)
        else:
            logger.warning("--no-chat: alert suppressed:\n%s", text)

    return 0


if __name__ == "__main__":
    sys.exit(main())
