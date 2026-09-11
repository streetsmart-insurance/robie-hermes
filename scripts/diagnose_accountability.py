#!/usr/bin/env python3
"""Why didn't the 9 AM accountability report go out? Read-only, plain English.

The report is a systemd timer, not a cron:

    OnCalendar=Mon..Fri *-*-* 09:00:00 America/New_York
    Unit=streetsmart-accountability.service

and the service runs scripts/run_accountability_job.py daily as the
streetsmart-hermes user. Silence can mean the timer is not enabled, the unit
failed, an EnvironmentFile is missing, or the job ran and terminated
UNVERIFIED. Those need completely different fixes and look identical from the
outside, so check all four and say which it was.

Touches nothing. systemctl queries are read-only; jobs.db is opened mode=ro.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

TIMER = "streetsmart-accountability.timer"
SERVICE = "streetsmart-accountability.service"
ENV_FILES = (
    "/etc/streetsmart-hermes/robie-accountability.env",
    "/etc/streetsmart-hermes/robie-ezlynx.env",
)
ACTIONS = ("accountability.daily", "accountability.weekly", "accountability.monthly")


def _sh(*args: str) -> str:
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=20)
    except Exception as exc:
        return f"<could not run {' '.join(args)}: {type(exc).__name__}: {exc}>"
    return ((out.stdout or "") + (out.stderr or "")).strip()


def section(title: str) -> None:
    print()
    print(title)
    print("-" * len(title))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db-path", required=True, type=Path, help="jobs.db to read (mode=ro)")
    ap.add_argument("--days", type=int, default=7, help="how far back to look for jobs")
    args = ap.parse_args(argv)

    findings: list[str] = []

    section("1. Is the timer switched on?")
    enabled = _sh("systemctl", "is-enabled", TIMER)
    active = _sh("systemctl", "is-active", TIMER)
    print(f"  is-enabled: {enabled}")
    print(f"  is-active:  {active}")
    if "enabled" not in enabled:
        findings.append(
            f"The timer is not enabled ({enabled or 'no answer'}). Nothing schedules the "
            "report, so it has never been due. Fix: enable and start "
            f"{TIMER}."
        )
    elif "active" not in active:
        findings.append(
            f"The timer is enabled but not active ({active}). It is installed and will "
            "not fire until started."
        )

    section("2. When did it last fire, and when is it next due?")
    print(_sh("systemctl", "list-timers", TIMER, "--all", "--no-pager") or "  <no timer listed>")

    section("3. Did the last run fail?")
    result = _sh("systemctl", "show", SERVICE, "-p", "Result", "--value")
    exec_status = _sh("systemctl", "show", SERVICE, "-p", "ExecMainStatus", "--value")
    last_start = _sh("systemctl", "show", SERVICE, "-p", "ExecMainStartTimestamp", "--value")
    print(f"  Result:              {result or '<none>'}")
    print(f"  ExecMainStatus:      {exec_status or '<none>'}")
    print(f"  last start:          {last_start or '<never started>'}")
    if not last_start:
        findings.append(
            "The service has never started. Combined with the timer state above, that "
            "says the report has not run at all, rather than running and failing."
        )
    elif result and result not in ("success", ""):
        findings.append(
            f"The last run ended with Result={result}, exit status {exec_status}. The job "
            "started and failed — the journal below says why."
        )

    section("4. Recent journal for the service")
    print(_sh("journalctl", "-u", SERVICE, "-n", "40", "--no-pager") or "  <no journal entries>")

    section("5. Are the environment files present?")
    for path in ENV_FILES:
        exists = Path(path).is_file()
        print(f"  {'ok ' if exists else 'MISSING'}  {path}")
        if not exists:
            findings.append(
                f"{path} does not exist. systemd refuses to start a unit whose "
                "EnvironmentFile is missing, so the service would fail immediately every "
                "time the timer fires."
            )

    section(f"6. Accountability jobs in the last {args.days} days")
    try:
        conn = sqlite3.connect(f"file:{args.db_path}?mode=ro", uri=True, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT action_type, status, completed_at, last_error, created_at"
                " FROM jobs WHERE action_type IN (?,?,?)"
                " AND created_at >= datetime('now', ?)"
                " ORDER BY created_at DESC LIMIT 25",
                (*ACTIONS, f"-{args.days} days"),
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        print(f"  <could not read {args.db_path}: {type(exc).__name__}: {exc}>")
        rows = []
        findings.append(
            f"Could not read the job database at {args.db_path}. That is itself a problem "
            "— it usually means a permissions issue on the file or its directory."
        )
    else:
        if not rows:
            print("  no accountability jobs created at all in this window")
            findings.append(
                "No accountability job rows were created in this window. The report never "
                "reached the job engine, so this is a scheduling or startup problem, not a "
                "verification one."
            )
        for row in rows:
            print(f"  {row['created_at']}  {row['action_type']:<24} {row['status']:<16} "
                  f"{(row['last_error'] or '')[:60]}")
        unverified = [r for r in rows if r["status"] == "UNVERIFIED"]
        if unverified:
            findings.append(
                f"{len(unverified)} accountability job(s) ran but finished UNVERIFIED. The "
                "work may well have happened; what failed was proving it. The last_error "
                "above names the reason."
            )

    section("WHAT THIS MEANS")
    if not findings:
        print("  Nothing obviously wrong. The timer is on, the service last exited cleanly,")
        print("  the environment files are present, and jobs are reaching the engine. If the")
        print("  report still is not arriving, the next place to look is delivery — whether")
        print("  the report was written and who it was sent to — not scheduling.")
        return 0
    for n, item in enumerate(findings, 1):
        print(f"  {n}. {item}")
        print()
    return 1


if __name__ == "__main__":
    sys.exit(main())
