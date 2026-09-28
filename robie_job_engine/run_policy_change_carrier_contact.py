#!/usr/bin/env python3
"""Phase 2 of the 4359 program: carrier-contact runner (runnable entry point).

For open policy changes the CSR hasn't progressed (nagged 7+ days ago),
this worker:

  1. Checks EZLynx documents via the read-only DocumentApi FIRST — an
     endorsement that already landed hands the change to phase 3 instead
     of bothering the carrier.
  1b. Cross-mailbox Gmail search (read-only domain-wide delegation):
     for each open change, searches robie@ + the assigned CSR's + the
     assigned producer's mailboxes for endorsement attachments or
     carrier replies. An endorsement found in mail counts like a
     DocumentApi hit; a carrier reply is logged and clears the
     manual-action queue. Nobody uninvolved is ever impersonated, and
     nothing is ever sent from an impersonated account.
  2. Resolves the carrier against the curated routing table and emails
     the carrier's policy-change address (email-first).
  3. Queues (never emails): unresolved carriers / missing directory
     routes (awaiting-directory), portal & phone-only routes
     (manual-action for an agent).

Modes:
  dry-run (default): everything runs for real EXCEPT the carrier mailer
      and all state saves. Nothing is sent; the carrier-contact store
      is untouched.
  live: sends carrier emails and persists state.

The worker never writes to EZLynx, never marks anything confirmed, and
fails closed when the endorsement check is unavailable.

Exit codes: 0 = run succeeded; 2 = fail-closed (contract error:
stale/missing report, unconfigured mailbox, DocumentApi outage);
1 = unexpected failure.
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

from .carrier_policy_change_routes import default_routing_table_path
from .overdue_policy_change_reports import (
    PolicyChangeReportContractError,
    load_approved_csr_directory,
    load_producer_fallbacks,
)
from .policy_change_carrier_contact import (
    JOB_TYPE,
    PolicyChangeCarrierContactWorker,
    default_carrier_store_path,
)

logger = logging.getLogger("run_policy_change_carrier_contact")

DEFAULT_SENT_STORE = "~/.robie/overdue_policy_change_reports/sent.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Phase-2 4359 carrier-contact runner.")
    parser.add_argument(
        "--mode", choices=("dry-run", "live"),
        default=os.environ.get("ROBIE_4359_CARRIER_MODE", "dry-run"),
        help="dry-run (default): no sends, no state writes. live: send + persist.")
    parser.add_argument(
        "--sent-store", default=os.environ.get("ROBIE_4359_SENT_STORE", DEFAULT_SENT_STORE),
        help="Path to phase 1's NotificationStore JSON (nag dates live here).")
    parser.add_argument(
        "--carrier-store", default=os.environ.get("ROBIE_4359_CARRIER_STORE", ""),
        help="Path to the carrier-contact store JSON "
             f"(default: {default_carrier_store_path()}).")
    parser.add_argument(
        "--routing-table", default=os.environ.get("ROBIE_4359_CARRIER_ROUTES", ""),
        help="Path to the carrier routing table JSON "
             f"(default: {default_routing_table_path()}).")
    parser.add_argument(
        "--report-mailbox", default=os.environ.get("ROBIE_4359_REPORT_MAILBOX", ""),
        help="Mailbox holding the daily 4359 CSV.")
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
    parser.add_argument(
        "--manifest", default=os.environ.get("ROBIE_4359_MANIFEST", ""),
        help="Path to the approved roster manifest JSON. When absent or "
             "unreadable the cross-mailbox search degrades to robie@-only "
             "(fail closed: no unverified impersonation).")
    parser.add_argument(
        "--producer-fallbacks",
        default=os.environ.get("ROBIE_4359_PRODUCER_FALLBACKS", ""),
        help="Path to producer work-email fallbacks JSON (optional).")
    return parser


def run(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    today = date.today()
    payload: dict[str, Any] = {
        "sent_store_path": str(args.sent_store),
        "carrier_store_path": (
            str(args.carrier_store or "").strip() or default_carrier_store_path()
        ),
        "report_max_age_hours": int(args.report_max_age_hours),
    }
    if args.report_mailbox:
        payload["report_mailbox"] = str(args.report_mailbox)
    sender_domains = str(args.report_allowed_sender_domains or "").strip()
    payload["report_allowed_sender_domains"] = [
        d.strip() for d in sender_domains.split(",") if d.strip()
    ] or ["ezlynx.com", "appliedsystems.com"]

    job = {"action_type": "contact_carriers", "payload": payload}
    logger.info("idempotency key: 4359-carrier-contact-%s", today.isoformat())

    # Approved roster for cross-mailbox search (CSR/producer -> mailbox).
    # Fail closed: without the roster the worker impersonates nobody
    # unverified and the mailbox search degrades to robie@-only.
    manifest = str(args.manifest or "").strip()
    if manifest:
        try:
            payload["roster_maps"] = load_approved_csr_directory(manifest)
        except PolicyChangeReportContractError as exc:
            logger.warning("roster unavailable (%s): mailbox search "
                           "degrades to robie@-only", exc)
    fallbacks_path = str(args.producer_fallbacks or "").strip()
    if fallbacks_path:
        try:
            payload["producer_fallbacks"] = load_producer_fallbacks(
                fallbacks_path)
        except PolicyChangeReportContractError as exc:
            logger.warning("producer fallbacks unavailable (%s): ignored",
                           exc)

    dry_run = args.mode != "live"
    worker = PolicyChangeCarrierContactWorker(
        routing_table_path=str(args.routing_table or "").strip() or None,
    )

    evidence = worker.perform(job, dry_run=dry_run, today=today)

    if dry_run:
        evidence["dry_run_note"] = (
            "No email was sent and no state file was modified. "
            "The receipts below are what a live run would send. "
            "The endorsement-landed check ran for real."
        )

    if args.evidence_out:
        out = Path(args.evidence_out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(evidence, indent=2, default=str), encoding="utf-8")
        print(f"evidence written to {out}", file=sys.stderr)

    summary = evidence.get("summary") or {}
    print(json.dumps({
        "ok": evidence.get("succeeded"),
        "mode": args.mode,
        "summary": summary,
        "receipt_count": len(evidence.get("receipts") or []),
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
