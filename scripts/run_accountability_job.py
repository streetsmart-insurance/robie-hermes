#!/usr/bin/env python3
"""Run one accountability occurrence through the durable worker and verifier."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from robie_job_engine.business_calendar import previous_business_day
from robie_job_engine.store import JobStore
from robie_job_engine.test_runtime import build_runtime_engine


ACTION = {
    "daily": "accountability.daily",
    "weekly": "accountability.weekly",
    "monthly": "accountability.monthly",
}


def occurrence_key(mode: str, *, now: datetime, holiday_calendar: str | None) -> str:
    eastern = now.astimezone(ZoneInfo("America/New_York"))
    if mode == "daily":
        period = previous_business_day(
            eastern.date(), holiday_calendar=holiday_calendar
        ).isoformat()
    elif mode == "weekly":
        period = eastern.date().isoformat()
    else:
        period = eastern.strftime("%Y-%m")
    return f"accountability:{mode}:{period}"


def run(
    *, mode: str, manifest_path: str, db_path: str, now: datetime | None = None
) -> dict[str, object]:
    manifest = Path(manifest_path).expanduser().resolve()
    if not manifest.is_file():
        raise FileNotFoundError(f"accountability manifest not found: {manifest}")
    config = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("accountability manifest must be a JSON object")
    environment = str((config.get("safety") or {}).get("environment") or "").casefold()
    if environment != "production":
        raise ValueError("server accountability runner requires safety.environment=Production")
    at = now or datetime.now(timezone.utc)
    holiday_calendar = (config.get("rules") or {}).get("holiday_calendar")
    key = occurrence_key(mode, now=at, holiday_calendar=holiday_calendar)
    store = JobStore(db_path)
    job = store.create_job(
        ACTION[mode],
        {
            "worker": "accountability-report",
            "task_name": f"StreetSmart {mode} accountability report",
            "manifest_path": str(manifest),
            "read_only": True,
            "reporting_period": "previous_business_day" if mode == "daily" else mode,
            "perform_timeout_seconds": 2400,
        },
        idempotency_key=key,
    )
    engine = build_runtime_engine(store, enforce_recording_policy=False)
    observed = engine.run(str(job["id"]))
    status = str(observed.get("status") or "")
    result = {
        "job_id": observed.get("id"),
        "idempotency_key": key,
        "mode": mode,
        "status": status,
        "reporting_period": key.rsplit(":", 1)[-1],
        "verified": status == "COMPLETE",
        "error": observed.get("error"),
    }
    if status != "COMPLETE":
        raise RuntimeError(json.dumps(result, sort_keys=True))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a verified StreetSmart accountability job")
    parser.add_argument("mode", choices=tuple(ACTION))
    parser.add_argument(
        "--manifest",
        default=os.environ.get(
            "ROBIE_ACCOUNTABILITY_MANIFEST",
            "/opt/streetsmart-hermes/accountability/connection-manifest.json",
        ),
    )
    parser.add_argument(
        "--db",
        default=os.environ.get(
            "ROBIE_JOB_DB", "/opt/streetsmart-hermes/robie-job-engine/data/jobs.db"
        ),
    )
    args = parser.parse_args()
    try:
        result = run(mode=args.mode, manifest_path=args.manifest, db_path=args.db)
    except Exception as exc:
        print(json.dumps({"verified": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
