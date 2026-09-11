#!/usr/bin/env python3
"""Why did a Google Chat message not become a job? Read-only.

A Chat request has to cross three gaps before the engine can do anything with
it, and all three fail silently from the outside:

    Chat message  ->  chat_event_queue row  ->  jobs row  ->  worker + verifier

The milestone harness can only see the last hop. When it says BLOCKED it means
"no job row appeared", which is true but says nothing about WHERE the chain
broke. This prints the state of each hop so the answer is a fact rather than a
guess, and names the next place to look.

Read-only: opens the live database read-only, including committed WAL records, reads systemd, reads the journal.
Writes nothing, touches no job.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

OK = "ok"
BAD = "PROBLEM"
INFO = "info"


def _unit_property(unit: str, prop: str) -> str:
    try:
        return subprocess.run(
            ["systemctl", "show", unit, "-p", prop, "--value"],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()
    except Exception:
        return ""


def _resolve_unit(units: list[str]) -> tuple[str | None, list[str]]:
    seen: list[str] = []
    for unit in units:
        loaded = _unit_property(unit, "LoadState")
        if loaded == "loaded":
            return unit, seen
        seen.append(f"{unit}={loaded or 'absent'}")
    return None, seen


def _gateway_state(units: list[str]) -> tuple[str, str, str | None]:
    unit, seen = _resolve_unit(units)
    if unit is None:
        return BAD, (
            "no gateway unit found under any known name - checked "
            + ", ".join(seen)
            + ". Pass --unit <name> if it is called something else here."
        ), None
    active = _unit_property(unit, "ActiveState")
    sub = _unit_property(unit, "SubState")
    pid = _unit_property(unit, "MainPID")
    since = _unit_property(unit, "ActiveEnterTimestamp")
    detail = f"{unit}: ActiveState={active} SubState={sub} MainPID={pid} since {since or 'unknown'}"
    if active != "active":
        return BAD, detail, unit
    return OK, detail, unit


def _journal(unit: str, lines: int) -> list[str]:
    try:
        out = subprocess.run(
            ["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "-o", "short-iso"],
            capture_output=True, text=True, timeout=60,
        )
    except Exception as exc:
        return [f"(could not read journal: {type(exc).__name__}: {exc})"]
    text = (out.stdout or out.stderr or "").strip()
    return text.splitlines() if text else ["(journal empty for this unit)"]


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=15)
    conn.execute("PRAGMA query_only=ON")
    conn.row_factory = sqlite3.Row
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _since_iso(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    try:
        return list(conn.execute(sql, args))
    except sqlite3.Error as exc:
        raise RuntimeError(f"{exc} while running: {sql}") from exc


def _first_column(conn: sqlite3.Connection, table: str, candidates: list[str]) -> str | None:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    for name in candidates:
        if name in cols:
            return name
    return None


def _present_columns(conn: sqlite3.Connection, table: str, candidates) -> list[str]:
    """Only the candidate columns this table actually has.

    The first version of this script assumed an "id" column and crashed on a
    table that does not have one, taking the journal section down with it. A
    diagnostic that dies on the shape of the thing it is diagnosing is worse
    than useless: it hides the answer it had already found.
    """
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    return [c for c in candidates if c and c in cols]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db-path", required=True, type=Path)
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--unit", action="append", default=None)
    ap.add_argument("--journal-lines", type=int, default=60)
    args = ap.parse_args(argv)

    units = args.unit or ["robie-gateway", "hermes-gateway"]
    findings: list[tuple[str, str, str]] = []

    print(f"database: {args.db_path}")
    print(f"window:   last {args.hours}h")
    print()

    state, detail, unit = _gateway_state(units)
    findings.append((
        "gateway service", state, detail,
    ))

    print("1. IS THE GATEWAY RUNNING?")
    print("--------------------------")
    print(f"  [{state:>7}] {detail}")
    print()

    if not args.db_path.is_file():
        print("2. THE DATABASE")
        print("---------------")
        print(f"  [{BAD:>7}] {args.db_path} does not exist")
        findings.append(("job database", BAD, f"{args.db_path} does not exist"))
        _verdict(findings, unit, args)
        return 1

    conn = _open_readonly(args.db_path)
    tables = _tables(conn)
    cutoff = _since_iso(args.hours)

    print("2. CHAT MESSAGES THAT REACHED THE BOX (chat_event_queue)")
    print("--------------------------------------------------------")
    if "chat_event_queue" not in tables:
        print(f"  [{BAD:>7}] no chat_event_queue table - Chat intake has never run against this database")
        findings.append(("chat intake table", BAD, "chat_event_queue does not exist"))
        recent_events = 0
    else:
        created = _first_column(conn, "chat_event_queue", ["created_at", "received_at", "enqueued_at"])
        total = _rows(conn, "SELECT COUNT(*) AS n FROM chat_event_queue")[0]["n"]
        recent_events = 0
        if created:
            recent_events = _rows(
                conn, f"SELECT COUNT(*) AS n FROM chat_event_queue WHERE {created} >= ?", (cutoff,)
            )[0]["n"]
        status_col = _first_column(conn, "chat_event_queue", ["status", "state"])
        print(f"  total rows: {total}    in the last {args.hours}h: {recent_events}")
        print("  columns: " + ", ".join(
            r[1] for r in conn.execute("PRAGMA table_info(chat_event_queue)")
        ))
        if status_col:
            for row in _rows(conn, f"SELECT {status_col} AS s, COUNT(*) AS n FROM chat_event_queue GROUP BY 1 ORDER BY 2 DESC"):
                print(f"    {row['s']}: {row['n']}")
        if created:
            cols = _present_columns(
                conn, "chat_event_queue",
                ["id", "event_id", "message_id", "name", status_col, "attempts",
                 "last_error", "error", created],
            ) or [created]
            newest = _rows(
                conn,
                f"SELECT {', '.join(cols)} FROM chat_event_queue ORDER BY {created} DESC LIMIT 5",
            )
            if newest:
                print("  newest:")
                for row in newest:
                    parts = []
                    for c in cols:
                        value = row[c]
                        if value is None:
                            continue
                        text = str(value)
                        if len(text) > 120:
                            text = text[:117] + "..."
                        parts.append(f"{c}={text}")
                    print("    " + "  ".join(parts))
        state = OK if recent_events else BAD
        findings.append((
            "chat messages reaching the box", state,
            f"{recent_events} in the last {args.hours}h ({total} ever)",
        ))
    print()

    print("3. JOBS CREATED (jobs)")
    print("----------------------")
    recent_jobs = 0
    if "jobs" not in tables:
        print(f"  [{BAD:>7}] no jobs table in this database")
        findings.append(("job rows", BAD, "no jobs table"))
    else:
        created = _first_column(conn, "jobs", ["created_at", "enqueued_at", "inserted_at"])
        total = _rows(conn, "SELECT COUNT(*) AS n FROM jobs")[0]["n"]
        if created:
            recent_jobs = _rows(conn, f"SELECT COUNT(*) AS n FROM jobs WHERE {created} >= ?", (cutoff,))[0]["n"]
        print(f"  total rows: {total}    in the last {args.hours}h: {recent_jobs}")
        action = _first_column(conn, "jobs", ["action_type", "action", "job_type"])
        status = _first_column(conn, "jobs", ["status", "state"])
        if action and status and created:
            rows = _rows(
                conn,
                f"SELECT {action} AS a, {status} AS s, COUNT(*) AS n FROM jobs "
                f"WHERE {created} >= ? GROUP BY 1,2 ORDER BY 3 DESC LIMIT 15",
                (cutoff,),
            )
            for row in rows:
                print(f"    {row['a']}  {row['s']}  x{row['n']}")
            err = _first_column(conn, "jobs", ["last_error", "error"])
            cols = _present_columns(
                conn, "jobs", ["id", "job_id", action, status, err, created]
            ) or [created]
            for row in _rows(conn, f"SELECT {', '.join(cols)} FROM jobs ORDER BY {created} DESC LIMIT 5"):
                parts = []
                for c in cols:
                    value = row[c]
                    if value is None:
                        continue
                    text = str(value)
                    if len(text) > 160:
                        text = text[:157] + "..."
                    parts.append(f"{c}={text}")
                print("    " + "  ".join(parts))
        findings.append((
            "job rows created", OK if recent_jobs else BAD,
            f"{recent_jobs} in the last {args.hours}h ({total} ever)",
        ))
    print()

    if unit:
        print(f"4. RECENT JOURNAL FOR {unit}")
        print("-" * (22 + len(unit)))
        for line in _journal(unit, args.journal_lines):
            print("  " + line)
        print()

    conn.close()
    return _verdict(findings, unit, args)


def _verdict(findings, unit, args) -> int:
    print("WHAT THIS MEANS")
    print("---------------")
    by_label = {label: (state, detail) for label, state, detail in findings}
    problems = [(l, s, d) for l, s, d in findings if s == BAD]

    gateway_ok = by_label.get("gateway service", (BAD, ""))[0] == OK
    events_ok = by_label.get("chat messages reaching the box", (BAD, ""))[0] == OK
    jobs_ok = by_label.get("job rows created", (BAD, ""))[0] == OK

    if not gateway_ok:
        print("  The gateway is not running. Nothing can receive a Chat message, so every")
        print("  other line above describes a box that is switched off. Fix that first;")
        print("  everything else here is unreadable until it is running.")
    elif not events_ok:
        print("  The gateway is running but no Chat message reached it in this window.")
        print("  The break is BEFORE the engine: the Chat app is not delivering to this")
        print("  box. Check that the Chat app's endpoint points at this environment and")
        print("  that the message was sent to the space the app is installed in. Nothing")
        print("  about the engine or the verifier is being tested until a row appears here.")
    elif not jobs_ok:
        print("  Chat messages ARE arriving and landing in chat_event_queue, but none of")
        print("  them became a job. The break is in the intake step that turns an event")
        print("  into executable work - look at the queue rows' status and last_error")
        print("  above, and at the journal. This is not a verifier problem.")
    else:
        print("  Chat messages arrived AND jobs were created. The chain is intact up to")
        print("  the engine, so any failure is in the worker or the verifier, which is")
        print("  what the milestone evaluate step is for. Run it.")
    print()

    if problems:
        print("  Problems:")
        for label, _state, detail in problems:
            print(f"    - {label}: {detail}")
        return 1
    print("  No problems found in the intake chain.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
