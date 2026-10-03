"""Partially repaired streetsmart-accountability-prod production_main.py.

Captured from the live file on 2026-09-20. The fetch is already a direct
assignment. The synthetic unverified-row fallback remains, with comments
between the condition and the loop, so the exact uncommented fallback block
does not match.
"""

from extractors.ezlynx_submission_center import (
    SubmissionCenterSourceError,
    fetch_live_submission_center,
    unverified_submission_row,
)


def build_department_data(target, sources):
    cois = get_pending_cois(target)
    submissions = fetch_live_submission_center()
    magellan = fetch_live_magellan_data(target.isoformat())
    departments = {}
    for row in submissions:
        owner = row.get("Assigned Producer") or row.get("Producer") or row.get("Assigned To") or ""
        departments[_department_for_name(owner, roster)]["submissions"].append(row)
    if submission_error:
        # Every team lead sees the unavailable server-audit section. This is an
        # explicit unknown, not a favorable zero and not a reason to discard
        # independently verified phone/queue evidence.
        for department in departments.values():
            department["submissions"].append(unverified_submission_row(submission_error))
    for source_key, destination_key in (
        ("performance", "email_performance"),
    ):
        departments[source_key] = destination_key
    return departments
