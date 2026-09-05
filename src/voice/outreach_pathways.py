"""Splice-replacement conversational pathways for ``call_type=client_outreach``.

Source of truth: StreetSmart Splice pack (Cancellation Notice + action-needed
scripts). No press-1 / press-2 / Splice toll-free / opt-out IVR.

Sales Center / winback / birthday / marketing Splice workflows are not ported.
Lead follow-up stays on its own prompt (Sales Center producer greeting +
label-invoker transfer).
"""

from __future__ import annotations

import re
from datetime import date
from typing import Iterable, Optional

from src.voice.context_hydrator import spoken_client_first_name

# Agency main RingCentral PBX. Same values as voice_client — kept here so
# this module does not import CarrierVoiceClient (circular).
AGENCY_MAIN_CALLBACK_DISPLAY = "732-462-8343"
AGENCY_MAIN_CALLBACK_SPOKEN = "seven three two, four six two, eight three four three"

PATHWAY_CANCELLATION = "cancellation"
PATHWAY_AUDIT = "audit"
PATHWAY_RETURNED_MAIL = "returned_mail"
PATHWAY_ESIGN = "esign"
PATHWAY_ADDITIONAL_INFO = "additional_info"
PATHWAY_RECOMMENDATIONS = "recommendations"
PATHWAY_UNRESPONSIVE = "unresponsive"
PATHWAY_GENERIC = "generic"

OUTREACH_PATHWAYS = (
    PATHWAY_CANCELLATION,
    PATHWAY_AUDIT,
    PATHWAY_RETURNED_MAIL,
    PATHWAY_ESIGN,
    PATHWAY_ADDITIONAL_INFO,
    PATHWAY_RECOMMENDATIONS,
    PATHWAY_UNRESPONSIVE,
    PATHWAY_GENERIC,
)

# Alias / label tokens that force the Cancellation Notice pathway.
_CANCELLATION_ALIAS_COLLAPSED = (
    "robiecancellation",
    "cancellationnotice",
)

# More specific pathways first so "audit incomplete" does not fall into generic.
_PATHWAY_PATTERNS = (
    (
        PATHWAY_RETURNED_MAIL,
        (
            r"returned\s+mail",
            r"bad\s+address",
            r"update(?:\s+your|\s+the)?\s+address",
            r"mail(?:ed)?\s+back",
        ),
    ),
    (
        PATHWAY_ESIGN,
        (
            r"e[-\s]?sign",
            r"electronic\s+signature",
            r"signature\s+needed",
            r"sign(?:ature)?\s+to\s+avoid",
        ),
    ),
    (
        PATHWAY_ADDITIONAL_INFO,
        (
            r"additional\s+info",
            r"need(?:s|ed)?\s+(?:more\s+)?(?:additional\s+)?info",
            r"missing\s+(?:info|information|document)",
            r"we\s+need\s+additional",
        ),
    ),
    (
        PATHWAY_RECOMMENDATIONS,
        (
            r"recommend",
        ),
    ),
    (
        PATHWAY_UNRESPONSIVE,
        (
            r"unresponsive",
            r"not\s+responding",
            r"haven['’]?t\s+heard",
            r"no\s+response\s+from\s+(?:the\s+)?(?:client|insured|customer)",
        ),
    ),
    (
        PATHWAY_AUDIT,
        (
            r"\baudit\b",
        ),
    ),
    (
        PATHWAY_CANCELLATION,
        (
            r"\bcancel",
            r"\bnon[-\s]?pay",
            r"overdue\s+payment",
            r"lapse\s+in\s+coverage",
            r"notice\s+of\s+cancellation",
            r"pending\s+cancel",
            r"make\s+a\s+payment",
        ),
    ),
)

_DATE_RE = re.compile(
    r"\b(?:by|due|before|on)\s+"
    r"("
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\s+\d{1,2}(?:,?\s*\d{4})?"
    r"|"
    r"\d{1,2}/\d{1,2}(?:/\d{2,4})?"
    r")\b",
    re.IGNORECASE,
)


def _collapse(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def infer_outreach_pathway(
    text: Optional[str] = None,
    labels: Optional[Iterable[str]] = None,
    alias: Optional[str] = None,
) -> str:
    """Resolve a Splice-replacement pathway from CSR copy / labels / alias.

    ``robie cancellation`` / cancel / non-pay → Cancellation Notice (default
    when the reason looks like cancel). Other keywords map to the matching
    Splice script. Otherwise ``generic`` (CSR What to say).
    """
    parts = [text or "", alias or ""]
    if labels:
        parts.extend(str(label) for label in labels if label)
    blob = " ".join(parts)
    collapsed = _collapse(blob)
    if any(token in collapsed for token in _CANCELLATION_ALIAS_COLLAPSED):
        return PATHWAY_CANCELLATION
    if alias and "cancel" in alias.lower():
        return PATHWAY_CANCELLATION

    lower = blob.lower()
    for pathway, patterns in _PATHWAY_PATTERNS:
        if any(re.search(pattern, lower) for pattern in patterns):
            return pathway
    return PATHWAY_GENERIC


def assigned_producer_first_name(full_name: Optional[str]) -> Optional[str]:
    """Speakable first name of the account Assigned Producer. Never invent."""
    return spoken_client_first_name(full_name)


def extract_action_date(
    instructions: Optional[str] = None,
    expiration_date: Optional[object] = None,
) -> Optional[str]:
    """Payment / action date from CSR copy, else policy expiration."""
    if instructions:
        match = _DATE_RE.search(instructions)
        if match:
            return " ".join(match.group(1).split())
    if expiration_date is None:
        return None
    if isinstance(expiration_date, date):
        return expiration_date.strftime("%m/%d/%Y")
    text = str(expiration_date).strip()
    return text or None


def _policy_type_phrase(line_of_business: Optional[str]) -> str:
    lob = (line_of_business or "").strip()
    if not lob:
        return "policy"
    if re.search(r"\bpolicy\b", lob, re.IGNORECASE):
        return lob
    return f"{lob} policy"


def _connect_offer(producer_first: Optional[str]) -> str:
    if producer_first:
        return f"If you want, I can connect you to {producer_first} now."
    return "If you want, I can connect you to your producer now."


def build_outreach_live_script(
    *,
    pathway: str,
    client_first: Optional[str],
    line_of_business: Optional[str],
    carrier_name: Optional[str],
    producer_first: Optional[str],
    action_date: Optional[str],
    csr_instructions: Optional[str] = None,
) -> str:
    """Conversational live opener for a Splice-replacement pathway."""
    greeting = f"Hi {client_first}" if client_first else "Hi"
    lob = (line_of_business or "insurance").strip() or "insurance"
    carrier = (carrier_name or "your carrier").strip() or "your carrier"
    policy_type = _policy_type_phrase(line_of_business)
    due = action_date or "the due date on your notice"
    connect = _connect_offer(producer_first)

    if pathway == PATHWAY_CANCELLATION:
        return (
            f"{greeting}, this is Robie from StreetSmart Insurance. "
            f"I'm calling with an important notice about your {lob} policy with {carrier}. "
            f"Your {policy_type} is set to be cancelled due to an overdue payment. "
            f"To avoid a lapse in coverage, please make a payment by {due}. "
            f"{connect}"
        )
    if pathway == PATHWAY_AUDIT:
        return (
            f"{greeting}, this is Robie from StreetSmart Insurance. "
            f"I'm calling about your {lob} policy with {carrier}. "
            f"Your audit is incomplete — please finish the audit so we can keep "
            f"your coverage in good standing. {connect}"
        )
    if pathway == PATHWAY_RETURNED_MAIL:
        return (
            f"{greeting}, this is Robie from StreetSmart Insurance. "
            f"I'm calling about your {lob} policy with {carrier}. "
            f"We received returned mail and need you to update your address. "
            f"{connect}"
        )
    if pathway == PATHWAY_ESIGN:
        return (
            f"{greeting}, this is Robie from StreetSmart Insurance. "
            f"I'm calling about your {lob} policy with {carrier}. "
            f"We need your e-signature to avoid an interruption in coverage. "
            f"{connect}"
        )
    if pathway == PATHWAY_ADDITIONAL_INFO:
        return (
            f"{greeting}, this is Robie from StreetSmart Insurance. "
            f"I'm calling about your {lob} policy with {carrier}. "
            f"We need additional information from you. {connect}"
        )
    if pathway == PATHWAY_RECOMMENDATIONS:
        return (
            f"{greeting}, this is Robie from StreetSmart Insurance. "
            f"I'm calling about your {lob} policy with {carrier} to follow up "
            f"on recommendations. {connect}"
        )
    if pathway == PATHWAY_UNRESPONSIVE:
        return (
            f"{greeting}, this is Robie from StreetSmart Insurance. "
            f"I'm reaching out about your policies. {connect}"
        )
    reason = (csr_instructions or "").strip()
    if reason:
        return (
            f"{greeting}, this is Robie from StreetSmart Insurance. "
            f"{reason} {connect}"
        )
    return (
        f"{greeting}, this is Robie from StreetSmart Insurance. "
        f"Do you have a moment to talk? {connect}"
    )


def build_outreach_voicemail_script(
    *,
    pathway: str,
    client_first: Optional[str],
    line_of_business: Optional[str],
    carrier_name: Optional[str],
    action_date: Optional[str],
    csr_instructions: Optional[str] = None,
) -> str:
    """Voicemail / busy close: same facts + agency main 732-462-8343. No transfer."""
    greeting = f"Hi {client_first}" if client_first else "Hello"
    lob = (line_of_business or "insurance").strip() or "insurance"
    carrier = (carrier_name or "your carrier").strip() or "your carrier"
    policy_type = _policy_type_phrase(line_of_business)
    due = action_date or "the due date on your notice"
    callback = (
        f"Please call us back at {AGENCY_MAIN_CALLBACK_DISPLAY} — "
        f"that's {AGENCY_MAIN_CALLBACK_SPOKEN}. Thank you!"
    )

    if pathway == PATHWAY_CANCELLATION:
        return (
            f"{greeting}, this is Robie from StreetSmart Insurance. "
            f"I'm calling with an important notice about your {lob} policy with {carrier}. "
            f"Your {policy_type} is set to be cancelled due to an overdue payment. "
            f"To avoid a lapse in coverage, please make a payment by {due}. "
            f"{callback}"
        )
    if pathway == PATHWAY_AUDIT:
        return (
            f"{greeting}, this is Robie from StreetSmart Insurance. "
            f"Your {lob} audit with {carrier} is incomplete. Please finish the audit. "
            f"{callback}"
        )
    if pathway == PATHWAY_RETURNED_MAIL:
        return (
            f"{greeting}, this is Robie from StreetSmart Insurance. "
            f"We received returned mail on your {lob} policy with {carrier}. "
            f"Please update your address. {callback}"
        )
    if pathway == PATHWAY_ESIGN:
        return (
            f"{greeting}, this is Robie from StreetSmart Insurance. "
            f"We need your e-signature on your {lob} policy with {carrier} "
            f"to avoid an interruption. {callback}"
        )
    if pathway == PATHWAY_ADDITIONAL_INFO:
        return (
            f"{greeting}, this is Robie from StreetSmart Insurance. "
            f"We need additional information on your {lob} policy with {carrier}. "
            f"{callback}"
        )
    if pathway == PATHWAY_RECOMMENDATIONS:
        return (
            f"{greeting}, this is Robie from StreetSmart Insurance. "
            f"Following up on recommendations for your {lob} policy with {carrier}. "
            f"{callback}"
        )
    if pathway == PATHWAY_UNRESPONSIVE:
        return (
            f"{greeting}, this is Robie from StreetSmart Insurance. "
            f"I'm reaching out about your policies. {callback}"
        )
    reason = (csr_instructions or "").strip()
    if reason:
        return (
            f"{greeting}, this is Robie from StreetSmart Insurance. "
            f"{reason} {callback}"
        )
    return (
        f"{greeting}, this is Robie from StreetSmart Insurance. "
        f"{callback}"
    )
