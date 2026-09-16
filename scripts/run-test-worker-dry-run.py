#!/usr/bin/env python3
"""Create and inspect strictly read-only TEST worker dry-run jobs on Hermes Test.

Generalized across the four verification workers: manual-renewal, audit,
mortgagee, policy-change. Every job is a dry-run rehearsal: dry_run=true,
voice_enabled=false, authorized_actions limited to read-only rows checks, and
it targets ONLY the Test job-engine database at
/opt/streetsmart-hermes-test/robie-job-engine.

Subcommands:
  create <worker> <run_id>   create (or reuse) the dry-run job, print its id
  status <job_id>            print the job's current status
  summary <job_id>           print a redacted, read-only safety summary (JSON)
  audit <job_id>             print the post-job audit checkpoint (JSON), which
                             the Test-before-Production gate needs to count a
                             "clean" job (environment TEST + verdict PASS)

Modelled on scripts/run-test-manual-renewal-dry-run.py.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

TEST_ROOT = Path("/opt/streetsmart-hermes-test/robie-job-engine")
TEST_DB = TEST_ROOT / "data/jobs.db"
sys.path.insert(0, str(TEST_ROOT))

from robie_job_engine.store import JobStore  # noqa: E402

WORKER_SPECS = {
    "manual-renewal": {
        "action_type": "manual_renewal_verification",
        "payload": {
            "worker": "manual-renewal",
            "report_id": "4247",
            "window_min_days": 30,
            "window_max_days": 45,
            "dry_run": True,
            "voice_enabled": False,
            "authorized_actions": ["read_report_rows", "check_cancellation"],
            "test_rehearsal": True,
        },
    },
    "audit": {
        "action_type": "audit_verification",
        "payload": {
            "worker": "audit-verification",
            "report_id": "4246",
            "dry_run": True,
            "voice_enabled": False,
            "authorized_actions": ["read_report_rows"],
            "test_rehearsal": True,
        },
    },
    "mortgagee": {
        "action_type": "mortgagee_verification",
        "payload": {
            "worker": "mortgagee-verification",
            "report_id": "4372",
            "dry_run": True,
            "voice_enabled": False,
            "authorized_actions": ["read_report_rows"],
            "test_rehearsal": True,
        },
    },
    "policy-change": {
        "action_type": "policy_change_verification",
        "payload": {
            "worker": "policy-change-verification",
            "report_id": "4359",
            "dry_run": True,
            "voice_enabled": False,
            "authorized_actions": ["read_report_rows"],
            "test_rehearsal": True,
        },
    },
}


def create(worker: str, run_id: str) -> None:
    spec = WORKER_SPECS.get(worker)
    if spec is None:
        print(
            f"unknown worker {worker!r}; expected one of {sorted(WORKER_SPECS)}",
            file=sys.stderr,
        )
        sys.exit(2)
    store = JobStore(str(TEST_DB))
    job = store.create_job(
        action_type=spec["action_type"],
        payload=dict(spec["payload"]),
        idempotency_key=f"ralph-test-worker-{worker}-{run_id}",
    )
    print(job["id"])


def status(job_id: str) -> None:
    job = JobStore(str(TEST_DB)).get_job(job_id)
    print((job or {}).get("status", "missing"))


def summary(job_id: str) -> None:
    job = JobStore(str(TEST_DB)).get_job(job_id) or {}
    payload = job.get("payload") or {}
    result = job.get("result") or {}
    detail = result.get("detail") or {}
    safe = {
        "environment": "Test",
        "host": "hermes-test-01",
        "job_id": job_id,
        "worker": payload.get("worker"),
        "report_id": payload.get("report_id"),
        "action_type": job.get("action_type"),
        "status": job.get("status", "missing"),
        "dry_run": True,
        "authorized_actions": payload.get("authorized_actions"),
        "rows_fetched": detail.get("rows_fetched"),
        "rows_in_window": detail.get("rows_in_window"),
        "rows_filtered": detail.get("rows_filtered"),
        "counts": detail.get("counts"),
        "error": job.get("last_error") or job.get("error"),
        "customer_record_changed": False,
        "email_sent": False,
        "voice_call_placed": False,
        "production_touched": False,
    }
    print(json.dumps(safe, indent=2, sort_keys=True, default=str))


def audit(job_id: str) -> None:
    store = JobStore(str(TEST_DB))
    try:
        job = store.get_job(job_id) or {}
    except KeyError:
        job = {}
    data = store.get_checkpoint(job_id, "post_job_audit")
    if not data:
        print(json.dumps({"verdict": None, "present": False}, indent=2))
        return
    print(
        json.dumps(
            {
                "verdict": data.get("verdict"),
                "present": True,
                "job_status": job.get("status"),
                "action_type": job.get("action_type"),
                "audit": data,
            },
            indent=2,
            sort_keys=True,
            default=str,
        )
    )


def usage() -> None:
    print(
        "usage: run-test-worker-dry-run.py "
        "(create <worker> <run_id> | status <job_id> | summary <job_id> | audit <job_id>)",
        file=sys.stderr,
    )
    print(f"workers: {sorted(WORKER_SPECS)}", file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        usage()
        sys.exit(2)
    command = sys.argv[1]
    try:
        if command == "create":
            worker, run_id = sys.argv[2:4]
            create(worker, run_id)
        elif command == "status":
            status(sys.argv[2])
        elif command == "summary":
            summary(sys.argv[2])
        elif command == "audit":
            audit(sys.argv[2])
        else:
            usage()
            sys.exit(2)
    except IndexError:
        usage()
        sys.exit(2)
