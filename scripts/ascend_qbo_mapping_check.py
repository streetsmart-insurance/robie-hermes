#!/usr/bin/env python3
"""Read-only check of the QBO ids the Ascend deposit mapping uses.

Prints, for the configured deposit account, income account and payee:
id, type, active, name. GET only. Exit 0 when all three are valid.

  PYTHONPATH=. python3 scripts/ascend_qbo_mapping_check.py
"""

import json
import sys

from robie_job_engine.ascend_destination_mapping import verify_qbo_mapping
from robie_job_engine.quickbooks_api import QuickBooksApiClient


def main() -> int:
    qb = QuickBooksApiClient()
    if not qb.is_configured:
        print("QuickBooks is not configured on this host.")
        return 2
    findings = verify_qbo_mapping(qb)
    findings["company"] = "production" if qb.config.is_production else "sandbox"
    print(json.dumps(findings, indent=2, default=str))
    return 0 if findings["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
