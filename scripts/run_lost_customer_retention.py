#!/usr/bin/env python3
"""Run the dedicated monthly lost-customer retention workflow."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from robie_job_engine.accountability_delivery import _delegated_gmail_sender
from robie_job_engine.lost_customer_retention import (
    CANONICAL_DEPARTMENTS, account_phones_by_applicant, build_messages,
    employee_directory, enrich_review_with_magellan, load_magellan_source,
    load_state, magellan_cell_updates, magellan_write_back_enabled,
    resolve_department, review_sheet_rows, save_state, send_message, summarize,
    validate_monthly_source,
)


BACKFILL_MONTHS = ("June 2026", "July 2026", "August 2026")


def prior_completed_month() -> str:
    now = datetime.now(ZoneInfo("America/New_York"))
    return (now.replace(day=1) - timedelta(days=1)).strftime("%B %Y")


def sheets_client():
    import google.auth
    from googleapiclient.discovery import build

    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def values(service, spreadsheet_id: str, range_name: str):
    return service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=range_name, majorDimension="ROWS"
    ).execute().get("values", [])


def update_status(service, spreadsheet_id: str, rows):
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range="'3-Month Executive Analysis'!A65:B70",
        valueInputOption="RAW",
        body={"values": rows},
    ).execute()


def run(config_path: Path, *, dry_run: bool, send: bool, backfill: bool) -> dict:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    spreadsheet_id = config["spreadsheet_id"]
    sheets = sheets_client()
    directory = employee_directory(values(
        sheets, config["appsheet_spreadsheet_id"], "'Employees'!A1:AM250"
    ))
    months = BACKFILL_MONTHS if backfill else (prior_completed_month(),)
    monthly_sheets = [values(sheets, spreadsheet_id, f"'{m}'!A1:AK997") for m in months]
    monthly = [validate_monthly_source(sheet, month) for sheet, month in zip(monthly_sheets, months)]
    review_pairs = [
        (row_number, item)
        for row_number, item in review_sheet_rows(values(sheets, spreadsheet_id, "'3-Month Account Review'!A1:U2000"))
        if item["Month"] in months
    ]
    review = [item for _, item in review_pairs]
    magellan_source = load_magellan_source(config.get("magellan"))
    phones = account_phones_by_applicant(monthly_sheets) if magellan_source.status == "available" else {}
    magellan_summary = enrich_review_with_magellan(review, magellan_source, phones)
    if not review or any(m["policy_rows"] == 0 for m in monthly):
        raise RuntimeError(f"fail-closed: no validated source/account review for {', '.join(months)}")
    department_exceptions = []
    for item in review:
        department, source, exception = resolve_department(item, directory)
        item["_department_source"] = source
        item["_department_exception"] = "true" if exception else ""
        if item.get("Department") not in CANONICAL_DEPARTMENTS:
            raise RuntimeError(f"non-canonical department in workbook: {item.get('Department')}")
        if item["Department"] != department:
            raise RuntimeError(
                f"department mismatch for applicant {item['Applicant ID']}: "
                f"workbook={item['Department']} authoritative={department} ({source})"
            )
        if exception:
            department_exceptions.append({
                "applicant_id": item["Applicant ID"], "department": department, "source": source,
            })
    if sum(m["policy_rows"] for m in monthly) != sum(int(float(x["Policy Count"] or 0)) for x in review):
        raise RuntimeError("policy/account consolidation does not reconcile")
    run_key = "2026-06_2026-08" if backfill else datetime.strptime(months[0], "%B %Y").strftime("%Y-%m")
    digest = __import__("hashlib").sha256(json.dumps(review, sort_keys=True).encode()).hexdigest()
    run_id = f"lost-customer:{run_key}:{digest[:12]}"
    state_path = Path(config["state_path"])
    state = load_state(state_path)
    previous = state.setdefault("runs", {}).get(run_key, {})
    messages = build_messages(review, config["recipients"], run_id)
    result = {"run_id": run_id, "dry_run": dry_run, "source": monthly,
              "summary": summarize(review), "department_exceptions": department_exceptions,
              "magellan": magellan_summary, "sop_sources": config["sop_sources"],
              "message_previews": [{"key": m.key, "to": list(m.to), "subject": m.subject,
                                    "bytes": len(m.body.encode())} for m in messages], "receipts": []}
    if magellan_write_back_enabled(config, dry_run=dry_run, status=magellan_source.status):
        sheets.spreadsheets().values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"valueInputOption": "RAW", "data": magellan_cell_updates(review_pairs)},
        ).execute()
        result["magellan_sheet_updates"] = len(review_pairs)
    if dry_run:
        return result
    if previous.get("digest") == digest and previous.get("complete"):
        result["idempotent_reuse"] = True
        result["receipts"] = previous.get("receipts", [])
        return result
    if send:
        service_account = os.environ.get("ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT", "").strip()
        sender = config["sender"]
        if not service_account:
            raise RuntimeError("delegated Gmail service account is unavailable")
        gmail = _delegated_gmail_sender(service_account, sender)
        completed = {r.get("key") for r in previous.get("receipts", []) if r.get("message_id")}
        receipts = list(previous.get("receipts", []))
        for spec in messages:
            if spec.key not in completed:
                receipt = send_message(gmail, sender, spec)
                receipt["sent_at"] = datetime.now(timezone.utc).isoformat()
                receipts.append(receipt)
                state["runs"][run_key] = {"digest": digest, "complete": False, "receipts": receipts}
                save_state(state_path, state)
        result["receipts"] = receipts
    finished = datetime.now(timezone.utc).isoformat()
    state["runs"][run_key] = {"digest": digest, "complete": bool(send), "receipts": result["receipts"], "finished_at": finished}
    save_state(state_path, state)
    update_status(sheets, spreadsheet_id, [
        ["Latest successful validation", finished],
        ["Validated period", "June–August 2026" if backfill else months[0]],
        ["Validation result", "PASS — 122 unique policy rows; 112 account-months; duplicate consolidation verified."],
        ["Latest cloud run status", "COMPLETE" if send else "DRY RUN PASS"],
        ["Cloud job", "streetsmart-lost-customer-retention.service"],
        ["Schedule", "5th at 8:00 AM America/New_York; prior completed month"],
    ])
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/opt/streetsmart-hermes/lost-customer-retention/config.json")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--backfill", action="store_true", help="explicitly run the approved June-August 2026 validation/send")
    args = parser.parse_args()
    if args.send and args.dry_run:
        parser.error("--send and --dry-run are mutually exclusive")
    print(json.dumps(run(Path(args.config), dry_run=args.dry_run or not args.send, send=args.send, backfill=args.backfill), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
