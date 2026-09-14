"""Triple verification before any consequential EZLynx write.

A write is permitted only when ALL of the following pass, in order:

Check 1 -- authorization: the target applicant id is on the compiled
    ``EZLYNX_WRITE_APPLICANT_IDS`` allowlist (see ``ezlynx_write_scope``).
    Proves somebody authorized this account. Says nothing about whether
    this particular job's data belongs to it.

Check 2 -- policy cross-reference: search EZLynx by the policy number the
    job was given (e.g. ``PAC00001215485``). The applicant id on the
    returned policy must equal the write target. The policy number is an
    independent identifier from the carrier/queue; the applicant id is what
    EZLynx claims owns it. If they disagree, the job is pointed at the
    wrong account.

Check 3 -- name cross-reference: the name on the applicant record must
    match the expected insured name after normalization. A second
    independent identifier, guarding against a consistently-wrong
    policy-number/applicant-id pair in the input data.

Any failure refuses the write with the evidence attached. Nothing here
performs network I/O itself: the two EZLynx reads are injected as
callables so the verifier is unit-testable and the live HTTP wiring
(API session, retry, auth) stays in one place when it is built.

Name-match rule (strict, documented): normalize both names (lowercase,
strip punctuation, collapse whitespace). Pass on exact normalized
equality; otherwise strip a trailing ``dba ...`` segment from the
*expected* name and compare again (covers "Green Lion Lawn Care LLC DBA
Lawn Buddies" vs "Green Lion Lawn Care LLC"). Anything else fails
closed -- near-misses are evidence of a wrong account, not a formatting
quirk.
"""

from __future__ import annotations

import re
from typing import Callable, Optional

from .ezlynx_write_scope import (
    applicant_is_write_allowed,
    normalize_applicant_id,
)

EZLYNX_WRITE_VERIFY_REFUSED = "EZLYNX_WRITE_VERIFY_REFUSED"

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")
_DBA_TAIL_RE = re.compile(r"\s+dba\s+.+$", re.IGNORECASE)


class EzlynxWriteVerifyError(RuntimeError):
    """Raised when any verification check fails before an EZLynx write."""


def normalize_name(value: object) -> str:
    """Lowercase, strip punctuation, collapse whitespace. Never rewrites identity."""

    return _WS_RE.sub(" ", _PUNCT_RE.sub("", str(value or "").lower())).strip()


def names_match(expected: object, actual: object) -> bool:
    """Strict name comparison with one documented exception: a trailing DBA segment."""

    want = normalize_name(expected)
    got = normalize_name(actual)
    if not want or not got:
        return False
    if want == got:
        return True
    want_no_dba = _DBA_TAIL_RE.sub("", want).strip()
    return bool(want_no_dba) and want_no_dba == got


def verify_write_target(
    applicant_id: object,
    *,
    expected_policy_number: object = None,
    expected_name: object = None,
    policy_search_fn: Optional[Callable[[str], Optional[dict]]] = None,
    applicant_fetch_fn: Optional[Callable[[str], Optional[dict]]] = None,
) -> dict:
    """Run the triple check. Returns the evidence bundle or raises.

    ``policy_search_fn(policy_number)`` must return a mapping with
    ``applicant_id`` (and ideally ``policy_number``) for the policy EZLynx
    finds, or ``None`` when the policy number is unknown to EZLynx.

    ``applicant_fetch_fn(applicant_id)`` must return a mapping with ``name``
    for the applicant record, or ``None`` when the record cannot be read.

    When ``expected_policy_number`` is not known for a write, check 2 is
    recorded as skipped and checks 1 and 3 still gate the write.
    """

    target = normalize_applicant_id(applicant_id)
    evidence: dict = {
        "applicant_id": target,
        "checks": {},
    }

    # Check 1 -- authorization.
    allowed = applicant_is_write_allowed(target)
    evidence["checks"]["allowlist"] = {
        "passed": allowed,
        "applicant_id": target,
    }
    if not allowed:
        raise EzlynxWriteVerifyError(
            f"{EZLYNX_WRITE_VERIFY_REFUSED}: check 1 (allowlist) failed -- "
            f"applicant {target or '<missing>'} is not authorized for EZLynx writes"
        )

    # Check 2 -- policy cross-reference.
    policy_number = str(expected_policy_number or "").strip()
    if not policy_number:
        evidence["checks"]["policy_cross_reference"] = {
            "passed": True,
            "skipped": True,
            "reason": "no policy number supplied for this write",
        }
    else:
        if policy_search_fn is None:
            raise EzlynxWriteVerifyError(
                f"{EZLYNX_WRITE_VERIFY_REFUSED}: check 2 (policy cross-reference) "
                "cannot run -- no policy_search_fn supplied"
            )
        found = policy_search_fn(policy_number)
        found_applicant = normalize_applicant_id((found or {}).get("applicant_id"))
        passed = bool(found) and found_applicant == target
        evidence["checks"]["policy_cross_reference"] = {
            "passed": passed,
            "expected_policy_number": policy_number,
            "policy_owner_applicant_id": found_applicant or None,
            "write_target_applicant_id": target,
        }
        if not passed:
            raise EzlynxWriteVerifyError(
                f"{EZLYNX_WRITE_VERIFY_REFUSED}: check 2 (policy cross-reference) "
                f"failed -- policy {policy_number} resolves to applicant "
                f"{found_applicant or '<not found>'}, not write target {target}"
            )

    # Check 3 -- name cross-reference.
    if applicant_fetch_fn is None:
        raise EzlynxWriteVerifyError(
            f"{EZLYNX_WRITE_VERIFY_REFUSED}: check 3 (name cross-reference) "
            "cannot run -- no applicant_fetch_fn supplied"
        )
    record = applicant_fetch_fn(target)
    actual_name = (record or {}).get("name")
    matched = names_match(expected_name, actual_name)
    evidence["checks"]["name_cross_reference"] = {
        "passed": matched,
        "expected_name": str(expected_name or ""),
        "applicant_record_name": str(actual_name or ""),
        "normalized_expected": normalize_name(expected_name),
        "normalized_actual": normalize_name(actual_name),
    }
    if not matched:
        raise EzlynxWriteVerifyError(
            f"{EZLYNX_WRITE_VERIFY_REFUSED}: check 3 (name cross-reference) "
            f"failed -- expected {str(expected_name or '')!r}, applicant "
            f"{target} record shows {str(actual_name or '')!r}"
        )

    evidence["verified"] = True
    return evidence
