"""Triple verification for EZLynx note/document filings.

Every email-sourced filing must prove, BEFORE the write, that the target
applicant is the right account for the email. Three independent checks,
fail-closed: any check that fails raises FilingTargetMismatch and nothing
is written.

The three checks
----------------
1. SOURCE CONSISTENCY -- the record the worker matched (queue row, search
   hit, prior lookup) must carry the same policy number and insured name
   as the email being filed. This is the check that would have caught the
   2026-09-25 Rivera misfire: the worker matched Laura Oloughlin's queue
   row (policy 29 1152005977 05) to Rivera's email (policy 29 1152014106 05)
   and never compared them.
2. POLICY ANCHORED TO APPLICANT -- the email's policy number must be
   anchored to the target applicant in EZLynx itself: either the digit
   groups of a discussion title on the applicant match the policy digits,
   or PolicyApi search ties the policy number to the applicant id. When
   PolicyApi finds the policy on a DIFFERENT applicant that is a hard
   fail, not a skip.
3. DISCUSSION ON APPLICANT -- for notes, the resolved discussion id must
   appear in the applicant's discussion list. A discussion id paired with
   the wrong applicant never writes.

Nothing here deletes, moves, or edits existing data. Verification only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


class FilingTargetMismatch(Exception):
    """Raised when the filing target fails verification. Fail-closed: the
    caller must not write when this is raised."""


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _name_tokens(value: Any) -> list[str]:
    return [t for t in re.findall(r"[a-z]+", str(value or "").lower()) if len(t) > 2]


def _discussion_id_of(record: dict[str, Any]) -> str:
    for key in ("discussionId", "DiscussionId", "id", "Id"):
        value = str(record.get(key) or "").strip()
        if value:
            return value
    return ""


def _discussion_title_of(record: dict[str, Any]) -> str:
    for key in ("title", "Title", "discussionTitle", "DiscussionTitle", "name", "Name"):
        value = str(record.get(key) or "").strip()
        if value:
            return value
    return ""


def _title_digit_groups(title: str) -> list[str]:
    return re.findall(r"\d+", title or "")


def _policy_digits_in_title(policy_digits: str, title: str) -> bool:
    """True when the title's digit groups identify the policy number.

    Accepts the groups joined ("29"+"1152014106"+"05") or the full digit
    string as one group, so both "29 1152014106 05" and "29115201410605"
    title formats match.
    """
    if not policy_digits:
        return False
    groups = _title_digit_groups(title)
    if not groups:
        return False
    return policy_digits == "".join(groups) or policy_digits in groups


@dataclass(frozen=True)
class FilingIdentity:
    """Who the email is about and what record the worker matched."""

    insured_name: str = ""
    policy_number: str = ""
    source_insured_name: str = ""
    source_policy_number: str = ""

    @classmethod
    def from_mapping(cls, mapping: dict[str, Any] | None) -> "FilingIdentity":
        mapping = mapping or {}
        return cls(
            insured_name=str(mapping.get("insured_name") or ""),
            policy_number=str(mapping.get("policy_number") or ""),
            source_insured_name=str(mapping.get("source_insured_name") or ""),
            source_policy_number=str(mapping.get("source_policy_number") or ""),
        )


def verify_filing_target(
    *,
    applicant_id: str,
    filing_identity: FilingIdentity | dict[str, Any],
    discussion_id: str | None = None,
    discussion_title: str | None = None,
    discussion_client: Any = None,
    policy_client: Any = None,
) -> dict[str, Any]:
    """Run the triple verification. Returns a checks dict on success.

    Raises FilingTargetMismatch on the first failing check. ``discussion_client``
    must provide ``get_discussions(applicant_id)``; ``policy_client`` (optional)
    must provide ``search_policy_by_number(policy_number)`` returning the
    ``{"status","data":{"results":[...]}}`` shape.
    """
    identity = (
        filing_identity
        if isinstance(filing_identity, FilingIdentity)
        else FilingIdentity.from_mapping(filing_identity)
    )
    applicant = str(applicant_id or "").strip()
    if not applicant:
        raise FilingTargetMismatch("applicant id is required for verification")
    if discussion_client is None:
        raise FilingTargetMismatch("a discussion client is required for verification")

    email_policy_digits = _digits(identity.policy_number)
    if not email_policy_digits:
        raise FilingTargetMismatch("email policy number is required for verification")
    if not _name_tokens(identity.insured_name):
        raise FilingTargetMismatch("email insured name is required for verification")

    # -- CHECK 1: the worker's matched record agrees with the email --------
    source_policy_digits = _digits(identity.source_policy_number)
    if not source_policy_digits:
        raise FilingTargetMismatch(
            "source policy number is required: the worker must state which "
            "record it matched before filing"
        )
    if source_policy_digits != email_policy_digits:
        raise FilingTargetMismatch(
            f"source policy {identity.source_policy_number.strip()!r} does not match "
            f"email policy {identity.policy_number.strip()!r}; refusing to file"
        )
    email_tokens = set(_name_tokens(identity.insured_name))
    source_tokens = set(_name_tokens(identity.source_insured_name))
    if not source_tokens:
        raise FilingTargetMismatch(
            "source insured name is required: the worker must state whose "
            "record it matched before filing"
        )
    if not (email_tokens & source_tokens):
        raise FilingTargetMismatch(
            f"source insured {identity.source_insured_name.strip()!r} does not match "
            f"email insured {identity.insured_name.strip()!r}; refusing to file"
        )

    # -- applicant's own discussions (independent EZLynx-side read) ---------
    discussions = discussion_client.get_discussions(applicant)
    if not discussions:
        raise FilingTargetMismatch(
            f"applicant {applicant} has no readable discussions; refusing to file"
        )
    records = [r for r in discussions if isinstance(r, dict)]

    # -- CHECK 3: discussion belongs to the applicant (notes) ---------------
    resolved_title = str(discussion_title or "")
    if discussion_id:
        match = [r for r in records if _discussion_id_of(r) == str(discussion_id).strip()]
        if not match:
            raise FilingTargetMismatch(
                f"discussion {str(discussion_id).strip()} is not on applicant "
                f"{applicant}; refusing to file"
            )
        resolved_title = _discussion_title_of(match[0])

    # -- CHECK 2: the policy is anchored to the applicant in EZLynx ---------
    policy_via: str | None = None
    titles_to_scan = [resolved_title] if resolved_title else [
        _discussion_title_of(r) for r in records
    ]
    if any(_policy_digits_in_title(email_policy_digits, t) for t in titles_to_scan):
        policy_via = "discussion-title"
    elif policy_client is not None:
        found = policy_client.search_policy_by_number(identity.policy_number)
        data = found.get("data") if isinstance(found, dict) else None
        rows = data.get("results") if isinstance(data, dict) else []
        exact = [
            r for r in (rows or [])
            if isinstance(r, dict) and _digits(r.get("policyNumber")) == email_policy_digits
        ]
        if len(exact) == 1:
            row_applicant = str(exact[0].get("accountId") or "").strip()
            if row_applicant == applicant:
                policy_via = "policy-api"
            else:
                raise FilingTargetMismatch(
                    f"policy {identity.policy_number.strip()!r} belongs to applicant "
                    f"{row_applicant}, not {applicant}; refusing to file"
                )
        elif len(exact) > 1:
            raise FilingTargetMismatch(
                f"policy {identity.policy_number.strip()!r} is ambiguous in "
                f"PolicyApi ({len(exact)} rows); refusing to file"
            )
    if policy_via is None:
        raise FilingTargetMismatch(
            f"policy {identity.policy_number.strip()!r} is not anchored to applicant "
            f"{applicant} (no discussion title carries its digits and PolicyApi "
            f"does not tie it here); refusing to file"
        )

    return {
        "applicant_id": applicant,
        "discussion_id": str(discussion_id or "").strip() or None,
        "discussion_title": resolved_title,
        "policy_number": identity.policy_number.strip(),
        "insured_name": identity.insured_name.strip(),
        "checks": ["source-consistency", "policy-anchored", "discussion-on-applicant"]
        if discussion_id
        else ["source-consistency", "policy-anchored", "applicant-live"],
        "policy_via": policy_via,
    }


__all__ = [
    "FilingIdentity",
    "FilingTargetMismatch",
    "verify_filing_target",
]
