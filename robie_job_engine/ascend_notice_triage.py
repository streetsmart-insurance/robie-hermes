"""Read-only triage for Ascend notice emails.

Actionable families:

- late_payment         -- "Past due payment" / "Payment failed"
- payment_confirmation -- payment received
- processing_payment   -- payment is processing
- paid_off             -- loan paid in full
- disputed_charge      -- "[Action Needed] Disputed charge". Note plus an
                          accounting task. The driver assigns that task.
- intent_to_cancel     -- "[URGENT] ... Policy(s) at risk for cancellation"
                          (Notice of Intent to Cancel). Note only. Never a
                          cancellation task and never the Ascend NOC label.
- cancellation         -- "canceled for non-payment" / "loan has been canceled"
- reinstatement        -- reinstatement approved. Note only.
- return_premium       -- "Return premium received"
- underwriting         -- underwriting request, counteroffer, document request
- new_program          -- new premium-finance program created. No category
                          discussion; the driver sends it to human review.

Informational mail (refund to the customer, potential policies, programs
ready, sign-in, MSA, agency remittance, and Ascend product/marketing mail
whose subject leads with "New in Ascend") is an explicit ignore type:
status ``ignored``, not a note and not a human-review item. There is no
house client for remittance. A real notice that merely mentions that
phrase in the body stays on its own type.

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
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
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
DISPUTED_CHARGE = "disputed_charge"
REINSTATEMENT = "reinstatement"
AGENCY_REMITTANCE = "agency_remittance"
REFUND = "refund"
POTENTIAL_POLICIES = "potential_policies"
PROGRAMS_READY = "programs_ready"
UNDERWRITING = "underwriting"
PAID_OFF = "paid_off"
SIGN_IN = "sign_in"
MSA = "msa"
PRODUCT_MAIL = "product_mail"
UNKNOWN = "unknown"

# Informational Ascend mail. The driver records status "ignored" and does
# not open a human-review item. UNKNOWN stays needs_human_review.
# Payment confirmation, processing payment, paid off, and underwriting
# file notes. Refund-to-customer, potential policies, programs ready,
# sign-in, MSA, agency remittance, and "New in Ascend" product mail
# stay ignored. Unmatched applicants and unresolved programs stay review.
IGNORE_TYPES = frozenset(
    {
        REFUND,
        POTENTIAL_POLICIES,
        PROGRAMS_READY,
        SIGN_IN,
        MSA,
        AGENCY_REMITTANCE,
        PRODUCT_MAIL,
    }
)

NOTICE_TYPES = (
    LATE_PAYMENT,
    INTENT_TO_CANCEL,
    CANCELLATION,
    RETURN_PREMIUM,
    NEW_PROGRAM,
    PROCESSING_PAYMENT,
    PAYMENT_CONFIRMATION,
    DISPUTED_CHARGE,
    REINSTATEMENT,
    UNDERWRITING,
    PAID_OFF,
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
# phrases, not a bare "cancellation". Product mail is only a subject that
# leads with "New in Ascend" (optional Re/Fwd). A later mention, including
# an insured named that way, does not match.
_SUBJECT_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        r"^\s*(?:(?:re|fwd|fw)\s*:\s*)*new in ascend\b",
        PRODUCT_MAIL,
    ),
    # These subjects also contain "payment", "refund", or "cancel". They
    # have to win before the looser patterns below.
    (r"disputed charge", DISPUTED_CHARGE),
    (r"remittance notification", AGENCY_REMITTANCE),
    (r"reinstatement", REINSTATEMENT),
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
    (r"underwriting request|counteroffer|document request", UNDERWRITING),
    (r"paid off", PAID_OFF),
    (r"\bsign[- ]?in\b", SIGN_IN),
    (r"\bmsa\b|master services? agreement", MSA),
)

_BODY_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"disputed charge|disputed the following payment", DISPUTED_CHARGE),
    (r"remittance notification", AGENCY_REMITTANCE),
    (r"reinstatement", REINSTATEMENT),
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
    # "refund" in passing and is classified above.
    (r"a refund (?:of|has been|to your customer)|refund has been initiated", REFUND),
    (r"potential polic", POTENTIAL_POLICIES),
    (r"programs ready", PROGRAMS_READY),
    (r"underwriting request|counteroffer|document request", UNDERWRITING),
    (r"has been paid off", PAID_OFF),
    (r"\bsign[- ]?in\b", SIGN_IN),
    (r"\bmsa\b|master services? agreement", MSA),
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
_CANCEL_EFFECTIVE_RE = re.compile(
    r"canceled effective\s*(\d{2}/\d{2}/\d{4})",
    re.IGNORECASE,
)


def classify_notice(subject: str, body: str) -> str:
    """Classify an Ascend email into a notice type.

    Never raises on odd input. A subject that leads with "New in Ascend"
    is product mail and is ignored. Other unrecognized mail is UNKNOWN
    so the caller routes it to human review.
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


_SUBJECT_INSURED_RES = (
    re.compile(r"Past due payment for (.+)$", re.IGNORECASE),
    re.compile(r"Payment failed for (.+)$", re.IGNORECASE),
    re.compile(r"Return premium received for (.+)$", re.IGNORECASE),
    re.compile(r"Processing payment for (.+)$", re.IGNORECASE),
    re.compile(r"Programs ready for (.+)$", re.IGNORECASE),
    re.compile(
        r"(?:Underwriting request|A counteroffer to your underwriting request) for (.+)$",
        re.IGNORECASE,
    ),
    re.compile(r"A refund(?: has been initiated)? for (.+)$", re.IGNORECASE),
    re.compile(r"A refund to your customer,\s*(.+?),", re.IGNORECASE),
    re.compile(r"Disputed charge for (.+)$", re.IGNORECASE),
    re.compile(r"reinstatement request has been approved for (.+)$", re.IGNORECASE),
    re.compile(r"coverage policy for (.+?) has been canceled", re.IGNORECASE),
    re.compile(r"coverage policy for (.+?) has been paid off", re.IGNORECASE),
    re.compile(r"\[URGENT\]\s+(.+?)\s+-\s+", re.IGNORECASE),
    re.compile(r"^(.+?)\s+Policy\(s\)\s+Payment Confirmation\s*$", re.IGNORECASE),
)


def _clean_party_name(value: str | None) -> str | None:
    """Drop a field label that got jammed onto the name (``Policy``, ``Reference``)."""
    text = str(value or "").strip()
    if not text:
        return None
    # Labels in these emails are often jammed onto the name with no space
    # ("LLCPolicy", "LLCReference"). Split on the label even then.
    text = re.split(
        r"(?:Policy|Reference|Identifier|Email|Effective)",
        text,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    text = text.strip(" \t.,-:")
    return text or None


def extract_insured_name(subject: str, body: str) -> str | None:
    """Best-effort insured name from the customer block, then the subject."""
    body_text = body or ""
    for pattern in (
        r"^Customer\s*([^\n\r]+)",
        r"^Insured\s*([^\n\r]+)",
        r"your customer,\s*([^,\n]+)",
        r"loan for your customer\s+(.+?)\s+has been",
        r"coverage policy for (.+?) has been canceled",
    ):
        match = re.search(pattern, body_text, re.IGNORECASE | re.MULTILINE)
        cleaned = _clean_party_name(match.group(1) if match else None)
        if cleaned:
            return cleaned
    subject_text = subject or ""
    for pattern in _SUBJECT_INSURED_RES:
        match = pattern.search(subject_text)
        cleaned = _clean_party_name(match.group(1) if match else None)
        if cleaned:
            return cleaned
    return None


def format_money_amount(raw: str | None) -> str | None:
    """Render a dollar amount as short US currency, with cents.

    ``$0,241.00`` and ``0,241.00`` become ``$241.00``. Amounts of
    ``$1,000`` and up keep a thousands separator. Cents are always two
    digits.
    """
    text = str(raw or "").strip().replace("$", "").replace(",", "").strip()
    if not text:
        return None
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return None
    if not amount.is_finite():
        return None
    amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"${amount:,.2f}"


def _first_money(body: str) -> str | None:
    match = _MONEY_RE.search(body or "")
    if not match:
        return None
    return format_money_amount(match.group(1))


def _money_matching(body: str, pattern: str) -> str | None:
    match = re.search(pattern, body or "", re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    return format_money_amount(match.group(1))


_DUE_ON_RE = re.compile(
    r"(?:which was due on|was due on|due on)\s+(\d{2}/\d{2}/\d{4})",
    re.IGNORECASE,
)
_FUTURE_CANCEL_RE = re.compile(
    r"(?:cancelation of your coverage on|no payment is received by)\s+(\d{2}/\d{2}/\d{4})",
    re.IGNORECASE,
)
_PAYMENT_FAILED_RE = re.compile(
    r"payment failed|couldn'?t process your payment|could not process your payment",
    re.IGNORECASE,
)


def _due_on_date(body: str) -> str | None:
    """The payment due date. Policy effective dates are not due dates."""
    match = _DUE_ON_RE.search(body or "")
    return match.group(1) if match else None


def _future_cancel_date(body: str) -> str | None:
    """The date coverage will cancel if the intent-to-cancel stays unpaid."""
    match = _FUTURE_CANCEL_RE.search(body or "")
    return match.group(1) if match else None


def _payment_failed(subject: str, body: str) -> bool:
    return _PAYMENT_FAILED_RE.search(f"{subject or ''}\n{body or ''}") is not None


_NOTICE_HEADINGS = {
    LATE_PAYMENT: "LATE PAYMENT",
    INTENT_TO_CANCEL: "INTENT TO CANCEL",
    RETURN_PREMIUM: "RETURN PREMIUM",
    NEW_PROGRAM: "NEW PROGRAM",
    PROCESSING_PAYMENT: "PROCESSING PAYMENT",
    PAYMENT_CONFIRMATION: "PAYMENT CONFIRMATION",
    DISPUTED_CHARGE: "DISPUTED CHARGE",
    REINSTATEMENT: "REINSTATEMENT",
    AGENCY_REMITTANCE: "AGENCY REMITTANCE",
    REFUND: "REFUND",
    POTENTIAL_POLICIES: "POTENTIAL POLICIES",
    PROGRAMS_READY: "PROGRAMS READY",
    UNDERWRITING: "UNDERWRITING",
    PAID_OFF: "LOAN PAID OFF",
    SIGN_IN: "SIGN IN",
    MSA: "MSA",
    PRODUCT_MAIL: "PRODUCT MAIL",
    UNKNOWN: "UNRECOGNIZED",
}


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


def _past_due_sentence(policy_phrase: str, amount: str | None, due: str | None) -> str:
    if amount and due:
        return f"{policy_phrase} is past due: {amount} was due {due}."
    if amount:
        return f"{policy_phrase} is past due: {amount}."
    if due:
        return f"{policy_phrase} is past due. The payment was due {due}."
    return f"{policy_phrase} is past due."


def _failed_payment_sentence(policy_phrase: str, amount: str | None) -> str:
    if amount:
        return f"{policy_phrase} payment failed: {amount} could not be processed."
    return f"{policy_phrase} payment failed."


def _intent_sentence(
    policy_phrase: str,
    amount: str | None,
    due: str | None,
    cancels_on: str | None,
) -> str:
    parts = [f"{policy_phrase} is at risk of cancellation."]
    if amount and due:
        parts.append(f"{amount} was due {due}.")
    elif amount:
        parts.append(f"{amount} is overdue.")
    elif due:
        parts.append(f"The payment was due {due}.")
    if cancels_on:
        parts.append(f"Coverage cancels on {cancels_on} if it stays unpaid.")
    return " ".join(parts)


def _with_policy(policy_phrase: str, statement: str) -> str:
    if policy_phrase == "The policy":
        return statement
    return f"{policy_phrase}. {statement}"


def _notice_detail(
    notice_type: str,
    subject: str,
    body: str,
    policy_numbers: list[str],
) -> str:
    """One plain-English sentence. No field names and no program id."""
    policy_phrase = _policy_phrase(policy_numbers)
    text = f"{subject or ''}\n{body or ''}"
    if notice_type == LATE_PAYMENT and _payment_failed(subject, body):
        amount = _money_matching(body, r"payment of\s+\$\s?([\d,]+\.\d{2})")
        if amount is None:
            # The failed total is on its own line under "Failed because".
            # The earlier sentence uses the same words, then the subtotal.
            amount = _money_matching(
                body,
                r"Failed because of[^\n]*\n+\s*\$\s?([\d,]+\.\d{2})",
            )
        if amount is None:
            amount = _first_money(body)
        return _failed_payment_sentence(policy_phrase, amount)
    if notice_type == LATE_PAYMENT:
        amount = _money_matching(body, r"past-due payment of\s+\$\s?([\d,]+\.\d{2})")
        if amount is None:
            amount = _first_money(body)
        return _past_due_sentence(policy_phrase, amount, _due_on_date(body))
    if notice_type == INTENT_TO_CANCEL:
        amount = _money_matching(
            body,
            r"(?:loan payment|overdue payment|past-due payment) of\s+\$\s?([\d,]+\.\d{2})",
        )
        if amount is None:
            amount = _first_money(body)
        return _intent_sentence(
            policy_phrase, amount, _due_on_date(body), _future_cancel_date(body)
        )
    if notice_type == RETURN_PREMIUM:
        amount = _money_matching(body, r"return premium of\s+\$\s?([\d,]+\.\d{2})")
        if amount is None:
            amount = _first_money(body)
        if amount:
            statement = f"Ascend received {amount} to apply to the loan."
        else:
            statement = "Ascend received a return premium to apply to the loan."
        return _with_policy(policy_phrase, statement)
    if notice_type == NEW_PROGRAM:
        return _with_policy(
            policy_phrase,
            "Ascend opened a new premium finance program.",
        )
    if notice_type == PAYMENT_CONFIRMATION:
        amount = _money_matching(body, r"payment of\s+\$\s?([\d,]+\.\d{2})")
        if amount is None:
            amount = _first_money(body)
        if amount:
            statement = f"Payment of {amount} was received."
        else:
            statement = "A payment was received."
        return _with_policy(policy_phrase, statement)
    if notice_type == PROCESSING_PAYMENT:
        amount = _money_matching(body, r"payment of\s+\$\s?([\d,]+\.\d{2})")
        if amount is None:
            amount = _first_money(body)
        if amount:
            statement = f"A payment of {amount} is processing."
        else:
            statement = "A payment is processing."
        return _with_policy(policy_phrase, statement)
    if notice_type == REFUND:
        amount = _money_matching(
            body, r"(?:refund of|Amount)\s+\$\s?([\d,]+\.\d{2})"
        )
        if amount is None:
            amount = _first_money(body)
        stopped = re.search(r"has been stopped", body or "", re.IGNORECASE)
        if amount and stopped:
            statement = f"A refund of {amount} was stopped."
        elif amount:
            statement = f"A refund of {amount} was issued."
        elif stopped:
            statement = "A refund was stopped."
        else:
            statement = "A refund was issued."
        return _with_policy(policy_phrase, statement)
    if notice_type == POTENTIAL_POLICIES:
        return "Some policies produced recently have not been purchased."
    if notice_type == PROGRAMS_READY:
        return "Coverage will end within 90 days and can be renewed."
    if notice_type == UNDERWRITING:
        if re.search(r"counteroffer", text, re.IGNORECASE):
            return "A counteroffer was approved on an underwriting request."
        if re.search(r"document request", text, re.IGNORECASE):
            return "Ascend asked for documents on an underwriting request."
        return "An underwriting request is in review."
    if notice_type == PAID_OFF:
        return _with_policy(policy_phrase, "The loan is paid in full.")
    if notice_type == DISPUTED_CHARGE:
        amount = _first_money(body)
        if amount:
            return f"A customer disputed a payment of {amount}."
        return "A customer disputed a payment."
    if notice_type == REINSTATEMENT:
        return "A reinstatement was approved. The carrier still has to accept it."
    if notice_type == AGENCY_REMITTANCE:
        amount = _money_matching(text, r"payment of\s+\$\s?([\d,]+\.\d{2})")
        if amount is None:
            amount = _first_money(text)
        if amount:
            return f"A payment of {amount} was remitted to the agency."
        return "A payment was remitted to the agency."
    if notice_type == SIGN_IN:
        return "This is a sign-in message."
    if notice_type == MSA:
        return "This is a master service agreement message."
    if notice_type == PRODUCT_MAIL:
        return "This is an Ascend product update."
    return "A person needs to read this message."


def insured_name_line(name: str | None) -> str:
    """The insured on its own line, with no period added.

    A name that already ends in ``LLC.`` or ``Inc.`` keeps that one period.
    A name that does not end in a period stays that way.
    """
    return str(name or "").strip()


def build_staff_note(
    notice_type: str,
    subject: str,
    body: str,
    policy_numbers: list[str],
    insured_name: str | None,
) -> str:
    """Staff-facing note for every notice type. Plain sentences only.

    The program id is not included. Callers that need it for traceability
    keep it on the triage result, the driver log, or the job summary.
    """
    if notice_type == CANCELLATION:
        return build_cancellation_note(
            body=body,
            policy_numbers=policy_numbers,
            insured_name=insured_name,
            amount=_first_money(body),
        )
    heading = _NOTICE_HEADINGS.get(notice_type, "UNRECOGNIZED")
    detail = _notice_detail(notice_type, subject, body, policy_numbers)
    lines = [f"{heading} notice from Ascend. {detail}"]
    insured = str(insured_name or "").strip()
    # A list mail names several insureds. One name on the next line would be wrong.
    if insured and notice_type != POTENTIAL_POLICIES:
        lines.append(insured_name_line(insured))
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
    if notice_type == DISPUTED_CHARGE:
        return {
            "ezlynx_workflow": "Ascend - Payments",
            "ezlynx_label": None,
            "zapier_task": True,
            "instruction": (
                "File a note on the Ascend - Payments discussion and assign "
                "an accounting task. Do not message the client."
            ),
        }
    if notice_type == REINSTATEMENT:
        return {
            "ezlynx_workflow": "Ascend - Cancellation Notices",
            "ezlynx_label": None,
            "zapier_task": False,
            "instruction": (
                "File a note only. Reinstatement does not create a task and "
                "does not apply the Ascend NOC label."
            ),
        }
    if notice_type in {PAYMENT_CONFIRMATION, PROCESSING_PAYMENT, PAID_OFF}:
        return {
            "ezlynx_workflow": "Ascend - Payments",
            "ezlynx_label": None,
            "zapier_task": False,
            "instruction": "File a note on the Ascend - Payments discussion. No task.",
        }
    if notice_type == UNDERWRITING:
        return {
            "ezlynx_workflow": "Ascend - Underwriting",
            "ezlynx_label": None,
            "zapier_task": False,
            "instruction": "File a note on the Ascend - Underwriting discussion. No task.",
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

    # The note is the email in plain English. It does not wait on the
    # program read, and it never includes the program id or status code.
    # Ignored and unrecognized mail still get a note for the record. The
    # driver does not file those.
    result["note_text"] = build_staff_note(
        notice_type, subject, body, policy_numbers, insured_name
    )

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


def build_intent_to_cancel_task_payload(
    triage_result: dict[str, Any],
    applicant_id: str,
    account_csr: str,
    due_date: str,
) -> dict[str, Any]:
    """Zapier CSR task for an intent-to-cancel notice.

    Built only when the driver flag is on. The assignee is the same CSR
    login the non-pay cancellation task uses. This is not a cancellation
    task and it does not apply the Ascend NOC label.
    """
    if triage_result.get("notice_type") != INTENT_TO_CANCEL:
        raise ValueError(
            "Intent-to-cancel CSR tasks are only built for intent_to_cancel "
            f"notices, got {triage_result.get('notice_type')!r}."
        )
    if not applicant_id or not str(applicant_id).strip():
        raise ValueError("applicant_id is required to assign an intent-to-cancel task")
    if not account_csr or not str(account_csr).strip():
        raise ValueError(
            "account_csr is required to assign an intent-to-cancel task"
        )
    insured = triage_result.get("insured_name") or "unknown insured"
    policies = triage_result.get("policy_numbers") or []
    policy_bits = f" ({', '.join(policies)})" if policies else ""
    return {
        "applicant_id": str(applicant_id).strip(),
        "task_title": f"Ascend intent to cancel - {insured}{policy_bits}",
        "assignee": str(account_csr).strip(),
        "source": ZAPIER_SOURCE,
        "due_date": validate_due_date(due_date),
        "email_subject": triage_result.get("email_subject") or "",
        "notice_type": INTENT_TO_CANCEL,
        "program_uuid": triage_result.get("program_uuid") or "",
        "note_text": triage_result.get("note_text") or "",
    }
