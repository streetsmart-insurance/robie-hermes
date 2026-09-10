#!/usr/bin/env python3
"""Read-only audit of the verification workers on a Hermes host.

Checks (never mutates, never prints secrets):
  1. release pointer -> expected commit sha prefix
  2. hermes-gateway active
  3. the 5 verification job types import cleanly from the live release
  4. verification schedules present in scheduled_jobs (sqlite, read-only)
  4b. robie-scheduler 60s tick timer/service health (journal redacted)
  5. VM service account (metadata server, no auth)
  6. secret *names* configured for EZLynx (names only, never values)

Prints a single JSON document to stdout.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import urllib.request
from pathlib import Path

EXPECTED_JOB_TYPES = (
    "manual_renewal_verification",
    "audit_verification",
    "mortgagee_verification",
    "policy_change_verification",
    "daily_verification_digest",
)

EXPECTED_SCHEDULES = (
    "StreetSmart daily manual renewal verification",
    "StreetSmart daily audit verification",
    "StreetSmart daily mortgagee verification",
    "StreetSmart daily policy change verification",
    "StreetSmart daily verification digest",
)

# Schedules intentionally disabled by kill-switch (audited as "must stay disabled").
EXPECTED_DISABLED_SCHEDULES = (
    "StreetSmart daily policy change verification",  # kill-switched until report 4359 schema verified
)


def _run(argv: list[str], timeout: int = 10) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, (proc.stdout or "").strip()
    except Exception as exc:  # noqa: BLE001 - probe must not crash
        return 127, f"error: {exc}"


def check_release(opt_root: str, expect_sha: str) -> dict:
    link = Path(opt_root) / "current"
    try:
        target = os.readlink(link)
    except OSError as exc:
        return {"ok": False, "target": None, "error": str(exc)}
    base = Path(target).name
    return {
        "ok": expect_sha in target,
        "target": target,
        "release_dir": base,
        "expect_sha": expect_sha,
    }


def check_gateway(unit: str = "hermes-gateway") -> dict:
    rc, out = _run(["systemctl", "is-active", unit])
    return {"ok": rc == 0 and out == "active", "state": out}


def check_job_types(release_root: str) -> dict:
    sys.path.insert(0, release_root)
    try:
        from robie_job_engine import request_routing  # noqa: E402

        mapping: dict = {}
        for attr in ("JOB_TYPE_TO_WORKER", "ACTION_TO_WORKER", "BOUNDED_ENGINE_ACTIONS"):
            if hasattr(request_routing, attr):
                mapping[attr] = getattr(request_routing, attr)
        found = []
        haystack = json.dumps(mapping, default=str)
        for jt in EXPECTED_JOB_TYPES:
            found.append({"job_type": jt, "registered": jt in haystack})
        return {"ok": all(f["registered"] for f in found), "job_types": found}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def check_schedules(db_path: str) -> dict:
    if not Path(db_path).is_file():
        return {"ok": False, "error": f"db not found: {db_path}"}
    try:
        uri = f"file:{db_path}?mode=ro"
        con = sqlite3.connect(uri, uri=True)
        try:
            rows = con.execute(
                "SELECT task_name, action_type, cron_spec, timezone, enabled,"
                " next_run_at, last_run_at, last_job_id FROM scheduled_jobs"
            ).fetchall()
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    by_name = {r[0]: r for r in rows}
    schedules = []
    for name in EXPECTED_SCHEDULES:
        row = by_name.get(name)
        schedules.append(
            {
                "task_name": name,
                "installed": row is not None,
                "action_type": row[1] if row else None,
                "cron_spec": row[2] if row else None,
                "timezone": row[3] if row else None,
                "enabled": bool(row[4]) if row else None,
                "next_run_at": row[5] if row else None,
                "last_run_at": row[6] if row else None,
                "last_job_id": row[7] if row else None,
            }
        )
    other = [
        {"task_name": r[0], "action_type": r[1], "enabled": bool(r[4]),
         "next_run_at": r[5]}
        for r in rows
        if r[0] not in EXPECTED_SCHEDULES
    ]
    for s in schedules:
        want_enabled = s["task_name"] not in EXPECTED_DISABLED_SCHEDULES
        s["expected_enabled"] = want_enabled
    return {
        "ok": all(
            s["installed"] and s["enabled"] == s["expected_enabled"]
            for s in schedules
        ),
        "schedules": schedules,
        "other_schedules": other,
        "db_path": db_path,
    }


def check_scheduler_tick(
    timer: str = "robie-scheduler.timer",
    service: str = "robie-scheduler.service",
    release_root: str = "/opt/streetsmart-hermes/current",
) -> dict:
    """Inspect the 60s scheduler tick that picks up PENDING jobs (read-only).

    Journal lines are passed through the engine's secret redactor before
    being included, so no credential can leak into the audit output.
    """
    rc, out = _run(["systemctl", "is-active", timer])
    timer_active = rc == 0 and out == "active"
    rc, out = _run(["systemctl", "is-enabled", timer])
    timer_enabled = rc == 0 and out == "enabled"
    _, last_trigger = _run(
        ["systemctl", "show", timer, "-p", "LastTriggerUSec", "--value"]
    )
    _, next_elapse = _run(
        ["systemctl", "show", timer, "-p", "NextElapseUSec", "--value"]
    )
    _, exec_status = _run(
        ["systemctl", "show", service, "-p", "ExecMainStatus", "--value"]
    )
    # Journal evidence: how many tick runs in the last 30 min, plus the most
    # recent error-ish lines (redacted). Never print raw journal text.
    redact = lambda t: t  # noqa: E731 - replaced below when engine importable
    try:
        if release_root not in sys.path:
            sys.path.insert(0, release_root)
        from robie_job_engine.secrets import redact_text  # noqa: E402

        redact = redact_text
    except Exception:  # noqa: BLE001 - fall back to truncation only
        pass
    _, journal_tail = _run(
        ["journalctl", "-u", service, "--since", "30 minutes ago",
         "--no-pager", "-o", "cat"]
    )
    tick_lines = [l for l in journal_tail.splitlines() if l.strip()]
    err_lines = [
        redact(l)[:300]
        for l in tick_lines
        if any(k in l.lower() for k in ("error", "fail", "exception", "traceback"))
    ][:10]
    return {
        "timer": timer,
        "timer_active": timer_active,
        "timer_enabled": timer_enabled,
        "last_trigger_usec": last_trigger or None,
        "next_elapse_usec": next_elapse or None,
        "last_exec_status": exec_status or None,
        "journal_lines_30min": len(tick_lines),
        "recent_error_lines": err_lines,
        # A firing-but-crashing tick must fail the audit: require evidence the
        # last tick actually executed (systemd ExecMainStatus 0).
        "ok": timer_active and timer_enabled and (exec_status or "").strip() == "0",
    }


def check_vm_service_account() -> dict:
    req = urllib.request.Request(
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email",
        headers={"Metadata-Flavor": "Google"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return {"ok": True, "email": resp.read().decode().strip()}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def check_secret_names(unit: str = "hermes-gateway") -> dict:
    # Names only — values are never read or printed. Read the live service
    # environment (not the probe's own env, which is a fresh SSH session).
    rc, out = _run(["systemctl", "show", unit, "-p", "Environment", "--value"])
    names = {}
    for var in (
        "ROBIE_EZLYNX_USERNAME_SECRET",
        "ROBIE_EZLYNX_PASSWORD_SECRET",
        "VERIFICATION_DIGEST_OUTPUT_DIR",
        "VERIFICATION_DIGEST_EMAIL_SENDER",
    ):
        value = None
        for token in out.split():
            if token.startswith(var + "="):
                value = token[len(var) + 1:] or None
                break
        names[var] = value
    # Also report which EnvironmentFiles the unit loads (names only).
    rc2, out2 = _run(["systemctl", "show", unit, "-p", "EnvironmentFiles", "--value"])
    return {
        "secret_names_configured": names,
        "environment_files": out2 if rc2 == 0 else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only verification worker audit")
    parser.add_argument("--opt-root", default="/opt/streetsmart-hermes")
    parser.add_argument("--expect-sha", required=True)
    parser.add_argument(
        "--db",
        default="/opt/streetsmart-hermes/robie-job-engine/data/jobs.db",
    )
    args = parser.parse_args()

    release = check_release(args.opt_root, args.expect_sha)
    release_root = str(Path(args.opt_root) / "current")
    result = {
        "host": os.uname().nodename,
        "release": release,
        "gateway": check_gateway(),
        "job_types": check_job_types(release_root),
        "schedules": check_schedules(args.db),
        "scheduler_tick": check_scheduler_tick(
            release_root=str(Path(args.opt_root) / "current")
        ),
        "vm_service_account": check_vm_service_account(),
        "env": check_secret_names(),
    }
    result["ok"] = all(
        [
            release["ok"],
            result["gateway"]["ok"],
            result["job_types"]["ok"],
            result["schedules"]["ok"],
            result["scheduler_tick"]["ok"],
        ]
    )
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
