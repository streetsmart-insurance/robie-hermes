#!/usr/bin/env python3
"""Run the read-only verification worker preview.

Reads the three verification report CSVs (4246 audits, 4247 manual
renewals, 4744 mortgagee) from disk, composes the "what the AI would do"
action per policy, and writes the grading Excel workbook.

READ-ONLY: no EZLynx writes, no notes, no outreach.

Usage:
    run_readonly_worker_preview.py \
        --report-4246 4246.csv --report-4247 4247.csv --report-4744 4744.csv \
        --knowledge carrier-contact-type-knowledge.md \
        --status-map policy_status_map.json \
        --out preview.xlsx

The status map is a live PolicyApi sweep,
{policy_number: {"status": ..., "cancellation_date": ...}}; re-sweep it
every run. Without --status-map the dead-policy gate stays off and the
workbook says statuses are UNVERIFIED.
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from robie_job_engine.readonly_worker_preview import (  # noqa: E402
    build_preview_workbook,
    compose_4246_action,
    compose_4247_action,
    compose_4744_action,
    load_knowledge_entries,
    load_status_map,
)

REPORT_4246_COLUMNS = ["Account Name", "Policy Number", "Master Company",
                       "Effective Date", "Current Policy Status"]
REPORT_4247_COLUMNS = ["Account Name", "Policy Number", "Master Company",
                       "Line Of Business", "Policy Expiration Date",
                       "Assigned Producer", "CSR"]
REPORT_4744_COLUMNS = ["Account Name", "Policy Number", "Master Company",
                       "Line Of Business", "Premium - Annualized",
                       "Assigned Producer", "CSR", "Policy Expiration Date"]


def read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--report-4246", required=True)
    ap.add_argument("--report-4247", required=True)
    ap.add_argument("--report-4744", required=True)
    ap.add_argument("--knowledge", default=None)
    ap.add_argument("--status-map", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    today = date.today()
    knowledge = load_knowledge_entries(args.knowledge) if args.knowledge else []
    status_map = load_status_map(args.status_map) if args.status_map else None
    if args.status_map and not status_map:
        print(f"WARNING: status map {args.status_map} unreadable — "
              "dead-policy gate OFF, statuses UNVERIFIED", file=sys.stderr)

    specs = [
        {"sheet": "4246 Audit Verification", "columns": REPORT_4246_COLUMNS,
         "rows": read_csv(args.report_4246), "composer": compose_4246_action},
        {"sheet": "4247 Manual Renewal", "columns": REPORT_4247_COLUMNS,
         "rows": read_csv(args.report_4247), "composer": compose_4247_action},
        {"sheet": "4744 Mortgagee", "columns": REPORT_4744_COLUMNS,
         "rows": read_csv(args.report_4744), "composer": compose_4744_action},
    ]
    wb = build_preview_workbook(specs, today, knowledge=knowledge,
                                status_map=status_map)
    out = args.out or f"worker-preview-{datetime.now().strftime('%Y%m%d-%H%M')}.xlsx"
    wb.save(out)
    total = sum(len(s["rows"]) for s in specs)
    print(f"Done. {total} policies -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
