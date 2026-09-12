#!/usr/bin/env python3
"""Zero-job-aware Chrome browser refresh script.

Verifies no jobs are currently RUNNING or VERIFYING before restarting the browser service,
preventing mid-flight job disruption.

Not a scheduled owner. `robie-chrome-refresh.timer` is retired from this
tree because a restart without login is the bug. Session heal is
`.github/workflows/monitor-ezlynx-session.yml`. Dusty will mask the live
03:30 unit separately. Do not re-enable a daily restart from a Production zip.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys


def has_active_jobs(db_path: str) -> bool:
    if not os.path.exists(db_path):
        return False
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        count = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN ('RUNNING', 'VERIFYING') AND (lease_expires_at IS NULL OR lease_expires_at > datetime('now'))"
        ).fetchone()[0]
        conn.close()
        return count > 0
    except Exception as exc:
        print(f"Warning: Failed to query jobs DB ({exc}); failing safe by assuming active jobs.")
        return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Recycle Chrome session if no jobs are active")
    parser.add_argument(
        "--db",
        default=os.environ.get("ROBIE_JOB_DB", "/opt/streetsmart-hermes/robie-job-engine/data/jobs.db"),
    )
    parser.add_argument("--service", default="robie-ezlynx-browser.service")
    parser.add_argument("--force", action="store_true", help="Force restart even if jobs are active")
    args = parser.parse_args()

    if not args.force and has_active_jobs(args.db):
        print("Active jobs currently RUNNING or VERIFYING. Skipping Chrome session refresh.")
        return 0

    print(f"Zero active jobs confirmed. Restarting {args.service}...")
    try:
        subprocess.run(["systemctl", "restart", args.service], check=True)
        print(f"Successfully refreshed {args.service}.")
        return 0
    except Exception as exc:
        print(f"Failed to restart {args.service}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
