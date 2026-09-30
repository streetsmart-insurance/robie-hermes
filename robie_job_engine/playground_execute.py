"""Deterministic Playground writes, carrier redirect, and EZLynx readback.

Gemini is not used here. Jev is not used here. A lookup either matches
the value we were told to write, or it does not. A mismatch is not success.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Any, Callable

from .playground_config import (
    carrier_must_redirect,
    carrier_sink_address,
    live_writes_enabled,
    write_allowed,
)
from .playground_guardrails import CARRIER_EMAIL, Proposal

SessionFactory = Callable[[str], Any]


@dataclass
class Readback:
    matched: bool
    observed: str
    human_required: bool
    detail: str

    @property
    def result(self) -> str:
        if self.matched:
            return "matched"
        if self.human_required:
            return "mismatch"
        return "not_checked"


@dataclass
class ApplyResult:
    applied: bool
    detail: str = ""
    observed: str = ""
    note_id: str = ""
    message_id: str = ""
    discussions: list[dict[str, Any]] | None = None


def normalize_value(value: str | None) -> str:
    return " ".join(str(value or "").casefold().split())


def compare_readback(*, expected: str, observed: str | None) -> Readback:
    """Anchor the end state. Missing or different is not a success."""
    if observed is None or str(observed).strip() == "":
        return Readback(
            matched=False,
            observed="",
            human_required=True,
            detail="EZLynx did not return a value.",
        )
    if normalize_value(expected) == normalize_value(observed):
        return Readback(
            matched=True,
            observed=str(observed).strip(),
            human_required=False,
            detail="matches",
        )
    return Readback(
        matched=False,
        observed=str(observed).strip(),
        human_required=True,
        detail=f"expected {expected} but EZLynx shows {observed}",
    )


def prepare_carrier_delivery(proposal: Proposal) -> Proposal:
    """Redirect practice and non-allowlisted carrier mail. Never the real carrier."""
    if proposal.kind != CARRIER_EMAIL:
        return proposal
    original = proposal.carrier_address
    if not carrier_must_redirect(proposal.applicant_id):
        return replace(proposal, extra={**proposal.extra, "redirected": False, "original_to": original})
    sink = carrier_sink_address()
    prefix = f"[PRACTICE - would have gone to {original}]"
    subject = proposal.subject or "Policy change request"
    if not subject.startswith("[PRACTICE - would have gone to "):
        subject = f"{prefix} {subject}".strip()
    return replace(
        proposal,
        subject=subject,
        new_value=sink,
        extra={
            **proposal.extra,
            "redirected": True,
            "original_to": original,
            "delivered_to": sink,
        },
    )


def policy_change_discussion(discussions: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """An existing titled Policy Change Request discussion. Never an untitled one."""
    for item in discussions or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("Title") or item.get("Subject") or "").strip()
        if not title:
            continue
        if "policy change request" in title.casefold():
            return item
    return None


def policy_change_note(proposal: Proposal, *, observed: str) -> str:
    client = proposal.client or proposal.applicant_id or "the client"
    before = proposal.old_value or "not on file"
    after = observed or proposal.new_value
    return (
        f"Playground: {proposal.field} for {client} was {before}. "
        f"It now shows {after}."
    )


def refuse_if_not_allowlisted(proposal: Proposal) -> str | None:
    if proposal.kind in {"sop", "lookup", "help"}:
        return None
    if not proposal.applicant_id:
        return "I don't have an EZLynx applicant id for that client, so I didn't change anything."
    if not write_allowed(proposal.applicant_id):
        return (
            "That client is not open for Playground writes. "
            "I can only change the practice client until real clients are turned on."
        )
    return None


def default_apply(proposal: Proposal) -> ApplyResult:
    """Live apply. Off unless ROBIE_PLAYGROUND_LIVE_WRITES is set.

    Notes go through DiscussionApi. Carrier mail uses the redirected address.
    Field edits stay refused until a field writer is connected, so we never
    claim an address or driver change we did not read back.
    """
    if not live_writes_enabled():
        return ApplyResult(
            applied=False,
            detail="Live writes are off, so I didn't change anything.",
        )
    if proposal.kind == "note":
        return _live_note(proposal)
    if proposal.kind == CARRIER_EMAIL:
        return _live_carrier_email(proposal)
    if proposal.kind == "cert_draft":
        return _live_cert_draft(proposal)
    return ApplyResult(
        applied=False,
        detail="That field writer is not connected, so I didn't change it.",
    )


def with_ezlynx_lock(text: str, session_for: SessionFactory | None = None):
    """The existing one-driver EZLynx lock. Questions do not take it."""
    if session_for is not None:
        return session_for(text)
    from .email_dispatch import ezlynx_write_session

    return ezlynx_write_session(text)


def _live_note(proposal: Proposal) -> ApplyResult:
    title = proposal.discussion_title.strip()
    if not title:
        return ApplyResult(applied=False, detail="That discussion has no title, so I didn't file a note.")
    from .ezlynx_api_only_writes import add_note_to_discussion

    with with_ezlynx_lock(proposal.new_value or title):
        filed = add_note_to_discussion(
            proposal.applicant_id,
            proposal.new_value or proposal.body or "Playground note",
            discussion_title=title,
        )
    note_id = str((filed or {}).get("note_id") or (filed or {}).get("ezlynx_note_id") or "")
    status = str((filed or {}).get("status") or "")
    if status != "filed" or not note_id:
        return ApplyResult(applied=False, detail="The note was not filed.")
    return ApplyResult(applied=True, detail="filed", observed=proposal.new_value, note_id=note_id)


def _live_carrier_email(proposal: Proposal) -> ApplyResult:
    prepared = prepare_carrier_delivery(proposal)
    recipient = str(prepared.extra.get("delivered_to") or prepared.carrier_address or "")
    if prepared.extra.get("redirected"):
        recipient = str(prepared.extra.get("delivered_to") or carrier_sink_address())
    from .verification_mailer import send_verification_email

    receipt = send_verification_email(
        to=[recipient],
        cc=[],
        subject=prepared.subject,
        text_body=prepared.body or "Policy change request.",
        html_body=None,
        plain_only=True,
    )
    message_id = str((receipt or {}).get("message_id") or "")
    if not message_id:
        return ApplyResult(applied=False, detail="The carrier email did not send.")
    return ApplyResult(
        applied=True,
        detail="sent",
        observed=recipient,
        message_id=message_id,
    )


def _live_cert_draft(proposal: Proposal) -> ApplyResult:
    """Create the draft text. Do not send it."""
    if os.environ.get("ROBIE_PLAYGROUND_SEND_CERTS", "").strip():
        return ApplyResult(applied=False, detail="Certificates are drafts. I won't send one.")
    draft = (
        f"Certificate draft for {proposal.client or proposal.applicant_id}. "
        f"Holder: {proposal.holder}. Not sent."
    )
    return ApplyResult(applied=True, detail="draft created", observed=draft)
