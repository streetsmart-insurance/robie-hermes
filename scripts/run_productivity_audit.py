#!/usr/bin/env python3
"""StreetSmart Productivity & Accountability CLI Runner.

Usage:
  python scripts/run_productivity_audit.py --sample
  python scripts/run_productivity_audit.py --calls call_logs.json --tasks ezlynx_tasks.json
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from robie_job_engine.productivity import (
    EZLynxActivityMetric,
    EZLynxTaskMetric,
    ProductivityAuditor,
    RingCentralCall,
)


def generate_sample_data():
    """Generates realistic test data demonstrating both compliant and at-risk employee workflows."""
    now = datetime.now(timezone.utc)
    
    calls = [
        # Jackie: Inbound missed calls with no return calls (Account Loss Risk)
        RingCentralCall(
            call_id="call-j1",
            direction="Inbound",
            from_number="5552345678",
            to_number="5559990002",
            result="Voicemail",
            duration_seconds=45,
            start_time=now - timedelta(hours=4),
            extension="102",
            employee_name="Jackie",
        ),
        RingCentralCall(
            call_id="call-j2",
            direction="Inbound",
            from_number="5558765432",
            to_number="5559990002",
            result="Missed",
            duration_seconds=0,
            start_time=now - timedelta(hours=2, minutes=15),
            extension="102",
            employee_name="Jackie",
        ),
        RingCentralCall(
            call_id="call-j3",
            direction="Inbound",
            from_number="5553334444",
            to_number="5559990002",
            result="Call connected",
            duration_seconds=320,
            start_time=now - timedelta(hours=1),
            extension="102",
            employee_name="Jackie",
        ),
        RingCentralCall(
            call_id="call-j4",
            direction="Outbound",
            from_number="5559990002",
            to_number="5553334444",
            result="Call connected",
            duration_seconds=180,
            start_time=now - timedelta(minutes=30),
            extension="102",
            employee_name="Jackie",
        ),
        
        # Producer 2 (High Performer): Returned missed call quickly
        RingCentralCall(
            call_id="call-p1",
            direction="Inbound",
            from_number="5557778888",
            to_number="5559990001",
            result="Missed",
            duration_seconds=0,
            start_time=now - timedelta(hours=3),
            extension="101",
            employee_name="Sarah",
        ),
        RingCentralCall(
            call_id="call-p2",
            direction="Outbound",
            from_number="5559990001",
            to_number="5557778888",
            result="Call connected",
            duration_seconds=410,
            start_time=now - timedelta(hours=2, minutes=45),  # 15m callback
            extension="101",
            employee_name="Sarah",
        ),
        RingCentralCall(
            call_id="call-p3",
            direction="Inbound",
            from_number="5551112222",
            to_number="5559990001",
            result="Call connected",
            duration_seconds=600,
            start_time=now - timedelta(hours=1, minutes=30),
            extension="101",
            employee_name="Sarah",
        ),
    ]

    tasks = [
        EZLynxTaskMetric(employee_name="Jackie", completed_today=2, overdue=5, open_total=8),
        EZLynxTaskMetric(employee_name="Sarah", completed_today=9, overdue=0, open_total=2),
    ]

    activities = [
        EZLynxActivityMetric(employee_name="Jackie", notes_count=3),
        EZLynxActivityMetric(employee_name="Sarah", notes_count=14),
    ]

    return calls, tasks, activities


def main():
    parser = argparse.ArgumentParser(description="StreetSmart Productivity & Accountability Audit")
    parser.add_argument("--sample", action="store_true", help="Run with realistic sample agency data")
    parser.add_argument("--calls", type=str, help="Path to RingCentral call logs JSON file")
    parser.add_argument("--tasks", type=str, help="Path to EZLynx tasks JSON file")
    parser.add_argument("--activities", type=str, help="Path to EZLynx activities JSON file")
    parser.add_argument("--json", action="store_true", help="Output raw JSON instead of Google Chat card")
    parser.add_argument("--warning-mins", type=int, default=30, help="SLA warning threshold for unreturned calls")
    args = parser.parse_args()

    auditor = ProductivityAuditor(sla_warning_minutes=args.warning_mins)

    if args.sample or (not args.calls and not args.tasks):
        calls, tasks, activities = generate_sample_data()
    else:
        calls = []
        if args.calls and Path(args.calls).exists():
            with open(args.calls, "r") as f:
                raw_calls = json.load(f)
                calls = [RingCentralCall.from_dict(c) for c in raw_calls]
        
        tasks = []
        if args.tasks and Path(args.tasks).exists():
            with open(args.tasks, "r") as f:
                raw_tasks = json.load(f)
                tasks = [EZLynxTaskMetric(**t) for t in raw_tasks]
                
        activities = []
        if args.activities and Path(args.activities).exists():
            with open(args.activities, "r") as f:
                raw_act = json.load(f)
                activities = [EZLynxActivityMetric(**a) for a in raw_act]

    audit = auditor.generate_audit(calls, tasks=tasks, activities=activities)

    if args.json:
        print(json.dumps(audit, indent=2, default=str))
    else:
        chat_card = auditor.format_google_chat_card(audit)
        print(chat_card)


if __name__ == "__main__":
    main()
