#!/usr/bin/env python3
"""Poll/report one-shot overdue clean Test job on hermes-test-01."""
from __future__ import annotations

import argparse
import re
import sqlite3
import subprocess
from pathlib import Path


def running() -> bool:
    try:
        out = subprocess.check_output(["pgrep", "-af", "run_one_overdue_clean_sheets"], text=True)
    except subprocess.CalledProcessError:
        return False
    return bool(out.strip())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["status", "report"], required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--db", default="/opt/streetsmart-hermes-test/robie-job-engine/data/jobs.db")
    args = ap.parse_args()
    out = Path(args.out)

    if args.mode == "status":
        if running():
            print("RUNNING")
            return
        if out.exists() and '"status"' in out.read_text(errors="replace"):
            print("DONE")
            return
        print("WAITING")
        return

    text = out.read_text(errors="replace") if out.exists() else ""
    print("==== OUT ====")
    print(text)
    print("==== SKIP/CONNIE LOGS ====")
    for line in text.splitlines():
        if re.search(
            r"skip|Connie|non-agency|1099|External Producer|SHEETS_DIRECTORY|AUDIT_SUMMARY|TEST ONLY|created_job_id|COMPLETE",
            line,
            re.I,
        ):
            print(line)
    print("==== JOB LEDGER ====")
    m = re.search(r'"created_job_id": "([0-9a-f-]{36})"', text) or re.search(
        r'"job_id": "([0-9a-f-]{36})"', text
    )
    print("parsed", m.group(1) if m else None)
    if not m:
        return
    jid = m.group(1)
    db_uri = f"file:{Path(args.db).expanduser().resolve()}?mode=ro"
    with sqlite3.connect(db_uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "select id,status,last_error,attempt_count,verification_count from jobs where id=?",
            (jid,),
        ).fetchone()
        print(dict(row) if row else None)
        atts = conn.execute(
            "select attempt_number,phase,outcome,substr(coalesce(detail_json,''),1,500) d from attempts where job_id=? order by rowid",
            (jid,),
        ).fetchall()
        for a in atts:
            print(dict(a))


if __name__ == "__main__":
    main()
