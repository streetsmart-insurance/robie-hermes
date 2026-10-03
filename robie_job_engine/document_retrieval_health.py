"""Read-only health check for the document-retrieval run.

Green means the last run is recent, exited 0, held nothing, and did not
record EZLYNX_WRITE_SCOPE_REFUSED or document_filed_note_held. This module
does not pull, file, or post a note. A Chat alert reuses the certificate
sweep poster and is deduped: one alert while the run is bad, and one
recovery note when it is healthy again.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

LAST_RUN_NAME = "document-retrieval-last-run.json"
ALERT_STATE_NAME = "document-retrieval-health-alert.json"
BAD_SIGNALS = ("EZLYNX_WRITE_SCOPE_REFUSED", "document_filed_note_held")
EASTERN = ZoneInfo("America/New_York")
# Weekday job is 08:00 America/New_York. 26 hours covers the next morning.
DEFAULT_MAX_AGE_S = 26 * 3600
WEEKEND_MAX_AGE_S = 80 * 3600


def state_dir() -> Path:
    override = str(os.environ.get("ROBIE_DOCUMENT_RETRIEVAL_STATE_DIR") or "").strip()
    if override:
        return Path(override)
    db = str(os.environ.get("ROBIE_JOB_DB") or "").strip()
    if db:
        return Path(db).expanduser().resolve().parent
    return Path("/opt/streetsmart-hermes/robie-job-engine/data")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_stamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def read_last_run(directory: str | Path) -> dict[str, Any] | None:
    path = Path(directory) / LAST_RUN_NAME
    if not path.is_file():
        return None
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _stale(run_at: datetime, now: datetime, max_age_s: int) -> bool:
    age = (now - run_at).total_seconds()
    eastern = now.astimezone(EASTERN)
    # Saturday, Sunday, and Monday before the morning job keep Friday's run.
    if eastern.weekday() >= 5 or (eastern.weekday() == 0 and eastern.hour < 8):
        return age > WEEKEND_MAX_AGE_S
    return age > max_age_s


def probe(
    *,
    directory: str | Path = "",
    max_age_s: int = DEFAULT_MAX_AGE_S,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate the last-run file. Never pulls and never files."""

    root = Path(directory) if directory else state_dir()
    clock = now or _utcnow()
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    run = read_last_run(root)
    checks: dict[str, Any] = {"last_run": run}
    signals = []
    if run is None:
        checks["run_fresh"] = False
        checks["run_fresh_reason"] = "no document retrieval run has been recorded"
        checks["run_clean"] = False
        checks["run_clean_reason"] = "no document retrieval run has been recorded"
    else:
        stamp = _parse_stamp(run.get("run_at"))
        if stamp is None:
            checks["run_fresh"] = False
            checks["run_fresh_reason"] = "the last run has no clock time"
        else:
            age = (clock - stamp).total_seconds()
            checks["run_age_s"] = round(age, 1)
            checks["run_fresh"] = not _stale(stamp, clock, max_age_s)
            if not checks["run_fresh"]:
                checks["run_fresh_reason"] = (
                    f"last run was {checks['run_age_s']} seconds ago"
                )
        exit_code = run.get("exit_code")
        held = int(run.get("held") or 0)
        raw_signals = run.get("signals") if isinstance(run.get("signals"), list) else []
        blob = " ".join(str(item) for item in raw_signals)
        blob += " " + json.dumps(run.get("carriers") or [])
        signals = [name for name in BAD_SIGNALS if name in blob or name in raw_signals]
        reasons = []
        if exit_code != 0:
            reasons.append(f"last run exit code was {exit_code}")
        if held:
            reasons.append(f"last run held {held}")
        if signals:
            reasons.append("last run showed " + " and ".join(signals))
        checks["run_clean"] = not reasons
        if reasons:
            checks["run_clean_reason"] = "; ".join(reasons)
        checks["signals"] = signals
    green = bool(checks.get("run_fresh") and checks.get("run_clean"))
    return {
        "green": green,
        "at": clock.isoformat(timespec="seconds"),
        "max_age_s": max_age_s,
        "checks": checks,
    }


def _alert_path(directory: Path) -> Path:
    return directory / ALERT_STATE_NAME


def _read_state(directory: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(_alert_path(directory).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _write_state(directory: Path, state: dict[str, Any]) -> None:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        _alert_path(directory).write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        return


def _reasons(report: dict[str, Any]) -> list[str]:
    checks = report["checks"]
    reasons = []
    if not checks.get("run_fresh"):
        reasons.append(str(checks.get("run_fresh_reason") or "no fresh run"))
    if not checks.get("run_clean"):
        reasons.append(str(checks.get("run_clean_reason") or "last run was not clean"))
    return reasons


def render_alert(report: dict[str, Any]) -> str:
    lines = [
        f"Document retrieval is not healthy (checked {report['at']}).",
        "What I found:",
    ]
    lines.extend(f"- {reason}." for reason in _reasons(report))
    lines.append(
        "Filing stays off until ROBIE_DOCUMENT_RETRIEVAL_FILE_EZLYNX is 1. "
        "This check did not file anything."
    )
    return "\n".join(lines)


def render_recovery(report: dict[str, Any]) -> str:
    run = (report["checks"].get("last_run") or {})
    return (
        f"Document retrieval is healthy again (checked {report['at']}). "
        f"Last run at {run.get('run_at')}, exit {run.get('exit_code')}, "
        f"{run.get('filed', 0)} filed, {run.get('held', 0)} held."
    )


def _post(poster: Any, text: str) -> dict[str, Any]:
    if poster is None:
        try:
            from .cert_notify import _default_chat_poster

            poster = _default_chat_poster()
        except Exception as exc:
            return {"posted": False, "skipped": f"chat identity unavailable: {exc}"}
    try:
        poster(text)
        return {"posted": True}
    except Exception as exc:
        return {"posted": False, "failed": f"{type(exc).__name__}: {exc}"}


def maybe_alert(
    report: dict[str, Any],
    *,
    directory: str | Path = "",
    poster: Any = None,
    alert_every_s: int = 4 * 3600,
    now: datetime | None = None,
) -> dict[str, Any]:
    """One Chat note while unhealthy, and one when it recovers. Never raises."""

    root = Path(directory) if directory else state_dir()
    state = _read_state(root)
    clock = now or _utcnow()
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    now_iso = clock.isoformat(timespec="seconds")
    result: dict[str, Any] = {"alerted": False}
    if report["green"]:
        if state.get("red_since"):
            posted = _post(poster, render_recovery(report))
            result["recovery"] = posted
            result["alerted"] = posted.get("posted", False)
        _write_state(root, {})
        result["state"] = "green"
        return result
    reasons = _reasons(report)
    reason_key = "|".join(sorted(reasons))
    last_alert = _parse_stamp(state.get("last_alert_at"))
    due = (
        last_alert is None
        or (clock - last_alert).total_seconds() >= alert_every_s
        or state.get("reason_key") != reason_key
    )
    if not due:
        result["state"] = "red-quiet"
        return result
    posted = _post(poster, render_alert(report))
    result["alerted"] = posted.get("posted", False)
    result["post"] = posted
    _write_state(root, {
        "red_since": state.get("red_since") or now_iso,
        "last_alert_at": now_iso,
        "reason_key": reason_key,
        "reasons": reasons,
    })
    result["state"] = "red-alerted"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Document retrieval health check.")
    parser.add_argument("--alert", action="store_true", help="send a Chat note when not healthy")
    parser.add_argument("--max-age-s", type=int, default=DEFAULT_MAX_AGE_S)
    parser.add_argument("--alert-every-s", type=int, default=4 * 3600)
    parser.add_argument("--state-dir", default="")
    args = parser.parse_args(list(argv) if argv is not None else None)
    directory = args.state_dir or state_dir()
    report = probe(directory=directory, max_age_s=args.max_age_s)
    alert_result: dict[str, Any] = {}
    if args.alert:
        alert_result = maybe_alert(
            report, directory=directory, alert_every_s=args.alert_every_s
        )
    print(json.dumps({"probe": report, "alert": alert_result}, indent=2, default=str))
    return 0 if report["green"] else 1


if __name__ == "__main__":
    sys.exit(main())
