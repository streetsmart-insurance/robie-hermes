#!/usr/bin/env python3
"""Read-only Test inspection of the EZLynx task fields (see docs/EZLYNX_TASK_FIELD_DISCOVERY.md).

Never saves. Refuses unless ROBIE_ENV=TEST, applicant 220250093, and the task is listed in
ROBIE_TASK_INTAKE_ALLOWED_TASK_IDS. Not run by CI or any scheduler.
"""
from __future__ import annotations

import argparse
import sys

from robie_job_engine import task_field_inspector as inspector


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=("dom", "api"))
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--applicant-id", required=True)
    parser.add_argument("--discussion-id", help="required for api")
    parser.add_argument("--output", required=True, help="new file; never overwritten")
    args = parser.parse_args(argv)
    try:
        if args.mode == "dom":
            inspector.run_dom_inspection(task_id=args.task_id, applicant_id=args.applicant_id,
                                         output_path=args.output)
        else:
            if not args.discussion_id:
                parser.error("--discussion-id is required for api")
            from robie_job_engine.ezlynx_task_intake import _build_discussion_client

            inspector.run_api_inspection(client=_build_discussion_client(), task_id=args.task_id,
                                         applicant_id=args.applicant_id, discussion_id=args.discussion_id,
                                         output_path=args.output)
    except inspector.InspectionRefused as exc:
        print(f"REFUSED: {exc}")
        return 2
    print(f"Observation written to {args.output}; meaning review is PENDING a person.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
