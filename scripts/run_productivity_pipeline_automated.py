#!/usr/bin/env python3
"""Compatibility entry point for evidence-backed accountability reports."""

from __future__ import annotations

import sys

from robie_job_engine.accountability_cli import main


def normalized_arguments(arguments: list[str]) -> list[str]:
    if arguments and arguments[0] in {"daily", "weekly", "monthly"}:
        return arguments
    if "--mode" in arguments:
        index = arguments.index("--mode")
        if index + 1 >= len(arguments):
            raise SystemExit("--mode requires daily, weekly, or monthly")
        return [arguments[index + 1], *arguments[:index], *arguments[index + 2:]]
    for index, argument in enumerate(arguments):
        if argument.startswith("--mode="):
            return [argument.split("=", 1)[1], *arguments[:index], *arguments[index + 1:]]
    return ["daily", *arguments]


if __name__ == "__main__":
    raise SystemExit(main(normalized_arguments(sys.argv[1:])))
