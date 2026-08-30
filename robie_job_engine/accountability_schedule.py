"""Install the verified daily, weekly, and monthly accountability schedules."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .chat_admin import next_cron_time
from .operations import OperationsStore


DEFAULT_SCHEDULES = (
    ("StreetSmart daily accountability report", "accountability.daily", "0 17 * * 1-5"),
    ("StreetSmart weekly accountability report", "accountability.weekly", "15 17 * * 5"),
    ("StreetSmart monthly accountability report", "accountability.monthly", "0 8 1 * *"),
)


def install_accountability_schedules(
    db_path: str,
    manifest_path: str,
    *,
    timezone_name: str = "America/New_York",
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    manifest = Path(manifest_path).expanduser().resolve()
    if not manifest.is_file():
        raise FileNotFoundError(f"accountability manifest not found: {manifest}")
    # Validate before mutating the schedule database.
    parsed = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict) or not parsed.get("output_dir"):
        raise ValueError("accountability manifest requires output_dir")
    ops = OperationsStore(db_path, artifact_root=str(Path(db_path).expanduser().resolve().parent / "artifacts"))
    installed: list[dict[str, Any]] = []
    for task_name, action_type, cron_spec in DEFAULT_SCHEDULES:
        payload = {
            "worker": "accountability-report",
            "task_name": task_name,
            "manifest_path": str(manifest),
            "read_only": True,
        }
        expected_next = next_cron_time(cron_spec, timezone_name, now=now)
        item = ops.ensure_recurring_job(
            task_name,
            action_type,
            payload,
            cron_spec,
            timezone_name,
            next_run_at=expected_next,
            target_ref=str(manifest),
            reconcile=True,
        )
        observed = ops.get_recurring_job(item["id"])
        if any(
            (
                observed.get("action_type") != action_type,
                observed.get("cron_spec") != cron_spec,
                observed.get("timezone") != timezone_name,
                observed.get("parameters") != payload,
                observed.get("enabled") != 1,
            )
        ):
            raise RuntimeError(f"schedule reread verification failed: {task_name}")
        installed.append(observed)
    return installed


def main() -> None:
    parser = argparse.ArgumentParser(description="Install StreetSmart accountability schedules")
    parser.add_argument("--db", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--timezone", default="America/New_York")
    args = parser.parse_args()
    installed = install_accountability_schedules(args.db, args.manifest, timezone_name=args.timezone)
    print(json.dumps([
        {
            "id": item["id"],
            "task_name": item["task_name"],
            "cron_spec": item["cron_spec"],
            "timezone": item["timezone"],
            "next_run_at": item["next_run_at"],
            "enabled": bool(item["enabled"]),
        }
        for item in installed
    ], indent=2))


if __name__ == "__main__":
    main()
