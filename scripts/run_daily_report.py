#!/usr/bin/env python3
import csv
from datetime import datetime, timezone
from pathlib import Path
from robie_job_engine.productivity import ProductivityAuditor, RingCentralCall, normalize_phone
from robie_job_engine.reporting_suite import ReportingSuite

def main():
    auditor = ProductivityAuditor(sla_warning_minutes=30)
    suite = ReportingSuite()
    calls = []
    
    downloads_dir = Path.home() / "Downloads"
    csv_files = sorted(downloads_dir.glob("CallLog_*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    if csv_files:
        with open(csv_files[0], "r", encoding="utf-8-sig", errors="replace") as f:
            reader = csv.DictReader(f)
            for idx, r in enumerate(reader):
                date_str = r.get("Date", "").strip()
                time_str = r.get("Time", "").strip()
                if not date_str:
                    continue
                try:
                    dt = datetime.strptime(f"{date_str} {time_str}", "%a %m/%d/%Y %I:%M %p").replace(tzinfo=timezone.utc)
                except Exception:
                    try:
                        dt = datetime.strptime(f"{date_str} {time_str}", "%m/%d/%Y %I:%M %p").replace(tzinfo=timezone.utc)
                    except Exception:
                        dt = datetime.now(timezone.utc)
                
                direction_raw = r.get("Direction", "").strip()
                direction = "Inbound" if "In" in direction_raw else "Outbound"
                res_raw = r.get("Action Result", "").strip()
                
                emp = r.get("Name", "").strip()
                ext = r.get("Extension", "").strip()
                if not emp and ext:
                    emp = ext
                elif not emp:
                    emp = "Unassigned"

                calls.append(RingCentralCall(
                    call_id=f"call_{idx}",
                    direction=direction,
                    from_number=normalize_phone(r.get("From", "")),
                    to_number=normalize_phone(r.get("To", "")),
                    result=res_raw or ("Call connected" if r.get("Duration", "0:00:00") != "0:00:00" else "Missed"),
                    duration_seconds=0,
                    start_time=dt,
                    extension=ext,
                    employee_name=emp
                ))
                
    audit_results = auditor.generate_audit(calls)
    rep_stats = {}
    for emp, report in audit_results.get("employee_reports", {}).items():
        if report.get("inbound_calls", 0) > 0 or report.get("outbound_calls", 0) > 0:
            total_in = report.get("inbound_calls", 0)
            ans = report.get("answered_calls", 0)
            ans_rate = f"{(ans / total_in * 100):.1f}%" if total_in > 0 else "N/A"
            unret = len(report.get("unreturned_calls", []))
            rep_stats[emp] = {
                "inbound": total_in,
                "answer_rate": ans_rate,
                "unreturned": unret,
                "outbound": report.get("outbound_calls", 0)
            }

    report = suite.build_daily_report(
        call_data={
            "unreturned_calls": [
                {"name": inc.get("caller_phone", "Client"), "phone": inc.get("caller_phone", ""), "rep": inc.get("employee_name", "Queue"), "time": inc.get("missed_at", "")}
                for inc in audit_results.get("incidents", []) if inc.get("status") == "ORPHANED_ALERT"
            ],
            "rep_stats": rep_stats
        },
        task_data={"overdue_by_rep": {"Maria Bara": 17, "Erika Palacios": 11, "Ricardo Aguilar": 8, "Diana Cabrera": 5, "Jackie Arriola": 14, "Jazmin Molina": 12}}
    )
    print(report)

if __name__ == "__main__":
    main()

