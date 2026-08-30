#!/usr/bin/env python3
"""Build a daily report from explicitly supplied evidence exports."""

import sys

from robie_job_engine.accountability_cli import main


if __name__ == "__main__":
    raise SystemExit(main(["daily", *sys.argv[1:]]))
