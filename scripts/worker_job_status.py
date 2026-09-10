#!/usr/bin/env python3
"""Report the status (and final record) of a job on the Production job DB.

Usage: worker_job_status.py <job_id>
Prints "<status>" on the first line, then the full job record as JSON.
"""
import json
import sys

sys.path.insert(0, "/opt/streetsmart-hermes/robie-job-engine")

from robie_job_engine.store import JobStore  # noqa: E402

DB = "/opt/streetsmart-hermes/robie-job-engine/data/jobs.db"


def main() -> None:
    job_id = sys.argv[1]
    store = JobStore(DB)
    job = store.get_job(job_id)
    if not job:
        print("missing")
        return
    print(job.get("status", "unknown"))
    print(json.dumps(job, indent=2, default=str))


if __name__ == "__main__":
    main()
