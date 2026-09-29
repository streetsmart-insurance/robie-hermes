"""Buddy for the certificate sweep: health probe + alert.

Carlo's standing rule (2026-09-28): prove Green on test, ship the buddy
(probe + alert that fails if Green isn't true) with the PR. If the buddy
can't be named, the feature stays Test-only. Carlo must never be the
first person who finds it broken in Prod.

What "Green" means for the cert sweep:

  1. Trigger alive — ``robie-cert-sweep.timer`` is active.
  2. Runs fresh    — the last recorded sweep run is younger than
                     ``CERT_SWEEP_HEALTH_MAX_AGE_S`` (default 900s = 3x
                     the 5-minute sweep cadence).
  3. Runs clean    — the last run exited 0 with zero ``errors``.
                     (UNVERIFIED items are the designed fail-closed state
                     and do NOT break Green.)

The probe is READ-ONLY: it never touches Gmail, EZLynx, or Zapier. It
reads the ``sweep_runs`` table — written by :mod:`cert_sweep_service`
after every run — and asks systemd about the timer.

Usage::

    python -m robie_job_engine.cert_sweep_health          # probe; exit 0/1
    python -m robie_job_engine.cert_sweep_health --alert   # probe; Chat-alert when red

The alert reuses the same Google Chat sink as :mod:`cert_notify`.
Alerts are deduped: one alert per red episode, a repeat at most every
``CERT_SWEEP_HEALTH_ALERT_EVERY_S`` (default 4h) while still red, and a
recovery notice when Green returns after red. Alert state lives in
``health_alert_state.json`` inside the sweep data dir.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

DB_FILENAME = "cert-sweep.db"
ALERT_STATE_FILENAME = "health_alert_state.json"
SWEEP_TIMER_UNIT = "robie-cert-sweep.timer"

#: Rows of run history to keep; the probe only ever reads the newest.
SWEEP_RUNS_KEEP = 1000

_RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS sweep_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at TEXT NOT NULL,
    exit_code INTEGER NOT NULL,
    filed INTEGER NOT NULL DEFAULT 0,
    unverified INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    elapsed_s REAL
);
"""


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default) or default


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def data_dir() -> str:
    """Sweep data dir (same resolution as cert_sweep.data_dir)."""
    path = os.path.expanduser(_env("CERT_SWEEP_DATA_DIR", "~/.cert-sweep"))
    os.makedirs(path, exist_ok=True)
    return path


def db_path_for(directory: str = "") -> str:
    return os.path.join(directory or data_dir(), DB_FILENAME)


# ---------------------------------------------------------------------------
# Run recording (called by cert_sweep_service after every run)
# ---------------------------------------------------------------------------

def record_sweep_run(*, exit_code: int, filed: int = 0, unverified: int = 0,
                     errors: int = 0, elapsed_s: float | None = None,
                     directory: str = "") -> dict[str, Any]:
    """Append one row to sweep_runs. Never raises (probe data must not
    fail the sweep itself). Returns {"recorded": bool}."""
    try:
        path = db_path_for(directory)
        conn = sqlite3.connect(path)
        try:
            conn.execute(_RUNS_SCHEMA)
            conn.execute(
                "INSERT INTO sweep_runs "
                "(run_at, exit_code, filed, unverified, errors, elapsed_s)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (_utcnow().isoformat(timespec="seconds"), int(exit_code),
                 int(filed), int(unverified), int(errors),
                 None if elapsed_s is None else float(elapsed_s)),
            )
            conn.execute(
                "DELETE FROM sweep_runs WHERE id NOT IN "
                f"(SELECT id FROM sweep_runs ORDER BY id DESC LIMIT {SWEEP_RUNS_KEEP})"
            )
            conn.commit()
        finally:
            conn.close()
        return {"recorded": True}
    except Exception as exc:  # the run record must never fail the service
        return {"recorded": False, "error": f"{type(exc).__name__}: {exc}"}


def last_run(directory: str = "") -> dict[str, Any] | None:
    """Newest sweep_runs row, or None when the table is empty/missing."""
    path = db_path_for(directory)
    if not os.path.exists(path):
        return None
    conn = sqlite3.connect(path)
    try:
        try:
            row = conn.execute(
                "SELECT run_at, exit_code, filed, unverified, errors,"
                " elapsed_s FROM sweep_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        except sqlite3.OperationalError:
            return None  # table predates the buddy
    finally:
        conn.close()
    if row is None:
        return None
    return {
        "run_at": row[0], "exit_code": row[1], "filed": row[2],
        "unverified": row[3], "errors": row[4], "elapsed_s": row[5],
    }


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------

def _default_timer_check() -> bool:
    """True when the sweep timer unit is active. Never raises."""
    try:
        out = subprocess.run(
            ["systemctl", "is-active", SWEEP_TIMER_UNIT],
            capture_output=True, timeout=15, check=False, text=True,
        )
        return out.stdout.strip() == "active"
    except Exception:
        return False


def probe(*, directory: str = "", max_age_s: int = 900,
          timer_check: Any = None) -> dict[str, Any]:
    """Evaluate Green. Returns a JSON-serializable report; the ``green``
    key is the verdict. Never raises."""
    timer_check = timer_check or _default_timer_check
    checks: dict[str, Any] = {}
    try:
        timer_active = bool(timer_check())
    except Exception as exc:
        timer_active = False
        checks["timer_error"] = f"{type(exc).__name__}: {exc}"
    checks["timer_active"] = timer_active

    run = last_run(directory)
    checks["last_run"] = run
    now = _utcnow()
    if run is None:
        checks["run_fresh"] = False
        checks["run_fresh_reason"] = "no recorded sweep runs yet"
        checks["run_clean"] = False
        checks["run_clean_reason"] = "no recorded sweep runs yet"
    else:
        try:
            run_at = datetime.fromisoformat(run["run_at"])
            if run_at.tzinfo is None:
                run_at = run_at.replace(tzinfo=timezone.utc)
            age_s = (now - run_at).total_seconds()
        except Exception:
            age_s = float("inf")
        checks["run_age_s"] = round(age_s, 1) if age_s != float("inf") else None
        checks["run_fresh"] = age_s <= max_age_s
        if not checks["run_fresh"]:
            checks["run_fresh_reason"] = (
                f"last run {checks['run_age_s']}s ago "
                f"(limit {max_age_s}s)")
        checks["run_clean"] = (run["exit_code"] == 0 and run["errors"] == 0)
        if not checks["run_clean"]:
            checks["run_clean_reason"] = (
                f"last run exit_code={run['exit_code']} "
                f"errors={run['errors']}")

    green = bool(checks["timer_active"] and checks["run_fresh"]
                 and checks["run_clean"])
    return {
        "green": green,
        "at": now.isoformat(timespec="seconds"),
        "max_age_s": max_age_s,
        "checks": checks,
    }


# ---------------------------------------------------------------------------
# Alert (deduped Chat alert when red)
# ---------------------------------------------------------------------------

def _alert_state_path(directory: str) -> str:
    return os.path.join(directory or data_dir(), ALERT_STATE_FILENAME)


def _read_alert_state(directory: str) -> dict[str, Any]:
    try:
        with open(_alert_state_path(directory), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _write_alert_state(directory: str, state: dict[str, Any]) -> None:
    try:
        with open(_alert_state_path(directory), "w", encoding="utf-8") as fh:
            json.dump(state, fh)
    except Exception:
        pass  # alert bookkeeping must never break the probe


def _red_reasons(report: dict[str, Any]) -> list[str]:
    checks = report["checks"]
    reasons = []
    if not checks.get("timer_active"):
        reasons.append(
            f"timer {SWEEP_TIMER_UNIT} is not active"
            + (f" ({checks['timer_error']})" if checks.get("timer_error") else ""))
    if not checks.get("run_fresh"):
        reasons.append(checks.get("run_fresh_reason") or "no fresh run")
    if not checks.get("run_clean"):
        reasons.append(checks.get("run_clean_reason") or "last run not clean")
    return reasons


def render_alert(report: dict[str, Any]) -> str:
    reasons = _red_reasons(report)
    lines = [
        "Certificate sweep buddy: NOT GREEN"
        f" (checked {report['at']}).",
        "What I found:",
    ]
    lines += [f"\u2022 {r}." for r in reasons]
    lines += [
        "The certificate inbox is not being swept right now — new mail is "
        "waiting. See deploy/systemd/robie-cert-sweep.RUNBOOK.md "
        "(buddy section) for the recovery steps.",
    ]
    return "\n".join(lines)


def render_recovery(report: dict[str, Any]) -> str:
    run = report["checks"].get("last_run") or {}
    return (
        "Certificate sweep buddy: GREEN again"
        f" (checked {report['at']}). Last run at {run.get('run_at')}, "
        f"exit 0, {run.get('filed', 0)} filed, "
        f"{run.get('unverified', 0)} held for human eyes."
    )


def maybe_alert(report: dict[str, Any], *, directory: str = "",
                poster: Any = None,
                alert_every_s: int = 4 * 3600) -> dict[str, Any]:
    """Send a Chat alert when red (deduped), a recovery note when Green
    returns after red. ``poster`` is a callable(text) -> Any; when None,
    the cert_notify Chat identity is used. Never raises."""
    directory = directory or data_dir()
    state = _read_alert_state(directory)
    now_iso = _utcnow().isoformat(timespec="seconds")
    result: dict[str, Any] = {"alerted": False}

    if report["green"]:
        if state.get("red_since"):
            text = render_recovery(report)
            result["recovery"] = _post(poster, text)
            result["alerted"] = result["recovery"].get("posted", False)
        _write_alert_state(directory, {})
        result["state"] = "green"
        return result

    reasons = _red_reasons(report)
    reason_key = "|".join(sorted(reasons))
    last_alert_at = state.get("last_alert_at")
    try:
        last_alert = (datetime.fromisoformat(last_alert_at)
                      if last_alert_at else None)
    except Exception:
        last_alert = None
    due = (last_alert is None
           or (_utcnow() - (last_alert if last_alert.tzinfo
                            else last_alert.replace(tzinfo=timezone.utc))
               ).total_seconds() >= alert_every_s
           or state.get("reason_key") != reason_key)
    if not due:
        result["state"] = "red-quiet"
        result["next_alert_not_before"] = (
            (last_alert + timedelta(seconds=alert_every_s)).isoformat()
            if last_alert else None)
        return result

    text = render_alert(report)
    posted = _post(poster, text)
    result["alerted"] = posted.get("posted", False)
    result["post"] = posted
    _write_alert_state(directory, {
        "red_since": state.get("red_since") or now_iso,
        "last_alert_at": now_iso,
        "reason_key": reason_key,
        "reasons": reasons,
    })
    result["state"] = "red-alerted"
    return result


def _post(poster: Any, text: str) -> dict[str, Any]:
    if poster is None:
        try:
            from .cert_notify import _default_chat_poster
            poster = _default_chat_poster()
        except Exception as exc:
            return {"posted": False,
                    "skipped": f"chat identity unavailable: {exc}"}
    try:
        poster(text)
        return {"posted": True}
    except Exception as exc:
        return {"posted": False,
                "failed": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cert_sweep_health",
        description="Certificate sweep buddy: health probe (+ optional alert).")
    parser.add_argument("--alert", action="store_true",
                        help="send a (deduped) Chat alert when not Green")
    parser.add_argument("--max-age-s", type=int, default=900,
                        help="max seconds since last run (default 900)")
    parser.add_argument("--alert-every-s", type=int, default=4 * 3600,
                        help="min seconds between repeat alerts (default 14400)")
    parser.add_argument("--data-dir", default="",
                        help="sweep data dir (default CERT_SWEEP_DATA_DIR)")
    args = parser.parse_args(argv)

    directory = args.data_dir or data_dir()
    report = probe(directory=directory, max_age_s=args.max_age_s)
    alert_result: dict[str, Any] = {}
    if args.alert:
        alert_result = maybe_alert(
            report, directory=directory, alert_every_s=args.alert_every_s)
    print(json.dumps({"probe": report, "alert": alert_result},
                     indent=2, default=str))
    # Exit 0 = Green. Non-zero = not Green (this is the buddy "failing").
    return 0 if report["green"] else 1


if __name__ == "__main__":
    sys.exit(main())
