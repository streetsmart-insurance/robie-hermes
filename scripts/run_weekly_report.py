#!/usr/bin/env python3
"""Build a weekly report from explicitly supplied evidence exports."""

import sys

from robie_job_engine.accountability_cli import main


if __name__ == "__main__":
    raise SystemExit(main(["weekly", *sys.argv[1:]]))
