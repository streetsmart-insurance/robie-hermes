#!/usr/bin/env python3
"""CLI: Test SSRobie CDP keep-alive on hermes-test-01."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from robie_job_engine.test_ssrobie_keepalive import main

if __name__ == "__main__":
    raise SystemExit(main())
