#!/usr/bin/env python3
"""Compatibility wrapper for the evidence-backed weekly report builder."""

import sys

from robie_job_engine.accountability_cli import main


if __name__ == "__main__":
    raise SystemExit(main(["weekly", *sys.argv[1:]]))
