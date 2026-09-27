"""Hello@ inbox classification + client-identity extraction.

Mirrors the certificate intake's pipeline shape (PR #609 philosophy) but
with hello-specific genuine categories. Hello mail is broader than
certificates: new business, renewals, midterm changes, client issues,
carrier notices, billing, endorsements, documents clients send in, and
general questions.

Classification actions (mirroring cert_verification.classify_requested_action
ordering philosophy — noise/autoresponder checks FIRST, because they quote
original request language):

    GENUINE      a real client/agency work item, with a request_type
    INTERNAL     agency-internal sender (Carlo/Jake forwarding carrier
                 notices into hello) — real work, already owned; not a
                 new client request to match
    AUTO_REPLY   out-of-office / bounce / delivery failure — never a request
    ACK          thank-you / receipt language — the work is already done
    NOISE        newsletters, carrier marketing, system notifications,
                 vendor spam — never a request
    UNKNOWN      could not classify — holds for human review

Genuine request types (hello-specific):

    new_business    quote requests for new coverage
    renewal         renewal quotes / renewal requests
    midterm         policy changes (add/remove vehicle, trailer, driver,
                    address, coverage changes)
    client_issue    complaints / problems needing resolution
    carrier_notice  cancellation / rescission / non-renewal / DNOC notices
                    needing agency action
    billing         premium finance, return premium, past-due, settlements
    endorsement     additional interest / additional insured / confirmations
    document        client sending documents in (license, dec pages, loss runs)
    general_question everything else that reads like a real client question

Identity extraction (extract_hello_identity): sender email, company/insured
name, policy numbers, MC numbers — the anchors the matcher uses against the
daily applicant report and the sender-alias store.

Read-only: this module never touches EZLynx, Zapier, or email. Pure text
classification.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Actions and request types
# ---------------------------------------------------------------------------

GENUINE = "genuine"
INTERNAL = "internal"
AUTO_REPLY = "auto_reply"
ACK = "acknowledgement"
NOISE = "noise"
UNKNOWN = "unknown"

# Canonical hello routing roles (from HelloIntake.assignment_for):
#   new_business -> originating_producer
#   renewal / midterm -> applicable_csr
# The hello-specific types below route to applicable_csr until the intake
# stub is extended; the wiring doc records this explicitly.

ROUTE_FOR_REQUEST_TYPE = {
    "new_business": "originating_producer",
    "renewal": "applicable_csr",
    "midterm": "applicable_csr",
    "client_issue": "applicable_csr",
    "carrier_notice": "applicable_csr",
    "billing": "applicable_csr",
    "endorsement": "applicable_csr",
    "document": "applicable_csr",
    "general_question": "applicable_csr",
}

# ---------------------------------------------------------------------------
# Autoresponder / bounce detection (same philosophy as certs: these quote
# original request language, so they MUST be checked before request shapes)
# ---------------------------------------------------------------------------

_AUTOREPLY_BODY_PATTERNS = [
    re.compile(r"\bautomated response\b", re.IGNORECASE),
    re.compile(r"\bautomatic reply\b", re.IGNORECASE),
    re.compile(r"\bauto[\-\s]?reply\b", re.IGNORECASE),
    re.compile(r"\bout of office\b", re.IGNORECASE),
    re.compile(r"\bdo not reply\b", re.IGNORECASE),
    re.compile(r"\bthis (mailbox|inbox|email) is not monitored\b", re.IGNORECASE),
    re.compile(r"\bdelivery (failure|status notification)\b", re.IGNORECASE),
    re.compile(r"\bmessage not delivered\b", re.IGNORECASE),
    re.compile(r"\bundeliverable\b", re.IGNORECASE),
    re.compile(r"\bdelivery has failed\b", re.IGNORECASE),
    re.compile(r"\bmail delivery (failed|failure)\b", re.IGNORECASE),
    re.compile(r"\bvacation responder\b", re.IGNORECASE),
]

# Narrow: genuine senders can use donotreply@ (RMIS lesson from certs).
_AUTOREPLY_SENDERS = (
    "mailer-daemon",
    "postmaster",
    "autoresponder",
    "canned.response",
)

_BOUNCE_SUBJECT_RE = re.compile(
    r"(?i)^\s*(undeliverable|undelivered|delivery (status notification|"
    r"failure)|failure notice|returned mail|mail delivery (failed|failure))"
)

_AUTOREPLY_SUBJECT_RE = re.compile(
    r"(?i)^\s*(automatic reply|auto reply|out of office)\b"
)

# A subject that starts with Re:/Fwd: inherits quoted request language —
# a purely polite reply body is an acknowledgement, not a new request.
_WAS_REPLY_RE = re.compile(r"(?i)^\s*(re|fwd?)\s*:")

# ---------------------------------------------------------------------------
# Noise: newsletters / marketing / system notifications
# ---------------------------------------------------------------------------

_NEWSLETTER_PATTERNS = [
    re.compile(r"\bunsubscribe\b", re.IGNORECASE),
    re.compile(r"\blist-unsubscribe\b", re.IGNORECASE),
    re.compile(r"\bsevere weather resources\b", re.IGNORECASE),
    re.compile(r"\bhelp is at hand\b", re.IGNORECASE),
    re.compile(r"\bget together\b.{0,30}\brequest\b", re.IGNORECASE),
    re.compile(r"\bjoin us\b.{0,40}\b(webinar|event|luncheon)\b", re.IGNORECASE),
    re.compile(r"\bmonthly newsletter\b", re.IGNORECASE),
    re.compile(r"\bindustry (news|update)\b", re.IGNORECASE),
]

# System notifications that are never client requests: text-message
# relays, call-analysis summaries, chat digests.
_SYSTEM_NOTIFICATION_PATTERNS = [
    re.compile(r"\bhas sent you a text message\b", re.IGNORECASE),
    re.compile(r"\bdo not reply\b.{0,40}\btext message\b", re.IGNORECASE),
    re.compile(r"\bcall analysis\b", re.IGNORECASE),
    re.compile(r"\bscheduled callback\b", re.IGNORECASE),
    re.compile(r"\bconversation digest\b", re.IGNORECASE),
    re.compile(r"\bmissed call\b", re.IGNORECASE),
]

_NOISE_SENDERS = (
    # Carrier marketing / vendor mail observed in hello@
    "swyfft.com",
    "britecore.com",
)

# ---------------------------------------------------------------------------
# Internal senders (agency staff forwarding into hello@)
# ---------------------------------------------------------------------------

_INTERNAL_DOMAINS = frozenset({
    "streetsmart.insurance",
    "ssinj.com",
})

# ---------------------------------------------------------------------------
# Acknowledgement: thank-you / receipt language — work already done
# ---------------------------------------------------------------------------

_ACK_RECEIPT_PATTERNS = [
    re.compile(r"\bthank\s*you\b", re.IGNORECASE),
    re.compile(r"\bthanks\b", re.IGNORECASE),
]

_ACK_COMPLETION_PATTERNS = [
    re.compile(r"\b(received|got) the\b.{0,40}\b(quote|policy|certificate|"
               r"endorsement|documents?)\b", re.IGNORECASE),
    re.compile(r"\ball (set|good|taken care of)\b", re.IGNORECASE),
]

# ---------------------------------------------------------------------------
# Genuine request-type patterns (observed in hello@ mail, Sep 2026)
# ---------------------------------------------------------------------------

_REQUEST_TYPE_PATTERNS: list[tuple[str, list[re.Pattern]]] = [
    ("new_business", [
        re.compile(r"\binsurance for a new\b", re.IGNORECASE),
        re.compile(r"\bnew business\b", re.IGNORECASE),
        re.compile(r"\bneed (a )?quote\b", re.IGNORECASE),
        re.compile(r"\b(need|want)\b.{0,20}\b(a )?quote\b", re.IGNORECASE),
        re.compile(r"\bplease\s+provide\b.{0,40}\bquote\b", re.IGNORECASE),
        re.compile(r"\bquote request\b", re.IGNORECASE),
        re.compile(r"\bstart(ing)? a (new )?policy\b", re.IGNORECASE),
        re.compile(r"\blooking for (new )?insurance\b", re.IGNORECASE),
        re.compile(r"\bnew setup\b", re.IGNORECASE),
    ]),
    ("renewal", [
        re.compile(r"\brenewal (quote|request|policy)\b", re.IGNORECASE),
        re.compile(r"\brenew (my|our|the) polic", re.IGNORECASE),
        re.compile(r"\brenewal is (due|coming)\b", re.IGNORECASE),
    ]),
    ("midterm", [
        re.compile(r"\bpolicy change\b", re.IGNORECASE),
        re.compile(r"\btrailer to add\b", re.IGNORECASE),
        re.compile(r"\badd\b.{0,30}\bto my policy\b", re.IGNORECASE),
        re.compile(r"\bremove\b.{0,30}\bfrom (my|the|our) polic", re.IGNORECASE),
        re.compile(r"\bchange of (address|vehicle|garaging)\b", re.IGNORECASE),
        re.compile(r"\bmid[\-\s]?term\b", re.IGNORECASE),
        re.compile(r"\badd a (driver|vehicle|truck)\b", re.IGNORECASE),
        re.compile(r"\bupdate (my|our|the) polic", re.IGNORECASE),
    ]),
    ("client_issue", [
        re.compile(r"\bissues? with (our|my) polic", re.IGNORECASE),
        re.compile(r"\bneed.{0,25}resolved immediately\b", re.IGNORECASE),
        re.compile(r"\bcomplaint\b", re.IGNORECASE),
        re.compile(r"\bwrong\b.{0,20}\b(on|with)\b.{0,20}\b(my|our) polic",
                   re.IGNORECASE),
        re.compile(r"\bthis is unacceptable\b", re.IGNORECASE),
    ]),
    ("carrier_notice", [
        re.compile(r"\bnotice of cancellation\b", re.IGNORECASE),
        re.compile(r"\bpolicy rescission\b", re.IGNORECASE),
        re.compile(r"\bcancellation notice\b", re.IGNORECASE),
        re.compile(r"\bdnoc\b", re.IGNORECASE),
        re.compile(r"\bnon[\-\s]?renewal\b", re.IGNORECASE),
        re.compile(r"\bnotice of intent to cancel\b", re.IGNORECASE),
    ]),
    ("billing", [
        re.compile(r"\breturn premium\b", re.IGNORECASE),
        re.compile(r"\bpremium financ", re.IGNORECASE),
        re.compile(r"\bsettlement offer\b", re.IGNORECASE),
        re.compile(r"\b(overdue|past due) invoice\b", re.IGNORECASE),
        re.compile(r"\bpayment (failed|declined)\b", re.IGNORECASE),
        re.compile(r"\bbilling (question|issue|dispute)\b", re.IGNORECASE),
    ]),
    ("endorsement", [
        re.compile(r"\badditional interest\b", re.IGNORECASE),
        re.compile(r"\badditional insured\b", re.IGNORECASE),
        re.compile(r"\bendorsement (added|issued|attached)\b", re.IGNORECASE),
        re.compile(r"\bpolicy confirmation\b", re.IGNORECASE),
        re.compile(r"\bcertificate holder\b", re.IGNORECASE),
    ]),
    ("document", [
        re.compile(r"\battach(ed|ing)\b.{0,40}\b(driver'?s license|dec page|"
                   r"loss runs?|acord|mvr)\b", re.IGNORECASE),
        re.compile(r"\benclosed\b.{0,40}\b(driver'?s license|dec page|"
                   r"loss runs?)\b", re.IGNORECASE),
        re.compile(r"\bhere (is|are) (my|our) (license|documents?)\b",
                   re.IGNORECASE),
    ]),
]

# A real client question with no stronger shape: question mark, or a direct
# ask to the agency.
_GENERAL_QUESTION_PATTERNS = [
    re.compile(r"\?\s*$"),
    re.compile(r"\b(can|could) you\b", re.IGNORECASE),
    re.compile(r"\bplease (help|advise|confirm|check|send|let me know)\b",
               re.IGNORECASE),
    re.compile(r"\bwhat (is|are|does|do|would)\b.{0,40}\b(polic|cover|premium|"
               r"claim)\b", re.IGNORECASE),
]


def _domain_of(email: str) -> str:
    parts = (email or "").strip().lower().split("@")
    return parts[-1] if len(parts) == 2 else ""


def _sender_is_autoresponder(sender: str | None) -> bool:
    local = (sender or "").split("@", 1)[0].lower()
    return any(token in local for token in _AUTOREPLY_SENDERS)


def _sender_is_noise(sender: str | None) -> bool:
    domain = _domain_of(sender)
    return any(domain == n or domain.endswith("." + n)
               for n in _NOISE_SENDERS)


def _sender_is_internal(sender: str | None) -> bool:
    domain = _domain_of(sender)
    return domain in _INTERNAL_DOMAINS


def classify_hello(subject: str, body: str,
                   sender: str | None = None) -> tuple[str, str | None, str]:
    """Classify one hello@ email.

    Returns (action, request_type, reason). request_type is set only for
    GENUINE; reason is a short human-readable explanation for the queue
    report.
    """
    subject = subject or ""
    body = body or ""
    text = f"{subject}\n{body}"

    # 1. Bounces and auto-replies are definitive — they quote original
    #    request language, so they must be checked before request shapes.
    if _BOUNCE_SUBJECT_RE.search(subject):
        return AUTO_REPLY, None, "bounce/delivery-failure subject"
    if _AUTOREPLY_SUBJECT_RE.search(subject):
        return AUTO_REPLY, None, "auto-reply subject"
    if _sender_is_autoresponder(sender):
        return AUTO_REPLY, None, f"autoresponder sender {sender}"
    body_has_genuine = any(
        p.search(body) for _, pats in _REQUEST_TYPE_PATTERNS for p in pats)
    if not body_has_genuine and any(
            p.search(body) for p in _AUTOREPLY_BODY_PATTERNS):
        return AUTO_REPLY, None, "auto-reply body language, no request shape"

    # 2. Newsletters / marketing / system notifications are never requests.
    if any(p.search(text) for p in _NEWSLETTER_PATTERNS):
        return NOISE, None, "newsletter/marketing language"
    if _sender_is_noise(sender):
        return NOISE, None, f"marketing/vendor sender {sender}"
    if any(p.search(text) for p in _SYSTEM_NOTIFICATION_PATTERNS):
        return NOISE, None, "system notification (text/call digest)"

    # 3. Agency-internal senders forwarding into hello@ — real work items,
    #    but already owned by the forwarder; not new client requests.
    if _sender_is_internal(sender):
        return INTERNAL, None, f"internal sender {sender}"

    # 4. Acknowledgements: receipt language means the work is done.
    if any(p.search(text) for p in _ACK_COMPLETION_PATTERNS):
        return ACK, None, "receipt/completion language"
    # A purely polite reply body ("Thank you!") with no request language is
    # an acknowledgement — even when the inherited Re:/Fwd: subject looks
    # like a request. Quoted request language is not a new request (the
    # cert-intake lesson). An original subject carrying a request shape
    # with a bare "thanks" body stays a candidate genuine request.
    was_reply = bool(_WAS_REPLY_RE.match(subject))
    body_has_request = any(
        p.search(body) for _, pats in _REQUEST_TYPE_PATTERNS for p in pats)
    body_polite = any(p.search(text) for p in _ACK_RECEIPT_PATTERNS)
    if body_polite and not body_has_request and was_reply:
        return ACK, None, "polite thank-you reply, no request language"
    typed = None
    for request_type, pats in _REQUEST_TYPE_PATTERNS:
        if any(p.search(text) for p in pats):
            typed = request_type
            break
    if typed:
        return GENUINE, typed, f"matched {typed} request shape"
    if body_polite:
        return ACK, None, "polite thank-you, no request language"

    # 5. A real question to the agency is still a genuine work item.
    if any(p.search(text) for p in _GENERAL_QUESTION_PATTERNS):
        return GENUINE, "general_question", "direct question to the agency"

    return UNKNOWN, None, "no request shape, noise, or ack signal found"


# ---------------------------------------------------------------------------
# Client-identity extraction
# ---------------------------------------------------------------------------

_ENTITY_SUFFIXES = ("llc", "inc", "corp", "ltd", "co", "company", "pllc",
                    "pa", "pc", "lp", "llp")

# "Renewal Quote for Jersey Strong Properties LLC", "Policy change -
# READY 2 ROLL MOVING LLC - 812601-884344-75", "Hearts For Home
# Healthcare, LLC, Policy: PHPK2734817-000". Ordered: the request-"for"
# shape first (so "Hearts *For* Home Healthcare" does not match the bare
# "for" inside a name), then policy-change, then COI-dash, then a leading
# "Name LLC," shape.
_COMPANY_PATTERNS = [
    re.compile(
        r"(?i)\b(?:quote|request|setup|insurance|policy)\s+for\s+"
        r"([A-Z][\w&.,'\- ]{1,60}?"
        r"(?:LLC|Inc\.?|Corp\.?|Ltd\.?|Co\.?|LLP|PLLC|PA|PC))\b"),
    re.compile(
        r"(?i)\bpolicy change\s*[-:]\s*([A-Z][\w&.,'\- ]{1,60}?"
        r"(?:LLC|Inc\.?|Corp\.?|Ltd\.?|Co\.?|LLP|PLLC|PA|PC))\b"),
    re.compile(
        r"(?i)\bcoi\s*[-:]\s*([A-Z][\w&.,'\- ]{1,60}?"
        r"(?:LLC|Inc\.?|Corp\.?|Ltd\.?|Co\.?|LLP|PLLC|PA|PC))\b"),
    re.compile(
        r"^([A-Z][\w&.,'\- ]{1,60}?(?:LLC|Inc\.?|Corp\.?|Ltd\.?|Co\.?|LLP|"
        r"PLLC|PA|PC))\s*[,:\-]",
        re.IGNORECASE),
]

# BP00109727, PHPK2734817-000, U26AC171531-00, 3AB025200,
# 812601-884344-75, 1-HNY-NJ-01-014332
_POLICY_PATTERNS = [
    re.compile(r"\b([A-Z]{2,5}\d{5,}(?:[-/][A-Z0-9]{1,6})?)\b"),
    re.compile(r"\b(\d{3,}-\d{3,}(?:-\d{2,})?)\b"),
    re.compile(r"\b([A-Z0-9]{2,}-[A-Z]{2,}-[A-Z]{2}-\d{2}-\d{4,})\b"),
]

_MC_RE = re.compile(r"\bMC\s*#?\s?(\d{5,8})\b", re.IGNORECASE)


def _clean_company(name: str | None) -> str | None:
    if not name:
        return None
    name = " ".join(name.split()).strip(" -:,")
    # Strip Re:/Fwd: prefixes that leaked in.
    name = re.sub(r"(?i)^(?:re|fwd?)\s*:\s*", "", name).strip()
    return name or None


def extract_hello_identity(subject: str, body: str,
                           sender: str | None = None) -> dict:
    """Extract client-identity anchors from a hello@ email.

    Returns {"sender_email", "sender_name", "company_name",
    "policy_numbers", "mc_numbers"}. Values may be None/empty when the
    email carries no usable anchor — a missing anchor is a hold, never a
    guess.
    """
    subject = subject or ""
    body = body or ""
    sender = (sender or "").strip()

    sender_email = None
    sender_name = None
    m = re.match(r"\s*(.*?)\s*<([^<>@\s]+@[^<>@\s]+)>\s*$", sender)
    if m:
        sender_name = m.group(1).strip().strip('"') or None
        sender_email = m.group(2).strip().lower()
    elif "@" in sender:
        sender_email = sender.strip().lower()

    company = None
    for pat in _COMPANY_PATTERNS:
        hit = pat.search(subject) or pat.search(body)
        if hit:
            company = _clean_company(hit.group(1))
            if company:
                break

    policies: list[str] = []
    for pat in _POLICY_PATTERNS:
        for hit in pat.findall(f"{subject}\n{body}"):
            hit = hit.strip().upper()
            if hit and hit not in policies:
                policies.append(hit)

    mc_numbers = list(dict.fromkeys(_MC_RE.findall(f"{subject}\n{body}")))

    return {
        "sender_email": sender_email,
        "sender_name": sender_name,
        "company_name": company,
        "policy_numbers": policies,
        "mc_numbers": mc_numbers,
    }
