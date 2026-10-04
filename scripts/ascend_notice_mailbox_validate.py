#!/usr/bin/env python3
"""Live read-only validation of one Ascend notice mailbox. Test only.

Builds the delegated Gmail client with readonly scope (modify=False), wraps
it so only messages.list/get can be called, scans a recent window into an
in-memory queue (nothing is kept), and prints counts only: pages, messages,
review/unrelated, notice families, errors. No subjects, bodies or sender
addresses are printed. No label, read-state, note or task change.

  ROBIE_ENV=TEST PYTHONPATH=. python3 scripts/ascend_notice_mailbox_validate.py \\
      --mailbox <mailbox> --delegation-service-account <service account email> --days 7

Blocked until the Gmail delegation service account exists (tracking issue TBD;
#760 is unrelated — it is "Disable Ascend notice driver schedule").
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from datetime import date, timedelta

from robie_job_engine.ascend_notice_discovery import ReviewQueue, scan
from robie_job_engine.ascend_notice_review_runner import ProposingQueue, ReadOnlyGmail


def main(argv=None, service_factory=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mailbox", required=True)
    ap.add_argument("--delegation-service-account", required=True)
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args(argv)
    if os.environ.get("ROBIE_ENV", "").upper() != "TEST":
        print("TEST_ONLY: refusing outside ROBIE_ENV=TEST")
        return 2
    if service_factory is None:
        from robie_job_engine.gmail_accountability import build_notice_gmail_service
        service_factory = lambda sa, mb: build_notice_gmail_service(sa, mb, modify=False)  # noqa: E731
    service = ReadOnlyGmail(service_factory(args.delegation_service_account, args.mailbox))
    queue = ProposingQueue(ReviewQueue(":memory:"))
    end = date.today() + timedelta(days=1)
    start = end - timedelta(days=max(args.days, 1) + 1)
    try:
        result = scan(service, args.mailbox, queue, start_date=start.isoformat(), end_date=end.isoformat())
        families = collections.Counter(r["item"].get("notice_family", "fetch_failed") for r in queue.rows())
    finally:
        queue.close()
    out = {k: result.get(k) for k in ("pages", "fetched", "review", "unrelated", "complete",
                                      "destination_writes", "gmail_label_changes")}
    out.update({"window": [start.isoformat(), end.isoformat()], "families": dict(families),
                "error_types": sorted({e.split(":")[-1] for e in result.get("errors", [])}),
                "scope": "gmail.readonly", "client_methods_allowed": ["messages.list", "messages.get"]})
    print(json.dumps(out, indent=2))
    ok = bool(result.get("complete")) and out["destination_writes"] == 0 and out["gmail_label_changes"] == 0
    print("MAILBOX VALIDATION PASSED" if ok else "MAILBOX VALIDATION FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
