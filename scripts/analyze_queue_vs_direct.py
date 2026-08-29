#!/usr/bin/env python3
"""Analyzes Direct Inbound Calls vs Queue Inbound Calls across the agency."""

import csv
from collections import defaultdict
from pathlib import Path

csv_path = Path.home() / "Downloads" / "CallLog_20260829-145920.csv"

def parse_dur(raw):
    if not raw: return 0
    p = str(raw).strip().split(":")
    if len(p) == 3: return int(p[0])*3600 + int(p[1])*60 + int(p[2])
    if len(p) == 2: return int(p[0])*60 + int(p[1])
    try: return int(float(raw))
    except: return 0

def analyze():
    queue_calls = defaultdict(lambda: {"answered": 0, "missed": 0, "talk_time": 0})
    direct_calls = defaultdict(lambda: {"answered": 0, "missed": 0, "talk_time": 0})

    EXCLUDED = {"Nelson Maldonado", "Alexis Martinez", "Sandy Santana", "RingCentral App", "Fax", "Direct Inward Dialing", "Main Line"}

    with open(csv_path, "r", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)
        for r in reader:
            if r.get("Direction") != "Incoming":
                continue
            ext = r.get("Extension", "")
            if not ext or " - " not in ext:
                continue
            emp = ext.split(" - ")[-1].strip()
            if emp in EXCLUDED:
                continue
                
            result = r.get("Action Result", r.get("Result", ""))
            dur = parse_dur(r.get("Duration", r.get("Length", "0")))
            to_num = r.get("To", "")
            
            is_queue = ("462-8343" in to_num) or ("Commercial Queue" in ext) or ("9006" in ext) or ("AI Receptionist" in ext)
            
            target = queue_calls[emp] if is_queue else direct_calls[emp]
            if result in ("Accepted", "Call connected"):
                target["answered"] += 1
                target["talk_time"] += dur
            else:
                target["missed"] += 1

    print("| Team Member | Direct Inbound (Answered) | Direct Inbound (Missed) | Direct Answer Rate | Queue Inbound (Answered) | Queue Talk Time | Queue Lifter Grade |")
    print("| :--- | :---: | :---: | :---: | :---: | :---: | :--- |")
    
    all_emps = sorted(set(list(queue_calls.keys()) + list(direct_calls.keys())))
    rows = []
    for emp in all_emps:
        q = queue_calls[emp]
        d = direct_calls[emp]
        total_direct = d["answered"] + d["missed"]
        if total_direct == 0 and q["answered"] == 0:
            continue
        d_rate = (d["answered"] / total_direct * 100) if total_direct > 0 else 0
        q_mins = q["talk_time"] / 60
        
        # Classify queue participation
        if q["answered"] >= 10:
            q_grade = "🛡️ **Heavy Queue Rescuer**"
        elif q["answered"] >= 4:
            q_grade = "🟢 **Active Queue Helper**"
        elif q["answered"] >= 1:
            q_grade = "🟡 **Occasional Queue Helper**"
        else:
            q_grade = "🔴 **Zero Queue Pickups**"
            
        rows.append((q["answered"], emp, d["answered"], d["missed"], d_rate, q["answered"], q_mins, q_grade))
        
    # Sort by queue answered desc
    rows.sort(key=lambda x: (x[0], x[4]), reverse=True)
    for _, emp, d_ans, d_miss, d_rate, q_ans, q_mins, q_grade in rows:
        print(f"| **{emp}** | {d_ans} | {d_miss} | {d_rate:.1f}% | **{q_ans}** | {q_mins:.1f} mins | {q_grade} |")

if __name__ == "__main__":
    analyze()
