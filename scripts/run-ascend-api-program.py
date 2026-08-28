#!/usr/bin/env python3
"""Plan or execute a bounded Ascend API program creation job.

Planning is the default and performs no network request.  Execution is
limited to ROBIE_ENV=TEST; Production remains gated until a sandbox job has
been created and independently read back successfully.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from robie_job_engine.ascend_api import ACTION_TYPE, WORKER_NAME, validate_create_payload
from robie_job_engine.store import JobStore
from robie_job_engine.test_runtime import build_runtime_engine


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload", required=True, help="JSON file containing program and billables")
    parser.add_argument("--db", help="jobs.db path; required with --execute")
    parser.add_argument("--execute", action="store_true", help="create in Ascend sandbox")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    source = Path(args.payload)
    payload = json.loads(source.read_text(encoding="utf-8"))
    plan = validate_create_payload(payload)
    if not args.execute:
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    if str(os.environ.get("ROBIE_ENV") or "").strip().upper() != "TEST":
        raise SystemExit("--execute is currently restricted to ROBIE_ENV=TEST")
    if not args.db:
        raise SystemExit("--db is required with --execute")
    payload = dict(payload)
    payload.update({"execute": True, "worker": WORKER_NAME})
    store = JobStore(args.db)
    job = store.create_job(ACTION_TYPE, payload, max_attempts=1)
    final = build_runtime_engine(store).run(job["id"])
    print(json.dumps({"job_id": job["id"], "status": str(final["status"])}, indent=2))
    return 0 if str(final["status"]) == "COMPLETE" else 1


if __name__ == "__main__":
    raise SystemExit(main())

