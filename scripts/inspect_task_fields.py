#!/usr/bin/env python3
"""Supervised, read-only Test inspection of the EZLynx task fields (docs/EZLYNX_TASK_FIELD_DISCOVERY.md).

Never saves. Refuses unless it is supervised and exclusive (Test VM, ROBIE_ENV=TEST, applicant 220250093,
exactly one task in ROBIE_TASK_INTAKE_ALLOWED_TASK_IDS, reassignment and live calls off, no active jobs,
a named operator who has confirmed browser ownership and exclusivity). Stops on an unexpected page or a
Cancel that cannot be verified. Not run by CI or any scheduler.
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
    parser.add_argument("--operator", required=True, help="the named person supervising this run")
    parser.add_argument("--confirm-browser-owner", action="store_true",
                        help="dom: I confirm this VM's persistent browser is the one to inspect and I own it for this run")
    parser.add_argument("--confirm-exclusive", action="store_true",
                        help="dom: I confirm nothing else (intake, jobs, people) is using the browser")
    parser.add_argument("--approved-fields-only", action="store_true",
                        help="dom: record ONLY the approved fields (no names-only list of the dialog's other controls)")
    parser.add_argument("--include-approved-values", action="store_true",
                        help="api: also record values for approved, non-credential keys (default: key names and types only)")
    args = parser.parse_args(argv)
    try:
        if args.mode == "dom":
            inspector.run_dom_inspection(
                task_id=args.task_id, applicant_id=args.applicant_id, output_path=args.output,
                operator=args.operator, confirm_browser_owner=args.confirm_browser_owner,
                confirm_exclusive=args.confirm_exclusive, dialog_inventory=not args.approved_fields_only)
        else:
            if not args.discussion_id:
                parser.error("--discussion-id is required for api")
            from robie_job_engine.ezlynx_task_intake import _build_discussion_client

            inspector.run_api_inspection(
                client=_build_discussion_client(), task_id=args.task_id, applicant_id=args.applicant_id,
                discussion_id=args.discussion_id, output_path=args.output, operator=args.operator,
                include_approved_values=args.include_approved_values)
    except inspector.InspectionRefused as exc:
        print(f"REFUSED (nothing was opened): {exc}")
        return 2
    except inspector.CancelFailed as exc:
        print(f"STOPPED, CANCEL NOT VERIFIED: {exc}\nClose the Edit Task dialog BY HAND WITHOUT SAVING, then report.")
        return 4
    except inspector.InspectionAborted as exc:
        print(f"STOPPED: {exc}")
        return 3
    print(f"Observation written to {args.output}; meaning review is PENDING a person.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
