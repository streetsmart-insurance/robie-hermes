#!/usr/bin/env python3
"""Prove the direct EZLynx Task API with one task on Buster Brown.

Default is a dry run: it prints the TaskCreationNote payload and makes no
token request and no EZLynx call. ``--live`` creates one task on the test
applicant (Buster Brown, 26356199) assigned to SSRobie, reads it back, and
prints only ids and fields. It never calls Zapier, so a pass means the
direct path itself worked.

Run on the Test host with a person watching the first Buster Brown write:

  ROBIE_EZLYNX_DISCUSSION_API=live \\
  ROBIE_EZLYNX_WRITE_APPLICANT_IDS=26356199 \\
  PYTHONPATH=. python3 scripts/prove_ezlynx_direct_task.py \\
      --act-as-username <EZLynx agency login> --live

Without --discussion-title the task goes on Buster Brown's ``Tasks by
Robie`` discussion, which is created (and read back) if it is missing.

Exit 0 only when the task note was read back as a TaskCreationNote with
assignedUserId 438318 (SSRobie).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from robie_job_engine import ezlynx_task_api
from robie_job_engine.ezlynx_user_ids import ezlynx_user_id_for

BUSTER_BROWN_APPLICANT_ID = "26356199"
ASSIGNEE = "SSRobie"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--act-as-username", required=True,
                        help="EZLynx agency login the vendor grant acts as (not ssr_user*)")
    parser.add_argument("--discussion-title", default="",
                        help="existing Buster Brown discussion; default is Tasks by Robie")
    parser.add_argument("--live", action="store_true",
                        help="create and read back one real task")
    args = parser.parse_args(argv)

    os.environ[ezlynx_task_api.ACT_AS_ENV] = args.act_as_username.strip()
    os.environ.setdefault(ezlynx_task_api.DIRECT_TASK_API_ENV, "1")
    assignee_id = ezlynx_user_id_for(ASSIGNEE)
    stamp = datetime.now(ZoneInfo("America/New_York"))
    title = f"[ROBIE TEST] Direct Task API proof {stamp:%Y-%m-%d %H:%M} ET"
    description = (
        "Test task from the direct EZLynx Task API proof. "
        "Assigned to SSRobie. Safe to close."
    )
    print(f"applicant_id={BUSTER_BROWN_APPLICANT_ID} (Buster Brown)")
    print(f"assignee={ASSIGNEE} assignedUserId={assignee_id}")
    print(f"act_as_username={args.act_as_username.strip()}")
    print("auth: grant_type=vendor_data_access, password not sent")
    print(f"discussion={args.discussion_title.strip() or 'Tasks by Robie (created if missing)'}")

    result = ezlynx_task_api.create_task(
        applicant_id=BUSTER_BROWN_APPLICANT_ID,
        title=title,
        description=description,
        assignee=ASSIGNEE,
        due_date=stamp.date().isoformat(),
        discussion_title=args.discussion_title,
        dry_run=not args.live,
    )
    safe = {k: v for k, v in result.items() if k != "payload"}
    if "payload" in result:
        print("payload:")
        print(json.dumps(result["payload"], indent=2))
    print("result:")
    print(json.dumps(safe, indent=2, default=str))
    if not args.live:
        return 0 if result.get("status") == ezlynx_task_api.DRY_RUN else 1
    if result.get("status") != ezlynx_task_api.CREATED:
        print(f"PROOF FAILED status={result.get('status')} reason={result.get('reason')}")
        return 1
    if str(result.get("assigned_user_id")) != str(assignee_id):
        print("PROOF FAILED assignee mismatch")
        return 1
    print(
        f"PROOF PASSED note_id={result.get('note_id')} task_id={result.get('task_id') or '(not returned)'} "
        f"discussion_id={result.get('discussion_id')} "
        f"discussion_created={bool(result.get('discussion_created'))}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
