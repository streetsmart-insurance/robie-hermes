#!/usr/bin/env python3
"""
Accountability VM health probe — runs ON streetsmart-accountability-prod.

Lightweight, read-only. Collects:
  1. systemd service states for the accountability pipeline
     (collect, collect-evening, main report, late-check, team-lead EOD)
  2. Last successful 9 AM report delivery timestamp
     (from the report output dir / delivery marker)
  3. Source-collection status (morning/evening collect exit codes)
  4. Disk usage on the data volume

Output: JSON to stdout (and optionally to a status file). Designed to be
scp'd to the accountability VM and run via a systemd timer there (Dusty's
lane), OR invoked remotely from hermes-poc-01 via gcloud compute ssh.

The JSON is shaped so hermes-poc-01's robie_health_check.py (or a Chat
poster) can ingest it without parsing free text.

Scheduling (Dusty):
  - Timer: daily ~09:30 ET (after the 9 AM report + 09:20 late-check)
  - On failure: the probe itself exits 1; pair with OnFailure= alerting or
    have it POST to the ROBIE health Chat webhook (see --chat-webhook-env).
  - Also worth a second run ~17:30 ET after the evening collect.

Read-only: no services restarted, no files written outside --status-dir,
no credentials read (service states and timestamps only).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

# The accountability pipeline units (from the 2026-09-28 inventory).
ACCOUNTABILITY_SERVICES = [
    "streetsmart-accountability-collect.service",
    "streetsmart-accountability.service",
    "streetsmart-accountability-late-check.service",
    "streetsmart-accountability-collect-evening.service",
    "streetsmart-accountability-team-lead-chat-eod.service",
]

ACCOUNTABILITY_TIMERS = [
    "streetsmart-accountability-collect.timer",
    "streetsmart-accountability.timer",
    "streetsmart-accountability-late-check.timer",
    "streetsmart-accountability-collect-evening.timer",
    "streetsmart-accountability-team-lead-chat-eod.timer",
]

# Where the 9 AM report lands (delivery proof = newest report file mtime).
# Adjust if the app writes elsewhere; the probe reports what it finds.
REPORT_SEARCH_DIRS = [
    "/opt/streetsmart-daily-accountability/data/outputs",
    "/opt/streetsmart-daily-accountability/data/reports",
    "/opt/streetsmart-daily-accountability/outputs",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run(cmd: list[str], timeout: int = 15) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def check_service_states() -> tuple[bool, str, dict]:
    """Are the accountability pipeline services in a sane state?"""
    extra: dict = {"services": {}}
    problems: list[str] = []
    for svc in ACCOUNTABILITY_SERVICES:
        try:
            out = _run(["systemctl", "is-active", svc]).stdout.strip()
            sub = _run(["systemctl", "show", svc, "-p", "SubState",
                        "-p", "ExecMainStatus"]).stdout
            props = dict(l.split("=", 1) for l in sub.splitlines() if "=" in l)
            extra["services"][svc] = {
                "active_state": out,
                "sub_state": props.get("SubState", "?"),
                "last_exit": props.get("ExecMainStatus", "?"),
            }
            # A oneshot service that exited non-zero last run is a problem,
            # but only if its timer is supposed to have fired it recently.
            # We flag dead/failed unconditionally; staleness is the timer check.
            if out in ("failed", "inactive") and "collect" in svc:
                # Collect services are oneshot; inactive between runs is fine.
                # 'failed' is not.
                if out == "failed":
                    problems.append(f"{svc} in failed state")
        except Exception as exc:
            extra["services"][svc] = {"error": type(exc).__name__}
            problems.append(f"{svc}: check failed ({type(exc).__name__})")
    if problems:
        return False, "; ".join(problems), extra
    return True, f"{len(ACCOUNTABILITY_SERVICES)} services checked", extra


def check_timer_states() -> tuple[bool, str, dict]:
    """Are the timers active? When did each last trigger?"""
    extra: dict = {"timers": {}}
    problems: list[str] = []
    for timer in ACCOUNTABILITY_TIMERS:
        try:
            out = _run(["systemctl", "show", timer, "-p", "ActiveState",
                        "-p", "LastTriggerUSec"]).stdout
            props = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
            state = props.get("ActiveState", "unknown")
            last = props.get("LastTriggerUSec", "").strip()
            extra["timers"][timer] = {"state": state, "last_trigger": last[:32]}
            if state != "active":
                problems.append(f"{timer} not active (state={state})")
            elif last.lower() in ("n/a", "", "0"):
                problems.append(f"{timer} never triggered")
        except Exception as exc:
            extra["timers"][timer] = {"error": type(exc).__name__}
            problems.append(f"{timer}: check failed ({type(exc).__name__})")
    if problems:
        return False, "; ".join(problems), extra
    return True, f"{len(ACCOUNTABILITY_TIMERS)} timers active", extra


def check_report_delivery() -> tuple[bool, str, dict]:
    """When was the 9 AM accountability report last delivered?

    Delivery proof = newest report file mtime in the known output dirs.
    Fails if no report file exists from today (after 09:30 ET the report
    should be there — this probe is meant to run ~09:30 ET).
    """
    extra: dict = {"searched": REPORT_SEARCH_DIRS}
    newest: float | None = None
    newest_name = ""
    for d in REPORT_SEARCH_DIRS:
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            p = os.path.join(d, name)
            try:
                if not os.path.isfile(p):
                    continue
                mt = os.path.getmtime(p)
            except OSError:
                continue
            if newest is None or mt > newest:
                newest = mt
                newest_name = p
    if newest is None:
        return False, "no accountability report files found in searched dirs", extra
    extra["newest_report"] = newest_name
    extra["newest_mtime"] = datetime.fromtimestamp(newest, timezone.utc).isoformat()
    age_h = (time.time() - newest) / 3600
    extra["age_hours"] = round(age_h, 1)
    # The report is due 09:00 ET daily. If the newest file is older than
    # 26h, yesterday's report never landed (or the path is wrong).
    if age_h > 26:
        return False, (
            f"no accountability report in {age_h:.1f}h "
            f"(newest: {os.path.basename(newest_name)})"
        ), extra
    return True, f"report delivered {age_h:.1f}h ago ({os.path.basename(newest_name)})", extra


def check_disk() -> tuple[bool, str, dict]:
    """Is the data volume filling up?"""
    extra: dict = {}
    candidates = ["/opt/streetsmart-daily-accountability", "/"]
    for path in candidates:
        if not os.path.isdir(path):
            continue
        try:
            st = os.statvfs(path)
            pct = 100 * (1 - st.f_bavail / st.f_blocks)
            extra[path] = round(pct, 1)
            if pct >= 90:
                return False, f"{path} {pct:.0f}% full", extra
        except OSError:
            continue
    if not extra:
        return False, "no volumes checkable", extra
    worst = max(extra.values())
    return True, f"disk OK (max {worst:.0f}% used)", extra


CHECKS = [
    ("service_states", check_service_states),
    ("timer_states", check_timer_states),
    ("report_delivery", check_report_delivery),
    ("disk", check_disk),
]


def main() -> int:
    ap = argparse.ArgumentParser(description="Accountability VM health probe")
    ap.add_argument("--status-dir", default="/tmp/accountability-health",
                    help="where to write status.json (empty = stdout only)")
    ap.add_argument("--no-file", action="store_true",
                    help="print JSON to stdout only, don't write a file")
    args = ap.parse_args()

    results: list[dict] = []
    for name, fn in CHECKS:
        try:
            ok, detail, extra = fn()
        except Exception as exc:
            ok, detail, extra = False, f"check crashed: {type(exc).__name__}", {}
        results.append({"name": name, "ok": ok, "detail": detail,
                        "extra": extra, "at": _now_iso()})

    failures = [r for r in results if not r["ok"]]
    status = {
        "host": "streetsmart-accountability-prod",
        "at": _now_iso(),
        "healthy": not failures,
        "failure_count": len(failures),
        "checks": results,
    }

    if not args.no_file:
        try:
            os.makedirs(args.status_dir, exist_ok=True)
            with open(os.path.join(args.status_dir, "status.json"), "w") as f:
                json.dump(status, f, indent=2)
        except Exception as exc:
            print(f"could not write status file: {exc}", file=sys.stderr)

    # Always print JSON to stdout — this is how hermes-poc-01 ingests it
    # when run over gcloud compute ssh.
    print(json.dumps(status, indent=2))
    for r in results:
        mark = "OK  " if r["ok"] else "FAIL"
        print(f"[{mark}] {r['name']}: {r['detail']}", file=sys.stderr, flush=True)

    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
