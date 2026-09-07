#!/usr/bin/env python3
"""One connected CDP job for a manual renewal shell.

Replaces the SSH/SCP micro-script loop. Same entrypoint as
``python3 -m src.ezlynx.policy_renewer``.

Verify on hermes-test-01 (``--env test``) before any Production zip.

    PYTHONPATH=. python3 scripts/run_manual_renewal.py --dry-run \\
        --applicant-id 21588091 --policy-id 123 --policy-number PWC1239278 \\
        --lob "Workers comp" --carrier "Associated Specialty Insurance" \\
        --premium 1200 --effective-date 2026-10-15 --expiration-date 2027-10-15 \\
        --writing-company "Associated Specialty" \\
        --discussion-title "Renewal Manual Workers comp | PWC1239278 Associated Specialty Insurance" \\
        --proof-json /tmp/yes-we-do.proof.json
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.ezlynx.policy_renewer import main


if __name__ == "__main__":
    raise SystemExit(main())
