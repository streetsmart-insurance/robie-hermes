#!/usr/bin/env python3
"""Look at one job before deciding whether to interrupt it. Read-only.

Written for the case where a deploy is blocked by a RUNNING job and the only
facts available are "it is running" and "the heartbeat is advancing". Those two
do not distinguish a worker doing real work from a zombie holding a lease, and
the difference decides whether interrupting is safe.

What it answers:
  - is the lease still held, and does it expire soon
  - has anything actually moved recently, or is the heartbeat the only motion
  - what has the job already done to the destination (playwright_exec)
  - what has been verified, if anything (verification_evidence)
  - how many attempts remain before the engine gives up on its own

Read-only: opens the database with mode=ro&immutable=1. Writes nothing, clears
no lease, transitions no job.

Redaction: job payloads can carry client data, so free text is never printed.
Payload keys are always listed; values are printed only for a small whitelist
of routing fields. Use --show-payload-values to add fields deliberately.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

SAFE_PAYLOAD_KEYS = (
    "worker", "applicant_id", "policy_number", "action", "kind",
    "space", "conversation_id", "gmail_message_id", "thread_id",
)


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        value = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _ago(ts: str | None, now: datetime) -> str:
    moment = _parse(ts)
    if moment is None:
        return "unknown"
    delta = (now - moment).total_seconds()
    if delta < 0:
        return f"in {abs(delta):.0f}s"
    if delta < 90:
        return f"{delta:.0f}s ago"
    if delta < 5400:
        return f"{delta / 60:.0f}m ago"
    return f"{delta / 3600:.1f}h ago"


def _redacted_payload(raw: str) -> tuple[list[str], dict]:
    try:
        payload = json.loads(raw)
    except Exception:
        return ["(payload is not JSON)"], {}
    if not isinstance(payload, dict):
        return [f"(payload is {type(payload).__name__}, not an object)"], {}
    shown = {k: payload[k] for k in SAFE_PAYLOAD_KEYS if k in payload}
    return sorted(payload.keys()), shown


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db-path", required=True, type=Path)
    ap.add_argument("--job-id", required=True)
    ap.add_argument("--show-payload-values", action="append", default=[],
                    help="extra payload key to print the value of; repeatable")
    args = ap.parse_args(argv)

    if not args.db_path.is_file():
        print(f"PROBLEM: {args.db_path} does not exist")
        return 2

    now = datetime.now(timezone.utc)
    conn = _open_readonly(args.db_path)

    rows = list(conn.execute("SELECT * FROM jobs WHERE id = ?", (args.job_id,)))
    if not rows:
        print(f"No job with id {args.job_id} in {args.db_path}")
        print()
        print("WHAT THIS MEANS")
        print("---------------")
        print("  The id is wrong, or it belongs to a different database (Test vs Production).")
        print("  Nothing is blocking on this box under that id.")
        return 2
    job = rows[0]

    print(f"database:   {args.db_path}")
    print(f"job:        {job['id']}")
    print(f"action:     {job['action_type']}")
    print(f"status:     {job['status']}" + (f"  (resume: {job['resume_status']})" if job["resume_status"] else ""))
    print(f"created:    {job['created_at']}  ({_ago(job['created_at'], now)})")
    print(f"updated:    {job['updated_at']}  ({_ago(job['updated_at'], now)})")
    print(f"completed:  {job['completed_at'] or '-'}")
    print(f"attempts:   {job['attempt_count']} of {job['max_attempts']}"
          f"   verifications: {job['verification_count']}")
    print(f"lease:      owner={job['lease_owner'] or '-'}  expires={job['lease_expires_at'] or '-'}"
          f"  ({_ago(job['lease_expires_at'], now)})")
    print(f"next wake:  {job['next_wakeup_at'] or '-'}")
    print(f"last error: {job['last_error'] or '-'}")
    print()

    keys, shown = _redacted_payload(job["payload_json"])
    for extra in args.show_payload_values:
        try:
            payload = json.loads(job["payload_json"])
            if isinstance(payload, dict) and extra in payload:
                shown[extra] = payload[extra]
        except Exception:
            pass
    print("PAYLOAD")
    print("-------")
    print("  keys: " + ", ".join(keys))
    for key, value in sorted(shown.items()):
        text = str(value)
        if len(text) > 120:
            text = text[:117] + "..."
        print(f"  {key} = {text}")
    print("  (other values withheld - may contain client data; --show-payload-values to add)")
    print()

    print("WHAT IT HAS DONE TO THE DESTINATION (playwright_exec)")
    print("-----------------------------------------------------")
    execs = list(conn.execute(
        "SELECT tool, status, code_preview, created_at, updated_at "
        "FROM playwright_exec WHERE job_id = ? ORDER BY id", (args.job_id,)))
    if not execs:
        print("  none - this job has not touched the browser")
    for row in execs:
        preview = (row["code_preview"] or "").replace("\n", " ")
        if len(preview) > 90:
            preview = preview[:87] + "..."
        print(f"  {row['created_at']}  {row['tool']}  {row['status']}"
              f"  updated {_ago(row['updated_at'], now)}")
        if preview:
            print(f"      {preview}")
    print()

    print("CHECKPOINTS")
    print("-----------")
    cps = list(conn.execute(
        "SELECT kind, created_at FROM checkpoints WHERE job_id = ? ORDER BY id", (args.job_id,)))
    if not cps:
        print("  none")
    for row in cps:
        print(f"  {row['created_at']}  {row['kind']}")
    print()

    print("ATTEMPTS")
    print("--------")
    attempts = list(conn.execute(
        "SELECT phase, attempt_number, outcome, created_at "
        "FROM attempts WHERE job_id = ? ORDER BY id", (args.job_id,)))
    if not attempts:
        print("  none recorded")
    for row in attempts:
        print(f"  {row['created_at']}  {row['phase']} #{row['attempt_number']}  {row['outcome']}")
    print()

    print("VERIFICATION EVIDENCE")
    print("---------------------")
    evidence = list(conn.execute(
        "SELECT verified, method, source, authoritative, locator, captured_at "
        "FROM verification_evidence WHERE job_id = ? ORDER BY id", (args.job_id,)))
    if not evidence:
        print("  none - nothing has independently confirmed anything for this job")
    for row in evidence:
        print(f"  {row['captured_at']}  verified={bool(row['verified'])}"
              f"  authoritative={bool(row['authoritative'])}"
              f"  method={row['method']}  source={row['source']}")
    conn.close()
    print()

    lease_expiry = _parse(job["lease_expires_at"])
    updated = _parse(job["updated_at"])
    newest_exec = max((_parse(r["updated_at"]) for r in execs), default=None) if execs else None
    lease_live = lease_expiry is not None and lease_expiry > now
    moved = max([t for t in (updated, newest_exec) if t], default=None)
    idle_seconds = (now - moved).total_seconds() if moved else None

    print("WHAT THIS MEANS")
    print("---------------")
    if job["status"] not in ("RUNNING", "PENDING"):
        print(f"  This job is {job['status']}. It is not holding anything up. If a deploy is")
        print("  still blocked, the blocker is a different job - list RUNNING jobs instead.")
    elif not lease_live:
        print("  The lease has EXPIRED. The engine's own recovery path will reclaim this job")
        print("  without anyone intervening. Interrupting by hand is not needed and adds a")
        print("  second writer for no gain. Wait one lease period and look again.")
    elif idle_seconds is not None and idle_seconds > 600:
        print(f"  The lease is live but nothing has moved in {idle_seconds / 60:.0f} minutes.")
        print("  A heartbeat alone does not mean work is happening - this looks like a worker")
        print("  that is stuck rather than busy. Check the gateway journal for its process")
        print("  before deciding; if the process is gone, the lease will expire on its own.")
    else:
        print("  The lease is live and something moved recently. This job is genuinely working.")
        print("  Let it finish if you can afford to wait.")
    print()
    if execs:
        print("  It has already touched the browser. Whatever it half-wrote to EZLynx is")
        print("  real and will still be there afterwards - check the destination before")
        print("  re-running anything against the same applicant.")
    else:
        print("  It has not touched the browser, so nothing is half-written to EZLynx.")
    print()
    print("  If you do interrupt: stop the WORKER PROCESS first, then let the lease lapse.")
    print("  Clearing the lease row while the worker is still alive gives you two writers")
    print("  against the same destination, which is the one outcome worse than waiting.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
