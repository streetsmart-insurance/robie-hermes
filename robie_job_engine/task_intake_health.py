#!/usr/bin/env python3
"""Health probe for the EZLynx Task Check-In intake.

Quiet when healthy. During weekdays 9:00–6:00 PM ET it alerts the ROBIE
health Chat when:

- the intake has not succeeded in 20 minutes, or the last run exited
  non-zero
- the check-in email is missing
- the newest Created Date, converted to Eastern, has not moved for longer
  than the stall threshold (default 90 minutes). That stall pages once
  per episode, then stays quiet until the row moves again (one recovery
  line). It does not page before 10:30 AM ET, so a quiet Monday morning
  does not page.
- N consecutive reports hash the same (default 3)
- the dry-run drop-in is still installed while live calls are on
- the EZLynx driver lease is not held by PRODUCTION. That pages once
  per outage, then one line when the lease is back with PRODUCTION
- the intake unit's effective environment is missing
  ROBIE_EZLYNX_WRITE_SCOPE=all, or an EnvironmentFile sets
  ROBIE_EZLYNX_WRITE_SCOPE or ROBIE_PLAYGROUND

Outside those hours the probe stays quiet, except for a leftover dry-run
drop-in while live. ``--simulate-failure --no-chat`` prints a failure and
does not post.
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from .chat_app_post import post_as_chat_app, robie_home_space
from .report_clock import age_minutes, as_eastern_clock, in_business_hours, report_created_et

logger = logging.getLogger("task_intake_health")

DEFAULT_FRESH_MINUTES = 20
DEFAULT_STALL_MINUTES = 90
DEFAULT_SAME_DIGEST_LIMIT = 3
STALL_QUIET_UNTIL = time(10, 30)
DEFAULT_DROPIN_DIR = "/etc/systemd/system/robie-task-intake.service.d"
RECOVERY_LINE = "the newest Created Date is moving again"
LEASE_RECOVERY_LINE = "the driver lease is back with PRODUCTION"
_ENV_FILE_ASSIGN_RE = re.compile(
    r"^(?:export\s+)?(?:ROBIE_EZLYNX_WRITE_SCOPE|ROBIE_PLAYGROUND)="
)


def default_db_path() -> str:
    return os.environ.get(
        "ROBIE_JOB_DB", "/opt/streetsmart-hermes/robie-job-engine/data/jobs.db"
    )


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def fresh_minutes() -> int:
    return _int_env("ROBIE_TASK_INTAKE_FRESH_MINUTES", DEFAULT_FRESH_MINUTES)


def stall_minutes() -> int:
    return _int_env("ROBIE_TASK_INTAKE_STALL_MINUTES", DEFAULT_STALL_MINUTES)


def same_digest_limit() -> int:
    return _int_env("ROBIE_TASK_INTAKE_SAME_DIGEST_LIMIT", DEFAULT_SAME_DIGEST_LIMIT)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return report_created_et(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _health_space() -> str:
    space = os.environ.get("ROBIE_HEALTH_CHAT_SPACE", "").strip()
    if space:
        return space
    return robie_home_space() or ""


def _load_heartbeats(db_path: str, limit: int) -> list[sqlite3.Row]:
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "ezlynx_task_intake_heartbeats" not in tables:
            return []
        return list(conn.execute(
            """SELECT created_at, status, message_id, digest, newest_created_et, row_count, error
               FROM ezlynx_task_intake_heartbeats
               ORDER BY id DESC LIMIT ?""",
            (limit,),
        ))
    finally:
        conn.close()


def _distinct_reports(rows: list[dict]) -> list[dict]:
    """One sample per report message, newest first.

    A 5-minute heartbeat rewrites the same message_id. Those ticks are
    one report, not three identical emails.
    """
    samples: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        message_id = str(row.get("message_id") or "")
        if message_id:
            if message_id in seen:
                continue
            seen.add(message_id)
        samples.append(row)
    return samples


def _dropin_dir(override: str | None) -> Path:
    if override:
        return Path(override)
    return Path(os.environ.get("ROBIE_TASK_INTAKE_UNIT_DIR", DEFAULT_DROPIN_DIR))


def _dry_run_while_live(dropin_dir: str | None) -> str | None:
    """The dry-run drop-in must not stay installed once live calls are on."""
    directory = _dropin_dir(dropin_dir)
    if not (directory / "10-dry-run.conf").is_file():
        return None
    live_file = (directory / "20-bland-prod.conf").is_file()
    live_env = os.environ.get("ROBIE_PHONE_LIVE_CALLS", "").strip() == "1"
    if not live_file and not live_env:
        return None
    return (
        "10-dry-run.conf is still installed on the task intake unit "
        "while live calls are enabled"
    )


def _load_episode(db_path: str) -> dict:
    episode = {"stall_open": False, "lease_open": False}
    if not os.path.exists(db_path):
        return episode
    try:
        conn = sqlite3.connect(db_path, timeout=5)
    except sqlite3.Error:
        logger.warning("health episode could not be read")
        return episode
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS ezlynx_task_intake_health_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )"""
        )
        rows = conn.execute(
            "SELECT key, value FROM ezlynx_task_intake_health_state "
            "WHERE key IN ('stall_open', 'lease_open')"
        ).fetchall()
        flags = {str(key): str(value) == "1" for key, value in rows}
        episode["stall_open"] = flags.get("stall_open", False)
        episode["lease_open"] = flags.get("lease_open", False)
    except sqlite3.Error:
        logger.warning("health episode could not be read")
    finally:
        conn.close()
    return episode


def _save_episode(db_path: str, episode: dict) -> None:
    if not os.path.exists(db_path):
        return
    try:
        conn = sqlite3.connect(db_path, timeout=5)
    except sqlite3.Error:
        logger.warning("health episode could not be saved")
        return
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS ezlynx_task_intake_health_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )"""
        )
        for key in ("stall_open", "lease_open"):
            conn.execute(
                """INSERT INTO ezlynx_task_intake_health_state (key, value)
                   VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (key, "1" if episode.get(key) else "0"),
            )
        conn.commit()
    except sqlite3.Error:
        logger.warning("health episode could not be saved")
    finally:
        conn.close()


def _driver_lease_line(episode: dict, *, reader=None) -> str:
    """Once per outage, then one line when the lease returns to PRODUCTION.

    A repeat probe during the same outage stays quiet. Hours outside the
    business window never reach this, so a weekend does not clear the episode.
    """
    from .ezlynx_driver_gate import LEASE_NOT_WITH_PRODUCTION, production_driver_refused

    if production_driver_refused(reader=reader):
        if episode.get("lease_open"):
            return ""
        episode["lease_open"] = True
        return LEASE_NOT_WITH_PRODUCTION
    if episode.get("lease_open"):
        episode["lease_open"] = False
        return LEASE_RECOVERY_LINE
    return ""


def _environment_file_paths(shown: str) -> list[str]:
    """Absolute paths from ``systemctl show -p EnvironmentFiles``.

    The line looks like ``EnvironmentFiles=/etc/robie.env (ignore_errors)``.
    The parenthetical flag is not a path.
    """
    text = shown or ""
    if "EnvironmentFiles=" in text:
        text = text.split("EnvironmentFiles=", 1)[1]
    return [token for token in text.split() if token.startswith("/")]


def _file_assigns_scope_or_playground(text: str) -> bool:
    """True when a non-comment line assigns either key. Any value counts."""
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if _ENV_FILE_ASSIGN_RE.match(line):
            return True
    return False


def _environment_file_override(shown: str | None) -> str:
    """EnvironmentFile= overrides Environment=, so a file that sets either key is red."""
    if not shown:
        return ""
    for path in _environment_file_paths(shown):
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError:
            logger.warning("could not read EnvironmentFile %s", path)
            continue
        if _file_assigns_scope_or_playground(text):
            return (
                "an EnvironmentFile sets ROBIE_EZLYNX_WRITE_SCOPE or ROBIE_PLAYGROUND"
            )
    return ""


def _effective_scope_problem(
    effective_environment: str | None,
    environment_files: str | None = None,
) -> str:
    """The intake unit's effective env must contain the all-clients scope.

    ``systemctl show -p Environment`` is the effective value. EnvironmentFile=
    overrides Environment= regardless of order, so every file from
    ``systemctl show -p EnvironmentFiles`` is read too. A file that assigns
    ROBIE_EZLYNX_WRITE_SCOPE or ROBIE_PLAYGROUND is red. The check stays off
    unless a caller passes the show output or
    ROBIE_TASK_INTAKE_CHECK_EFFECTIVE_ENV=1.
    """
    text = effective_environment
    files = environment_files
    if text is None and files is None:
        if os.environ.get("ROBIE_TASK_INTAKE_CHECK_EFFECTIVE_ENV", "").strip() != "1":
            return ""
        text = _systemctl_show("Environment")
        files = _systemctl_show("EnvironmentFiles")
    missing = ""
    if "ROBIE_EZLYNX_WRITE_SCOPE=all" not in (text or ""):
        missing = "effective environment is missing ROBIE_EZLYNX_WRITE_SCOPE=all"
    override = _environment_file_override(files)
    if missing and override:
        return f"{missing}; {override}"
    return missing or override


def _systemctl_show(prop: str) -> str:
    binary = os.environ.get("ROBIE_SYSTEMCTL", "systemctl")
    try:
        proc = subprocess.run(
            [binary, "show", "robie-task-intake.service", "-p", prop, "--no-pager"],
            check=False, capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout or ""


def _systemctl_environment() -> str:
    return _systemctl_show("Environment")


def check_task_intake(
    *,
    now: datetime | None = None,
    db_path: str | None = None,
    heartbeats: list[dict] | None = None,
    fresh_limit: int | None = None,
    stall_limit: int | None = None,
    digest_limit: int | None = None,
    episode: dict | None = None,
    dropin_dir: str | None = None,
    driver_reader=None,
    effective_environment: str | None = None,
    environment_files: str | None = None,
) -> list[str]:
    """Problems to alert on. Empty means quiet.

    Outside weekdays 9:00–6:00 PM ET this returns no problems, except a
    leftover dry-run drop-in while live calls are enabled. A newest-row
    stall pages once per episode and stays quiet until that row is fresh
    again. It does not page before 10:30 AM ET. A driver lease that is
    not held by PRODUCTION pages once per outage, then one recovery line
    when the lease is back. An EnvironmentFile that sets the write scope
    or the playground flag is red, because that file overrides Environment=.
    """
    moment = as_eastern_clock(now or datetime.now(timezone.utc))
    dropin_problem = _dry_run_while_live(dropin_dir)
    if not in_business_hours(moment):
        return [dropin_problem] if dropin_problem else []

    def _with_runtime(problems: list[str]) -> list[str]:
        lease = _driver_lease_line(episode, reader=driver_reader)
        if lease:
            problems.append(lease)
        scope = _effective_scope_problem(effective_environment, environment_files)
        if scope:
            problems.append(scope)
        return problems

    fresh = fresh_minutes() if fresh_limit is None else fresh_limit
    stall = stall_minutes() if stall_limit is None else stall_limit
    repeats = same_digest_limit() if digest_limit is None else digest_limit
    path = db_path or default_db_path()
    persist = episode is None and heartbeats is None
    if episode is None:
        episode = _load_episode(path) if persist else {}
    rows = heartbeats
    if rows is None:
        rows = [dict(row) for row in _load_heartbeats(path, max(repeats, 20))]

    problems: list[str] = []
    if not rows:
        problems.append(
            f"the task intake has not recorded a run in the last {fresh} minutes"
        )
        if dropin_problem:
            problems.append(dropin_problem)
        problems = _with_runtime(problems)
        if persist:
            _save_episode(path, episode)
        return problems

    latest = rows[0]
    status = str(latest.get("status") or "")
    ran_at = _parse_ts(str(latest.get("created_at") or ""))
    age = (moment - as_eastern_clock(ran_at)) if ran_at else None
    if status == "no_email":
        problems.append("the Task Check-In email is missing")
    if status not in {"ok", "no_email"}:
        detail = str(latest.get("error") or status or "non-zero")[:160]
        problems.append(f"the last intake exited non-zero ({detail})")
    if age is None or age > timedelta(minutes=fresh):
        problems.append(
            f"the task intake has not succeeded in the last {fresh} minutes"
        )

    newest_raw = str(latest.get("newest_created_et") or "")
    newest = report_created_et(newest_raw) if newest_raw else None
    stalled = newest is not None and age_minutes(newest, moment) > stall
    quiet_morning = (moment.hour, moment.minute) < (
        STALL_QUIET_UNTIL.hour, STALL_QUIET_UNTIL.minute,
    )
    if stalled and not quiet_morning:
        if not episode.get("stall_open"):
            problems.append(
                f"the newest Created Date has not moved for {stall} minutes "
                f"(latest task {newest.strftime('%Y-%m-%d %H:%M %Z')})"
            )
            episode["stall_open"] = True
    elif (
        not stalled
        and episode.get("stall_open")
        and not quiet_morning
        and newest is not None
    ):
        episode["stall_open"] = False
        problems.append(RECOVERY_LINE)

    error = str(latest.get("error") or "")
    if error.lower().startswith("batch cap"):
        problems.append(error[:240])

    samples = _distinct_reports(rows)
    digests = [
        str(row.get("digest") or "") for row in samples if str(row.get("digest") or "")
    ]
    if repeats > 1 and len(digests) >= repeats and len(set(digests[:repeats])) == 1:
        problems.append(
            f"{repeats} consecutive Task Check-In reports hashed the same"
        )
    if dropin_problem:
        problems.append(dropin_problem)
    problems = _with_runtime(problems)
    if persist:
        _save_episode(path, episode)
    return problems


def format_alert(problems: list[str]) -> str:
    lines = [
        "Heads up — Robie's Task Check-In intake needs attention:",
        "",
    ]
    lines.extend(f"- {problem}" for problem in problems)
    lines += [
        "",
        "This alert did not dial anyone and did not change EZLynx.",
    ]
    return "\n".join(lines)


def alert(problems: list[str]) -> None:
    space = _health_space()
    if not space:
        raise RuntimeError("no health Chat space configured (ROBIE_HEALTH_CHAT_SPACE)")
    post_as_chat_app(space, format_alert(problems))
    logger.warning("Alert posted to %s: %s problems", space, len(problems))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Task Check-In intake health")
    parser.add_argument(
        "--simulate-failure", action="store_true",
        help="Report a failure even when the intake is healthy",
    )
    parser.add_argument(
        "--no-chat", action="store_true",
        help="Print a failure and do not post to Chat",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.simulate_failure:
        problems = ["simulated task-intake health failure"]
    else:
        try:
            problems = check_task_intake()
        except Exception as exc:  # noqa: BLE001 — a broken probe is a failure
            problems = [f"the task intake health check crashed: {type(exc).__name__}"]

    if not problems:
        logger.info("Task intake healthy — quiet.")
        return 0

    text = format_alert(problems)
    print(text)
    if args.no_chat:
        return 2
    try:
        alert(problems)
    except Exception as exc:  # noqa: BLE001
        logger.error("Alert post failed: %s", type(exc).__name__)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
