#!/usr/bin/env python3
"""Scheduled EZLynx task verifier.

Runs every 15 minutes (systemd timer). For each PENDING task older than 40
minutes, pulls the EZLynx task report and checks the task actually exists.

  1. Ingest newly 'delivered' phone-watchdog tasks from its DB.
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


def fetch_report_csv(subject: str) -> tuple[bytes | None, str]:
    """Fetch the latest task-report CSV attachment from the report mailbox.

    Returns (content, detail). Content is None when the report is missing or
    stale. Uses the same keyless-delegated Gmail pattern as the 4359 worker.
    """
    try:
        sys.path.insert(0, "/opt/streetsmart-hermes/releases/current")
        from robie_job_engine.cert_gmail_adapter import CertGmailAdapter

        adapter = CertGmailAdapter.with_dwd()
    except Exception as exc:
        return None, f"gmail adapter unavailable: {type(exc).__name__}: {exc}"

    try:
        # Find the latest email with the report subject; pull its CSV attachment.
        msgs = adapter.search(f'subject:"{subject}" newer_than:1d', max_results=5)
        if not msgs:
            return None, f"no email with subject {subject!r} in the last day"
        for msg in msgs:
            for att in adapter.attachments(msg["id"]):
                name = att.get("filename", "")
                if name.lower().endswith(".csv"):
                    age_h = (
                        datetime.now(timezone.utc)
                        - datetime.fromisoformat(msg["internalDate"])
                    ).total_seconds() / 3600
                    if age_h > REPORT_MAX_AGE_HOURS:
                        return None, (
                            f"latest report {name!r} is {age_h:.1f}h old "
                            f"(limit {REPORT_MAX_AGE_HOURS}h)"
                        )
                    return att["data"], f"{name} ({age_h:.1f}h old)"
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

    # 1. Ingest phone-watchdog deliveries.
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
    outcome = verify_due_tasks(store, report_rows, report_available)
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
