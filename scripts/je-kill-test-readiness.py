#!/usr/bin/env python3
"""Read-only JE-KILL Stage 4 readiness report for hermes-test-01.

Prints clear blockers (fixture age, CDP AUTHENTICATED, leases, release SHA)
without mutating Jobs, Chrome, or the fixture. Exit 0 when ready; exit 2 when
blocked. Never prints secret payloads.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from robie_job_engine.je_kill_preflight import (  # noqa: E402
    job_inventory_report,
    live_preflight_errors,
    read_cdp_tabs,
    read_job_inventory,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--test-root",
        default=os.environ.get("TEST_ROOT", "/opt/streetsmart-hermes-test"),
    )
    parser.add_argument(
        "--fixture",
        default=os.environ.get(
            "FIXTURE", "/opt/streetsmart-hermes-test/je-kill/fixture.json"
        ),
    )
    parser.add_argument(
        "--job-db",
        default=os.environ.get(
            "ROBIE_JOB_DB",
            "/opt/streetsmart-hermes-test/robie-job-engine/data/jobs.db",
        ),
    )
    parser.add_argument(
        "--expected-sha",
        default=os.environ.get("EXPECTED_SHA", ""),
        help="12+ char commit SHA the Test release pointer must match",
    )
    parser.add_argument(
        "--cdp-url",
        default=os.environ.get("ROBIE_PLAYWRIGHT_CDP_URL", "http://127.0.0.1:9222"),
    )
    args = parser.parse_args()

    unleased: list[str] = []
    try:
        unleased = job_inventory_report(read_job_inventory(args.job_db))["unleased"]
    except Exception:  # noqa: BLE001 — surfaced via live_preflight when db broken
        pass

    blocking = live_preflight_errors(
        fixture_path=args.fixture,
        cdp_url=args.cdp_url,
        job_db=args.job_db,
        test_root=args.test_root if args.expected_sha else None,
        expected_sha=args.expected_sha or None,
    )
    if not args.expected_sha:
        blocking.append(
            "EXPECTED_SHA not set — refuse readiness without a release SHA "
            "(workflow always sets it)"
        )

    report = {
        "ready": not blocking,
        "test_root": args.test_root,
        "fixture": args.fixture,
        "expected_sha": args.expected_sha or None,
        "blocking": blocking,
        "unleased_ignored": unleased,
        "cdp_page_count": None,
        "cdp_urls": [],
    }
    try:
        tabs = read_cdp_tabs(args.cdp_url)
        pages = [t for t in tabs if str(t.get("type") or "") == "page"]
        report["cdp_page_count"] = len(pages)
        report["cdp_urls"] = [str(t.get("url") or "")[:160] for t in pages[:5]]
    except Exception as exc:  # noqa: BLE001
        report["cdp_snapshot_error"] = f"{type(exc).__name__}: {exc}"

    print(json.dumps(report, indent=2, sort_keys=True))
    if blocking:
        print("JE-KILL TEST READINESS: BLOCKED", file=sys.stderr)
        for item in blocking:
            print(f"- {item}", file=sys.stderr)
        return 2
    print("JE-KILL TEST READINESS: OK", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
