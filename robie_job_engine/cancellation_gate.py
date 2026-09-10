"""Real-time cancellation gate for the verification workers.

Every worker MUST call :func:`check_policy_active` before ANY outreach on a
policy. Cancelled or inactive policies are never emailed, called, or uploaded
to lender portals.

Semantics ported from the proven renewal-automation-system gate (EZLynx
PolicyCard ``GetPolicies``): a policy is active only when
``policyStatusViewModelID == 1`` AND ``cancellationDate`` is null. (Raw Looker
CSVs show 1-year boundaries and hide mid-term cancels, so the live API check is
mandatory.)

Fail CLOSED: if the check cannot run for any reason — no client, no applicant
id, transport error, ambiguous match — the gate returns
``(False, "cancellation check unavailable")`` (or a more specific reason) so
callers mark the policy ``not_done`` and never reach out.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

logger = logging.getLogger("robie.cancellation_gate")

CHECK_UNAVAILABLE = "cancellation check unavailable"


class EzlynxPolicyCardPort(Protocol):
    """Minimal port the gate needs from the EZLynx client.

    ``get_policies`` performs the real-time PolicyCard lookup
    (``GET /PolicyAPI/v1/PolicyCard/GetPolicies?applicantId=``) and returns the
    raw policy dicts, each carrying at least ``policyStatusViewModelID``,
    ``cancellationDate``, and ``policyNumber``. The concrete client is wired by
    the worker agents; this module never guesses policy state.
    """

    def get_policies(self, applicant_id: str) -> list[dict[str, Any]]: ...


def _is_active_status(status: Any) -> bool:
    try:
        return int(str(status).strip()) == 1
    except (TypeError, ValueError):
        return False


def _cancellation_date(raw: Any) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def check_policy_active(
    *,
    applicant_id: str | None = None,
    policy_number: str | None = None,
    ezlynx_client: EzlynxPolicyCardPort | None = None,
) -> tuple[bool, str]:
    """Return ``(is_active, reason)`` for a policy, fail closed.

    - ``(True, "active")`` — safe to proceed with outreach.
    - ``(False, reason)`` — do NOT reach out; record the policy ``not_done``
      (or ``pending`` with the reason) and move on.
    """
    if ezlynx_client is None:
        logger.warning("cancellation gate: no EZLynx client; failing closed")
        return False, CHECK_UNAVAILABLE
    applicant = str(applicant_id or "").strip()
    if not applicant:
        logger.warning("cancellation gate: no applicant_id; failing closed")
        return False, CHECK_UNAVAILABLE
    try:
        policies = ezlynx_client.get_policies(applicant)
    except Exception as exc:
        logger.warning(
            "cancellation gate: policy lookup failed (%s); failing closed",
            type(exc).__name__,
        )
        return False, CHECK_UNAVAILABLE
    if not policies:
        return False, "cancellation check unavailable: no policies returned"

    target: dict[str, Any] | None = None
    wanted = str(policy_number or "").strip().casefold()
    if wanted:
        for policy in policies:
            number = str((policy or {}).get("policyNumber") or "").strip().casefold()
            if number and number == wanted:
                target = policy
                break
        if target is None:
            return False, "cancellation check unavailable: policy not found"
    else:
        if len(policies) != 1:
            return (
                False,
                "cancellation check unavailable: ambiguous without policy_number",
            )
        target = policies[0]

    status = (target or {}).get("policyStatusViewModelID")
    cancel_date = _cancellation_date((target or {}).get("cancellationDate"))
    if _is_active_status(status) and cancel_date is None:
        return True, "active"
    return (
        False,
        f"inactive/cancelled: policyStatusViewModelID={status} "
        f"cancellationDate={cancel_date}",
    )
