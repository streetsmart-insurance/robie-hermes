"""Splice-replacement conversational pathways for ``call_type=client_outreach``.

Source of truth: Carlo's Google Doc Manual WFs/Scripts
https://docs.google.com/document/d/1cZCe_9cz_fuNWYZLjdNvP1Z3jhkUIqGrYZq2pwJdaPM/edit

Use ONLY those written Manual WF body sentences. Conversational Robie — no
press-1 / press-2 / press-4 / press-6, no Splice toll-free, no opt-out IVR.

``<<Agent>>`` in Splice = account Assigned Producer (first name in client copy).
Voicemail / callback is always agency main 732-462-8343.
Warm-transfer on a clear yes to Assigned Producer DID — not Sales Center,
not the label invoker.

Not ported from this pass: Birthday, Additional Policy, Applicant Created,
New Customer/Welcome, Policy Reinstatement, Policy Renewed, Upcoming
Renewal/Expiration EZLynx automations, Winback, Sales Center
New/Contacted/Quoted/Won. Sales Center Reviewed Status is already
``Robie lead follow-up`` (leave that path alone).
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
PATHWAY_RENEWAL_REACHOUT = "renewal_reachout"
PATHWAY_GENERIC = "generic"

OUTREACH_PATHWAYS = (
    PATHWAY_CANCELLATION,
    PATHWAY_AUDIT,
    PATHWAY_RETURNED_MAIL,
    PATHWAY_ESIGN,
    PATHWAY_ADDITIONAL_INFO,
    PATHWAY_RECOMMENDATIONS,
    PATHWAY_UNRESPONSIVE,
    PATHWAY_RENEWAL_REACHOUT,
    PATHWAY_GENERIC,
)

# Exact body sentences from the Google Doc Manual WFs/Scripts tab.
# Cancellation Notice is the separate Splice PDF (not in that doc body).
MANUAL_WF_BODIES = {
    PATHWAY_AUDIT: (
        "It appears that an audit for your account is currently incomplete. "
        "Please take the necessary steps to finalize this audit as soon as possible."
    ),
    PATHWAY_RECOMMENDATIONS: (
        "We are following up on some recommendations that were made for your account. "
        "Please take the necessary steps to address these recommendations as soon as possible."
    ),
    PATHWAY_RETURNED_MAIL: (
        "We have received some returned mail for your account. "
        "Please contact our office to update your information as soon as possible."
    ),
    PATHWAY_ESIGN: (
        "We are following up on an e-signature request for your account. "
        "Please complete the e-signature process as soon as possible."
    ),
    PATHWAY_ADDITIONAL_INFO: (
        "We are following up on a request for additional information for your account. "
        "Please provide the requested information as soon as possible."
    ),
    PATHWAY_UNRESPONSIVE: "We are reaching out regarding your policies.",
    PATHWAY_RENEWAL_REACHOUT: (
        "Your insurance policy will be up for renewal soon. "
        "We want to ensure you have the proper coverage and would like to discuss your options."
    ),
}

# Alias / label tokens that force the Cancellation Notice pathway.
_CANCELLATION_ALIAS_COLLAPSED = (
    "robiecancellation",
    "cancellationnotice",
)

# CSR must label/note this explicitly. Never infer from generic "renewal"
# or from the manual renewal pipeline.
_RENEWAL_REACHOUT_COLLAPSED = (
    "renewalreachout",
    "robierenewalreachout",
    "renewalreachouttemplate",
)
_RENEWAL_REACHOUT_PHRASE = re.compile(
    r"renewal\s+reach[\s-]*out|robie\s+renewal\s+reach",
    re.IGNORECASE,
)

# More specific pathways first so "audit incomplete" does not fall into generic.
# renewal_reachout is handled separately (explicit CSR phrase only).
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
    when the reason looks like cancel). ``renewal_reachout`` fires only when
    the CSR explicitly labels/notes that phrase — never from generic
    "renewal" copy or the manual renewal pipeline. Otherwise the matching
    Manual WF, or ``generic`` (CSR What to say).
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
    if any(token in collapsed for token in _RENEWAL_REACHOUT_COLLAPSED):
        return PATHWAY_RENEWAL_REACHOUT
    if _RENEWAL_REACHOUT_PHRASE.search(blob):
        return PATHWAY_RENEWAL_REACHOUT

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


def _callback_sentence() -> str:
    return (
        f"Please call us back at {AGENCY_MAIN_CALLBACK_DISPLAY} — "
        f"that's {AGENCY_MAIN_CALLBACK_SPOKEN}. Thank you!"
    )


def _wrap_live(greeting: str, body: str, connect: str) -> str:
    return f"{greeting}, this is Robie from StreetSmart Insurance. {body} {connect}"


def _wrap_voicemail(greeting: str, body: str) -> str:
    return f"{greeting}, this is Robie from StreetSmart Insurance. {body} {_callback_sentence()}"


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
    """Conversational live opener using Carlo's Manual WF body (or Cancellation PDF)."""
    greeting = f"Hi {client_first}" if client_first else "Hi"
    lob = (line_of_business or "insurance").strip() or "insurance"
    carrier = (carrier_name or "your carrier").strip() or "your carrier"
    policy_type = _policy_type_phrase(line_of_business)
    due = action_date or "the due date on your notice"
    connect = _connect_offer(producer_first)

    if pathway == PATHWAY_CANCELLATION:
        body = (
            f"I'm calling with an important notice about your {lob} policy with {carrier}. "
            f"Your {policy_type} is set to be cancelled due to an overdue payment. "
            f"To avoid a lapse in coverage, please make a payment by {due}."
        )
        return _wrap_live(greeting, body, connect)

    body = MANUAL_WF_BODIES.get(pathway)
    if body:
        return _wrap_live(greeting, body, connect)

    reason = (csr_instructions or "").strip()
    if reason:
        return _wrap_live(greeting, reason, connect)
    return _wrap_live(greeting, "Do you have a moment to talk?", connect)


def build_outreach_voicemail_script(
    *,
    pathway: str,
    client_first: Optional[str],
    line_of_business: Optional[str],
    carrier_name: Optional[str],
    action_date: Optional[str],
    csr_instructions: Optional[str] = None,
) -> str:
    """Voicemail / busy close: same Manual WF facts + 732-462-8343. No transfer."""
    greeting = f"Hi {client_first}" if client_first else "Hello"
    lob = (line_of_business or "insurance").strip() or "insurance"
    carrier = (carrier_name or "your carrier").strip() or "your carrier"
    policy_type = _policy_type_phrase(line_of_business)
    due = action_date or "the due date on your notice"

    if pathway == PATHWAY_CANCELLATION:
        body = (
            f"I'm calling with an important notice about your {lob} policy with {carrier}. "
            f"Your {policy_type} is set to be cancelled due to an overdue payment. "
            f"To avoid a lapse in coverage, please make a payment by {due}."
        )
        return _wrap_voicemail(greeting, body)

    body = MANUAL_WF_BODIES.get(pathway)
    if body:
        return _wrap_voicemail(greeting, body)

    reason = (csr_instructions or "").strip()
    if reason:
        return _wrap_voicemail(greeting, reason)
    return (
        f"{greeting}, this is Robie from StreetSmart Insurance. "
        f"{_callback_sentence()}"
    )
