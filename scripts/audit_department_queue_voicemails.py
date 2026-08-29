#!/usr/bin/env python3
"""Audits Department Queue Voicemails (Commercial, Trucking, Personal Lines) and Mike Sosa."""

import csv
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

csv_path = Path.home() / "Downloads" / "CallLog_20260829-145920.csv"

def normalize_phone(raw):
    digits = "".join(c for c in str(raw) if c.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        return digits[1:]
    return digits[-10:] if len(digits) >= 10 else digits

def parse_rc_time(raw):
    clean = " ".join(str(raw).split())
    for day in ("Mon ", "Tue ", "Wed ", "Thu ", "Fri ", "Sat ", "Sun "):
        if clean.startswith(day):
            clean = clean[len(day):].strip()
    try:
        return datetime.strptime(clean, "%m/%d/%Y %I:%M %p").replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)

def main():
    with open(csv_path, "r", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)
        all_calls = list(reader)

    # 1. Mike Sosa Breakdown
    mike_calls = [c for c in all_calls if "Mike Sosa" in c.get("Extension", "") or "1002" in c.get("Extension", "")]
    mike_in = [c for c in mike_calls if c.get("Direction") == "Incoming"]
    mike_out = [c for c in mike_calls if c.get("Direction") == "Outgoing"]

    # Check if Mike answered queue calls or direct calls
    mike_queue_ans = [c for c in mike_in if "Queue" in c.get("Extension", "") or c.get("Action Result") == "Accepted"]
    
    print("=== MIKE SOSA AUDIT (Service vs Leads) ===")
    print(f"Total Mike Calls: {len(mike_calls)} | Inbound: {len(mike_in)} | Outbound: {len(mike_out)}")
    print(f"Mike Inbound Answered: {len([c for c in mike_in if c.get('Action Result') in ('Accepted', 'Call connected')])}")
    print(f"Mike Inbound Missed: {len([c for c in mike_in if c.get('Action Result') in ('Missed', 'Voicemail')])}")
    
    # 2. Extract All Outbound Calls for Reconciliation
    outbound_calls = defaultdict(list)
    for c in all_calls:
        if c.get("Direction") == "Outgoing":
            to_norm = normalize_phone(c.get("To", ""))
            if to_norm:
                outbound_calls[to_norm].append({
                    "rep": c.get("Extension", "").split(" - ")[-1],
                    "time": parse_rc_time(c.get("Date", "") + " " + c.get("Time", "")),
                    "duration": c.get("Duration", "0"),
                })

    # 3. Department Service Queues
    # Commercial Queue: 9006 / Commercial
    # Trucking Queue: 9020 / Trucking
    # Personal Lines Queue: 104 / Personal Lines
    # Spanish Commercial: 9008 / Spanish Commercial
    
    SERVICE_QUEUES = {
        "Commercial Queue": ["9006 - Commercial", "Commercial"],
        "Trucking Queue": ["9020 - Trucking", "Trucking"],
        "Personal Lines Queue": ["104 - Personal Lines", "Personal Lines"],
        "Spanish Commercial Queue": ["9008 - Spanish Commercial", "Spanish Commercial"],
    }

    print("\n" + "="*80)
    print("=== DEPARTMENT SERVICE QUEUE VOICEMAIL & CALLBACK AUDIT (PAST 7 DAYS) ===")
    print("="*80)

    for q_name, q_match in SERVICE_QUEUES.items():
        q_calls = [c for c in all_calls if any(m in c.get("Extension", "") for m in q_match)]
        q_vms = [c for c in q_calls if c.get("Action Result") == "Voicemail" or c.get("Result") == "Voicemail"]
        
        # Reconcile VMs
        returned_count = 0
        orphaned_vms = []
        
        for vm in q_vms:
            from_norm = normalize_phone(vm.get("From", ""))
            caller_name = vm.get("Name", "Unknown")
            vm_time = parse_rc_time(vm.get("Date", "") + " " + vm.get("Time", ""))
            
            # Check if any outbound call was made to from_norm AFTER vm_time
            callbacks = [ob for ob in outbound_calls.get(from_norm, []) if ob["time"] >= vm_time]
            if callbacks:
                returned_count += 1
            else:
                orphaned_vms.append({
                    "caller": caller_name,
                    "phone": vm.get("From", ""),
                    "date": vm.get("Date", ""),
                    "time": vm.get("Time", ""),
                    "duration": vm.get("Duration", ""),
                })

        print(f"\n📁 {q_name.upper()}")
        print(f"  • Total Queue Calls: {len(q_calls)}")
        print(f"  • Total Voicemails Left: {len(q_vms)}")
        print(f"  • Voicemails Returned: {returned_count} ({returned_count/max(len(q_vms), 1)*100:.1f}%)")
        print(f"  • Voicemails Abandoned (NEVER Called Back): {len(orphaned_vms)} ({len(orphaned_vms)/max(len(q_vms), 1)*100:.1f}%)")
        
        if orphaned_vms:
            print(f"  🚨 Unreturned {q_name} Voicemails:")
            for i, ovm in enumerate(orphaned_vms[:8]):
                print(f"     {i+1}. {ovm['caller']} ({ovm['phone']}) — {ovm['date']} at {ovm['time']} (VM Duration: {ovm['duration']})")
            if len(orphaned_vms) > 8:
                print(f"     ... and {len(orphaned_vms)-8} more unreturned voicemails in {q_name}")

if __name__ == "__main__":
    main()
