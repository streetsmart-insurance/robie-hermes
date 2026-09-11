#!/usr/bin/env python3
"""Create and inspect one strictly read-only Manual Renewals job on Hermes Test."""

from __future__ import annotations

import json
import sys
from pathlib import Path

TEST_ROOT = Path("/opt/streetsmart-hermes-test/robie-job-engine")
TEST_DB = TEST_ROOT / "data/jobs.db"
sys.path.insert(0, str(TEST_ROOT))

from robie_job_engine.store import JobStore  # noqa: E402


def create(run_id: str) -> None:
    store = JobStore(str(TEST_DB))
    job = store.create_job(
        action_type="manual_renewal_verification",
        payload={
            "worker": "manual-renewal",
            "report_id": "4247",
            "window_min_days": 30,
            "window_max_days": 45,
            "dry_run": True,
            "voice_enabled": False,
            "authorized_actions": ["read_report_rows", "check_cancellation"],
            "test_rehearsal": True,
        },
        idempotency_key=f"rh-023-test-readonly-{run_id}",
    )
    print(job["id"])


def status(job_id: str) -> None:
    job = JobStore(str(TEST_DB)).get_job(job_id)
    print((job or {}).get("status", "missing"))


def summary(job_id: str) -> None:
    job = JobStore(str(TEST_DB)).get_job(job_id) or {}
    result = job.get("result") or {}
    detail = result.get("detail") or {}
    safe = {
        "environment": "Test",
        "host": "hermes-test-01",
        "job_id": job_id,
        "job_type": job.get("action_type"),
        "status": job.get("status", "missing"),
        "dry_run": True,
        "authorized_actions": ["read_report_rows", "check_cancellation"],
        "rows_fetched": detail.get("rows_fetched"),
        "rows_in_window": detail.get("rows_in_window"),
        "rows_filtered": detail.get("rows_filtered"),
        "counts": detail.get("counts"),
        "error": job.get("error"),
        "customer_record_changed": False,
        "email_sent": False,
        "voice_call_placed": False,
        "production_touched": False,
    }
    print(json.dumps(safe, indent=2, sort_keys=True))


if __name__ == "__main__":
    command, value = sys.argv[1:3]
    {"create": create, "status": status, "summary": summary}[command](value)
