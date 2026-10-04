#!/usr/bin/env python3
"""Daily 4359 liveness publisher (runnable entry point).

Pipeline (all API, no browser, read-only except the Google Sheet publish):
  1. Pull the live 4359 report: the daily "ROBIE daily CSV - 4359 Policy
     Change" email in the report mailbox (default robie@streetsmart.insurance)
     via keyless-delegated Gmail. Fails closed when the report is missing or
     older than --report-max-age-hours (default 48).
  2. Take ALL rows with Request Status = Open -- no 14-day cutoff. The
     downstream daily accountability report needs liveness for every open
     change request, including brand-new ones.
  3. Liveness gate per policy number via PolicyApi: the same
     classify_policy_liveness() used by the weekly 4359 nag worker
     (LIVE / DEAD / HOLD verdicts, never auto-close).
  4. Publish one row per open request to a Google Sheet tab so the
     accountability report (which runs on a different VM) can filter out
     DEAD policies without calling the PolicyApi itself.

     Columns: Policy Number | Account Name | Applicant ID | Verdict | Reason |
     Checked Date

Modes:
  dry-run (default): runs the fetch + classification for real but writes
      nothing to the sheet. Prints the summary and (optionally) writes the
      evidence file.
  live: clears and rewrites the tab with today's verdicts.

Exit codes: 0 = run succeeded; 2 = fail-closed (contract error: stale/missing
report, unconfigured sheets/EZLynx access); 1 = unexpected failure.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .overdue_policy_change_reports import (
    ROSTER_SPREADSHEET_ID,
    PolicyChangeReportContractError,
    classify_policy_liveness,
    default_policy_search,
    default_queue_reader,
)

logger = logging.getLogger("publish_4359_liveness")

LIVENESS_TAB_DEFAULT = "4359-Liveness"
LIVENESS_COLUMNS = [
    "Policy Number",
    "Account Name",
    "Applicant ID",
    "Verdict",
    "Reason",
    "Checked Date",
]

SHEETS_WRITE_SCOPE = "https://www.googleapis.com/auth/spreadsheets"


class LivenessPublishError(RuntimeError):
    """The liveness publish pipeline failed closed."""


def default_spreadsheet_id() -> str:
    """Which sheet gets the liveness tab.

    Defaults to the spreadsheet the 4359 worker already reads (the service
    account already has access); override with ROBIE_4359_LIVENESS_SPREADSHEET_ID
    or --spreadsheet-id.
    """
    return str(os.environ.get("ROBIE_4359_LIVENESS_SPREADSHEET_ID") or ROSTER_SPREADSHEET_ID).strip()


def open_status_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Every 4359 row whose Request Status is Open (all ages, no 14-day bar)."""
    return [
        row for row in rows
        if str(row.get("Request Status") or "").strip().casefold() == "open"
    ]


def build_liveness_rows(
    open_rows: list[dict[str, str]],
    policy_search: Callable[[str], list[dict[str, Any]]],
    today: date,
) -> list[dict[str, str]]:
    """Run classify_policy_liveness() on each open row; return sheet-ready rows."""
    results: list[dict[str, str]] = []
    checked = today.isoformat()
    for row in open_rows:
        policy_number = str(row.get("Policy Number") or "").strip()
        account_name = str(row.get("Account Name") or "").strip()
        applicant_id = str(row.get("Applicant ID") or "").strip()
        try:
            verdict = classify_policy_liveness(
                policy_search, policy_number, applicant_id, today
            )
            verdict_name = str(verdict.get("verdict") or "HOLD").strip().upper()
            reason = str(verdict.get("reason") or "").strip()
            if verdict_name not in ("LIVE", "DEAD", "HOLD"):
                verdict_name = "HOLD"
                reason = f"unexpected classifier verdict {verdict.get('verdict')!r}; {reason}".strip("; ")
        except Exception as exc:  # noqa: BLE001 -- a lookup failure holds the row, never drops it
            verdict_name = "HOLD"
            reason = f"liveness lookup failed: {type(exc).__name__}: {exc}"
        results.append({
            "Policy Number": policy_number,
            "Account Name": account_name,
            "Applicant ID": applicant_id,
            "Verdict": verdict_name,
            "Reason": reason,
            "Checked Date": checked,
        })
    return results


def _sheets_write_service() -> Any:
    """Sheets API client with write scope (same auth pattern as sheets_sync)."""
    import google.auth
    from googleapiclient.discovery import build

    token_file = os.environ.get("ROBIE_GOOGLE_TOKEN_FILE", "").strip()
    if token_file:
        from google.oauth2.credentials import Credentials

        creds = Credentials.from_authorized_user_file(token_file)
    else:
        creds, _ = google.auth.default(scopes=[SHEETS_WRITE_SCOPE])
        if getattr(creds, "requires_scopes", False):
            creds = creds.with_scopes([SHEETS_WRITE_SCOPE])
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _tab_exists(service: Any, spreadsheet_id: str, tab: str) -> bool:
    metadata = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets(properties(title))",
    ).execute()
    titles = {
        sheet.get("properties", {}).get("title", "")
        for sheet in (metadata.get("sheets") or [])
    }
    return tab in titles


def ensure_tab(service: Any, spreadsheet_id: str, tab: str) -> bool:
    """Create the liveness tab when it is missing. Returns True if created."""
    if _tab_exists(service, spreadsheet_id, tab):
        return False
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": tab}}}]},
    ).execute()
    return True


def write_liveness_tab(
    service: Any,
    spreadsheet_id: str,
    tab: str,
    rows: list[Mapping[str, str]],
) -> dict[str, Any]:
    """Clear the tab and rewrite header + rows. Returns the Sheets receipt."""
    created = ensure_tab(service, spreadsheet_id, tab)
    values = [LIVENESS_COLUMNS]
    for row in rows:
        values.append([str(row.get(column) or "") for column in LIVENESS_COLUMNS])
    range_a1 = f"'{tab}'!A1:F"
    service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id, range=range_a1
    ).execute()
    receipt = service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=range_a1,
        valueInputOption="RAW",
        body={"values": values},
    ).execute()
    return {
        "tab": tab,
        "tab_created": created,
        "rows_written": len(rows),
        "updated_cells": receipt.get("updatedCells"),
    }


def run(
    *,
    queue_reader: Callable[[Mapping[str, Any]], list[dict[str, str]]] = default_queue_reader,
    policy_search: Callable[[str], list[dict[str, Any]]] = default_policy_search,
    spreadsheet_id: str,
    tab: str,
    payload: Mapping[str, Any] | None = None,
    today: date | None = None,
    dry_run: bool = True,
    sheets_service: Any = None,
) -> tuple[int, dict[str, Any]]:
    """Fetch 4359, classify every Open row, publish the liveness tab."""
    run_date = today or date.today()
    try:
        rows = queue_reader(dict(payload or {}))
        open_rows = open_status_rows(rows)
        liveness_rows = build_liveness_rows(open_rows, policy_search, run_date)
        live = sum(1 for row in liveness_rows if row["Verdict"] == "LIVE")
        dead = sum(1 for row in liveness_rows if row["Verdict"] == "DEAD")
        hold = sum(1 for row in liveness_rows if row["Verdict"] == "HOLD")
        summary: dict[str, Any] = {
            "resource_id": "robie-4359-liveness",
            "queue_rows": len(rows),
            "open_rows": len(open_rows),
            "live": live,
            "dead": dead,
            "hold": hold,
            "checked_date": run_date.isoformat(),
        }
        if dry_run:
            summary["mode"] = "dry-run"
            summary["published"] = False
            return 0, summary
        service = sheets_service if sheets_service is not None else _sheets_write_service()
        receipt = write_liveness_tab(service, spreadsheet_id, tab, liveness_rows)
        summary["mode"] = "live"
        summary["published"] = True
        summary["spreadsheet_id"] = spreadsheet_id
        summary["publish_receipt"] = receipt
        return 0, summary
    except (PolicyChangeReportContractError, LivenessPublishError) as exc:
        return 2, {"error": str(exc), "checked_date": run_date.isoformat()}
    except Exception as exc:  # noqa: BLE001 -- unexpected failure, exit 1
        logger.exception("4359 liveness publish failed unexpectedly")
        return 1, {"error": f"{type(exc).__name__}: {exc}",
                   "checked_date": run_date.isoformat()}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Daily 4359 liveness publisher: classify every Open 4359 "
                    "row via PolicyApi and publish verdicts to a Google Sheet tab.")
    parser.add_argument(
        "--mode", choices=("dry-run", "live"),
        default=os.environ.get("ROBIE_4359_LIVENESS_MODE", "dry-run"),
        help="dry-run (default): no sheet writes. live: clear and rewrite the tab.")
    parser.add_argument(
        "--spreadsheet-id", default="",
        help="Google Sheet for the liveness tab "
             "(default ROBIE_4359_LIVENESS_SPREADSHEET_ID, else the 4359 roster sheet).")
    parser.add_argument(
        "--tab", default=os.environ.get("ROBIE_4359_LIVENESS_TAB", LIVENESS_TAB_DEFAULT),
        help="Tab to publish verdicts to (default 4359-Liveness).")
    parser.add_argument(
        "--report-mailbox", default=os.environ.get("ROBIE_4359_REPORT_MAILBOX", ""),
        help="Mailbox holding the daily 4359 CSV (default robie@streetsmart.insurance).")
    parser.add_argument(
        "--report-max-age-hours", type=int,
        default=int(os.environ.get("ROBIE_4359_REPORT_MAX_AGE_HOURS", "48")),
        help="Fail closed when the newest 4359 report is older than this.")
    parser.add_argument(
        "--report-allowed-sender-domains",
        default=os.environ.get("ROBIE_4359_REPORT_ALLOWED_SENDER_DOMAINS", ""),
        help="Comma-separated sender domains accepted for the 4359 report email "
             "(default: ezlynx.com,appliedsystems.com).")
    parser.add_argument(
        "--evidence-out", default="",
        help="Write the run evidence JSON here (default: stdout only).")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload: dict[str, Any] = {}
    if args.report_mailbox:
        payload["report_mailbox"] = str(args.report_mailbox)
    payload["report_max_age_hours"] = int(args.report_max_age_hours)
    sender_domains = str(args.report_allowed_sender_domains or "").strip()
    payload["report_allowed_sender_domains"] = [
        d.strip() for d in sender_domains.split(",") if d.strip()
    ] or ["ezlynx.com", "appliedsystems.com"]

    spreadsheet_id = str(args.spreadsheet_id or "").strip() or default_spreadsheet_id()
    if not spreadsheet_id:
        print("error: --spreadsheet-id (or ROBIE_4359_LIVENESS_SPREADSHEET_ID) is required",
              file=sys.stderr)
        return 2
    tab = str(args.tab or "").strip() or LIVENESS_TAB_DEFAULT

    exit_code, summary = run(
        spreadsheet_id=spreadsheet_id,
        tab=tab,
        payload=payload,
        dry_run=args.mode != "live",
    )
    ran_at = datetime.now(timezone.utc).isoformat()
    evidence = {"ran_at": ran_at, "succeeded": exit_code == 0, **summary}
    text = json.dumps(evidence, indent=2)
    if args.evidence_out:
        Path(args.evidence_out).expanduser().write_text(text + "\n", encoding="utf-8")
    print(text)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
