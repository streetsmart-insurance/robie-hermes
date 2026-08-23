from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone

from .context_policy import JobContextManager
from .operations import OperationsStore
from .store import JobStore


def run_once(db_path: str) -> dict[str, int]:
    jobs = JobStore(db_path)
    ops = OperationsStore(db_path)
    woke = jobs.wake_due()
    expired_contexts = JobContextManager(
        db_path,
        inactivity_minutes=int(os.environ.get("ROBIE_DM_CONTEXT_TTL_MINUTES", "120")),
        context_char_budget=int(os.environ.get("ROBIE_CONTEXT_CHAR_BUDGET", "12000")),
    ).expire_due()
    created = 0
    for schedule in ops.claim_due_schedules():
        window = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
        bucket = int(window[-2:]) // max(1, schedule["interval_minutes"])
        key = f"schedule:{schedule['id']}:{window[:-2]}:{bucket}"
        job = jobs.create_job(schedule["action_type"], schedule["payload"], idempotency_key=key)
        ops.advance_schedule(schedule["id"], job["id"])
        created += 1
    synced = 0
    try:
        from .sheets_sync import sync_from_env
        synced = 1 if sync_from_env(db_path) is not None else 0
    except Exception as exc:
        # Scheduling remains durable if the dashboard is temporarily unavailable.
        print(json.dumps({"dashboard_sync_error": f"{type(exc).__name__}: {exc}"}))
    return {
        "woke": len(woke),
        "expired_contexts": len(expired_contexts),
        "created": created,
        "dashboard_synced": synced,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="ROBIE durable scheduler tick")
    parser.add_argument("--db", default=os.environ.get(
        "ROBIE_JOB_DB", "/opt/streetsmart-hermes/robie-job-engine/data/jobs.db"))
    args = parser.parse_args()
    print(json.dumps(run_once(args.db), sort_keys=True))


if __name__ == "__main__":
    main()
