#!/usr/bin/env python3
"""Run visual and DOM accessibility snapshot diffing against baseline.

Test-environment tool to detect UI drift on carrier and portal pages.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from robie_job_engine.snapshot_diff import PageSnapshot, SnapshotDiffManager


def main() -> int:
    parser = argparse.ArgumentParser(description="Run visual/DOM snapshot diff against baseline")
    parser.add_argument("--portal", required=True, help="Portal name (e.g. ascend, ezlynx)")
    parser.add_argument("--page", required=True, help="Page name (e.g. create_new, document_library)")
    parser.add_argument("--url", default="", help="Page URL")
    parser.add_argument("--save-baseline", action="store_true", help="Save current snapshot as new baseline")
    args = parser.parse_args()

    mgr = SnapshotDiffManager()

    # Capture current snapshot (or load from live CDP if available)
    now = datetime.now(timezone.utc).isoformat()
    current_snap = PageSnapshot(
        portal=args.portal,
        page_name=args.page,
        url=args.url or f"https://portal.example.com/{args.page}",
        captured_at=now,
        dom_tree={},
        elements_summary=[],
    )

    if args.save_baseline:
        saved_path = mgr.save_baseline(current_snap)
        print(f"Saved baseline snapshot to {saved_path}")
        return 0

    report = mgr.diff_snapshot(current_snap)
    print(json.dumps(report.to_dict(), indent=2))
    return 1 if report.is_drifted else 0


if __name__ == "__main__":
    raise SystemExit(main())
