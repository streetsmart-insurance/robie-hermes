#!/usr/bin/env python3
"""Operator entry for the Ascend locator + artifact audit.

CI: fixture punch list + artifact-path assertions. No live Ascend.
Live: hermes-test-01 only (ROBIE_ENV=TEST). Does not bind, email, merge,
or deploy. Does not overwrite Loom ascend-finance.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from robie_job_engine.ascend_locator_audit import main


if __name__ == "__main__":
    raise SystemExit(main())
