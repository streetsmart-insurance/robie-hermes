#!/usr/bin/env python3
"""Daily 4359 policy-change follow-up runner (runnable entry point).

Phase 2 of the 4359 worker. Every day it:

  1. Pulls the live 4359 report (same keyless-delegated Gmail path as
     phase 1) to know which change requests are still Open.
  2. Reads CSR replies in robie@streetsmart.insurance, matches them to
     the nag threads phase 1 recorded (thread IDs in the sent store),
     and classifies each reply: in_progress / blocked / docs_claimed /
     no_signal. Ambiguous or automated replies never move the status.
     A genuine signal suppresses further Tuesday nags for that change.
  3. For docs_claimed changes: checks the applicant's documents via the
     read-only DocumentApi. The ONLY machine confirmation is the change
     dropping off the open 4359 queue; anything else is needs_human
     (both sides summarized, never guessed) or discrepancy (exact field
     mismatches).
  4. On Tuesdays: emails Carlo the 14-day escalation digest for changes
     whose first nag is more than 14 days ago and which are not
     confirmed. Each change escalates once.

Modes:
  dry-run (default): everything runs for real EXCEPT the escalation
      mailer and all state saves. Nothing is sent; sent/follow-up stores
      are untouched.
  live: sends the escalation digest (Tuesdays only) and persists state.

The worker never writes to EZLynx and never deletes anything.

Exit codes: 0 = run succeeded; 2 = fail-closed (contract error:
stale/missing report, unconfigured mailbox); 1 = unexpected failure.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

from .overdue_policy_change_reports import (
    ACTION,
    NotificationStore,
    PolicyChangeReportContractError,
    _name_key,
    load_approved_csr_directory,
)
from .policy_change_followup import (
    JOB_TYPE,
    PolicyChangeFollowupWorker,
    default_followup_store_path,
)

logger = logging.getLogger("run_policy_change_followup")

DEFAULT_SENT_STORE = "~/.robie/overdue_policy_change_reports/sent.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Daily 4359 policy-change follow-up runner (phase 2).")
    parser.add_argument(
        "--mode", choices=("dry-run", "live"),
        default=os.environ.get("ROBIE_4359_FOLLOWUP_MODE", "dry-run"),
        help="dry-run (default): no sends, no state writes. live: send + persist.")
    parser.add_argument(
        "--manifest", default=os.environ.get("ROBIE_4359_MANIFEST", ""),
        help="Path to the approved roster manifest JSON (optional; enables "
             "the terminated-CSR reply check).")
    parser.add_argument(
        "--sent-store", default=os.environ.get("ROBIE_4359_SENT_STORE", DEFAULT_SENT_STORE),
        help="Path to phase 1's NotificationStore JSON (thread IDs live here).")
    parser.add_argument(
        "--followup-store", default=os.environ.get("ROBIE_4359_FOLLOWUP_STORE", ""),
        help="Path to the follow-up store JSON "
             f"(default: {default_followup_store_path()}).")
    parser.add_argument(
        "--report-mailbox", default=os.environ.get("ROBIE_4359_REPORT_MAILBOX", ""),
        help="Mailbox holding the daily 4359 CSV and the CSR reply threads.")
    parser.add_argument(
        "--report-max-age-hours", type=int,
        default=int(os.environ.get("ROBIE_4359_REPORT_MAX_AGE_HOURS", "48")),
        help="Fail closed when the newest 4359 report is older than this.")
    parser.add_argument(
        "--report-allowed-sender-domains",
        default=os.environ.get("ROBIE_4359_REPORT_ALLOWED_SENDER_DOMAINS", ""),
        help="Comma-separated sender domains accepted for the 4359 report email.")
    parser.add_argument(
        "--evidence-out", default="",
        help="Write the run evidence JSON here (default: stdout only).")
    return parser


def build_csr_is_active(manifest: str):
    """Reply-from-terminated-CSR check. None when no manifest is given."""
    manifest = str(manifest or "").strip()
    if not manifest:
        return None
    roster = load_approved_csr_directory(manifest)
    directory = roster.get("directory") or {}

    def csr_is_active(csr: str) -> bool:
        return _name_key(csr) in directory

    return csr_is_active


def run(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    today = date.today()
    payload: dict[str, Any] = {
        "sent_store_path": str(args.sent_store),
        "followup_store_path": (
            str(args.followup_store or "").strip() or default_followup_store_path()
        ),
    }
    if args.report_mailbox:
        payload["report_mailbox"] = str(args.report_mailbox)
    payload["report_max_age_hours"] = int(args.report_max_age_hours)
    sender_domains = str(args.report_allowed_sender_domains or "").strip()
    payload["report_allowed_sender_domains"] = [
        d.strip() for d in sender_domains.split(",") if d.strip()
    ] or ["ezlynx.com", "appliedsystems.com"]

    job = {"action_type": ACTION, "payload": payload}
    logger.info("idempotency key: 4359-followup-%s", today.isoformat())

    captured: list[dict[str, Any]] = []
    dry_run = args.mode != "live"
    worker = PolicyChangeFollowupWorker(
        # In dry-run the worker never calls the mailer: it builds an
        # inline dry-run receipt instead, so a default-constructed worker
        # can never send by accident. In live mode the default mailer
        # sends via keyless-delegated Gmail.
        sent_store=NotificationStore(args.sent_store),
        csr_is_active=build_csr_is_active(args.manifest),
    )

    evidence = worker.perform(job, dry_run=dry_run, today=today)

    if dry_run:
        evidence["dry_run_note"] = (
            "No email was sent and no state file was modified. "
            "The escalation receipt below is what a live Tuesday run would send. "
            "Reply ingestion and confirmation verdicts ran for real."
        )
        receipt = (evidence.get("escalation") or {}).get("receipt")
        evidence["would_send_escalation"] = [receipt] if receipt else []

    if args.evidence_out:
        out = Path(args.evidence_out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(evidence, indent=2, default=str), encoding="utf-8")
        print(f"evidence written to {out}", file=sys.stderr)

    print(json.dumps({
        "ok": evidence.get("succeeded"),
        "mode": args.mode,
        "summary": {k: v for k, v in evidence.items()
                    if k in ("open_queue_rows", "tracked_changes", "reply_ingestion",
                             "confirmations", "escalation", "status_counts")},
        "error": evidence.get("error"),
    }, indent=2, default=str))
    return (0 if evidence.get("succeeded") else 2), evidence


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    try:
        code, _ = run(args)
        return code
    except PolicyChangeReportContractError as exc:
        print(f"FAIL-CLOSED: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
