#!/usr/bin/env python3
"""Fetch one day's four worker CSVs from the robie@ mailbox via DWD.

Pipeline per requested day:
  1. DWD impersonation of robie@streetsmart.insurance (domain-wide
     delegation from the hermes-poc service account).
  2. Envelope filter: subject + sender allowlist + Gmail date window, via
     gmail_report_ingestion.list_candidate_emails — which paginates
     properly (maxResults=100/page, bounded 10 pages) instead of the old
     maxResults=20 single page that silently missed 4247 and 4372.
  3. Strict delivery-date enforcement: the message's internalDate must fall
     on the requested day in America/New_York. Out-of-window deliveries are
     rejected, never "close enough".
  4. Routing by CSV header fingerprint (the envelope carries no report
     identity); dedupe by (message_id, attachment sha256) so a redelivery
     never overwrites silently.
  5. Atomic write (tmp file + os.replace) so a partial download can never
     present as a complete CSV.

This is a PROVISIONING script, not production automation. Live worker runs
consume validated CSVs through ingest_daily_reports' destination-verified
path; Dusty owns the Production promotion decision. Do not present this
script as production-ready.

Usage:
    fetch_worker_csvs_dwd.py [--day YYYY-MM-DD] [--out-dir DIR]

Defaults: --day today, --out-dir <repo>/worker-csvs/.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gmail_report_ingestion as ing

SA = "hermes-poc@streetsmart-hermes-poc.iam.gserviceaccount.com"
USER = "robie@streetsmart.insurance"
REPORT_IDS = ("4247", "4246", "4372", "4359")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT_DIR = os.path.join(REPO_ROOT, "worker-csvs")

# Report deliveries land in the mailbox; the date that matters is the
# delivery timestamp in the agency's timezone, not UTC.
try:
    from zoneinfo import ZoneInfo
    DELIVERY_TZ = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - zoneinfo is stdlib
    DELIVERY_TZ = timezone.utc


def build_dwd_service(user: str = USER):
    """Build a Gmail service impersonating `user` via domain-wide delegation.

    Google client imports are local to this function: everything else in
    this module (fetch logic, date enforcement, dedup, atomic writes) must
    stay importable and testable without the Google client libraries.
    """
    import google.auth
    from google.auth import iam
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    source, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"])
    request = Request()
    signer = iam.Signer(request, source, SA)
    delegated = service_account.Credentials(
        signer=signer,
        service_account_email=SA,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        subject=user,
    )
    delegated.refresh(request)
    return build("gmail", "v1", credentials=delegated, cache_discovery=False)


def delivery_day(message: dict) -> date:
    """The America/New_York calendar day a message was delivered."""
    ts = int(message.get("internalDate") or 0) / 1000
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(
        DELIVERY_TZ).date()


def _atomic_write(path: str, data: bytes) -> None:
    """Write data atomically: tmp file + os.replace."""
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def fetch_worker_csvs(service, *, day: date, out_dir: str,
                      report_ids=REPORT_IDS) -> dict:
    """Fetch one day's worker CSVs. Returns a summary dict.

    Strict about dates (only deliveries on `day`), duplicates (message-id +
    attachment-hash dedup), and writes (atomic). Raises
    GmailReportIngestionError when a requested report has no delivery.
    """
    os.makedirs(out_dir, exist_ok=True)
    candidates = ing.list_candidate_emails(service, day=day)

    seen_hashes: set[str] = set()
    seen_messages: set[str] = set()
    saved: dict[str, dict] = {}
    skipped: list[str] = []

    for message in candidates:
        message_id = str(message.get("id") or "")
        if message_id in seen_messages:
            skipped.append(f"SKIP {message_id}: duplicate message")
            continue
        seen_messages.add(message_id)

        got_day = delivery_day(message)
        if got_day != day:
            # The envelope query is date-windowed, but Gmail's after/before
            # boundaries are not the same as the agency-day window — enforce
            # strictly so yesterday's late delivery can't become today's CSV.
            skipped.append(
                f"SKIP {message_id}: delivered {got_day.isoformat()}, "
                f"requested {day.isoformat()}")
            continue

        try:
            filename, content = ing.download_csv_attachment(
                service, message, report_id="?")
        except ing.GmailReportIngestionError as exc:
            skipped.append(f"SKIP {message_id}: {exc}")
            continue

        content_hash = hashlib.sha256(content).hexdigest()
        if content_hash in seen_hashes:
            skipped.append(
                f"SKIP {filename} ({message_id}): duplicate attachment "
                f"(sha256 {content_hash[:12]}…) already saved")
            continue

        try:
            headers = content.decode("utf-8-sig").splitlines()[0]
            import csv as _csv
            import io as _io
            report_id = ing.fingerprint_report_id(
                next(_csv.reader(_io.StringIO(headers))))
        except Exception as exc:
            skipped.append(f"SKIP {filename} ({message_id}): "
                           f"fingerprint failed ({exc})")
            continue

        if report_id not in report_ids:
            skipped.append(
                f"SKIP {filename} ({message_id}): fingerprinted as "
                f"{report_id}, not a worker report")
            continue
        if report_id in saved:
            skipped.append(
                f"SKIP {filename} ({message_id}): already saved {report_id}")
            continue

        out = os.path.join(out_dir, f"report_{report_id}_{day.isoformat()}.csv")
        _atomic_write(out, content)
        seen_hashes.add(content_hash)
        saved[report_id] = {
            "filename": filename,
            "message_id": message_id,
            "bytes": len(content),
            "sha256": content_hash,
            "path": out,
        }

    missing = [rid for rid in report_ids if rid not in saved]
    if missing:
        raise ing.GmailReportIngestionError(
            f"no delivery found for {', '.join(missing)} on {day.isoformat()} "
            f"(checked {len(candidates)} candidate emails)")
    return {
        "day": day.isoformat(),
        "out_dir": out_dir,
        "saved": saved,
        "skipped": skipped,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", default=date.today().isoformat(),
                        help="delivery day YYYY-MM-DD (default: today)")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR,
                        help="where to save report CSVs (default: <repo>/worker-csvs/)")
    args = parser.parse_args(argv)

    day = date.fromisoformat(args.day)
    service = build_dwd_service()
    summary = fetch_worker_csvs(service, day=day, out_dir=args.out_dir)

    for rid in REPORT_IDS:
        info = summary["saved"][rid]
        print(f"saved {rid}: {info['filename']} ({info['bytes']} bytes) "
              f"-> {info['path']}")
    for line in summary["skipped"]:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
