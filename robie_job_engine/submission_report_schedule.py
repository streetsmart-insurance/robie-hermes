"""Install the approved weekly overdue-submission producer-report schedule."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .chat_admin import next_cron_time
from .job_type_gate import assert_job_type_production_ready
from .operations import OperationsStore
from .overdue_submission_reports import ACTION, JOB_TYPE, RESOURCE_ID
from .runtime_env import PRODUCTION_ENV_NAMES, current_robie_env


TASK_NAME = "Weekly EZLynx overdue submission producer reports"
CRON_SPEC = "0 9 * * 1"


def install_submission_report_schedule(
    db_path: str,
    manifest_path: str,
    *,
    timezone_name: str = "America/New_York",
    now: datetime | None = None,
) -> dict[str, Any]:
    if current_robie_env() in PRODUCTION_ENV_NAMES:
        assert_job_type_production_ready(JOB_TYPE)
    manifest = Path(manifest_path).expanduser().resolve()
    if not manifest.is_file():
        raise FileNotFoundError(f"accountability manifest not found: {manifest}")
    parsed = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict) or not dict(parsed.get("google_sheets") or {}).get("enabled"):
        raise ValueError("schedule requires the approved active-employee Google Sheets roster")
    store = OperationsStore(
        db_path,
        artifact_root=str(Path(db_path).expanduser().resolve().parent / "artifacts"),
    )
    payload = {
        "worker": "overdue-submission-reports",
        "task_name": TASK_NAME,
        "resource_id": RESOURCE_ID,
        "manifest_path": str(manifest),
        "authorized_actions": [ACTION],
        "scope": {
            "time_frame": "All Submissions",
            "assigned_producer": "Streetsmart Insurance",
            "my_submissions": False,
            "page_size": 100,
            "status_sort": "ascending",
            "inspection_boundary": "first_closed_row_or_pager_exhausted",
        },
    }
    scheduled = store.ensure_recurring_job(
        TASK_NAME,
        JOB_TYPE,
        payload,
        CRON_SPEC,
        timezone_name,
        next_run_at=next_cron_time(CRON_SPEC, timezone_name, now=now),
        target_ref=RESOURCE_ID,
        reconcile=True,
    )
    observed = store.get_recurring_job(scheduled["id"])
    if any(
        (
            observed.get("action_type") != JOB_TYPE,
            observed.get("cron_spec") != CRON_SPEC,
            observed.get("timezone") != timezone_name,
            observed.get("parameters") != payload,
            observed.get("enabled") != 1,
        )
    ):
        raise RuntimeError("submission report schedule reread verification failed")
    return observed


def main() -> None:
    parser = argparse.ArgumentParser(description="Install weekly overdue Submission Center reports")
    parser.add_argument("--db", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--timezone", default="America/New_York")
    args = parser.parse_args()
    item = install_submission_report_schedule(args.db, args.manifest, timezone_name=args.timezone)
    print(json.dumps({
        "id": item["id"],
        "task_name": item["task_name"],
        "cron_spec": item["cron_spec"],
        "timezone": item["timezone"],
        "next_run_at": item["next_run_at"],
        "enabled": bool(item["enabled"]),
    }, indent=2))


if __name__ == "__main__":
    main()
