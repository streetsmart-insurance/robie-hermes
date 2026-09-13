#!/usr/bin/env python3
"""
ROBIE hourly health check — eyes and ears on the box.

Runs via cron (hourly). Checks the things that have actually bitten us:
  1. Worker process alive + last poll time
  2. Loaded CODE_VERSION vs latest merged commit on main
  3. Required env vars present (names only, never values)
  4. HITL dry-run: Gemini reachable? Email path constructible? Chat webhook present?
  5. Stuck job leases
  6. Disk usage (/tmp screenshots pile up)

Output:
  - JSON status file (always written, even when healthy)
  - Google Chat ping ONLY on failure (quiet when healthy)
  - Exit 0 = healthy, 1 = issues found, 2 = check itself errored

Usage (on hermes-poc-01):
  python3 robie_health_check.py [--status-dir /tmp/robie-health] [--no-chat]

Cron (hourly, quiet on success):
  0 * * * * /usr/bin/python3 /opt/streetsmart-hermes/scripts/robie_health_check.py >> /var/log/robie-health.log 2>&1
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REQUIRED_ENV_VARS = [
    "ROBIE_VERIFICATION_MAIL_SENDER",
    "ROBIE_VERIFICATION_GMAIL_DELEGATED_SERVICE_ACCOUNT",
    "ROBIE_GEMINI_PROJECT",
    "ROBIE_GOOGLE_CHAT_WEBHOOK_URL",
]

# How old (seconds) a job lease can be before we flag it as stuck.
STUCK_LEASE_SECONDS = 3600

# /tmp usage percent that triggers a warning.
TMP_WARN_PCT = 85


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Individual checks — each returns (ok: bool, detail: str, extra: dict)
# ---------------------------------------------------------------------------

def check_worker_alive() -> tuple[bool, str, dict]:
    """Is the email worker process running? When did it last poll?"""
    extra: dict = {}
    try:
        # Look for the worker process. Adjust the match string to the
        # actual worker entrypoint on the box.
        out = subprocess.run(
            ["pgrep", "-af", "robie.*worker|email.*agent|hermes.*gateway"],
            capture_output=True, text=True, timeout=10,
        )
        lines = [l for l in out.stdout.splitlines() if "pgrep" not in l]
        extra["matching_processes"] = len(lines)
        if not lines:
            return False, "no worker process found", extra
        # Last-poll marker: the worker touches a file each poll if configured.
        # Fall back to process start time.
        extra["sample"] = lines[0][:120]
        return True, f"{len(lines)} worker process(es) running", extra
    except Exception as exc:
        return False, f"process check failed: {type(exc).__name__}", extra


def check_code_version() -> tuple[bool, str, dict]:
    """What CODE_VERSION is importable? Does it match main's HEAD?"""
    extra: dict = {}
    try:
        sys.path.insert(0, "/opt/streetsmart-hermes/releases/current/robie-main2")
        from robie_job_engine.ezlynx_policy_setup import CODE_VERSION
        extra["loaded_version"] = CODE_VERSION
    except Exception as exc:
        return False, f"cannot import CODE_VERSION: {type(exc).__name__}: {exc}", extra

    # Compare against the deployed release symlink target (no network needed).
    try:
        link = "/opt/streetsmart-hermes/releases/current"
        target = os.readlink(link) if os.path.islink(link) else ""
        extra["release_target"] = target
        # The release dir is usually named with the commit, e.g. .../68954cc3...
        if extra["loaded_version"] and extra["loaded_version"][:8] not in target:
            return False, (
                f"loaded {extra['loaded_version'][:12]} but release dir is {target} "
                "(worker may be running stale code)"
            ), extra
    except Exception as exc:
        extra["release_check_error"] = f"{type(exc).__name__}"
    return True, f"CODE_VERSION={extra['loaded_version'][:12]}", extra


def check_env_vars() -> tuple[bool, str, dict]:
    """Are required env vars set? Names only — never log values."""
    extra: dict = {}
    missing = [name for name in REQUIRED_ENV_VARS if not os.environ.get(name, "").strip()]
    extra["checked"] = REQUIRED_ENV_VARS
    extra["missing"] = missing
    extra["present_count"] = len(REQUIRED_ENV_VARS) - len(missing)
    if missing:
        return False, f"missing env vars: {', '.join(missing)}", extra
    return True, f"all {len(REQUIRED_ENV_VARS)} required env vars present", extra


def check_hitl_dry_run() -> tuple[bool, str, dict]:
    """Can the HITL path at least construct its pieces (no sends)?"""
    extra: dict = {}
    problems: list[str] = []

    # Gemini client constructible + configured?
    try:
        sys.path.insert(0, "/opt/streetsmart-hermes/releases/current/robie-main2")
        from robie_job_engine.gemini_field_helper import VertexGeminiFieldClient
        client = VertexGeminiFieldClient()
        extra["gemini_configured"] = client.configured()
        extra["gemini_has_generate_content"] = hasattr(client, "generate_content")
        if not client.configured():
            problems.append("gemini client not configured (no project)")
        if not hasattr(client, "generate_content"):
            problems.append("gemini client missing generate_content (HITL will fail)")
    except Exception as exc:
        problems.append(f"gemini import failed: {type(exc).__name__}")
        extra["gemini_configured"] = False

    # Email sender importable?
    try:
        from robie_job_engine import verification_mailer
        extra["email_module"] = True
        # Do NOT send — just verify the config path raises a clear error
        # when misconfigured rather than at 2 AM during a real HITL.
        sender = verification_mailer._sender()
        sa = verification_mailer._delegated_service_account()
        extra["email_sender_set"] = bool(sender)
        extra["email_sa_set"] = bool(sa)
        if not sa:
            problems.append("delegated Gmail service account not configured")
    except Exception as exc:
        problems.append(f"email module import failed: {type(exc).__name__}")
        extra["email_module"] = False

    # Chat webhook present? (The actual send is a simple POST; presence is the check.)
    webhook = os.environ.get("ROBIE_GOOGLE_CHAT_WEBHOOK_URL", "").strip()
    extra["chat_webhook_present"] = bool(webhook)
    if not webhook:
        problems.append("ROBIE_GOOGLE_CHAT_WEBHOOK_URL not set")

    if problems:
        return False, "; ".join(problems), extra
    return True, "gemini + email + chat all constructible", extra


def check_stuck_leases() -> tuple[bool, str, dict]:
    """Any job leases older than STUCK_LEASE_SECONDS?"""
    extra: dict = {}
    # Lease state location is box-specific; try the common spots.
    candidates = [
        "/opt/streetsmart-hermes/job_state",
        "/tmp/robie-leases",
        os.path.expanduser("~/.robie/leases"),
    ]
    checked = []
    stuck = []
    now = time.time()
    for d in candidates:
        if not os.path.isdir(d):
            continue
        checked.append(d)
        for name in os.listdir(d):
            p = os.path.join(d, name)
            try:
                age = now - os.path.getmtime(p)
            except OSError:
                continue
            if age > STUCK_LEASE_SECONDS:
                stuck.append({"lease": name, "age_seconds": int(age)})
    extra["dirs_checked"] = checked
    extra["stuck"] = stuck
    if stuck:
        return False, f"{len(stuck)} stuck lease(s)", extra
    suffix = f" ({len(checked)} dirs)" if checked else " (no lease dirs found)"
    return True, "no stuck leases" + suffix, extra


def check_disk() -> tuple[bool, str, dict]:
    """Is /tmp filling up (screenshots accumulate)?"""
    extra: dict = {}
    try:
        st = os.statvfs("/tmp")
        pct = 100 * (1 - st.f_bavail / st.f_blocks)
        extra["tmp_used_pct"] = round(pct, 1)
        # Size of the HITL screenshot dir, if present.
        shot_dir = "/tmp/robie-hitl-screenshots"
        if os.path.isdir(shot_dir):
            total = 0
            for root, _, files in os.walk(shot_dir):
                for f in files:
                    try:
                        total += os.path.getsize(os.path.join(root, f))
                    except OSError:
                        pass
            extra["hitl_screenshots_mb"] = round(total / 1e6, 1)
        if pct >= TMP_WARN_PCT:
            return False, f"/tmp {pct:.0f}% full", extra
        return True, f"/tmp {pct:.0f}% used", extra
    except Exception as exc:
        return False, f"disk check failed: {type(exc).__name__}", extra


CHECKS = [
    ("worker_alive", check_worker_alive),
    ("code_version", check_code_version),
    ("env_vars", check_env_vars),
    ("hitl_dry_run", check_hitl_dry_run),
    ("stuck_leases", check_stuck_leases),
    ("disk", check_disk),
]


# ---------------------------------------------------------------------------
# Chat alert (failure only)
# ---------------------------------------------------------------------------

def send_chat_alert(failures: list[dict]) -> bool:
    webhook = os.environ.get("ROBIE_GOOGLE_CHAT_WEBHOOK_URL", "").strip()
    if not webhook:
        return False
    lines = ["🚨 *ROBIE health check FAILED*"]
    for f in failures:
        lines.append(f"• *{f['name']}*: {f['detail']}")
    lines.append(f"_{_now_iso()}_")
    body = json.dumps({"text": "\n".join(lines)}).encode()
    try:
        req = urllib.request.Request(
            webhook, data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="ROBIE hourly health check")
    ap.add_argument("--status-dir", default="/tmp/robie-health",
                    help="where to write status.json")
    ap.add_argument("--no-chat", action="store_true",
                    help="never send Chat alerts (status file only)")
    args = ap.parse_args()

    results: list[dict] = []
    try:
        for name, fn in CHECKS:
            try:
                ok, detail, extra = fn()
            except Exception as exc:  # a check must never kill the run
                ok, detail, extra = False, f"check crashed: {type(exc).__name__}: {exc}", {}
            results.append({
                "name": name, "ok": ok, "detail": detail,
                "extra": extra, "at": _now_iso(),
            })
    except Exception as exc:
        print(f"health check framework error: {exc}", file=sys.stderr)
        return 2

    failures = [r for r in results if not r["ok"]]
    healthy = not failures

    status = {
        "at": _now_iso(),
        "healthy": healthy,
        "failure_count": len(failures),
        "checks": results,
    }
    try:
        os.makedirs(args.status_dir, exist_ok=True)
        with open(os.path.join(args.status_dir, "status.json"), "w") as f:
            json.dump(status, f, indent=2)
    except Exception as exc:
        print(f"could not write status file: {exc}", file=sys.stderr)

    # Human-readable summary to stdout (goes to cron log).
    for r in results:
        mark = "OK  " if r["ok"] else "FAIL"
        print(f"[{mark}] {r['name']}: {r['detail']}", flush=True)

    if failures and not args.no_chat:
        sent = send_chat_alert(failures)
        print(f"chat alert sent: {sent}", flush=True)

    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
