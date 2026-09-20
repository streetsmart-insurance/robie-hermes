#!/usr/bin/env python3
"""Read-only destination-of-truth dump for one Job Engine job.

Paste a job UUID or unique prefix. Prints the jobs.db row, related
durable_work_items, Playwright / gateway evidence pointers, and
destination ids (note/doc/discussion/applicant). No writes, retries, or kills.

    PYTHONPATH=. python3 scripts/job_debug_dump.py <job-id>
    PYTHONPATH=. python3 scripts/job_debug_dump.py <job-id> --db /opt/streetsmart-hermes/robie-job-engine/data/jobs.db
    PYTHONPATH=. python3 -m robie_job_engine.job_debug_dump <job-id>
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from robie_job_engine.job_debug_dump import main


if __name__ == "__main__":
    raise SystemExit(main())
