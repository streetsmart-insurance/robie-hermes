#!/usr/bin/env python3
"""Daily runner for the four ROBIE verification workers.

Runs 4247, 4246, 4372, 4359 against the day's validated CSVs, builds the
morning digest (done / not done / pending by department), and saves state.

Usage:
    run_daily_workers.py --csv-dir DIR --queue-dir DIR --out DIR
                         [--mode dry_run|live] [--date YYYY-MM-DD]

Defaults to dry_run. Live mode requires ROBIE_LIVE_OUTREACH=1 AND --live,
plus business hours (enforced in verification_workers).

State: the 4246 audit queue lives in --queue-dir/audit-working-queue.json.
Keep --queue-dir OUTSIDE the release tree so deploys never wipe it.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import verification_workers as vw

REPORTS = ["4247", "4246", "4372", "4359"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv-dir", required=True,
                    help="directory holding report_<id>_<date>.csv files")
    ap.add_argument("--queue-dir", required=True,
                    help="durable state dir (audit queue lives here)")
    ap.add_argument("--out", required=True,
                    help="directory for digest + run logs")
    ap.add_argument("--mode", default="dry_run", choices=["dry_run", "live"])
    ap.add_argument("--date", default=date.today().isoformat())
    args = ap.parse_args(argv)

    day = date.fromisoformat(args.date)
    os.makedirs(args.out, exist_ok=True)
    os.makedirs(args.queue_dir, exist_ok=True)

    runs = []
    for report_id in REPORTS:
        csv_path = os.path.join(args.csv_dir,
                                f"report_{report_id}_{day.isoformat()}.csv")
        if not os.path.exists(csv_path):
            print(f"SKIP {report_id}: no CSV at {csv_path}")
            continue
        with open(csv_path, "rb") as f:
            csv_bytes = f.read()
        print(f"RUN {report_id} ({len(csv_bytes)} bytes) mode={args.mode} ...")
        run = vw.run_worker(report_id, day=day, mode=args.mode,
                            queue_dir=args.queue_dir, csv_bytes=csv_bytes)
        runs.append(run)
        print(f"  items={run.work_items} actions={len(run.actions)} "
              f"errors={len(run.errors)}")

    digest = vw.build_digest(runs)
    digest_path = os.path.join(args.out, f"digest_{day.isoformat()}.md")
    with open(digest_path, "w") as f:
        f.write(digest)
    print(f"digest written to {digest_path}")

    # Machine-readable run summary for the scheduler / Dusty.
    summary_path = os.path.join(args.out, f"runs_{day.isoformat()}.json")
    import json
    with open(summary_path, "w") as f:
        json.dump([{
            "report_id": r.report_id, "worker": r.worker, "day": r.day,
            "mode": r.mode, "ingested_rows": r.ingested_rows,
            "work_items": r.work_items,
            "done": sum(1 for a in r.actions if a.status == "done"),
            "pending": sum(1 for a in r.actions if a.status == "pending"),
            "errors": r.errors,
            "status_counts": {
                status: sum(1 for a in r.actions if a.status == status)
                for status in {a.status for a in r.actions}
            },
        } for r in runs], f, indent=2)
    print(f"summary written to {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
