"""Universal subject line formatter and identifier enforcement for StreetSmart Insurance.

Enforces that every outbound email regarding a policy or client account
strictly contains both the Named Insured and Policy Number in the subject line.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger("subject_formatter")


class SubjectMissingMetadataError(Exception):
    """Raised when an outbound email subject cannot be populated with mandatory insured name and policy number."""
    pass


# Backward compatibility alias
AuditSubjectMissingMetadataError = SubjectMissingMetadataError


def format_subject_with_insured_and_policy(
    subject: str,
    insured_name: Optional[str] = None,
    policy_number: Optional[str] = None,
) -> str:
    """Enforces that Named Insured and Policy Number are present in the subject line.

    Hardcodes inclusion across every outbound email, modifying the subject line
    even when using canned templates or custom subjects.

    Examples:
        >>> format_subject_with_insured_and_policy("Action Required: Audit Update", "Acme LLC", "12345")
        'Action Required: Audit Update - Acme LLC - Policy #12345'
        >>> format_subject_with_insured_and_policy("Action Required: Audit Update - Acme LLC", "Acme LLC", "12345")
        'Action Required: Audit Update - Acme LLC - Policy #12345'
        >>> format_subject_with_insured_and_policy("Acme LLC - Policy #12345", "Acme LLC", "12345")
        'Acme LLC - Policy #12345'
    """
    subj = (subject or "").strip()
    ins = (insured_name or "").strip()
    pol = (policy_number or "").strip()

    if not ins and not pol:
        return subj

    def is_present(target: str, text: str) -> bool:
        if not target or not text:
            return False
        clean_target = re.sub(r"[^a-zA-Z0-9]", "", target).lower()
        clean_text = re.sub(r"[^a-zA-Z0-9]", "", text).lower()
        return clean_target in clean_text if clean_target else False

    has_ins = is_present(ins, subj)
    has_pol = is_present(pol, subj)

    if has_ins and has_pol:
        return subj

    clean_subj = re.sub(r"[\s\-_:;|]+$", "", subj)

    additions: list[str] = []
    if not has_ins and ins:
        additions.append(ins)
    if not has_pol and pol:
        if re.match(r"^(?:policy|pol)\s*#", pol, re.IGNORECASE) or pol.startswith("#"):
            additions.append(pol)
        else:
            additions.append(f"Policy #{pol}")

    added_str = " - ".join(additions)
    if not clean_subj:
        return added_str
    return f"{clean_subj} - {added_str}"


def ensure_subject_has_identifiers(
    subject: str,
    insured_name: Optional[str] = None,
    policy_number: Optional[str] = None,
    raise_if_missing: bool = False,
) -> str:
    """Formats the subject with insured and policy, optionally raising if either is missing."""
    ins = (insured_name or "").strip()
    pol = (policy_number or "").strip()

    if raise_if_missing:
        if not ins or not pol:
            raise SubjectMissingMetadataError(
                f"CRITICAL SAFETY GATE: Refusing to dispatch outbound email with subject '{subject}'. "
                f"Named Insured ('{ins}') and Policy Number ('{pol}') must both be provided "
                "across every outbound email that goes out."
            )

    return format_subject_with_insured_and_policy(
        subject=subject,
        insured_name=ins,
        policy_number=pol,
    )
