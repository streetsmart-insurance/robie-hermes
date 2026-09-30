"""Plain-English Playground replies in the shared status format.

Summary first, then the Details block, then ``Ref: job <full id>`` last.
Practice mode is a tag on the first line while Buster Brown is the only
client Robie may change.
"""

from __future__ import annotations

from .playground_config import buster_brown_only_mode, confirm_timeout_minutes
from .playground_guardrails import Proposal
from .status_format import render_simple_status

HELP_MENU = """Here's what I can do in the Playground:

- Answer a procedure question and name the document it came from
- Look up a client
- Draft a certificate (I create it; I don't send it)
- Add a note on a discussion that already has a title
- Change an address, a phone number, or an email
- Add a driver or a vehicle
- Email a carrier to request a policy change

I won't delete or cancel anything. I won't bind, issue, reinstate, or non-renew. I won't change billing, a mortgagee, premium, dates, limits, or coverage. I won't email or text a client. I won't read our code or tokens.

Before I change anything, I tell you the client, the field, the old value, and the new value, and I wait for you to say go."""


def tag_practice(text: str) -> str:
    body = str(text or "").strip()
    if not body or not buster_brown_only_mode():
        return body + ("\n" if body and not body.endswith("\n") else "")
    if body.startswith("Practice mode"):
        return body if body.endswith("\n") else body + "\n"
    tagged = "Practice mode\n\n" + body
    return tagged if tagged.endswith("\n") else tagged + "\n"


def status_reply(
    *,
    headline: str,
    what_happened: str,
    anything_needed: str,
    status_line: str,
    details: str = "",
    job_id: str | None = None,
) -> str:
    return tag_practice(
        render_simple_status(
            headline=headline,
            what_happened=what_happened,
            anything_needed=anything_needed,
            status_line=status_line,
            details=details,
            job_id=job_id,
        )
    )


def help_reply(*, job_id: str | None = None) -> str:
    return status_reply(
        headline="Here's what I can and can't do.",
        what_happened="This is the Playground menu.",
        anything_needed="Tell me the task in a sentence.",
        status_line="Waiting for a task.",
        details=HELP_MENU,
        job_id=job_id,
    )


def blocked_reply(*, reason: str, job_id: str) -> str:
    return status_reply(
        headline="I can't do that.",
        what_happened=reason,
        anything_needed="A person has to do that. I won't.",
        status_line="Blocked.",
        details=reason,
        job_id=job_id,
    )


def clarify_reply(*, question: str, job_id: str) -> str:
    return status_reply(
        headline=question,
        what_happened="I need one detail before I can start.",
        anything_needed=question,
        status_line="Waiting for you.",
        details="I didn't guess, and I didn't change anything.",
        job_id=job_id,
    )


def confirmation_reply(proposal: Proposal, *, job_id: str) -> str:
    minutes = confirm_timeout_minutes()
    old = proposal.old_value or "not on file"
    new = str(proposal.extra.get("display_new") or proposal.new_value or "(missing)")
    client = proposal.client or proposal.applicant_id or "that client"
    lines = [
        f"Client: {client}",
        f"Field: {proposal.field}",
        f"Old value: {old}",
        f"New value: {new}",
    ]
    if proposal.subject:
        lines.append(f"Subject: {proposal.subject}")
    lines.append(
        f"If I don't hear go or yes in this thread within {minutes} minutes, I'll cancel this."
    )
    details = "\n".join(lines)
    return status_reply(
        headline="On it. Here is exactly what I will change. Nothing is changed yet.",
        what_happened=f"{client}: {proposal.field} would go from {old} to {new}.",
        anything_needed="Reply go or yes in this thread.",
        status_line="Waiting for you.",
        details=details,
        job_id=job_id,
    )


def working_reply(proposal: Proposal, *, job_id: str) -> str:
    client = proposal.client or proposal.applicant_id or "that client"
    return status_reply(
        headline="On it. Making that change now.",
        what_happened=f"You said go. I'm changing {proposal.field} for {client}.",
        anything_needed="No.",
        status_line="Working.",
        details=f"New value: {proposal.new_value}",
        job_id=job_id,
    )


def matched_reply(proposal: Proposal, *, observed: str, job_id: str) -> str:
    client = proposal.client or proposal.applicant_id or "that client"
    old = proposal.old_value or "not on file"
    return status_reply(
        headline=f"Done. {client}'s {proposal.field} now matches what you asked for.",
        what_happened=f"It was {old}. EZLynx now shows {observed}.",
        anything_needed="No.",
        status_line="Confirmed.",
        details="\n".join(
            [
                f"Client: {client}",
                f"Field: {proposal.field}",
                f"Before: {old}",
                f"After: {observed}",
                "Readback: matches.",
            ]
        ),
        job_id=job_id,
    )


def mismatch_reply(proposal: Proposal, *, observed: str, job_id: str) -> str:
    client = proposal.client or proposal.applicant_id or "that client"
    shown = observed or "nothing came back"
    return status_reply(
        headline="I could not confirm that change. A person needs to look at it.",
        what_happened=(
            f"I expected {proposal.field} for {client} to be {proposal.new_value}. "
            f"EZLynx shows {shown}."
        ),
        anything_needed="A person needs to check EZLynx.",
        status_line="Not confirmed.",
        details="I am not calling this a success.",
        job_id=job_id,
    )


def cancelled_reply(*, reason: str, job_id: str) -> str:
    return status_reply(
        headline=reason,
        what_happened="Nothing was changed.",
        anything_needed="Send it again if you still want it done.",
        status_line="Cancelled.",
        details=reason,
        job_id=job_id,
    )


def sop_reply(*, answer: str, source: str, job_id: str) -> str:
    if not answer:
        return status_reply(
            headline="I don't have that in the procedures I can read.",
            what_happened="No loaded StreetSmart procedure matched that question.",
            anything_needed="No.",
            status_line="No matching procedure.",
            details="I didn't guess.",
            job_id=job_id,
        )
    return status_reply(
        headline=answer,
        what_happened="This comes from a StreetSmart procedure, not a guess.",
        anything_needed="No.",
        status_line="Answered.",
        details=f"Source: {source}" if source else "",
        job_id=job_id,
    )


def lookup_reply(*, found: str, job_id: str) -> str:
    if not found:
        return status_reply(
            headline="I couldn't look that up.",
            what_happened="EZLynx didn't return a value for that.",
            anything_needed="No.",
            status_line="Not found.",
            details="I didn't guess.",
            job_id=job_id,
        )
    return status_reply(
        headline=found,
        what_happened="This is what EZLynx returned.",
        anything_needed="No.",
        status_line="Looked up.",
        details=found,
        job_id=job_id,
    )
