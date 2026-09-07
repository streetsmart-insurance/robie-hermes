#!/usr/bin/env python3
"""Export evidence tables used by the prior-business-day team-lead digest."""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict
from datetime import date, datetime, time, timezone
from pathlib import Path

from robie_job_engine.center_audits import audit_sales_records, enrich_sales_last_touches, parse_sales_csv
from robie_job_engine.department_accountability import parse_overdue_activity_detail


def _active_roster(path: Path) -> dict[str, str]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = csv.DictReader(handle)
        return {
            str(row.get("Name", "")).strip(): str(row.get("Department", "")).strip()
            for row in rows
            if str(row.get("Employment Status", "")).strip().casefold() == "active"
            and str(row.get("Name", "")).strip()
            and str(row.get("Department", "")).strip()
        }


def _write(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        raise RuntimeError(f"refusing to write empty evidence export: {path}")
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-date", required=True)
    parser.add_argument("--overdue-activity", type=Path, required=True)
    parser.add_argument("--activity-window", type=Path, required=True)
    parser.add_argument("--sales-center", type=Path, required=True)
    parser.add_argument("--roster", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    report_date = date.fromisoformat(args.report_date)
    roster = _active_roster(args.roster)
    tasks = parse_overdue_activity_detail(
        args.overdue_activity,
        as_of=report_date,
        employee_departments=roster,
    )
    sales = enrich_sales_last_touches(
        parse_sales_csv(args.sales_center),
        args.activity_window,
        window_days=7,
    )
    sales_findings = audit_sales_records(
        sales,
        as_of=datetime.combine(report_date, time(23, 59), tzinfo=timezone.utc),
        untouched_days=5,
    )
    sales_rows: list[dict[str, object]] = []
    for finding in sales_findings:
        row = asdict(finding)
        row["team_department"] = roster.get(finding.producer, "UNVERIFIED")
        sales_rows.append(row)

    suffix = report_date.isoformat()
    _write(args.output_dir / f"OVERDUE_TASK_DETAIL_{suffix}.csv", [asdict(task) for task in tasks])
    _write(args.output_dir / f"SALES_UNTOUCHED_DETAIL_{suffix}.csv", sales_rows)
    print(f"overdue_tasks={len(tasks)} sales_candidates={len(sales_rows)}")


if __name__ == "__main__":
    main()
