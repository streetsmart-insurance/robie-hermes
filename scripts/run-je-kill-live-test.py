#!/usr/bin/env python3
"""Entry point for the guarded Test-only JE-KILL-01 browser proof."""

from robie_job_engine.je_kill_live import main


if __name__ == "__main__":
    raise SystemExit(main())
