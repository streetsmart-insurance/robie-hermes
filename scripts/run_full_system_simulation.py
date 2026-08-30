#!/usr/bin/env python3
"""Safe, synthetic demonstration of fail-closed report behavior.

This file intentionally contains no StreetSmart employee metrics, customer
allegations, production destinations, or claimed agency grades.
"""

from robie_job_engine.reporting_suite import ReportingSuite


def main() -> None:
    print("SIMULATION ONLY — SYNTHETIC DATA — NEVER DISTRIBUTE AS AN AGENCY REPORT\n")
    report = ReportingSuite().build_weekly_report(
        call_data={"source_status": "synthetic", "employee_rows": []},
        task_data={"source_status": "synthetic"},
        sales_data={"source_status": "synthetic"},
        retention_data={"source_status": "synthetic"},
        email_data={"source_status": "synthetic"},
        submission_data={"source_status": "synthetic"},
        tracker_data={"source_status": "synthetic"},
    )
    print(report)


if __name__ == "__main__":
    main()
