#!/usr/bin/env python3
"""Operator entry point for the local/Test quote-replay harness.

Requires a real --quote-pdf path. Does not invent a PDF.
Does not claim live Test COMPLETE.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from robie_job_engine.quote_replay import main


if __name__ == "__main__":
    raise SystemExit(main())
