#!/usr/bin/env python3
"""Health check for the EZLynx task intake.

Runs on a schedule after the intake should have finished. Checks the
OUTCOME (not just that the server is up):

1. The intake recorded a successful run recently (the report lands every
   30 minutes; the check allows 75 minutes of slack).
2. No task-intake jobs are stuck in RUNNING (a crashed worker).
3. No task-intake jobs went UNVERIFIED or FAILED in the last 24 hours.

Quiet when healthy (exit 0, no Chat post). On any failure it posts a
plain-English alert to the ROBIE health Chat and exits 2.

Both-ways proof: --prove-alert deliberately fails the first check and
posts the alert, proving the alert path works end to end.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

from .ezlynx_task_jobs import ACTION_TYPE
from .models import JobStatus
from .store import JobStore

logger = logging.getLogger("ezlynx_task_intake_health")

INTAKE_FRESH_MINUTES = 75
STUCK_RUNNING_MINUTES = 120
FAILURE_WINDOW_HOURS = 24


def default_db_path() -> str:
    return os.environ.get(
        "ROBIE_JOB_DB", "/opt/streetsmart-hermes/robie-job-engine/data/jobs.db"
    )


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def check_intake(now: datetime | None = None) -> list[str]:
    """Return a list of problems (empty = healthy)."""
    now = now or datetime.now(timezone.utc)
    store = JobStore(default_db_path())
    problems: list[str] = []

    # 1. Intake recency.
    with store.connect() as conn:
        row = conn.execute(
            """SELECT message_id, created_at, status FROM ezlynx_task_intake_runs
               ORDER BY created_at DESC LIMIT 1"""
        ).fetchone()
    if row is None:
        problems.append("the task intake has never recorded a run")
    else:
        created = _parse_ts(row["created_at"])
        age = (now - created) if created else None
        if row["status"] != "ok":
            problems.append(
                f"the last intake run ({row['message_id']}) ended with status "
                f"{row['status']}"
            )
        elif age is None or age > timedelta(minutes=INTAKE_FRESH_MINUTES):
            age_str = "unknown age" if age is None else f"{int(age.total_seconds() // 60)} min ago"
            problems.append(
                f"no successful intake run in the last {INTAKE_FRESH_MINUTES} min "
                f"(last ok run {age_str}) — the 30-minute report may be missing or stuck"
            )

    # 2. Stuck RUNNING jobs (crashed worker never released the lease).
    with store.connect() as conn:
        rows = conn.execute(
            """SELECT id, payload_json, updated_at FROM jobs
               WHERE action_type=? AND status=?""",
            (ACTION_TYPE, JobStatus.RUNNING.value),
        ).fetchall()
    for row in rows:
        updated = _parse_ts(row["updated_at"])
        age = (now - updated) if updated else None
        if age is None or age > timedelta(minutes=STUCK_RUNNING_MINUTES):
            import json
            try:
                task_id = json.loads(row["payload_json"] or "{}").get("task_id", "?")
            except Exception:
                task_id = "?"
            problems.append(
                f"task {task_id} has been RUNNING for over "
                f"{STUCK_RUNNING_MINUTES} min (job {row['id'][:8]}) — the worker may have crashed"
            )

    # 3. Recent UNVERIFIED / FAILED jobs.
    cutoff = (now - timedelta(hours=FAILURE_WINDOW_HOURS)).isoformat()
    with store.connect() as conn:
        rows = conn.execute(
            """SELECT id, status, last_error, payload_json, updated_at FROM jobs
               WHERE action_type=? AND status IN (?,?) AND updated_at >= ?""",
            (ACTION_TYPE, JobStatus.UNVERIFIED.value, JobStatus.FAILED.value, cutoff),
        ).fetchall()
    for row in rows:
        import json
        try:
            task_id = json.loads(row["payload_json"] or "{}").get("task_id", "?")
        except Exception:
            task_id = "?"
        err = (row["last_error"] or "")[:160]
        problems.append(
            f"task {task_id} is {row['status']} (job {row['id'][:8]}): {err}"
        )

    return problems


def _health_space() -> str:
    space = os.environ.get("TASK_INTAKE_HEALTH_CHAT_SPACE", "").strip()
    if space:
        return space
    from .chat_app_post import robie_home_space
    return robie_home_space() or ""


def alert(problems: list[str]) -> None:
    """Post a plain-English alert to the ROBIE health Chat."""
    from .chat_app_post import post_as_chat_app

    space = _health_space()
    if not space:
        raise RuntimeError("no health Chat space configured (TASK_INTAKE_HEALTH_CHAT_SPACE)")

    lines = [
        "Heads up — Roby's EZLynx task intake needs attention:",
        "",
    ]
    lines.extend(f"- {p}" for p in problems)
    lines += [
        "",
        "The intake checks the Robie AI task report every 30 minutes. "
        "Nothing was changed in EZLynx by this alert.",
    ]
    post_as_chat_app(space, "\n".join(lines))
    logger.warning(f"Alert posted to {space}: {len(problems)} problems")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="EZLynx task intake health check")
    parser.add_argument(
        "--prove-alert", action="store_true",
        help="Deliberately fail and post the alert (both-ways proof)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.prove_alert:
        problems = ["PROOF TEST — this is a deliberate alert to verify the health check posts correctly."]
        try:
            alert(problems)
        except Exception as e:  # noqa: BLE001
            print(f"PROVE-ALERT FAILED: {e}")
            return 2
        print("PROVE-ALERT POSTED — confirm it arrived in the health Chat, then ignore it.")
        return 0

    try:
        problems = check_intake()
    except Exception as e:  # noqa: BLE001 — a broken check is itself a failure
        problems = [f"the health check itself crashed: {e}"]

    if not problems:
        logger.info("Intake healthy — quiet.")
        return 0

    try:
        alert(problems)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Alert post failed: {e}")
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
