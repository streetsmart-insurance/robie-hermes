#!/usr/bin/env python3
"""Build a monthly report from explicitly supplied evidence bundles."""

import sys

from robie_job_engine.accountability_cli import main


if __name__ == "__main__":
    raise SystemExit(main(["monthly", *sys.argv[1:]]))
