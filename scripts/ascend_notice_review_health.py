#!/usr/bin/env python3
"""Outcome-based health check for the Ascend notice review runner (Test).

Reads the runner's state dir (status.json written by run_once) and verifies
the OUTCOME, not just that a process is alive:

- status.json exists, parses, and is fresh (ran_at within --max-age-minutes)
- last run status was "ok" (not "incomplete")
- mode is "propose_only"
- destination_writes == 0 and gmail_label_changes == 0 for every mailbox
  (the propose-only invariant: nothing filed, no mail relabeled)
- state files are 0600

Quiet when healthy (exit 0, one line to stdout).
Plain-English ALERT lines to stdout + exit 1 when anything is wrong.
Does not send anything anywhere; the caller (cron/timer) routes the output.

Usage:
  ROBIE_ASCEND_NOTICE_REVIEW_STATE_DIR=/path/to/state \\
      python3 scripts/ascend_notice_review_health.py [--max-age-minutes 30]
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_DIR_ENV = "ROBIE_ASCEND_NOTICE_REVIEW_STATE_DIR"
EXPECTED = ("checkpoints.json", "status.json", "review_queue.db")


def _problems(state_dir: Path, max_age: timedelta) -> list[str]:
    problems: list[str] = []
    status_path = state_dir / "status.json"
    if not status_path.is_file():
        return [f"status.json is missing in {state_dir} (runner has never completed a run)"]
    try:
        status = json.loads(status_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return [f"status.json is unreadable: {exc}"]
    if not isinstance(status, dict):
        return ["status.json is not a JSON object"]

    ran_at = status.get("ran_at")
    try:
        ran = datetime.fromisoformat(str(ran_at))
        if ran.tzinfo is None:
            ran = ran.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return [f"status.json ran_at is not a valid timestamp: {ran_at!r}"]
    age = datetime.now(timezone.utc) - ran
    if age > max_age:
        problems.append(
            f"last run is stale: ran_at={ran_at} ({age} ago, limit {max_age}) — "
            "the timer may have stopped firing"
        )

    run_status = status.get("status")
    if run_status == "incomplete":
        problems.append("last run did not complete (status=incomplete) — check errors in status.json")
    elif run_status not in ("ok",):
        # "disabled"/"skipped" are quiet: feature off or a transient lock race.
        pass

    if status.get("mode") != "propose_only":
        problems.append(f"unexpected mode {status.get('mode')!r} (expected propose_only)")

    for mailbox, mb in (status.get("mailboxes") or {}).items():
        if not isinstance(mb, dict):
            continue
        dw = mb.get("destination_writes", 0) or 0
        gl = mb.get("gmail_label_changes", 0) or 0
        if dw:
            problems.append(f"{mailbox}: destination_writes={dw} — propose-only invariant broken")
        if gl:
            problems.append(f"{mailbox}: gmail_label_changes={gl} — read-only invariant broken")
        for err in (mb.get("errors") or [])[:5]:
            problems.append(f"{mailbox}: run error: {err}")

    for name in EXPECTED:
        p = state_dir / name
        if p.exists():
            mode = stat.S_IMODE(p.stat().st_mode)
            if mode != 0o600:
                problems.append(f"{name} has permissions {oct(mode)}, expected 0o600")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Outcome health check for the Ascend notice review runner")
    ap.add_argument("--state-dir", default=os.environ.get(STATE_DIR_ENV, ""))
    ap.add_argument("--max-age-minutes", type=int, default=30)
    args = ap.parse_args(argv)
    if not args.state_dir:
        print(f"ALERT: {STATE_DIR_ENV} is not set — health check cannot run")
        return 1
    state_dir = Path(args.state_dir)
    if not state_dir.is_dir():
        print(f"ALERT: state dir {state_dir} does not exist — runner has never run here")
        return 1
    problems = _problems(state_dir, timedelta(minutes=args.max_age_minutes))
    if problems:
        for p in problems:
            print("ALERT:", p)
        return 1
    print("OK: Ascend notice review runner healthy (last run ok, propose-only, no writes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
