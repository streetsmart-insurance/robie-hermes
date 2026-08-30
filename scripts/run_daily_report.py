#!/usr/bin/env python3
import csv
from datetime import datetime, timezone
from pathlib import Path
from robie_job_engine.productivity import ProductivityAuditor, RingCentralCall
from robie_job_engine.reporting_suite import ReportingSuite

def main():
    auditor = ProductivityAuditor(sla_warning_minutes=30)
    suite = ReportingSuite()
    calls = []
    
    csv_files = sorted(Path.home().glob("Downloads/CallLog_*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    if csv_files:
        with open(csv_files[0], "r", encoding="utf-8-sig", errors="replace") as f:
            reader = csv.DictReader(f)
            for idx, r in enumerate(reader):
                date_str = r.get("Date", "").strip()
                time_str = r.get("Time", "").strip()
                if not date_str:
                    continue
                try:
                    dt = datetime.strptime(f"{date_str} {time_str}", "%m/%d/%Y %I:%M %p").replace(tzinfo=timezone.utc)
                except Exception:
                    dt = datetime.now(timezone.utc)
                calls.append(RingCentralCall(
                    call_id=f"call_{idx}",
                    direction=r.get("Direction", "").strip(),
                    from_number=r.get("From", "").strip(),
                    to_number=r.get("To", "").strip(),
                    result=r.get("Action Result", "").strip(),
                    duration_seconds=0,
                    start_time=dt,
                    extension=r.get("Extension", "").strip(),
                    employee_name=r.get("Name", "").strip()
                ))
                
    audit_results = auditor.generate_audit(calls)
    report = suite.build_daily_report(
        call_data=audit_results,
        task_data={"overdue_count": 98, "reps_with_backlog": {"Maria Bara": 17, "Erika Palacios": 11, "Ricardo Aguilar": 8, "Diana Cabrera": 5}}
    )
    print(report)

if __name__ == "__main__":
    main()
