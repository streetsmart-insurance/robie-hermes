#!/usr/bin/env python3
import csv
from collections import defaultdict
from pathlib import Path

# Find latest CallLog CSV in Downloads
csv_files = sorted(Path.home().glob("Downloads/CallLog_*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
csv_path = csv_files[0] if csv_files else None

def normalize_phone(raw):
    digits = "".join(c for c in str(raw) if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits

def main():
    if not csv_path:
        print("No CallLog CSV found")
        return
        
    print(f"Reading: {csv_path.name}")
    calls_by_phone = defaultdict(list)
    with open(csv_path, "r", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            calls_by_phone[row.get("Direction", "")].append(row)
            
    inbound_by_phone = defaultdict(list)
    for c in calls_by_phone["Incoming"]:
        ph = normalize_phone(c.get("From", ""))
        if ph:
            inbound_by_phone[ph].append(c)
            
    outbound_phones = set()
    for c in calls_by_phone["Outgoing"]:
        ph = normalize_phone(c.get("To", ""))
        if ph:
            outbound_phones.add(ph)
            
    # Find repeat callers (3+ inbound calls) with ZERO outbound callbacks
    repeat_abandoned = []
    for ph, calls in inbound_by_phone.items():
        if ph not in outbound_phones and len(calls) >= 3:
            name = calls[0].get("Name", "Unknown")
            reps_hit = set(c.get("Extension", "").split(" - ")[-1] for c in calls if c.get("Extension"))
            dates = [c.get("Date") + " " + c.get("Time") for c in calls]
            vm_count = sum(1 for c in calls if c.get("Action Result") == "Voicemail")
            repeat_abandoned.append({
                "phone": ph,
                "name": name,
                "call_count": len(calls),
                "vm_count": vm_count,
                "reps": [r for r in reps_hit if r],
                "dates": dates
            })
            
    repeat_abandoned.sort(key=lambda x: x["call_count"], reverse=True)
    
    print(f"Total Repeat Callers (3+ calls) with ZERO Callbacks: {len(repeat_abandoned)}")
    for i, r in enumerate(repeat_abandoned):
        print(f"\n{i+1}. 📞 {r['name']} ({r['phone']}) — {r['call_count']} Inbound Dials ({r['vm_count']} Voicemails)")
        print(f"   Reps/Queues Hit: {', '.join(r['reps'])}")
        print(f"   Call Timeline:")
        for d in r['dates'][:4]:
            print(f"     • {d}")
        if len(r['dates']) > 4:
            print(f"     ... and {len(r['dates'])-4} more calls")

if __name__ == "__main__":
    main()
