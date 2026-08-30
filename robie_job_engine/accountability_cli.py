"""Command-line report builder for browser-downloaded evidence exports."""

from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .center_audits import (
    audit_sales_records,
    audit_retention_records,
    audit_submission_records,
    finding_dicts,
    parse_retention_csv,
    parse_sales_csv,
    parse_submission_csv,
)
from .ezlynx_productivity_sync import EZLynxProductivityParser
from .operational_trackers import (
    TRACKER_DEFINITIONS,
    audit_tracker_csv,
    findings_as_dicts as tracker_dicts,
    voicemail_email_attestations,
)
from .productivity import ProductivityAuditor, RingCentralCall, normalize_phone
from .reporting_suite import ReportingSuite


def _first(row: dict[str, Any], *names: str) -> str:
    normalized = {re.sub(r"[^a-z0-9]", "", str(k).lower()): v for k, v in row.items() if k}
    for name in names:
        value = normalized.get(re.sub(r"[^a-z0-9]", "", name.lower()))
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _parse_timestamp(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in ("%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _duration_seconds(value: str) -> int:
    if not value:
        return 0
    if value.isdigit():
        return int(value)
    parts = value.split(":")
    try:
        numbers = [int(part) for part in parts]
    except ValueError:
        return 0
    if len(numbers) == 3:
        return numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
    if len(numbers) == 2:
        return numbers[0] * 60 + numbers[1]
    return 0


def load_ringcentral_csv(path: Path) -> tuple[list[RingCentralCall], list[str]]:
    calls: list[RingCentralCall] = []
    errors: list[str] = []
    with path.open(encoding="utf-8-sig", errors="replace", newline="") as handle:
        for row_number, row in enumerate(csv.DictReader(handle), start=2):
            timestamp = _parse_timestamp(_first(row, "Start Time", "StartTime", "Date/Time", "Time"))
            if timestamp is None:
                errors.append(f"row {row_number}: missing or invalid timestamp")
                continue
            data = {
                "call_id": _first(row, "Call ID", "Session ID", "ID") or f"ringcentral-row-{row_number}",
                "direction": _first(row, "Direction", "Call Direction"),
                "from_number": _first(row, "From", "From Number", "Caller ID"),
                "to_number": _first(row, "To", "To Number", "Dialed Number"),
                "result": _first(row, "Result", "Action", "Call Result", "Disposition"),
                "duration_seconds": _duration_seconds(_first(row, "Duration", "Duration Seconds", "Talk Time")),
                "start_time": timestamp,
                "extension": _first(row, "Extension", "Extension ID"),
                "employee_name": _first(row, "Employee", "User", "Name", "Extension Name", "Answered By"),
            }
            calls.append(RingCentralCall.from_dict(data))
    return calls, errors


def _json(path: Optional[Path]) -> dict[str, Any]:
    if path is None:
        return {"source_status": "not supplied"}
    if not path.exists():
        return {"source_status": f"missing file: {path}"}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    data.setdefault("source_status", "available")
    return data


def _call_report(
    path: Optional[Path],
    as_of: datetime,
    tasks_path: Optional[Path],
    activities_path: Optional[Path],
    magellan_data: Optional[dict[str, Any]] = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if path is None or not path.exists():
        status = "not supplied" if path is None else f"missing file: {path}"
        return {"source_status": status, "rep_stats": {}, "employee_rows": []}, {"source_status": "not supplied", "overdue_by_rep": {}}
    calls, row_errors = load_ringcentral_csv(path)
    tasks = EZLynxProductivityParser.parse_tasks_csv(tasks_path) if tasks_path and tasks_path.exists() else None
    activities = EZLynxProductivityParser.parse_activities_csv(activities_path) if activities_path and activities_path.exists() else None
    audit = ProductivityAuditor().generate_audit(calls, tasks=tasks, activities=activities, reference_time=as_of)
    reports = audit.get("employee_reports", {})
    employee_rows: list[dict[str, Any]] = []
    rep_stats: dict[str, dict[str, Any]] = {}
    for employee, report in reports.items():
        inbound_total = report.get("inbound_total", 0)
        answer_rate = round(report.get("inbound_answered", 0) / inbound_total * 100, 1) if inbound_total else None
        unreturned = report.get("missed_calls_orphaned", 0)
        employee_rows.append({
            "employee": employee,
            "calls_presented": inbound_total,
            "answered": report.get("inbound_answered", 0),
            "missed_or_voicemail": report.get("inbound_missed", 0) + report.get("inbound_voicemails", 0),
            "unreturned": unreturned,
            "outbound": report.get("outbound_total", 0),
            "status": report.get("score_status", "UNVERIFIED"),
        })
        rep_stats[employee] = {
            "inbound": report.get("inbound_total", 0),
            "answer_rate": "NOT EVALUABLE" if answer_rate is None else f"{answer_rate}%",
            "unreturned": unreturned,
        }
    total_inbound = sum(item["calls_presented"] for item in employee_rows)
    total_answered = sum(item["answered"] for item in employee_rows)
    magellan_by_phone = {
        normalize_phone(str(item.get("phone") or item.get("from_number") or "")): item
        for item in (magellan_data or {}).get("records", [])
        if normalize_phone(str(item.get("phone") or item.get("from_number") or ""))
    }
    call_data = {
        "source_status": "available" if calls and not row_errors else ("empty export" if not calls else f"partial: {len(row_errors)} rejected row(s)"),
        "answer_rate": "NOT EVALUABLE" if not total_inbound else f"{round(total_answered / total_inbound * 100, 1)}%",
        "unreturned_total": sum(item["unreturned"] for item in employee_rows),
        "employee_rows": employee_rows,
        "rep_stats": rep_stats,
        "unreturned_calls": [
            {
                "name": "Caller",
                "phone": item.get("caller_phone", ""),
                "rep": item.get("employee_name", "Queue"),
                "time": item.get("missed_at", ""),
                "call_id": item.get("call_id", ""),
                "magellan_sentiment": (magellan_by_phone.get(item.get("caller_phone", "")) or {}).get("sentiment"),
                "magellan_tags": (magellan_by_phone.get(item.get("caller_phone", "")) or {}).get("tags", []),
            }
            for item in audit.get("incidents", [])
            if item.get("status") == "ORPHANED_ALERT"
        ],
        "row_errors": row_errors,
    }
    task_data = {
        "source_status": "available" if tasks is not None else "not supplied",
        "total_overdue": sum(task.overdue for task in tasks or []),
        "overdue_by_rep": {task.employee_name: task.overdue for task in tasks or []},
    }
    return call_data, task_data


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build evidence-backed StreetSmart accountability reports")
    parser.add_argument("mode", choices=("daily", "weekly", "monthly"))
    parser.add_argument("--as-of", help="ISO timestamp; defaults to now in UTC")
    parser.add_argument("--ringcentral", type=Path)
    parser.add_argument("--tasks", type=Path)
    parser.add_argument("--activities", type=Path)
    parser.add_argument("--sales-json", type=Path)
    parser.add_argument("--sales", type=Path, help="EZLynx Sales Center CSV export")
    parser.add_argument("--sales-untouched-days", type=int, default=5)
    parser.add_argument("--retention", type=Path)
    parser.add_argument("--retention-summary-json", type=Path)
    parser.add_argument("--submissions", type=Path)
    parser.add_argument("--email-json", type=Path)
    parser.add_argument("--appsheet-json", type=Path)
    parser.add_argument("--magellan-json", type=Path)
    parser.add_argument("--tracker", action="append", default=[], metavar="KEY=CSV", help=f"Repeatable. Keys: {', '.join(TRACKER_DEFINITIONS)}")
    parser.add_argument("--monthly-kpis-json", type=Path)
    parser.add_argument("--churn-json", type=Path)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    as_of = _parse_timestamp(args.as_of) if args.as_of else datetime.now(timezone.utc)
    if as_of is None:
        raise SystemExit("--as-of must be a valid ISO timestamp")
    suite = ReportingSuite()
    magellan_data = _json(args.magellan_json)
    sales_data = _json(args.sales_json)
    if args.sales and args.sales.exists():
        sales_findings = audit_sales_records(
            parse_sales_csv(args.sales),
            as_of=as_of,
            untouched_days=max(1, args.sales_untouched_days),
        )
        sales_data.update(
            {
                "source_status": "available",
                "inactive_opportunity_count": len(sales_findings),
                "exceptions": finding_dicts(sales_findings),
                "untouched_days_threshold": max(1, args.sales_untouched_days),
            }
        )
    call_data, task_data = _call_report(args.ringcentral, as_of, args.tasks, args.activities, magellan_data)
    if args.mode == "daily":
        report = suite.build_daily_report(call_data, task_data, magellan_data, sales_data)
    elif args.mode == "weekly":
        retention = _json(args.retention_summary_json)
        if args.retention and args.retention.exists():
            findings = audit_retention_records(parse_retention_csv(args.retention), as_of=as_of)
            retention.update({"source_status": "available", "exception_count": len(findings), "exceptions": finding_dicts(findings)})
        submissions: dict[str, Any] = {"source_status": "not supplied"}
        if args.submissions and args.submissions.exists():
            findings = audit_submission_records(parse_submission_csv(args.submissions), as_of=as_of)
            submissions = {"source_status": "available", "open_over_30_count": len(findings), "exceptions": finding_dicts(findings)}
        all_tracker_findings = []
        missed_call_reconciliation = []
        workload_attestations = []
        tracker_errors = []
        for spec in args.tracker:
            try:
                key, raw_path = spec.split("=", 1)
                definition = TRACKER_DEFINITIONS[key]
                path = Path(raw_path)
                if not path.exists():
                    tracker_errors.append(f"{key}: missing file {path}")
                    continue
                findings = audit_tracker_csv(path, definition, as_of=as_of.date())
                if key == "voicemail_email":
                    workload_attestations.extend(voicemail_email_attestations(path))
                if key == "missed_calls" and call_data.get("source_status") == "available":
                    missed_call_reconciliation.extend(findings)
                else:
                    all_tracker_findings.extend(findings)
            except (ValueError, KeyError):
                tracker_errors.append(f"invalid tracker specification: {spec}")
        tracker_data = {
            "source_status": "available" if args.tracker and not tracker_errors else ("not supplied" if not args.tracker else "; ".join(tracker_errors)),
            "exceptions": tracker_dicts(all_tracker_findings),
            "missed_call_reconciliation": tracker_dicts(missed_call_reconciliation),
        }
        phone_by_name = {
            str(item.get("employee") or "").casefold(): int(item.get("unreturned") or 0)
            for item in call_data.get("employee_rows", [])
        }
        email_by_user = (_json(args.email_json).get("by_employee") or {}) if args.email_json else {}
        mismatches = []
        for item in workload_attestations:
            email_fact = dict(email_by_user.get(item.get("email"), {}) or {})
            evidenced = phone_by_name.get(str(item.get("employee") or "").casefold(), 0) + int(email_fact.get("stalled_threads") or 0)
            reported = item.get("reported_unresolved")
            if reported is not None and reported != evidenced:
                mismatches.append({**item, "evidenced_unresolved": evidenced})
        tracker_data["attestation_mismatches"] = mismatches
        if args.tracker and not tracker_errors:
            tracker_data["exception_count"] = len(all_tracker_findings)
        report = suite.build_weekly_report(
            call_data,
            task_data,
            sales_data,
            retention,
            _json(args.email_json),
            submissions,
            tracker_data,
            _json(args.appsheet_json),
            magellan_data,
        )
    else:
        churn_data = _json(args.churn_json)
        churn_cases = churn_data.get("cases", []) if churn_data.get("source_status") == "available" else []
        report = suite.build_monthly_report(_json(args.monthly_kpis_json), churn_cases, sales_data)
    if args.output:
        args.output.write_text(report + "\n", encoding="utf-8")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
