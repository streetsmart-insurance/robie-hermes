#!/usr/bin/env python3
"""Read-only audit of the verification workers on a Hermes host.

Checks (never mutates, never prints secrets):
  1. release pointer -> expected commit sha prefix
  2. hermes-gateway active
  3. the 5 verification job types import cleanly from the live release
  4. verification schedules present in scheduled_jobs (sqlite, read-only)
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
    return {
        "ok": all(s["installed"] and s["enabled"] for s in schedules),
        "schedules": schedules,
        "other_schedules": other,
        "db_path": db_path,
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
        "vm_service_account": check_vm_service_account(),
        "env": check_secret_names(),
    }
    result["ok"] = all(
        [
            release["ok"],
            result["gateway"]["ok"],
            result["job_types"]["ok"],
            result["schedules"]["ok"],
        ]
    )
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
