#!/usr/bin/env python3
"""Run the Phase 1 document-puller pilot (read-only).

Reads a pilot JSON (see pilot/pilot-10.json), pulls each policy's
document via its carrier adapter, and writes per-policy evidence JSON
plus a summary into the run directory.

The pilot does NOT run against live portals by default: without
``--browser`` no browser_factory is wired and the run fails closed (no
browser_factory means the run is refused before any adapter code
executes). Pass ``--browser box`` (or ``sandbox``) ONLY after Carlo
approves the pilot plan — that is what lets the worker touch live
carrier portals (read-only: login + download only).

The hermetic smoke check is ``pytest tests/test_phase1_doc_pull.py
tests/test_phase1_browser.py``.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from robie_job_engine.phase1_browser import default_browser_factory
from robie_job_engine.phase1_doc_pull import load_pilot, run_pilot


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 1 read-only document-puller pilot")
    ap.add_argument("--pilot", required=True, help="pilot JSON path")
    ap.add_argument(
        "--run-dir",
        default="",
        help="run directory (default: pilot/runs/<timestamp>)",
    )
    ap.add_argument(
        "--browser",
        choices=("box", "sandbox"),
        default=None,
        help=(
            "wire the live Playwright browser port (REQUIRES Carlo's pilot "
            "approval). Without this flag the run fails closed."
        ),
    )
    args = ap.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    run_dir = (
        Path(args.run_dir)
        if args.run_dir
        else repo_root / "pilot" / "runs" / datetime.now().strftime("%Y%m%d-%H%M%S")
    )
    pilot = load_pilot(args.pilot)
    print(f"pilot: {len(pilot)} policies -> {run_dir}")
    browser_factory = default_browser_factory() if args.browser else None
    if browser_factory is not None:
        print(f"live browser wiring ENABLED (runtime from each adapter's spec)")
    else:
        print("no --browser flag: live portals disabled, run will fail closed")
    try:
        evidence = run_pilot(pilot, run_dir=run_dir, browser_factory=browser_factory)
    except RuntimeError as exc:
        print(f"REFUSED: {exc}")
        return 2
    counts: dict[str, int] = {}
    for ev in evidence:
        counts[ev.status] = counts.get(ev.status, 0) + 1
    print(json.dumps(counts, indent=2))
    print(f"evidence: {run_dir / 'evidence'}")
    bad = (
        counts.get("failed", 0)
        + counts.get("blocked", 0)
        + counts.get("needs_review", 0)
    )
    if bad:
        print(f"{bad} polic(ies) did not produce a verified document — see evidence")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
