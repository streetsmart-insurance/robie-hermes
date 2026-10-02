"""Code guardrails for the Playground. Prompts are not the enforcement.

Blocked requests never become a write. Allowed writes still wait for go.
A vague ask gets one question. Missing fields are not filled in by guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field as dataclass_field

from .answer_only import is_vague_short_request
from .playground_config import BUSTER_BROWN_APPLICANT_ID, practice_applicant_ids

HELP = "help"
STOP = "stop"
GO = "go"
VAGUE = "vague"
BLOCKED = "blocked"
SOP = "sop"
LOOKUP = "lookup"
CERT_DRAFT = "cert_draft"
NOTE = "note"
SIMPLE_EDIT = "simple_edit"
ADD_DRIVER = "add_driver"
ADD_VEHICLE = "add_vehicle"
CARRIER_EMAIL = "carrier_email"
REMEMBER = "remember"
FORGET = "forget"
RECALL = "recall"

ALLOWED_WRITES = frozenset(
    {CERT_DRAFT, NOTE, SIMPLE_EDIT, ADD_DRIVER, ADD_VEHICLE, CARRIER_EMAIL}
)

_MENTION = re.compile(r"^@\s*robie\b[:,\s]*", re.IGNORECASE)
_HELP = re.compile(
    r"^(?:help|what can you do|what do you do|what can i ask|menu|capabilities)\??$"
)
_GO = re.compile(
    r"^(?:yes|yes please|yes go|go|go ahead|approved|approve|confirm)[.!]?$"
)
_STOP_EXACT = frozenset(
    {
        "stop",
        "stop now",
        "stop it",
        "cancel that",
        "cancel the job",
        "cancel that job",
        "cancel this job",
    }
)

_DELETE = re.compile(
    r"\b(?:delete|remove|erase|drop)\b.{0,32}\b(?:polic(?:y|ies)|client|customer|"
    r"applicant|driver|vehicle|document|note)s?\b"
    r"|\bcancel\b.{0,24}\b(?:the |this |that )?(?:polic(?:y|ies)|client|customer|"
    r"applicant|driver|vehicle|document|note)s?\b(?! change)",
    re.IGNORECASE,
)
_BIND = re.compile(
    r"\b(?:bind|reinstate|non-?renew)\b"
    r"|\bissue\b.{0,24}\b(?:policy|binder|coverage)\b",
    re.IGNORECASE,
)
_MONEY = re.compile(
    r"\b(?:change|update|add|remove|set|make|take)\b.{0,40}\b(?:payments?|billing|"
    r"mortgagee|mortgage|escrow)\b"
    r"|\b(?:payments?|billing|mortgagee|mortgage|escrow)\b.{0,24}\b(?:change|update|to)\b",
    re.IGNORECASE,
)
_COVERAGE = re.compile(
    r"\b(?:change|update|set|increase|decrease|raise|lower|endorse)\b.{0,48}\b"
    r"(?:premium|effective date|effective-date|limits?|coverage)\b"
    r"|\b(?:premium|effective date|effective-date|limits?|coverage)\b.{0,32}\b"
    r"(?:change|update|to)\b",
    re.IGNORECASE,
)
_CLIENT_MESSAGE = re.compile(
    r"\b(?:e-?mail|text|sms)\s+(?:the\s+|our\s+|a\s+)?(?:client|insured|customer|applicant|policyholder)\b"
    r"|\b(?:send|message)\s+(?:the\s+|our\s+|a\s+)?(?:client|insured|customer|policyholder)\b"
    r"|\b(?:client|insured|customer|policyholder)\b.{0,24}\b(?:an?\s+)?(?:e-?mail|text|sms)\b",
    re.IGNORECASE,
)
_SOURCE = re.compile(
    r"\b(?:source code|jobs\.db|google_token|token file|api keys?|\.env)\b"
    r"|\b(?:read|open|show|cat|grep|print)\b.{0,40}\b(?:source|tokens?|jobs\.db)\b",
    re.IGNORECASE,
)
_SEND_CERT = re.compile(
    r"\b(?:send|e-?mail|text)\b.{0,40}\b(?:certificate of insurance|certificates?|cert\b|cois?)\b"
    r"|\b(?:certificate of insurance|certificates?|cert\b|cois?)\b.{0,32}\b"
    r"(?:to the holder|to the client|to the insured)\b",
    re.IGNORECASE,
)
_NOTE = re.compile(
    r"\b(?:file|add|leave|put)\b.{0,24}\bnotes?\b|\bnotes?\b.{0,24}\bdiscussion\b",
    re.IGNORECASE,
)
_UNTITLED = re.compile(
    r"\b(?:untitled|new discussion|create a discussion|start a discussion)\b",
    re.IGNORECASE,
)
_CERT = re.compile(
    r"\b(?:certificate of insurance|certificates?|cert request|cois?)\b",
    re.IGNORECASE,
)
_CARRIER_EMAIL = re.compile(
    r"\b(?:e-?mail|send)\b.{0,60}\bcarrier\b|\bcarrier\b.{0,40}\bat\s+\S+@\S+",
    re.IGNORECASE,
)
_ADDRESS = re.compile(
    r"\b(?:mailing address|garaging address|street address|address)\b",
    re.IGNORECASE,
)
_PHONE = re.compile(r"\bphone(?: number)?\b", re.IGNORECASE)
_EMAIL_FIELD = re.compile(
    r"\b(?:change|update|set|correct)\b.{0,32}\b(?:e-?mail address|e-?mail)\b",
    re.IGNORECASE,
)
_ADD_DRIVER = re.compile(r"\badd\b.{0,24}\bdrivers?\b", re.IGNORECASE)
_ADD_VEHICLE = re.compile(r"\badd\b.{0,24}\b(?:vehicles?|cars?)\b", re.IGNORECASE)
_LOOKUP = re.compile(
    r"\b(?:look up|lookup|find|what(?:'s| is)|show me)\b",
    re.IGNORECASE,
)
_WRITE_VERB = re.compile(
    r"\b(?:change|update|delete|remove|send|e-?mail|text|bind|add|issue|draft|"
    r"file|create|pay|cancel|set|correct|upload|endorse|reinstate|quotes?)\b",
    re.IGNORECASE,
)
_EDIT_VERB = re.compile(r"\b(?:change|update|set|correct)\b", re.IGNORECASE)
_EMAIL_ADDRESS = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_APPLICANT = re.compile(
    r"\b(?:applicant(?:\s+id)?|account(?:\s+id)?)\s*#?\s*([1-9]\d{4,})\b",
    re.IGNORECASE,
)
_FROM_TO = re.compile(
    r"\bfrom\s+(.+?)\s+to\s+(.+?)(?:\s+for\b|$)",
    re.IGNORECASE,
)
_TO_VALUE = re.compile(r"\bto\s+(.+?)(?:\s+for\b|$)", re.IGNORECASE)
_CLIENT_FOR = re.compile(r"\bfor\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b")
_CLIENT_POSS = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)'s\b")
_DISCUSSION = re.compile(
    r"\bexisting\s+(.+?)\s+discussion\b"
    r"|\bdiscussion\s+titled\s+[\"']?(.+?)[\"']?(?:\s+saying|\s+for|\s*$)"
    r"|\bon the\s+[\"'](.+?)[\"']\s+discussion\b",
    re.IGNORECASE,
)
_HOLDER = re.compile(
    r"\bholder\s+(.+?)(?:,|\s+applicant\b|$)",
    re.IGNORECASE,
)
_REMEMBER = re.compile(
    r"^(?:please\s+)?remember(?:\s+(?P<target>for\s+.+?))?\s+that\s+(?P<fact>.+)$",
    re.IGNORECASE | re.DOTALL,
)
_REMEMBER_TEAM = re.compile(r"for the team", re.IGNORECASE)
_REMEMBER_AGENCY = re.compile(r"for the agency|for everyone", re.IGNORECASE)
_REMEMBER_NAMED_TEAM = re.compile(
    r"for the (Commercial|Personal|Trucking) team",
    re.IGNORECASE,
)
_REMEMBER_CLIENT = re.compile(
    r"for (.+?) applicant (\d{5,})",
    re.IGNORECASE,
)
_REMEMBER_CLIENT_NAME = re.compile(r"for ([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)$")
_FORGET = re.compile(
    r"^(?:please\s+)?forget(?:\s+that\s+)(.+)$",
    re.IGNORECASE | re.DOTALL,
)
_RECALL = re.compile(
    r"^what do you remember(?:\s+about\s+(.+))?$",
    re.IGNORECASE | re.DOTALL,
)
_REMEMBER_BARE = re.compile(r"^(?:please\s+)?remember[.!]?$", re.IGNORECASE)
_FORGET_BARE = re.compile(r"^(?:please\s+)?forget(?:\s+that)?[.!]?$", re.IGNORECASE)


@dataclass
class Proposal:
    kind: str
    client: str = ""
    applicant_id: str = ""
    field: str = ""
    old_value: str = ""
    new_value: str = ""
    discussion_title: str = ""
    carrier_address: str = ""
    holder: str = ""
    subject: str = ""
    body: str = ""
    requested_by: str = ""
    requested_at: str = ""
    extra: dict = dataclass_field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "client": self.client,
            "applicant_id": self.applicant_id,
            "field": self.field,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "discussion_title": self.discussion_title,
            "carrier_address": self.carrier_address,
            "holder": self.holder,
            "subject": self.subject,
            "body": self.body,
            "requested_by": self.requested_by,
            "requested_at": self.requested_at,
            "redirected": bool(self.extra.get("redirected")),
            "original_to": str(self.extra.get("original_to") or ""),
            "display_new": str(self.extra.get("display_new") or ""),
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "Proposal":
        raw = dict(data or {})
        proposal = cls(
            kind=str(raw.get("kind") or ""),
            client=str(raw.get("client") or ""),
            applicant_id=str(raw.get("applicant_id") or ""),
            field=str(raw.get("field") or ""),
            old_value=str(raw.get("old_value") or ""),
            new_value=str(raw.get("new_value") or ""),
            discussion_title=str(raw.get("discussion_title") or ""),
            carrier_address=str(raw.get("carrier_address") or ""),
            holder=str(raw.get("holder") or ""),
            subject=str(raw.get("subject") or ""),
            body=str(raw.get("body") or ""),
            requested_by=str(raw.get("requested_by") or ""),
            requested_at=str(raw.get("requested_at") or ""),
        )
        proposal.extra = {
            "redirected": bool(raw.get("redirected")),
            "original_to": str(raw.get("original_to") or ""),
            "display_new": str(raw.get("display_new") or ""),
        }
        return proposal


@dataclass(frozen=True)
class Decision:
    intent: str
    blocked: bool = False
    code: str = ""
    reason: str = ""
    question: str = ""
    proposal: Proposal | None = None


def normalize(text: str) -> str:
    body = str(text or "").strip()
    if body.lower().startswith("subject:") and "\n\n" in body:
        body = body.split("\n\n", 1)[1]
    body = _MENTION.sub("", body.strip())
    return " ".join(body.casefold().split())


def is_playground_stop(text: str) -> bool:
    from .chat_turn_control import is_stop_command

    if is_stop_command(text):
        return True
    return normalize(text).strip(" .!") in _STOP_EXACT


def is_go(text: str) -> bool:
    return _GO.match(normalize(text).strip()) is not None


def is_help(text: str) -> bool:
    return _HELP.match(normalize(text).strip(" .!")) is not None


def mentioned_client(text: str) -> str:
    return _client_name(str(text or ""))


def _command_text(original: str) -> str:
    body = str(original or "").strip()
    if body.lower().startswith("subject:") and "\n\n" in body:
        body = body.split("\n\n", 1)[1].strip()
    body = _MENTION.sub("", body.strip())
    return " ".join(body.split())


def _apply_remember_target(target: str, proposal: Proposal) -> None:
    target = " ".join(str(target or "").split())
    if not target:
        return
    named_team = _REMEMBER_NAMED_TEAM.fullmatch(target)
    if named_team:
        proposal.field = "team"
        proposal.subject = named_team.group(1).title()
        return
    if _REMEMBER_TEAM.fullmatch(target):
        proposal.field = "team"
        return
    if _REMEMBER_AGENCY.fullmatch(target):
        proposal.field = "agency"
        return
    client = _REMEMBER_CLIENT.fullmatch(target)
    if client:
        proposal.field = "client"
        proposal.client = " ".join(client.group(1).split())
        proposal.applicant_id = client.group(2)
        return
    named = _REMEMBER_CLIENT_NAME.fullmatch(target)
    if named:
        proposal.field = "client"
        proposal.client = named.group(1)


def _memory_command(original: str) -> Decision | None:
    text = _command_text(original)
    if _REMEMBER_BARE.match(text):
        return Decision(REMEMBER, question="What should I remember?")
    if _FORGET_BARE.match(text):
        return Decision(FORGET, question="What should I forget?")
    remember = _REMEMBER.match(text)
    if remember:
        fact = " ".join((remember.group("fact") or "").split()).strip()
        proposal = Proposal(kind=REMEMBER, body=fact)
        _apply_remember_target(remember.group("target") or "", proposal)
        return Decision(REMEMBER, proposal=proposal)
    forget = _FORGET.match(text)
    if forget:
        fact = " ".join(forget.group(1).split()).strip(" .")
        return Decision(FORGET, proposal=Proposal(kind=FORGET, body=fact))
    recall = _RECALL.match(text.rstrip(" ?.!"))
    if recall:
        about = " ".join((recall.group(1) or "").split()).strip(" ?.!")
        return Decision(RECALL, proposal=Proposal(kind=RECALL, body=about, field=about))
    return None


def _client_name(original: str) -> str:
    match = _CLIENT_FOR.search(original)
    if match:
        return match.group(1).strip()
    match = _CLIENT_POSS.search(original)
    if match:
        return match.group(1).strip()
    if re.search(r"\bbuster brown\b", original, re.IGNORECASE):
        return "Buster Brown"
    return ""


def _applicant_id(original: str, client: str) -> str:
    match = _APPLICANT.search(original)
    if match:
        return match.group(1)
    if client.casefold() == "buster brown":
        ids = sorted(practice_applicant_ids())
        for applicant in ids:
            if applicant == BUSTER_BROWN_APPLICANT_ID:
                return applicant
        return BUSTER_BROWN_APPLICANT_ID
    return ""


def _from_to(original: str) -> tuple[str, str]:
    match = _FROM_TO.search(original)
    if not match:
        return "", ""
    return match.group(1).strip(" ."), match.group(2).strip(" .")


def _new_value(original: str) -> str:
    old, new = _from_to(original)
    if new:
        return new
    match = _TO_VALUE.search(original)
    if not match:
        return ""
    value = match.group(1).strip(" .")
    value = re.split(r",|\s+applicant\b|\s+saying\b", value, maxsplit=1, flags=re.I)[0]
    return value.strip(" .")


def _discussion_title(original: str) -> str:
    match = _DISCUSSION.search(original)
    if not match:
        return ""
    title = next(group for group in match.groups() if group)
    return " ".join(title.split()).strip(" .\"'")


def _is_question(norm: str) -> bool:
    return norm.endswith("?") or norm.startswith(
        ("how ", "what ", "when ", "where ", "why ", "who ", "which ")
    )


def _is_reply_request(norm: str) -> bool:
    """Detect a request for Robie to reply (not to send an email to someone else).

    "Please reply to this email" = the user wants a reply, allow it.
    "Email this to jake@" = the user wants Robie to send an email, block it.
    """
    # Explicit reply requests: reply/respond to this thread/email/message
    if re.search(
        r"\b(?:please\s+)?(?:reply|respond)\s+(?:to\s+)?(?:this\s+)?(?:email|thread|message)\b",
        norm,
        re.IGNORECASE,
    ):
        return True
    return False


def _blocked(norm: str) -> tuple[str, str] | None:
    if _SOURCE.search(norm):
        return (
            "read_source_or_tokens",
            "I can't read source code, job files, or tokens.",
        )
    if _DELETE.search(norm):
        return (
            "delete_or_cancel",
            "I can't delete or cancel a policy, client, driver, vehicle, document, or note.",
        )
    if _BIND.search(norm):
        return (
            "bind_issue_reinstate_nonrenew",
            "I can't bind, issue, reinstate, or non-renew a policy.",
        )
    if _MONEY.search(norm):
        return (
            "payment_billing_mortgagee",
            "I can't change payments, billing, or a mortgagee.",
        )
    if _COVERAGE.search(norm):
        return (
            "premium_effective_limit_coverage",
            "I can't change premium, an effective date, a limit, or coverage.",
        )
    if _CLIENT_MESSAGE.search(norm):
        return (
            "client_email_or_text",
            "I can't email or text a client.",
        )
    if _SEND_CERT.search(norm):
        return (
            "send_certificate",
            "I can draft a certificate. I can't send it.",
        )
    if _UNTITLED.search(norm) or (
        _NOTE.search(norm) and not _discussion_title(norm) and not _is_question(norm)
    ):
        return (
            "untitled_note",
            "I only add notes on a discussion that already has a title. I won't start a new one.",
        )
    return None


def _base_proposal(kind: str, original: str) -> Proposal:
    client = _client_name(original)
    return Proposal(
        kind=kind,
        client=client,
        applicant_id=_applicant_id(original, client),
    )


def _missing_client(proposal: Proposal) -> str:
    if proposal.client or proposal.applicant_id:
        return ""
    return "Which client is this for?"


def _match_allowed(original: str, norm: str) -> Decision | None:
    if _CARRIER_EMAIL.search(norm):
        proposal = _base_proposal(CARRIER_EMAIL, original)
        proposal.field = "carrier email"
        proposal.old_value = "not sent"
        address = _EMAIL_ADDRESS.search(original)
        proposal.carrier_address = address.group(0) if address else ""
        proposal.new_value = proposal.carrier_address
        proposal.subject = "Policy change request"
        proposal.body = " ".join(original.split())
        if not proposal.carrier_address:
            return Decision(VAGUE, question="What email address should the carrier get?")
        question = _missing_client(proposal)
        if question:
            return Decision(VAGUE, question=question, proposal=proposal)
        return Decision(CARRIER_EMAIL, proposal=proposal)

    if _CERT.search(norm) and re.search(r"\b(?:draft|create|prepare|make)\b", norm):
        proposal = _base_proposal(CERT_DRAFT, original)
        proposal.field = "certificate draft"
        proposal.old_value = "none"
        holder = _HOLDER.search(original)
        proposal.holder = holder.group(1).strip(" .") if holder else ""
        proposal.new_value = proposal.holder or "draft only, not sent"
        question = _missing_client(proposal)
        if question:
            return Decision(VAGUE, question=question, proposal=proposal)
        if not proposal.holder:
            return Decision(VAGUE, question="Who should the certificate name as the holder?")
        return Decision(CERT_DRAFT, proposal=proposal)

    if _NOTE.search(norm) and not _is_question(norm):
        title = _discussion_title(original)
        if not title:
            return Decision(
                BLOCKED,
                blocked=True,
                code="untitled_note",
                reason="I only add notes on a discussion that already has a title. I won't start a new one.",
            )
        proposal = _base_proposal(NOTE, original)
        proposal.field = "note"
        proposal.discussion_title = title
        proposal.old_value = "none"
        proposal.new_value = f"note on {title}"
        question = _missing_client(proposal)
        if question:
            return Decision(VAGUE, question=question, proposal=proposal)
        return Decision(NOTE, proposal=proposal)

    if _ADD_DRIVER.search(norm):
        proposal = _base_proposal(ADD_DRIVER, original)
        proposal.field = "driver"
        proposal.old_value = "none"
        name = re.search(r"\bdrivers?\s+(.+?)(?:\s+for\b|$)", original, re.IGNORECASE)
        proposal.new_value = name.group(1).strip(" .") if name else ""
        if not proposal.new_value:
            return Decision(VAGUE, question="What is the driver's name?")
        question = _missing_client(proposal)
        if question:
            return Decision(VAGUE, question=question, proposal=proposal)
        return Decision(ADD_DRIVER, proposal=proposal)

    if _ADD_VEHICLE.search(norm):
        proposal = _base_proposal(ADD_VEHICLE, original)
        proposal.field = "vehicle"
        proposal.old_value = "none"
        name = re.search(
            r"\b(?:vehicles?|cars?)\s+(.+?)(?:\s+for\b|$)",
            original,
            re.IGNORECASE,
        )
        proposal.new_value = name.group(1).strip(" .") if name else ""
        if not proposal.new_value:
            return Decision(VAGUE, question="Which vehicle should I add?")
        question = _missing_client(proposal)
        if question:
            return Decision(VAGUE, question=question, proposal=proposal)
        return Decision(ADD_VEHICLE, proposal=proposal)

    if _EDIT_VERB.search(norm) and (_ADDRESS.search(norm) or _PHONE.search(norm) or _EMAIL_FIELD.search(norm)):
        proposal = _base_proposal(SIMPLE_EDIT, original)
        if _PHONE.search(norm) and not _ADDRESS.search(norm):
            proposal.field = "phone"
        elif _EMAIL_FIELD.search(norm) and not _ADDRESS.search(norm):
            proposal.field = "email"
        elif "garaging" in norm:
            proposal.field = "garaging address"
        else:
            proposal.field = "mailing address"
        old, new = _from_to(original)
        proposal.old_value = old
        proposal.new_value = new or _new_value(original)
        if proposal.field == "email" and "@" not in proposal.new_value:
            return Decision(VAGUE, question="What should the new email address be?")
        if not proposal.new_value:
            return Decision(VAGUE, question=f"What should the new {proposal.field} be?")
        question = _missing_client(proposal)
        if question:
            return Decision(VAGUE, question=question, proposal=proposal)
        return Decision(SIMPLE_EDIT, proposal=proposal)

    if _LOOKUP.search(original) and not _WRITE_VERB.search(norm):
        proposal = _base_proposal(LOOKUP, original)
        return Decision(LOOKUP, proposal=proposal)
    return None


def classify_playground_request(text: str) -> Decision:
    """Classify one Playground message. Blocked wins over any allowed reading."""
    original = str(text or "").strip()
    if original.lower().startswith("subject:") and "\n\n" in original:
        original = original.split("\n\n", 1)[1].strip()
    norm = normalize(original)
    if not norm:
        return Decision(VAGUE, question="What should I do? Name the client and the change.")
    if is_help(original):
        return Decision(HELP)
    if is_playground_stop(original):
        return Decision(STOP)
    if is_go(original):
        return Decision(GO)
    memory = _memory_command(original)
    if memory is not None:
        return memory
    blocked = _blocked(norm)
    if blocked:
        code, reason = blocked
        return Decision(BLOCKED, blocked=True, code=code, reason=reason)
    if is_vague_short_request(original):
        return Decision(
            VAGUE,
            question="What should I do? Name the client and the task you want finished.",
        )
    allowed = _match_allowed(original, norm)
    if allowed is not None:
        return allowed
    if _LOOKUP.search(original):
        return Decision(LOOKUP, proposal=_base_proposal(LOOKUP, original))
    # Reply requests are not write actions (2026-10-02 fix: "reply to this
    # email" was blocked because "email" matched the write-verb list).
    if _is_reply_request(norm):
        return Decision(SOP, proposal=_base_proposal(SOP, original))
    if _WRITE_VERB.search(norm) and not _is_question(norm):
        return Decision(
            BLOCKED,
            blocked=True,
            code="unknown_write",
            reason="I can't do that from the Playground. I only handle the short list in help.",
        )
    return Decision(SOP, proposal=_base_proposal(SOP, original))
