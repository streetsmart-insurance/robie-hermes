"""Plain-English Playground replies in the shared status format.

Summary first, then the Details block, then ``Ref: job <full id>`` last.
Practice mode is a tag on the first line for a test client. A real client
is not tagged once all-clients mode is actually on.
"""

from __future__ import annotations

from .playground_config import buster_brown_only_mode, confirm_timeout_minutes
from .playground_guardrails import Proposal
from .playground_voice import line
from .status_format import render_simple_status


def _remembered(memory_lines: list[str] | None) -> str:
    facts = [str(item).strip() for item in (memory_lines or []) if str(item).strip()]
    if not facts:
        return ""
    return "I remember:\n" + "\n".join(facts[:3])


def tag_practice(text: str, *, applicant_id: str = "", client_name: str = "") -> str:
    body = str(text or "").strip()
    if not body or not buster_brown_only_mode(applicant_id, client_name):
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
    applicant_id: str = "",
    client_name: str = "",
) -> str:
    return tag_practice(
        render_simple_status(
            headline=headline,
            what_happened=what_happened,
            anything_needed=anything_needed,
            status_line=status_line,
            details=details,
            job_id=job_id,
        ),
        applicant_id=applicant_id,
        client_name=client_name,
    )


def help_reply(*, job_id: str | None = None) -> str:
    return status_reply(
        headline="Here's what I can and can't do.",
        what_happened="This is the Playground menu.",
        anything_needed="Tell me the task in a sentence.",
        status_line="Waiting for a task.",
        details=line("help_menu"),
        job_id=job_id,
    )


def blocked_reply(
    *,
    reason: str,
    job_id: str,
    applicant_id: str = "",
    client_name: str = "",
) -> str:
    return status_reply(
        headline=line("blocked_headline"),
        what_happened=reason,
        anything_needed="A person has to do that. I won't.",
        status_line="Blocked.",
        details=reason,
        job_id=job_id,
        applicant_id=applicant_id,
        client_name=client_name,
    )


def clarify_reply(*, question: str, job_id: str, memory_lines: list[str] | None = None) -> str:
    details = "I didn't guess, and I didn't change anything."
    remembered = _remembered(memory_lines)
    if remembered:
        details = details + "\n" + remembered
    return status_reply(
        headline=question,
        what_happened="I need one detail before I can start.",
        anything_needed=question,
        status_line="Waiting for you.",
        details=details,
        job_id=job_id,
    )


def confirmation_reply(
    proposal: Proposal,
    *,
    job_id: str,
    memory_lines: list[str] | None = None,
) -> str:
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
    remembered = _remembered(memory_lines)
    if remembered:
        lines.append(remembered)
    details = "\n".join(lines)
    return status_reply(
        headline="On it. Here is exactly what I will change. Nothing is changed yet.",
        what_happened=f"{client}: {proposal.field} would go from {old} to {new}.",
        anything_needed="Reply go or yes in this thread.",
        status_line="Waiting for you.",
        details=details,
        job_id=job_id,
        applicant_id=proposal.applicant_id,
        client_name=proposal.client,
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
        applicant_id=proposal.applicant_id,
        client_name=proposal.client,
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
        applicant_id=proposal.applicant_id,
        client_name=proposal.client,
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
        applicant_id=proposal.applicant_id,
        client_name=proposal.client,
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


def memory_saved_reply(*, fact: str, scope: str, job_id: str) -> str:
    who = "the whole team" if scope == "team" else "you"
    return status_reply(
        headline=line("memory_saved_headline"),
        what_happened=f"I saved this for {who}.",
        anything_needed="No.",
        status_line="Remembered.",
        details=(
            f"{fact}\n"
            "This does not override a block or the write allowlist."
        ),
        job_id=job_id,
    )


def memory_refused_reply(*, job_id: str) -> str:
    return status_reply(
        headline=line("memory_refused_headline"),
        what_happened="That looks like a password, token, or payment detail.",
        anything_needed="Leave secrets out of chat and email.",
        status_line="Not stored.",
        details="I did not store it, and I will not repeat it.",
        job_id=job_id,
    )


def memory_forgotten_reply(*, facts: list[str], job_id: str) -> str:
    if not facts:
        return status_reply(
            headline=line("memory_none_headline"),
            what_happened="Nothing stored matched that.",
            anything_needed="No.",
            status_line="Nothing forgotten.",
            details="I didn't guess, and I didn't change anything.",
            job_id=job_id,
        )
    return status_reply(
        headline=line("memory_forgotten_headline"),
        what_happened="I hid the matching notes. Past EZLynx changes stay in the change list.",
        anything_needed="No.",
        status_line="Forgotten.",
        details="\n".join(facts),
        job_id=job_id,
    )


def memory_list_reply(*, about: str, facts: list[str], job_id: str) -> str:
    if not facts:
        return status_reply(
            headline=line("memory_none_headline"),
            what_happened="Nothing stored matched that." if about else "I don't have any notes yet.",
            anything_needed="No.",
            status_line="Nothing stored.",
            details=about or "I didn't guess.",
            job_id=job_id,
        )
    base = line("memory_list_headline").rstrip(".")
    headline = f"{base} about {about}." if about else f"{base}."
    return status_reply(
        headline=headline,
        what_happened="These are the notes I have. They do not change what I am allowed to do.",
        anything_needed="No.",
        status_line="Remembered.",
        details="\n".join(facts),
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
