#!/usr/bin/env python3
"""CLI wrapper for the 4372 Test-only metadata probe.

See robie_job_engine/probe_4372_mortgagee_metadata.py and
docs/MORTGAGEE_ENRICHMENT_4372.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from robie_job_engine.probe_4372_mortgagee_metadata import main

if __name__ == "__main__":
    raise SystemExit(main())
