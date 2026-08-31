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
    audit_overdue_submission_records,
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
from .ringcentral_workbooks import load_evidence_manifest, read_workbook
from .role_accountability import build_role_rows


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


def _call_from_row(row: dict[str, Any], row_number: int) -> tuple[RingCentralCall | None, str | None]:
    timestamp = _parse_timestamp(_first(row, "Call Start Time", "Start Time", "StartTime", "Date/Time", "Time"))
    if timestamp is None:
        return None, f"row {row_number}: missing or invalid timestamp"
    direction = _first(row, "Direction", "Call Direction")
    employee = _first(row, "Employee", "User", "Name", "Extension Name", "Answered By")
    if not employee:
        employee = _first(row, "To Name") if direction.casefold().startswith("in") else _first(row, "From Name")
    result = _first(row, "Result", "Action", "Call Result", "Disposition")
    answered_by = _first(row, "Answered By", "Connected To", "Forwarded To")
    if not answered_by and direction.casefold().startswith("in") and any(
        token in result.casefold() for token in ("connect", "answer", "success")
    ):
        answered_by = _first(row, "To Name")
    data = {
        "call_id": _first(row, "Call ID", "Session ID", "Session Id", "ID") or f"ringcentral-row-{row_number}",
        "direction": direction,
        "from_number": _first(row, "From", "From Number", "Caller ID"),
        "to_number": _first(row, "To", "To Number", "Dialed Number"),
        "result": result,
        "duration_seconds": _duration_seconds(_first(row, "Call Length", "Duration", "Duration Seconds", "Talk Time")),
        "start_time": timestamp,
        "extension": _first(row, "Extension", "Extension ID"),
        "employee_name": employee,
        "queue_name": _first(row, "Queue", "Queue Name", "Call Queue", "Called Queue"),
        "answered_by": answered_by,
        "queue_wait_seconds": _duration_seconds(_first(row, "Queue Wait Time", "Wait Time", "Hold Time")),
    }
    return RingCentralCall.from_dict(data), None


def _calls_from_rows(rows: list[dict[str, Any]]) -> tuple[list[RingCentralCall], list[str]]:
    calls: list[RingCentralCall] = []
    errors: list[str] = []
    seen: set[tuple[Any, ...]] = set()
    for row_number, row in enumerate(rows, start=2):
        call, error = _call_from_row(row, row_number)
        if error:
            errors.append(error)
            continue
        assert call is not None
        fingerprint = (
            call.call_id, call.direction, call.from_number, call.to_number,
            call.result, call.duration_seconds, call.start_time.isoformat(),
            call.extension, call.employee_name, call.queue_name, call.answered_by,
        )
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        calls.append(call)
    return calls, errors


def load_ringcentral_csv(path: Path) -> tuple[list[RingCentralCall], list[str]]:
    with path.open(encoding="utf-8-sig", errors="replace", newline="") as handle:
        return _calls_from_rows(list(csv.DictReader(handle)))


def load_ringcentral_source(
    path: Path,
    *,
    expected_kind: str,
    as_of: datetime | None = None,
) -> tuple[list[RingCentralCall], list[str], dict[str, Any]]:
    """Load legacy CSV or checksum-bound XLSX collection evidence."""
    if path.suffix.casefold() == ".csv":
        calls, errors = load_ringcentral_csv(path)
        errors.append("legacy CSV lacks current-user/current-queue coverage proof")
        return calls, errors, {"format": "csv", "coverage_verified": False}
    if path.suffix.casefold() == ".xlsx":
        workbook = read_workbook(path, required_sheets=("Calls",))
        calls, errors = _calls_from_rows(workbook["tables"]["Calls"]["rows"])
        errors.append("direct XLSX lacks a checksum-bound collection manifest and coverage proof")
        return calls, errors, {"format": "xlsx", "coverage_verified": False, "workbooks": [workbook]}
    if path.suffix.casefold() != ".json":
        raise ValueError("RingCentral evidence must be CSV, XLSX, or a collector JSON manifest")
    evidence = load_evidence_manifest(path, expected_kind=expected_kind, as_of=as_of)
    call_rows = [
        row
        for workbook in evidence["workbooks"]
        for row in ((workbook.get("tables") or {}).get("Calls") or {}).get("rows", [])
    ]
    calls, errors = _calls_from_rows(call_rows)
    queue_rows = [
        row
        for workbook in evidence["workbooks"]
        for row in ((workbook.get("tables") or {}).get("Queues") or {}).get("rows", [])
    ]
    user_rows = [
        row
        for workbook in evidence["workbooks"]
        for row in ((workbook.get("tables") or {}).get("Users") or {}).get("rows", [])
    ]
    return calls, errors, {
        "format": "xlsx-manifest",
        "coverage_verified": True,
        "queue_rows": queue_rows,
        "user_rows": user_rows,
        "attachment_sha256": [item["sha256"] for item in evidence["attachments"]],
    }


def _integer(value: Any) -> int:
    text = str(value or "").replace(",", "").strip()
    try:
        return int(float(text))
    except ValueError:
        return 0


def _queue_export_rows(rows: list[dict[str, Any]], calls: list[RingCentralCall]) -> tuple[list[dict[str, Any]], list[str]]:
    detailed = {row["queue"].casefold(): row for row in _queue_rows(calls)}
    output: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[str] = set()
    for row in rows:
        name = _first(row, "Name")
        folded = name.casefold()
        if not name or folded in seen:
            errors.append(f"Queues worksheet has a missing or duplicate queue name: {name or '<blank>'}")
            continue
        seen.add(folded)
        offered = _integer(_first(row, "# Inbound"))
        answered = _integer(_first(row, "# Answered"))
        abandoned = _integer(_first(row, "# Abandoned"))
        refused = _integer(_first(row, "# Refused"))
        if answered + abandoned > offered:
            errors.append(f"Queues worksheet totals are inconsistent for {name}")
        detail = detailed.get(folded, {})
        detailed_offered = detail.get("offered")
        if detailed_offered is not None and detailed_offered != offered:
            errors.append(
                f"queue reconciliation mismatch for {name}: Calls={detailed_offered}, Queues={offered}"
            )
        output.append({
            "queue": name,
            "offered": offered,
            "answered": answered,
            "abandoned_or_voicemail": abandoned,
            "refused_member_legs": refused,
            "answer_rate": "NOT EVALUABLE" if not offered else f"{round(answered / offered * 100, 1)}%",
            "max_wait_seconds": None,
            "answered_by": detail.get("answered_by", {}),
            "missed_by": detail.get("missed_by", {}),
        })
    return output, errors


def _queue_rows(calls: list[RingCentralCall]) -> list[dict[str, Any]]:
    """Aggregate queue sessions and member call legs without double-counting offers."""
    sessions: dict[tuple[str, str], list[RingCentralCall]] = {}
    labels: dict[str, str] = {}
    for index, call in enumerate(calls):
        queue = call.queue_name.strip()
        if call.direction != "Inbound" or not queue:
            continue
        folded = queue.casefold()
        labels.setdefault(folded, queue)
        sessions.setdefault((folded, call.call_id or f"row-{index}"), []).append(call)

    output = []
    for folded, label in sorted(labels.items(), key=lambda item: item[1].casefold()):
        queue_sessions = [legs for (queue, _), legs in sessions.items() if queue == folded]
        answered_by: dict[str, int] = {}
        missed_by: dict[str, int] = {}
        answered = 0
        waits = []
        for legs in queue_sessions:
            connected = [leg for leg in legs if leg.result == "Call connected"]
            if connected:
                answered += 1
                winner = connected[0].answered_by or connected[0].employee_name
                if winner and winner not in {"Unassigned", "Queue"}:
                    answered_by[winner] = answered_by.get(winner, 0) + 1
            waits.extend(leg.queue_wait_seconds for leg in legs if leg.queue_wait_seconds > 0)
            for leg in legs:
                if any(term in leg.result.casefold() for term in ("miss", "reject", "refus", "no answer")):
                    member = leg.employee_name
                    if member and member not in {"Unassigned", "Queue"}:
                        missed_by[member] = missed_by.get(member, 0) + 1
        offered = len(queue_sessions)
        output.append({
            "queue": label,
            "offered": offered,
            "answered": answered,
            "abandoned_or_voicemail": offered - answered,
            "answer_rate": "NOT EVALUABLE" if not offered else f"{round(answered / offered * 100, 1)}%",
            "max_wait_seconds": max(waits) if waits else None,
            "answered_by": answered_by,
            "missed_by": missed_by,
        })
    return output


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
    mode: str,
    magellan_data: Optional[dict[str, Any]] = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if path is None or not path.exists():
        status = "not supplied" if path is None else f"missing file: {path}"
        return {"source_status": status, "rep_stats": {}, "employee_rows": []}, {"source_status": "not supplied", "overdue_by_rep": {}}
    expected_kind = "daily" if mode == "daily" else "weekly"
    calls, row_errors, evidence = load_ringcentral_source(path, expected_kind=expected_kind, as_of=as_of)
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
    queue_rows = _queue_rows(calls)
    if evidence.get("queue_rows"):
        queue_rows, queue_errors = _queue_export_rows(evidence["queue_rows"], calls)
        row_errors.extend(queue_errors)
    call_data = {
        "source_status": "available" if calls and not row_errors else ("empty export" if not calls else f"partial: {len(row_errors)} evidence error(s)"),
        "answer_rate": "NOT EVALUABLE" if not total_inbound else f"{round(total_answered / total_inbound * 100, 1)}%",
        "unreturned_total": sum(item["unreturned"] for item in employee_rows),
        "employee_rows": employee_rows,
        "queue_rows": queue_rows,
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
        "evidence_format": evidence.get("format"),
        "coverage_verified": evidence.get("coverage_verified", False),
        "attachment_sha256": evidence.get("attachment_sha256", []),
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
    parser.add_argument("--ringcentral", type=Path, help="RingCentral CSV, XLSX, or collector evidence manifest")
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
    parser.add_argument("--roles-json", type=Path, help="Approved employee-to-role registry")
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
    roles_data = _json(args.roles_json)
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
    call_data, task_data = _call_report(args.ringcentral, as_of, args.tasks, args.activities, args.mode, magellan_data)
    role_rows = build_role_rows(
        roles_data.get("employees", {}),
        call_data=call_data,
        task_data=task_data,
        sales_data=sales_data,
        email_data=_json(args.email_json),
        magellan_data=magellan_data,
    ) if roles_data.get("employees") else []
    if args.mode == "daily":
        report = suite.build_daily_report(call_data, task_data, magellan_data, sales_data, role_rows)
    elif args.mode == "weekly":
        retention = _json(args.retention_summary_json)
        if args.retention and args.retention.exists():
            findings = audit_retention_records(parse_retention_csv(args.retention), as_of=as_of)
            retention.update({"source_status": "available", "exception_count": len(findings), "exceptions": finding_dicts(findings)})
        submissions: dict[str, Any] = {"source_status": "not supplied"}
        if args.submissions and args.submissions.exists():
            findings = audit_overdue_submission_records(parse_submission_csv(args.submissions), as_of=as_of)
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
        counts_by_key: dict[str, int] = {}
        policy_change_blockers: dict[str, int] = {}
        for item in all_tracker_findings:
            counts_by_key[item.tracker_key] = counts_by_key.get(item.tracker_key, 0) + 1
            if item.tracker_key == "policy_changes":
                policy_change_blockers[item.blocker_party] = policy_change_blockers.get(item.blocker_party, 0) + 1
        tracker_data["counts_by_key"] = counts_by_key
        tracker_data["policy_change_blockers"] = policy_change_blockers
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
            role_rows,
        )
    else:
        churn_data = _json(args.churn_json)
        churn_cases = churn_data.get("cases", []) if churn_data.get("source_status") == "available" else []
        report = suite.build_monthly_report(
            _json(args.monthly_kpis_json),
            churn_cases,
            sales_data,
            role_rows,
            churn_source_status=str(churn_data.get("source_status") or "not supplied"),
        )
    if args.output:
        args.output.write_text(report + "\n", encoding="utf-8")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
