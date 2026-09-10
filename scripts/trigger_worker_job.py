#!/usr/bin/env python3
"""Create a single verification worker job on the Production job DB.

Usage: trigger_worker_job.py <action_type> <payload_json> <idempotency_key>
Prints the created job id to stdout.
"""
import json
import sys

sys.path.insert(0, "/opt/streetsmart-hermes/robie-job-engine")

from robie_job_engine.store import JobStore  # noqa: E402

DB = "/opt/streetsmart-hermes/robie-job-engine/data/jobs.db"


def main() -> None:
    action_type = sys.argv[1]
    payload = json.loads(sys.argv[2])
    idempotency_key = sys.argv[3]
    store = JobStore(DB)
    job = store.create_job(
        action_type=action_type,
        payload=payload,
        idempotency_key=idempotency_key,
    )
    print(job["id"])


if __name__ == "__main__":
    main()
