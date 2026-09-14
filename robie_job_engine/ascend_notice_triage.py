"""Read-only triage for Ascend notice emails landing in the hello inbox.

Covers the three notice families Carlo asked for plus the closely related
return-premium notice:

- late_payment   -- "Past due payment for {Insured}"
- cancellation   -- "{Insured} canceled for non-payment ..." (loan canceled)
- return_premium -- "Return premium received for Policy {id}"
- new_program    -- new premium-finance program created
- unknown        -- anything else; always routed to human review

The module never writes to Ascend or EZLynx.  It classifies the email,
resolves the Ascend program (preferring the program UUID embedded in the
email's dashboard link, falling back to a policy-number search), reads the
program back, and returns a structured triage recommendation: which EZLynx
workflow/label to use and what note to file.  A notice is only marked
actionable when the program resolved cleanly; every failure mode sets
``needs_human_review`` instead of failing silently.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from .ascend_api import AscendApiClient, AscendApiError

LATE_PAYMENT = "late_payment"
CANCELLATION = "cancellation"
RETURN_PREMIUM = "return_premium"
NEW_PROGRAM = "new_program"
UNKNOWN = "unknown"

NOTICE_TYPES = (LATE_PAYMENT, CANCELLATION, RETURN_PREMIUM, NEW_PROGRAM, UNKNOWN)

# Subject-line patterns observed in real Ascend mail (Sep 2026).  Kept as
# ordered (pattern, type) pairs so the first match wins; the unknown fallback
# is intentional -- an unrecognized notice must go to a human, never be
# auto-filed.
_SUBJECT_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"past[ -]?due payment", LATE_PAYMENT),
    (r"payment failed|failed payment", LATE_PAYMENT),
    (r"canceled? (for|due to) non[- ]?pay", CANCELLATION),
    (r"notice of cancel", CANCELLATION),
    (r"loan has been canceled|cancellation", CANCELLATION),
    (r"return premium received", RETURN_PREMIUM),
    (r"program created|new program|finance agreement", NEW_PROGRAM),
)

_BODY_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"past-due payment of", LATE_PAYMENT),
    (r"has a past[ -]?due payment", LATE_PAYMENT),
    (r"canceled for non-payment", CANCELLATION),
    (r"loan has been canceled effective", CANCELLATION),
    (r"return premium of \$", RETURN_PREMIUM),
    (r"will be applied toward the loan", RETURN_PREMIUM),
)

_DASHBOARD_PROGRAM_RE = re.compile(
    r"dashboard\.useascend\.com/programs/([0-9a-fA-F-]{36})"
)
# In the real emails the ID often jams against the next field label with no
# space ("Policy IDMXL0446256Effective date08/20/2026"), so the capture stops
# before a following "Effective" label, a non-ID character, or end of text.
_POLICY_ID_RE = re.compile(
    r"Policy ID\s*([A-Za-z0-9][A-Za-z0-9\-/]{3,40}?)"
    r"(?=Effective\b|[^A-Za-z0-9\-/]|$)"
)
_MONEY_RE = re.compile(r"\$\s?([\d,]+\.\d{2})")
_DATE_RE = re.compile(r"\b(\d{2}/\d{2}/\d{4})\b")


def classify_notice(subject: str, body: str) -> str:
    """Classify an Ascend email into a notice type.

    Never raises on odd input; unrecognized mail is UNKNOWN so the caller
    routes it to human review.
    """
    subject_text = subject or ""
    body_text = body or ""
    for pattern, notice_type in _SUBJECT_PATTERNS:
        if re.search(pattern, subject_text, re.IGNORECASE):
            return notice_type
    for pattern, notice_type in _BODY_PATTERNS:
        if re.search(pattern, body_text, re.IGNORECASE):
            return notice_type
    return UNKNOWN


def extract_program_uuid(text: str) -> str | None:
    """Extract the Ascend program UUID from a dashboard link in the email."""
    match = _DASHBOARD_PROGRAM_RE.search(text or "")
    if not match:
        return None
    try:
        return str(UUID(match.group(1)))
    except (TypeError, ValueError, AttributeError):
        return None


def extract_policy_numbers(body: str) -> list[str]:
    """Extract carrier policy IDs listed in the notice body, de-duplicated."""
    seen: list[str] = []
    for match in _POLICY_ID_RE.finditer(body or ""):
        value = match.group(1).strip()
        if value and value not in seen:
            seen.append(value)
    return seen


def extract_insured_name(subject: str, body: str) -> str | None:
    """Best-effort insured name from the subject or body."""
    subject_text = subject or ""
    match = re.search(r"Past due payment for (.+)$", subject_text, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    match = re.search(
        r"coverage policy for (.+?) has been canceled", body or "", re.IGNORECASE
    )
    if match:
        return match.group(1).strip()
    match = re.search(r"^Customer\s*([^\n\r]+)", body or "", re.IGNORECASE | re.MULTILINE)
    if match:
        return match.group(1).strip()
    match = re.search(r"^Insured\s*([^\n\r]+)", body or "", re.IGNORECASE | re.MULTILINE)
    if match:
        return match.group(1).strip()
    return None


def _first_money(body: str) -> str | None:
    match = _MONEY_RE.search(body or "")
    return f"${match.group(1)}" if match else None


def _first_date(body: str) -> str | None:
    match = _DATE_RE.search(body or "")
    return match.group(1) if match else None


def recommended_action(notice_type: str) -> dict[str, Any]:
    """EZLynx-side recommendation for a notice type (advisory only)."""
    if notice_type == LATE_PAYMENT:
        return {
            "ezlynx_workflow": "AscendNOC",
            "ezlynx_label": "AscendNOC",
            "instruction": (
                "Open the AscendNOC workflow for the matched program, file the "
                "email and note the past-due amount and due date. Assign for "
                "tomorrow when past 2:30 PM Central."
            ),
        }
    if notice_type == CANCELLATION:
        return {
            "ezlynx_workflow": "Service-Cancellation",
            "ezlynx_label": "email received",
            "instruction": (
                "Flag for human review before any EZLynx cancellation: confirm "
                "the loan cancel effective date and overdue balance against "
                "the carrier policy, then follow the manual cancellation SOP."
            ),
        }
    if notice_type == RETURN_PREMIUM:
        return {
            "ezlynx_workflow": "record",
            "ezlynx_label": "email received",
            "instruction": (
                "File the notice on the policy workflow and confirm unearned "
                "commission was sent; funds are applied toward the Ascend loan."
            ),
        }
    if notice_type == NEW_PROGRAM:
        return {
            "ezlynx_workflow": "record",
            "ezlynx_label": "email received",
            "instruction": (
                "File the new-program notice on the policy workflow and verify "
                "the program details match the bound policy."
            ),
        }
    return {
        "ezlynx_workflow": "human review",
        "ezlynx_label": "email received",
        "instruction": "Unrecognized Ascend notice -- route to a CSR for manual triage.",
    }


def triage_notice(
    client: AscendApiClient, subject: str, body: str
) -> dict[str, Any]:
    """Classify one Ascend email and resolve its program.  Read-only.

    Returns a dict with: notice_type, insured_name, policy_numbers,
    program_uuid, program (read-back record or None), lookup_method,
    recommendation, note_text, needs_human_review, and review_reason.
    API failures are reported via needs_human_review, never swallowed.
    """
    text = f"{subject or ''}\n{body or ''}"
    notice_type = classify_notice(subject, body)
    insured_name = extract_insured_name(subject, body)
    policy_numbers = extract_policy_numbers(body)
    program_uuid = extract_program_uuid(text)

    result: dict[str, Any] = {
        "notice_type": notice_type,
        "insured_name": insured_name,
        "policy_numbers": policy_numbers,
        "program_uuid": program_uuid,
        "program": None,
        "lookup_method": None,
        "recommendation": recommended_action(notice_type),
        "note_text": "",
        "needs_human_review": False,
        "review_reason": "",
    }

    if notice_type == UNKNOWN:
        result["needs_human_review"] = True
        result["review_reason"] = "Unrecognized Ascend notice type"
        return result

    program: dict[str, Any] | None = None
    try:
        if program_uuid:
            program = client.get_program(program_uuid)
            result["lookup_method"] = "program_uuid_from_email"
        else:
            for policy_number in policy_numbers:
                found = client.find_program_by_policy(policy_number)
                if found:
                    program = found.get("program")
                    result["program_uuid"] = found.get("program_id")
                    result["lookup_method"] = "policy_number_search"
                    break
    except AscendApiError as exc:
        result["needs_human_review"] = True
        result["review_reason"] = f"Ascend API lookup failed: {exc}"
        return result

    if program is None:
        result["needs_human_review"] = True
        result["review_reason"] = (
            "No Ascend program resolved for this notice; "
            "do not file it against an assumed policy."
        )
        return result

    result["program"] = program

    amount = _first_money(body)
    date = _first_date(body)
    lines = [
        f"Ascend notice: {notice_type.replace('_', ' ')}.",
        f"Email subject: {(subject or '').strip()}",
    ]
    if insured_name:
        lines.append(f"Insured: {insured_name}")
    if policy_numbers:
        lines.append(f"Policies: {', '.join(policy_numbers)}")
    if amount:
        lines.append(f"Amount: {amount}")
    if date:
        lines.append(f"Date: {date}")
    if program_uuid:
        lines.append(f"Ascend program: {program_uuid}")
    program_status = program.get("status")
    if program_status:
        lines.append(f"Ascend program status: {program_status}")
    result["note_text"] = "\n".join(lines)
    return result
