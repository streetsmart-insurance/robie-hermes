#!/usr/bin/env python3
"""Did the job engine actually start completing work? Pass/fail, from the data.

Run this BEFORE the change to capture a baseline, and again AFTER. "It looks
better" is not an outcome; this prints one of:

    PASS       jobs are completing with authoritative evidence
    NO CHANGE  same gaps as the baseline — the fix did not take effect
    BLOCKED    could not tell (no jobs since the baseline)

Read-only (sqlite mode=ro). Writes one small baseline file, nothing else.

    python3 scripts/verify_engine_working.py --db-path /opt/streetsmart-hermes-test/robie-job-engine/data/jobs.db --baseline # before deploy
    python3 scripts/verify_engine_working.py --db-path /opt/streetsmart-hermes-test/robie-job-engine/data/jobs.db           # after deploy
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

VERIFIER_GAP = "no independent verifier registered"
DEST_GAP = "no independent destination verifier registered"
CHECKPOINT_GAP = "no structured destination action checkpoint"
GAPS = (DEST_GAP, VERIFIER_GAP, CHECKPOINT_GAP)


def snapshot(db: Path) -> dict:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS total,"
            " SUM(CASE WHEN completed_at IS NOT NULL THEN 1 ELSE 0 END) AS completed,"
            " SUM(CASE WHEN last_error IN (?,?,?) THEN 1 ELSE 0 END) AS gaps,"
            " MAX(updated_at) AS last_activity"
            " FROM jobs",
            GAPS,
        ).fetchone()
        ev = conn.execute("SELECT COUNT(*) AS n FROM verification_evidence").fetchone()["n"]
        chat = conn.execute(
            "SELECT COUNT(*) AS n,"
            " SUM(CASE WHEN completed_at IS NOT NULL THEN 1 ELSE 0 END) AS done"
            " FROM jobs WHERE action_type = 'hermes.google_chat_task'"
        ).fetchone()
        return {
            "total": row["total"] or 0,
            "completed": row["completed"] or 0,
            "gaps": row["gaps"] or 0,
            "evidence_rows": ev or 0,
            "chat_jobs": chat["n"] or 0,
            "chat_completed": chat["done"] or 0,
            "last_activity": row["last_activity"],
        }
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Did the job engine actually start completing work? Pass/fail, from the data."
    )
    ap.add_argument(
        "--db-path",
        required=True,
        type=Path,
        help="path to jobs.db (required; must contain '-test' unless --allow-production is set)",
    )
    ap.add_argument(
        "--allow-production",
        action="store_true",
        help="permit running against a database path that does not contain '-test'",
    )
    ap.add_argument("--baseline", action="store_true", help="record the before state")
    ap.add_argument(
        "--state",
        type=Path,
        default=Path("engine_baseline.json"),
        help="path to baseline JSON file",
    )
    args = ap.parse_args(argv)

    resolved_path = args.db_path.resolve()
    resolved_str = str(resolved_path)
    is_test = "-test" in resolved_str
    env_concluded = "TEST" if is_test else "PRODUCTION"

    print(f"database:    {resolved_path}")
    print(f"environment: {env_concluded}")

    if not is_test and not args.allow_production:
        print(
            f"FAIL  database path does not contain '-test' and --allow-production"
            f" was not passed: {resolved_path}"
        )
        return 2

    if not resolved_path.is_file():
        print(f"FAIL  not a file: {resolved_path}")
        return 2

    now = snapshot(resolved_path)

    if args.baseline:
        args.state.write_text(json.dumps(now, indent=2))
        print("BASELINE RECORDED")
        for k, v in now.items():
            print(f"  {k:<18} {v}")
        print(f"\n  saved to {args.state}")
        print("  Deploy, run a Bond-like job on UAT, then re-run without --baseline.")
        return 0

    if not args.state.is_file():
        print("FAIL  no baseline. Run with --baseline first, before deploying.")
        return 2
    before = json.loads(args.state.read_text())

    print("                     before    after    delta")
    for k in ("total", "completed", "gaps", "evidence_rows", "chat_jobs", "chat_completed"):
        b, a = before.get(k, 0), now.get(k, 0)
        print(f"  {k:<18} {b:>6}   {a:>6}   {a - b:>+6}")

    new_jobs = now["total"] - before["total"]
    new_completed = now["completed"] - before["completed"]
    new_evidence = now["evidence_rows"] - before["evidence_rows"]
    new_gaps = now["gaps"] - before["gaps"]
    new_chat_done = now["chat_completed"] - before["chat_completed"]

    print()
    if new_jobs == 0:
        print("BLOCKED    no new jobs since the baseline — nothing ran, so nothing")
        print("           is proven either way. Run a job, then re-run this.")
        return 3

    if new_completed > 0 and new_evidence > 0:
        print(f"PASS       {new_completed} job(s) completed and {new_evidence} evidence")
        print("           row(s) were written since the baseline.")
        if new_chat_done > 0:
            print(f"           {new_chat_done} of them were hermes.google_chat_task —")
            print("           that is the Bond path working.")
        if new_gaps > 0:
            print(f"           NOTE: {new_gaps} new job(s) still hit an engine gap.")
            print("           Run robie_engine_triage.py to see which action types.")
        return 0

    if new_evidence > 0 and new_completed == 0:
        print(f"PARTIAL    {new_evidence} evidence row(s) written but nothing completed.")
        print("           The verifier is RUNNING but not returning authoritative")
        print("           evidence, or a postcondition is failing. That is progress —")
        print("           check the last_error on the newest jobs.")
        return 1

    print(f"NO CHANGE  {new_jobs} new job(s), {new_gaps} new engine gap(s), 0 completed,")
    print("           0 new evidence. The verifier is not being reached.")
    print("           Do NOT write more verifiers — find out which JobEngine ran")
    print("           these jobs first. This is the same signature as the email path.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
