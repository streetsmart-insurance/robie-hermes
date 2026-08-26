"""Production release remains FAIL until an independent reviewer says otherwise."""

from __future__ import annotations

PRODUCTION_RELEASE_DECISION = "FAIL"
INDEPENDENT_REVIEWER_REQUIRED = True


def production_release_decision(*, independent_reviewer_pass: bool = False) -> str:
    """This tree must not self-certify Production.

    An independent reviewer decision is out of scope for this repository
    change. The in-repo gate therefore stays FAIL even if a caller claims
    a pass.
    """
    del independent_reviewer_pass
    return PRODUCTION_RELEASE_DECISION
