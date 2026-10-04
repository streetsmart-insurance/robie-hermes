#!/usr/bin/env python3
"""Print the Ascend driver stall verdict from the counts file.

Exit 0 when quiet (including fewer than 4 live runs or a missing log).
Exit 2 when the last 4 completed live runs each saw actionable notices
and filed nothing. Exit 1 when the log exists but cannot be read.

This does not post to Chat. The hourly health check
(``scripts/robie_health_check.py``) runs the same check and alerts.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from robie_job_engine.ascend_driver_stall import (  # noqa: E402
    STALL_LIVE_RUNS,
    evaluate_stall_file,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=STALL_LIVE_RUNS)
    parser.add_argument(
        "--log",
        default=None,
        help="Run log path. Default is ASCEND_DRIVER_RUN_LOG or the state dir.",
    )
    args = parser.parse_args(argv)
    verdict = evaluate_stall_file(args.log, need=args.runs)
    print(json.dumps(verdict, indent=2, sort_keys=True))
    status = str(verdict.get("status") or "")
    if status == "ALERT" and str(verdict.get("detail") or "").startswith(
        "ascend driver run log unreadable"
    ):
        return 1
    if status == "ALERT":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
