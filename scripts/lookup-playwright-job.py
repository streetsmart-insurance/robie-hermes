#!/usr/bin/env python3
"""Read-only Playwright/EZLynx job lookup for operators and the next LLM.

Prints playwright_exec rows, CDP tab snapshots (url+title only), and the
PR 57 trace zip path. No cookies, secrets, or passwords. Does not bind a
port, deploy, or authorize COMPLETE.

    PYTHONPATH=. python3 scripts/lookup-playwright-job.py <job-id>
    PYTHONPATH=. python3 -m robie_job_engine.playwright_observability <job-id>

For the full jobs.db + destination dump (not Playwright-only):
    PYTHONPATH=. python3 scripts/job_debug_dump.py <job-id>
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from robie_job_engine.playwright_observability import main


if __name__ == "__main__":
    raise SystemExit(main())
