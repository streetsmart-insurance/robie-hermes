"""Install meeting-synthesis, staff-fun, and holiday-alert schedules."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .chat_admin import next_cron_time
from .meeting_synthesis import ACTION_TYPE as SYNTH_ACTION
from .meeting_synthesis import TASK_NAME as SYNTH_TASK
from .operations import OperationsStore
from .request_routing import WORKER_FOR_ACTION
from .staff_fun import ACTION_TYPE as FUN_ACTION
from .staff_fun import TASK_NAME as FUN_TASK
from .staff_holiday_alert import ACTION_TYPE as HOLIDAY_ACTION
from .staff_holiday_alert import TASK_NAME as HOLIDAY_TASK


DEFAULT_SCHEDULES = (
    # Mondays ~8:00 AM America/New_York
    (SYNTH_TASK, SYNTH_ACTION, "0 8 * * 1"),
    # 1st of each month ~9:00 AM America/New_York
    (FUN_TASK, FUN_ACTION, "0 9 1 * *"),
    # Weekday mornings ~9:00 AM America/New_York
    (HOLIDAY_TASK, HOLIDAY_ACTION, "0 9 * * 1-5"),
)


def install_staff_jobs_schedules(
    db_path: str,
    *,
    timezone_name: str = "America/New_York",
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    ops = OperationsStore(db_path)
    installed: list[dict[str, Any]] = []
    for task_name, action_type, cron_spec in DEFAULT_SCHEDULES:
        payload = {"worker": WORKER_FOR_ACTION[action_type]}
        expected_next = next_cron_time(cron_spec, timezone_name, now=now)
        item = ops.ensure_recurring_job(
            task_name,
            action_type,
            payload,
            cron_spec,
            timezone_name,
            next_run_at=expected_next,
            target_ref="staff-automation",
            reconcile=True,
        )
        observed = ops.get_recurring_job(item["id"])
        installed.append(
            {
                "task_name": task_name,
                "action_type": action_type,
                "cron_spec": cron_spec,
                "next_run_at": observed["next_run_at"],
            }
        )
    return installed


def main() -> None:
    import argparse
    import json
    import os

    parser = argparse.ArgumentParser(description="Install staff automation schedules")
    parser.add_argument(
        "--db",
        default=os.environ.get(
            "ROBIE_JOB_DB", "/opt/streetsmart-hermes/robie-job-engine/data/jobs.db"
        ),
    )
    args = parser.parse_args()
    print(json.dumps(install_staff_jobs_schedules(args.db), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
