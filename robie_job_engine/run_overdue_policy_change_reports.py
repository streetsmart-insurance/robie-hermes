#!/usr/bin/env python3
"""Weekly 4359 overdue policy-change runner (runnable entry point).

Pipeline (all API, no browser, read-only except the outbound email):
  1. Pull the live 4359 report: the daily "ROBIE daily CSV - 4359 Policy
     Change" email in the report mailbox (default robie@streetsmart.insurance)
     via keyless-delegated Gmail. Fails closed when the report is missing or
     older than --report-max-age-hours (default 48).
  2. Qualify: Request Status = Open AND age > 14 days. The change request's
     Open status is authoritative -- a Complete EZLynx task never suppresses it.
  3. Liveness gate per policy number via PolicyApi (dead policies excluded,
     no-result/ambiguous rows held, never emailed).
  4. DiscussionApi context per applicant (title/noteCount/lastModified only).
  5. Email each CSR for a status update; CC the CSR's department manager,
     carlo@streetsmart.insurance, Jake Ferrara, Gabriela Chutin,
     Sandy Santana, and the assigned producer. The email carries the
     abbreviated policy-change SOP closure gate.
  6. Dedupe: re-nag only when still open after RENAG_DAYS (7).

Modes:
  dry-run (default): everything above runs for real EXCEPT the mailer and
      the sent-store save. Recipients/bodies are printed and written to the
      evidence file; nothing is sent and the sent store is untouched.
  live: sends via the keyless-delegated Gmail mailer and records sends in
      the NotificationStore.

Seeding:
  --seed-file <json>: record manually-sent nag emails in the sent store so
      the first weekly run does not re-nag them. The JSON is a list of
      {"csr", "policy_number", "created_date", "sent_date"} objects.

The worker never writes to EZLynx and never deletes anything.

Exit codes: 0 = run succeeded (emails sent or nothing due); 2 = fail-closed
(contract error: stale/missing report, unconfigured roster, unresolved CSR
identity, DiscussionApi down); 1 = unexpected failure.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .overdue_policy_change_reports import (
    ACTION,
    JOB_TYPE,
    NotificationStore,
    OverduePolicyChangeReportWorker,
    OverduePolicyChangeReportVerifier,
    PolicyChangeReportContractError,
    default_mailer,
)

logger = logging.getLogger("run_overdue_policy_change_reports")

DEFAULT_SENT_STORE = "~/.robie/overdue_policy_change_reports/sent.json"


class DryRunNotificationStore(NotificationStore):
    """A NotificationStore that never persists.

    is_due() checks run against the real store contents (so the dry run
    shows what a live run would do), but mark_sent() results are discarded.
    """

    def save(self) -> None:  # noqa: D102
        logger.info("dry-run: sent store NOT saved (would persist %d keys)", len(self._sent))


def dry_run_mailer_factory(captured: list[dict[str, Any]]):
    """Fake mailer: record what would be sent, send nothing."""

    def mailer(*, to: list[str], cc: list[str], subject: str,
               text_body: str, html_body: str) -> dict[str, Any]:
        record = {
            "kind": "dry-run",
            "destination": list(to),
            "cc": list(cc),
            "subject": subject,
            "text_body": text_body,
            "html_body": html_body,
            "message_id": f"dry-run-{len(captured)}",
            "sender": "robie@streetsmart.insurance",
        }
        captured.append(record)
        return record

    return mailer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Weekly 4359 overdue policy-change CSR nag runner.")
    parser.add_argument(
        "--mode", choices=("dry-run", "live"), default=os.environ.get("ROBIE_4359_MODE", "dry-run"),
        help="dry-run (default): no sends, no sent-store writes. live: send for real.")
    parser.add_argument(
        "--manifest", default=os.environ.get("ROBIE_4359_MANIFEST", ""),
        help="Path to the approved roster manifest JSON (required).")
    parser.add_argument(
        "--sent-store", default=os.environ.get("ROBIE_4359_SENT_STORE", DEFAULT_SENT_STORE),
        help="Path to the NotificationStore JSON.")
    parser.add_argument(
        "--exclusions-path", default=os.environ.get("ROBIE_4359_EXCLUSIONS", ""),
        help="Optional stale-row denylist JSON.")
    parser.add_argument(
        "--producer-fallbacks-path", default=os.environ.get("ROBIE_4359_PRODUCER_FALLBACKS", ""),
        help="Optional producer email fallbacks JSON.")
    parser.add_argument(
        "--report-mailbox", default=os.environ.get("ROBIE_4359_REPORT_MAILBOX", ""),
        help="Mailbox holding the daily 4359 CSV (default robie@streetsmart.insurance).")
    parser.add_argument(
        "--report-max-age-hours", type=int,
        default=int(os.environ.get("ROBIE_4359_REPORT_MAX_AGE_HOURS", "48")),
        help="Fail closed when the newest 4359 report is older than this.")
    parser.add_argument(
        "--evidence-out", default="",
        help="Write the run evidence JSON here (default: stdout only).")
    parser.add_argument(
        "--seed-file", default="",
        help="Seed the sent store from this JSON file and exit (no emails).")
    parser.add_argument(
        "--idempotency-key", default="",
        help="Override the idempotency key (default 4359-weekly-YYYY-MM-DD).")
    return parser


def seed_sent_store(seed_path: str, sent_store_path: str) -> dict[str, Any]:
    """Record manually-sent nag emails in the sent store.

    Seed entries: {"csr", "policy_number", "created_date", "sent_date"}.
    created_date is the 4359 "Change Request Created Date" (the dedup key).
    """
    entries = json.loads(Path(seed_path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(entries, list) or not entries:
        raise PolicyChangeReportContractError(f"seed file {seed_path} has no entries")
    store = NotificationStore(sent_store_path)
    seeded: list[dict[str, str]] = []
    for entry in entries:
        csr = str(entry.get("csr") or "").strip()
        policy = str(entry.get("policy_number") or "").strip()
        created = str(entry.get("created_date") or "").strip()
        sent = str(entry.get("sent_date") or "").strip()
        if not (csr and policy and created and sent):
            raise PolicyChangeReportContractError(f"seed entry missing fields: {entry!r}")
        sent_date = datetime.strptime(sent[:10], "%Y-%m-%d").date()
        store.mark_sent({"CSR": csr, "Policy Number": policy, "created_date": created}, sent_date)
        seeded.append({"csr": csr, "policy_number": policy,
                       "created_date": created, "sent_date": sent_date.isoformat()})
    store.save()
    return {"seeded": seeded, "sent_store": str(Path(sent_store_path).expanduser())}


def run(args: argparse.Namespace, *, verifier_factory=None) -> tuple[int, dict[str, Any]]:
    today = date.today()
    if args.seed_file:
        result = seed_sent_store(args.seed_file, args.sent_store)
        print(json.dumps({"ok": True, "seed": result}, indent=2))
        return 0, result

    manifest = str(args.manifest or "").strip()
    if not manifest:
        print("error: --manifest (approved roster manifest) is required", file=sys.stderr)
        return 2, {"error": "manifest required"}

    payload: dict[str, Any] = {"manifest_path": manifest}
    if args.sent_store:
        payload["sent_store_path"] = str(args.sent_store)
    if args.exclusions_path:
        payload["exclusions_path"] = str(args.exclusions_path)
    if args.producer_fallbacks_path:
        payload["producer_fallbacks_path"] = str(args.producer_fallbacks_path)
    if args.report_mailbox:
        payload["report_mailbox"] = str(args.report_mailbox)
    payload["report_max_age_hours"] = int(args.report_max_age_hours)

    job = {"action_type": ACTION, "payload": payload}
    idempotency_key = args.idempotency_key or f"4359-weekly-{today.isoformat()}"

    captured: list[dict[str, Any]] = []
    dry_run = args.mode != "live"
    if dry_run:
        store: NotificationStore = DryRunNotificationStore(args.sent_store)
        worker = OverduePolicyChangeReportWorker(
            mailer=dry_run_mailer_factory(captured),
            sent_store=store,
        )
    else:
        worker = OverduePolicyChangeReportWorker(
            mailer=default_mailer,
            sent_store=NotificationStore(args.sent_store),
        )

    try:
        result = worker.perform(job, idempotency_key=idempotency_key)
    except PolicyChangeReportContractError as exc:
        print(f"FAIL-CLOSED: {exc}", file=sys.stderr)
        return 2, {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1, {"error": f"{type(exc).__name__}: {exc}"}

    evidence: dict[str, Any] = {
        "mode": args.mode,
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "job_type": JOB_TYPE,
        "succeeded": result.succeeded,
        "error": result.error,
        "hold_status": str(result.hold_status) if result.hold_status else None,
        "summary": result.destination,
        "detail": result.detail,
        "would_send" if dry_run else "sent": captured if dry_run else result.destination.get("delivery_receipts"),
    }
    if dry_run:
        evidence["dry_run_note"] = (
            "No email was sent and the sent store was not modified. "
            "Recipients and bodies below are what a live run would produce. "
            "Delivery verification is skipped in dry-run (nothing was sent)."
        )
        evidence["verification"] = {"skipped": True, "reason": "dry-run sent nothing"}
    elif result.succeeded:
        # --live: prove what was sent and what was held via Gmail read-back.
        try:
            factory = verifier_factory or OverduePolicyChangeReportVerifier
            verification = factory().verify(
                job, {"destination": result.destination})
            evidence["verification"] = {
                "verified": verification.verified,
                "error": verification.error,
                "expected": verification.evidence.expected,
                "observed": verification.evidence.observed,
                "method": verification.evidence.method,
                "captured_at": verification.evidence.captured_at,
            }
        except Exception as exc:  # noqa: BLE001
            evidence["verification"] = {
                "verified": False,
                "error": f"UNVERIFIED: verifier raised {type(exc).__name__}: {exc}",
            }

    if args.evidence_out:
        out = Path(args.evidence_out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(evidence, indent=2, default=str), encoding="utf-8")
        print(f"evidence written to {out}", file=sys.stderr)

    print(json.dumps({
        "ok": result.succeeded,
        "mode": args.mode,
        "summary": result.destination,
        "error": result.error,
    }, indent=2, default=str))
    return (0 if result.succeeded else 2), evidence


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    code, _ = run(args)
    return code


if __name__ == "__main__":
    sys.exit(main())
