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
# Proposed Doc add (do not auto-sync): generic cancel body below matches the
# account-level Manual WF shape (Audit / Recommendations / Returned Mail).
# Runtime stays here in build_outreach_* when hydrate LOB/carrier are missing
# or placeholder.
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

# Exact EZLynx org label names for Carlo / Admin to create (names only —
# this repo never invents organizationLabelId values). CSRs click these
# like Splice WFs. Close variants (spaces / hyphens / underscores,
# optional ``robie `` prefix, brackets) also dispatch.
EZLYNX_ADMIN_OUTREACH_LABELS = (
    "Robie client outreach",
    "Robie cancellation",
    "Robie audit",
    "Robie returned mail",
    "Robie e-sign",
    "Robie esign",
    "Robie additional info",
    "Robie recommendations",
    "Robie unresponsive",
    "Robie renewal reach-out",
    "Robie renewal reachout",
)

# Expected pathway when the CSR clicks that Admin name and the note body
# does not itself name a different Manual WF. ``Robie client outreach``
# stays generic (CSR What to say) unless the body matches a pathway.
EZLYNX_ADMIN_OUTREACH_LABEL_PATHWAYS = (
    ("Robie client outreach", PATHWAY_GENERIC),
    ("Robie cancellation", PATHWAY_CANCELLATION),
    ("Robie audit", PATHWAY_AUDIT),
    ("Robie returned mail", PATHWAY_RETURNED_MAIL),
    ("Robie e-sign", PATHWAY_ESIGN),
    ("Robie esign", PATHWAY_ESIGN),
    ("Robie additional info", PATHWAY_ADDITIONAL_INFO),
    ("Robie recommendations", PATHWAY_RECOMMENDATIONS),
    ("Robie unresponsive", PATHWAY_UNRESPONSIVE),
    ("Robie renewal reach-out", PATHWAY_RENEWAL_REACHOUT),
    ("Robie renewal reachout", PATHWAY_RENEWAL_REACHOUT),
)

# Whole-label collapsed slugs. Optional ``robie`` prefix is stripped
# before lookup so a standalone label ``audit`` still dispatches.
_OUTREACH_DISPATCH_SLUGS = frozenset(
    {
        "clientoutreach",
        "cancellation",
        "audit",
        "returnedmail",
        "esign",
        "additionalinfo",
        "recommendations",
        "unresponsive",
        "renewalreachout",
    }
)

# Specific WF labels force a pathway. ``clientoutreach`` is omitted so
# that label defers to note-body inference (generic unless the body matches).
_SLUG_TO_FORCED_PATHWAY = {
    "cancellation": PATHWAY_CANCELLATION,
    "audit": PATHWAY_AUDIT,
    "returnedmail": PATHWAY_RETURNED_MAIL,
    "esign": PATHWAY_ESIGN,
    "additionalinfo": PATHWAY_ADDITIONAL_INFO,
    "recommendations": PATHWAY_RECOMMENDATIONS,
    "unresponsive": PATHWAY_UNRESPONSIVE,
    "renewalreachout": PATHWAY_RENEWAL_REACHOUT,
}

# Free-text / title phrases require the ``robie `` prefix so a carrier
# note that mentions "payroll audit" does not dispatch client outreach.
_ROBIE_OUTREACH_PHRASE_RE = re.compile(
    r"\[?\s*robie[\s_\-]+(?P<token>"
    r"client[\s_\-]+outreach|"
    r"cancellation|"
    r"audit|"
    r"returned[\s_\-]+mail|"
    r"e[\s_\-]?sign|"
    r"additional[\s_\-]+info|"
    r"recommendations|"
    r"unresponsive|"
    r"renewal[\s_\-]*reach[\s_\-]*out"
    r")\b\s*\]?",
    re.IGNORECASE,
)

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


def _slug_from_collapsed_label(collapsed: str) -> Optional[str]:
    """Return the dispatch slug for a whole label (optional ``robie`` prefix)."""
    if collapsed.startswith("robie"):
        collapsed = collapsed[len("robie") :]
    if collapsed in _OUTREACH_DISPATCH_SLUGS:
        return collapsed
    return None


def is_client_outreach_dispatch_label(label_name: Optional[str]) -> bool:
    """True when a standalone org label should dispatch ``client_outreach``.

    Matches the Admin names and close variants: spaces / hyphens / underscores,
    optional ``robie `` prefix, brackets. Does not match Birthday, winback,
    Sales Center, new customer, or ``Robie Call`` / ``Robie lead follow-up``.
    """
    if not label_name:
        return False
    return _slug_from_collapsed_label(_collapse(label_name)) is not None


def text_has_client_outreach_trigger(text: Optional[str]) -> bool:
    """True when title/note text contains a ``robie ``-prefixed outreach phrase.

    Bare pathway words (``audit``, ``cancellation``) in a sentence do not
    dispatch — that would hijack carrier ``Robie Call`` notes. Whole-string
    collapsed equality still accepts ``robieaudit`` / ``[robie_audit]``.
    """
    if not text:
        return False
    if _ROBIE_OUTREACH_PHRASE_RE.search(text):
        return True
    collapsed = _collapse(text)
    return collapsed.startswith("robie") and collapsed[len("robie") :] in _OUTREACH_DISPATCH_SLUGS


def strip_client_outreach_trigger_phrases(text: str) -> str:
    """Remove robie-prefixed outreach phrases so they are not self-markers."""
    return _ROBIE_OUTREACH_PHRASE_RE.sub(" ", text)


def pathway_forced_by_outreach_label(label_name: Optional[str]) -> Optional[str]:
    """Pathway forced by a specific WF label, or None to defer to note body.

    ``Robie client outreach`` / ``client outreach`` return None (generic unless
    the note body matches a pathway). Unknown labels return None.
    """
    if not label_name:
        return None
    slug = _slug_from_collapsed_label(_collapse(label_name))
    if not slug:
        return None
    return _SLUG_TO_FORCED_PATHWAY.get(slug)


def _forced_pathway_from_robie_phrases(text: Optional[str]) -> Optional[str]:
    """First specific ``Robie <pathway>`` phrase in free text, if any."""
    if not text:
        return None
    for match in _ROBIE_OUTREACH_PHRASE_RE.finditer(text):
        forced = _SLUG_TO_FORCED_PATHWAY.get(_collapse(match.group("token")))
        if forced:
            return forced
    collapsed = _collapse(text)
    if collapsed.startswith("robie"):
        return _SLUG_TO_FORCED_PATHWAY.get(collapsed[len("robie") :])
    return None


def infer_outreach_pathway(
    text: Optional[str] = None,
    labels: Optional[Iterable[str]] = None,
    alias: Optional[str] = None,
) -> str:
    """Resolve a Splice-replacement pathway from CSR copy / labels / alias.

    Specific org labels (``Robie audit``, ``Robie cancellation``, …) win over
    note-body heuristics so a clicked WF label is the pathway. ``Robie client
    outreach`` does not force a pathway — the note body is inferred, else
    ``generic`` (CSR What to say).

    ``robie cancellation`` / cancel / non-pay → Cancellation Notice (default
    when the reason looks like cancel). ``renewal_reachout`` fires only when
    the CSR explicitly labels/notes that phrase — never from generic
    "renewal" copy or the manual renewal pipeline. Otherwise the matching
    Manual WF, or ``generic`` (CSR What to say).
    """
    if labels:
        for label in labels:
            forced = pathway_forced_by_outreach_label(str(label) if label else None)
            if forced:
                return forced
    if alias:
        forced = pathway_forced_by_outreach_label(alias)
        if forced:
            return forced
        forced = _forced_pathway_from_robie_phrases(alias)
        if forced:
            return forced
    forced = _forced_pathway_from_robie_phrases(text)
    if forced:
        return forced

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


# Hydrate stubs / spoken placeholders. Bare "Commercial" is the hydrator
# fallback when no real LOB is on file — "Commercial Auto" stays speakable.
_PLACEHOLDER_CARRIERS = frozenset(
    {
        "unknown",
        "unknown carrier",
        "your carrier",
        "n/a",
        "na",
        "none",
        "null",
        "-",
        "--",
    }
)
_PLACEHOLDER_LOBS = frozenset(
    {
        "commercial",
        "insurance",
        "unknown",
        "n/a",
        "na",
        "none",
        "null",
        "-",
        "--",
    }
)

# Proposed Google Doc Manual WF / Cancellation body (not in the Doc today).
# Same account-level shape as Audit / Returned Mail — no LOB, carrier, or
# payment/non-pay language unless the CSR What to say explicitly includes it.
CANCELLATION_GENERIC_BODY = (
    "We are following up on a cancellation notice for your account. "
    "Please contact our office as soon as possible."
)
CANCELLATION_CONTACT_CLAUSE = "Please contact our office as soon as possible."
_PAYMENT_LANGUAGE_RE = re.compile(
    r"\b(?:"
    r"non[-\s]?pay|"
    r"overdue(?:\s+payment)?|"
    r"make\s+a\s+payment|"
    r"payment\s+due|"
    r"past\s+due|"
    r"payment"
    r")\b",
    re.IGNORECASE,
)


def _normalized_spoken_token(value: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).lower()


def is_placeholder_carrier(carrier_name: Optional[str]) -> bool:
    """True when carrier is missing or a hydrate stub (never speak it)."""
    cleaned = _normalized_spoken_token(carrier_name)
    return not cleaned or cleaned in _PLACEHOLDER_CARRIERS


def is_placeholder_lob(line_of_business: Optional[str]) -> bool:
    """True when LOB is missing or a generic hydrate stub (never speak it).

    Bare ``Commercial`` is a stub. ``Commercial Auto`` / ``Commercial Package``
    are real lines and stay speakable.
    """
    cleaned = _normalized_spoken_token(line_of_business)
    return not cleaned or cleaned in _PLACEHOLDER_LOBS


def csr_instructions_include_payment(csr_instructions: Optional[str]) -> bool:
    """True when CSR What to say explicitly mentions payment / non-pay."""
    return bool(_PAYMENT_LANGUAGE_RE.search(csr_instructions or ""))


def _cancellation_csr_payment_clause(csr_instructions: Optional[str]) -> str:
    """Append CSR payment facts only when the CSR wrote them. Never invent."""
    text = (csr_instructions or "").strip()
    if text and csr_instructions_include_payment(text):
        return f" {text}"
    return ""


def _cancellation_body(
    line_of_business: Optional[str],
    carrier_name: Optional[str],
    csr_instructions: Optional[str] = None,
) -> str:
    """Cancellation spoken body. Soften when LOB/carrier look unknown.

    Default copy is an account-level cancellation notice. No non-pay / overdue
    / make-a-payment language unless CSR What to say explicitly includes it.
    """
    lob_ok = not is_placeholder_lob(line_of_business)
    carrier_ok = not is_placeholder_carrier(carrier_name)
    payment = _cancellation_csr_payment_clause(csr_instructions)
    if lob_ok and carrier_ok:
        lob = line_of_business.strip()
        carrier = carrier_name.strip()
        return (
            f"I'm calling with an important notice about your {lob} policy with {carrier}. "
            f"We received a cancellation notice for your account. "
            f"{CANCELLATION_CONTACT_CLAUSE}{payment}"
        )
    if lob_ok:
        lob = line_of_business.strip()
        return (
            f"I'm calling with an important notice about your {lob} policy. "
            f"We received a cancellation notice for your account. "
            f"{CANCELLATION_CONTACT_CLAUSE}{payment}"
        )
    if carrier_ok:
        carrier = carrier_name.strip()
        return (
            f"I'm calling with an important notice about your policy with {carrier}. "
            f"We received a cancellation notice for your account. "
            f"{CANCELLATION_CONTACT_CLAUSE}{payment}"
        )
    if payment:
        return f"{CANCELLATION_GENERIC_BODY}{payment}"
    return CANCELLATION_GENERIC_BODY


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
    connect = _connect_offer(producer_first)

    if pathway == PATHWAY_CANCELLATION:
        body = _cancellation_body(
            line_of_business, carrier_name, csr_instructions=csr_instructions
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

    if pathway == PATHWAY_CANCELLATION:
        body = _cancellation_body(
            line_of_business, carrier_name, csr_instructions=csr_instructions
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
