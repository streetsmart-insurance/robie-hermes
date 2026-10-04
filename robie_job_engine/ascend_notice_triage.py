"""Read-only triage for Ascend notice emails.

Actionable families:

- late_payment      -- "Past due payment" / "Payment failed"
- intent_to_cancel  -- "[URGENT] ... Policy(s) at risk for cancellation"
                       (Notice of Intent to Cancel). Note only. Never a
                       cancellation task and never the Ascend NOC label.
- cancellation      -- "canceled for non-payment" / "loan has been canceled"
- return_premium    -- "Return premium received"
- new_program       -- new premium-finance program created

Informational mail (processing payment, payment confirmation, refunds,
potential policies, programs ready, underwriting, paid off, sign-in, MSA)
is an explicit ignore type: status ``ignored``, not human-review noise.

- unknown -- anything else; always routed to human review

The module never writes to Ascend or EZLynx.  It classifies the email,
resolves the Ascend program (preferring the program UUID embedded in the
email's dashboard link, falling back to a policy-number search), reads the
program back, and returns a structured triage recommendation.  A notice is
only marked actionable when the program resolved cleanly; every failure
mode sets ``needs_human_review`` instead of failing silently.  Ignored
types return before any API call.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from .ascend_api import AscendApiClient, AscendApiError
from .zapier_tasks import validate_due_date

LATE_PAYMENT = "late_payment"
INTENT_TO_CANCEL = "intent_to_cancel"
CANCELLATION = "cancellation"
RETURN_PREMIUM = "return_premium"
NEW_PROGRAM = "new_program"
PROCESSING_PAYMENT = "processing_payment"
PAYMENT_CONFIRMATION = "payment_confirmation"
REFUND = "refund"
POTENTIAL_POLICIES = "potential_policies"
PROGRAMS_READY = "programs_ready"
UNDERWRITING = "underwriting"
PAID_OFF = "paid_off"
SIGN_IN = "sign_in"
MSA = "msa"
UNKNOWN = "unknown"

# Informational Ascend mail. The driver records status "ignored" and does
# not open a human-review item. UNKNOWN stays needs_human_review.
IGNORE_TYPES = frozenset(
    {
        PROCESSING_PAYMENT,
        PAYMENT_CONFIRMATION,
        REFUND,
        POTENTIAL_POLICIES,
        PROGRAMS_READY,
        UNDERWRITING,
        PAID_OFF,
        SIGN_IN,
        MSA,
    }
)

NOTICE_TYPES = (
    LATE_PAYMENT,
    INTENT_TO_CANCEL,
    CANCELLATION,
    RETURN_PREMIUM,
    NEW_PROGRAM,
    *tuple(sorted(IGNORE_TYPES)),
    UNKNOWN,
)

# Source tag sent with every Zapier-fired task so the Zap (and the audit log)
# can tell inbox-triage tasks apart from other task creators.
ZAPIER_SOURCE = "inbox-triage"

# Subject-line patterns. First match wins. Intent-to-cancel is listed
# before any cancellation pattern: "[URGENT] ... Policy(s) at risk for
# cancellation" contains the word "cancellation" and must not become a
# cancellation task. Cancellation itself is only the two non-payment
# phrases, not a bare "cancellation".
_SUBJECT_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"past[ -]?due payment", LATE_PAYMENT),
    (r"payment failed|failed payment", LATE_PAYMENT),
    (r"policy\(s\) at risk for cancellation|at risk for cancellation", INTENT_TO_CANCEL),
    (r"notice of intent to cancel", INTENT_TO_CANCEL),
    (r"canceled? (for|due to) non[- ]?pay", CANCELLATION),
    (r"loan has been canceled", CANCELLATION),
    (r"return premium received", RETURN_PREMIUM),
    (r"program created|new program|finance agreement", NEW_PROGRAM),
    (r"processing payment", PROCESSING_PAYMENT),
    (r"payment confirmation", PAYMENT_CONFIRMATION),
    (r"\brefund\b", REFUND),
    (r"potential polic", POTENTIAL_POLICIES),
    (r"programs ready", PROGRAMS_READY),
    (r"underwriting request|counteroffer", UNDERWRITING),
    (r"paid off", PAID_OFF),
    (r"\bsign[- ]?in\b", SIGN_IN),
    (r"\bmsa\b|master service agreement", MSA),
)

_BODY_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"notice of intent to cancel", INTENT_TO_CANCEL),
    (r"failure to pay will result in the cancelation", INTENT_TO_CANCEL),
    (r"past-due payment of", LATE_PAYMENT),
    (r"has a past[ -]?due payment", LATE_PAYMENT),
    (r"canceled for non-payment", CANCELLATION),
    (r"loan has been canceled effective", CANCELLATION),
    (r"return premium of \$", RETURN_PREMIUM),
    (r"will be applied toward the loan", RETURN_PREMIUM),
    (r"processing payment", PROCESSING_PAYMENT),
    (r"payment confirmation", PAYMENT_CONFIRMATION),
    # Specific refund sentences only. A disputed-charge notice mentions
    # "refund" in passing and must stay UNKNOWN for a human.
    (r"a refund (?:of|has been|to your customer)|refund has been initiated", REFUND),
    (r"potential polic", POTENTIAL_POLICIES),
    (r"programs ready", PROGRAMS_READY),
    (r"underwriting request|counteroffer", UNDERWRITING),
    (r"has been paid off", PAID_OFF),
    (r"\bsign[- ]?in\b", SIGN_IN),
    (r"\bmsa\b|master service agreement", MSA),
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
_CANCEL_EFFECTIVE_RE = re.compile(
    r"canceled effective\s*(\d{2}/\d{2}/\d{4})",
    re.IGNORECASE,
)


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
        r"[Cc]overage policy for (.+?) has been canceled", subject_text, re.IGNORECASE
    )
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


def cancel_effective_date(body: str) -> str | None:
    """The loan cancel date, not the policy effective date."""
    match = _CANCEL_EFFECTIVE_RE.search(body or "")
    return match.group(1) if match else None


def _policy_phrase(policy_numbers: list[str]) -> str:
    policies = [str(item).strip() for item in policy_numbers if str(item or "").strip()]
    if len(policies) == 1:
        return f"Policy {policies[0]}"
    if len(policies) > 1:
        return "Policies " + ", ".join(policies)
    return "The policy"


def build_cancellation_note(
    *,
    body: str,
    policy_numbers: list[str],
    insured_name: str | None,
    amount: str | None,
) -> str:
    """Staff-facing cancellation note. Plain sentences, no field labels.

    The first line names the non-pay cancellation, the policy, and the
    cancel date. Robie does not apply the Ascend NOC label.
    """
    policy_phrase = _policy_phrase(policy_numbers)
    when = cancel_effective_date(body)
    if when:
        first = (
            "NON-PAY CANCELLATION notice from Ascend. "
            f"{policy_phrase} was canceled on {when}."
        )
    else:
        first = (
            "NON-PAY CANCELLATION notice from Ascend. "
            f"{policy_phrase} was canceled for non-payment."
        )
    lines = [first]
    insured = str(insured_name or "").strip()
    if insured and amount:
        lines.append(f"{insured} still has an overdue balance of {amount}.")
    elif insured:
        lines.append(f"This notice is for {insured}.")
    elif amount:
        lines.append(f"The overdue balance is {amount}.")
    lines.append("The loan was canceled because the payment was not made.")
    return "\n".join(lines)


def recommended_action(notice_type: str) -> dict[str, Any]:
    """EZLynx-side recommendation for a notice type (advisory only)."""
    if notice_type == INTENT_TO_CANCEL:
        return {
            "ezlynx_workflow": "Ascend NOC",
            "ezlynx_label": None,
            "zapier_task": False,
            "instruction": (
                "File a note on the existing Ascend NOC workflow. This is a "
                "Notice of Intent to Cancel: the policy is not canceled. Do "
                "not apply the Ascend NOC label and do not create a "
                "cancellation task."
            ),
        }
    if notice_type == LATE_PAYMENT:
        return {
            "ezlynx_workflow": "Ascend NOC",
            "ezlynx_label": "Ascend NOC",
            "instruction": (
                "Open the Ascend NOC workflow for the matched program, file "
                "the email and note the past-due amount and due date. If a "
                "workflow already exists for this program, keep it assigned "
                "to the person already working it (check the Activity "
                "notes); new tasks go to the CSR or assigned producer. Do "
                "not assign tasks to anyone after 4:30 PM EST -- hold them "
                "for the next business day."
            ),
        }
    if notice_type == CANCELLATION:
        return {
            "ezlynx_workflow": "Service-Cancellation",
            "ezlynx_label": None,
            "zapier_task": True,
            "instruction": (
                "File a note only. Do not apply the Ascend NOC label. That "
                "label emails or texts the client, and Robie does not send "
                "those. Apply the cancellation transaction in EZLynx ONLY if "
                "the policy is manual -- download policies are handled by "
                "the carrier download. First check the History of transactions "
                "and the Activity notes: if the cancellation was already "
                "applied, do not apply it again. Cancel type is always "
                "'Cancel Confirmation'. Enter the return premium from the "
                "notice; if the notice shows none, enter $0. After applying, "
                "EZLynx auto-runs a cancellation workflow assigned to the "
                "CSR -- enter notes there. Assign an EZLynx follow-up task "
                "via the Zapier task Zap (build_cancellation_task_payload) "
                "to the CSR on the account; a human CSR/AP reviews before "
                "anything is closed."
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
    if notice_type in IGNORE_TYPES:
        return {
            "ezlynx_workflow": None,
            "ezlynx_label": None,
            "zapier_task": False,
            "instruction": (
                "Informational Ascend mail. Do not file a note, apply a "
                "label, or create a task."
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
        "email_subject": (subject or "").strip(),
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

    if notice_type in IGNORE_TYPES:
        result["ignored"] = True
        result["needs_human_review"] = False
        return result

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
    if notice_type == CANCELLATION:
        result["note_text"] = build_cancellation_note(
            body=body,
            policy_numbers=policy_numbers,
            insured_name=insured_name,
            amount=amount,
        )
        return result
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


def build_cancellation_task_payload(
    triage_result: dict[str, Any],
    applicant_id: str,
    account_csr: str,
    due_date: str,
) -> dict[str, Any]:
    """Build the Zapier EZLynx-task payload for a cancellation notice.

    The task goes to the CSR on the matched EZLynx account -- ``account_csr``
    is resolved dynamically by the caller from the account (there is no
    static default assignee).  ``due_date`` is required (ISO YYYY-MM-DD):
    the Zapier firing layer rejects task payloads without a valid due date.
    Triage itself never invents the applicant or the CSR.  Firing happens
    through the zapier skill's ``bin/zap-trigger``
    (see ``robie_job_engine/zapier_tasks.py``), never from inside this
    read-only module.
    """
    if triage_result.get("notice_type") != CANCELLATION:
        raise ValueError(
            "Zapier cancellation tasks are only built for cancellation notices, "
            f"got {triage_result.get('notice_type')!r}. "
            "Intent-to-cancel is a note, never a cancellation task."
        )
    if not applicant_id or not str(applicant_id).strip():
        raise ValueError("applicant_id is required to assign a cancellation task")
    if not account_csr or not str(account_csr).strip():
        raise ValueError(
            "account_csr (the CSR on the matched EZLynx account) is required "
            "to assign a cancellation task"
        )

    insured = triage_result.get("insured_name") or "unknown insured"
    policies = triage_result.get("policy_numbers") or []
    policy_bits = f" ({', '.join(policies)})" if policies else ""
    return {
        "applicant_id": str(applicant_id).strip(),
        "task_title": f"Ascend cancellation notice - {insured}{policy_bits}",
        "assignee": str(account_csr).strip(),
        "source": ZAPIER_SOURCE,
        "due_date": validate_due_date(due_date),
        "email_subject": triage_result.get("email_subject") or "",
        "notice_type": CANCELLATION,
        "program_uuid": triage_result.get("program_uuid") or "",
        "note_text": triage_result.get("note_text") or "",
    }
