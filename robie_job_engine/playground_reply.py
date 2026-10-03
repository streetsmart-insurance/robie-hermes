"""Plain-English Playground replies.

One short outcome or one question. Job ids stay in the ledger.
Practice mode is a prefix for a test client. A real client is not tagged
once all-clients mode is actually on.
"""

from __future__ import annotations

from .playground_config import buster_brown_only_mode, confirm_timeout_minutes
from .playground_execute import capability_menu
from .playground_guardrails import Proposal
from .playground_voice import line
from .user_reply import format_user_reply


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
    tagged = "Practice mode. " + body
    return tagged if tagged.endswith("\n") else tagged + "\n"


def status_reply(
    *,
    headline: str,
    what_happened: str = "",
    anything_needed: str = "",
    status_line: str = "",
    details: str = "",
    job_id: str | None = None,
    applicant_id: str = "",
    client_name: str = "",
) -> str:
    """One plain sentence. Extra sections and the job id are not shown."""
    del what_happened, anything_needed, status_line, details, job_id
    sentence = format_user_reply(headline)
    return tag_practice(
        sentence,
        applicant_id=applicant_id,
        client_name=client_name,
    )


def help_reply(*, job_id: str | None = None) -> str:
    del job_id
    return tag_practice(capability_menu())


def blocked_reply(
    *,
    reason: str,
    job_id: str,
    applicant_id: str = "",
    client_name: str = "",
) -> str:
    sentence = str(reason or "").strip() or line("blocked_headline")
    return status_reply(
        headline=sentence,
        job_id=job_id,
        applicant_id=applicant_id,
        client_name=client_name,
    )


def clarify_reply(*, question: str, job_id: str, memory_lines: list[str] | None = None) -> str:
    del memory_lines
    return status_reply(headline=question, job_id=job_id)


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
    subject = f" Subject {proposal.subject}." if proposal.subject else ""
    if proposal.kind == "note":
        new = f'note on {proposal.discussion_title}, exact text: "{proposal.body}"'
    remembered = _remembered(memory_lines)
    memory = f" {remembered}" if remembered else ""
    sentence = (
        f"Nothing is changed yet: say go in this thread to change {client}'s "
        f"{proposal.field} from {old} to {new}.{subject} "
        f"If I don't hear go within {minutes} minutes, I'll cancel this.{memory}"
    )
    return status_reply(
        headline=" ".join(sentence.split()),
        job_id=job_id,
        applicant_id=proposal.applicant_id,
        client_name=proposal.client,
    )


def working_reply(proposal: Proposal, *, job_id: str) -> str:
    client = proposal.client or proposal.applicant_id or "that client"
    return status_reply(
        headline=f"On it: I'm changing {proposal.field} for {client}.",
        job_id=job_id,
        applicant_id=proposal.applicant_id,
        client_name=proposal.client,
    )


def matched_reply(
    proposal: Proposal,
    *,
    observed: str,
    job_id: str,
    note: str = "",
) -> str:
    client = proposal.client or proposal.applicant_id or "that client"
    extra = f" {note.strip()}" if str(note or "").strip() else ""
    return status_reply(
        headline=(
            f"Done: {client}'s {proposal.field} now shows {observed}.{extra}"
        ),
        job_id=job_id,
        applicant_id=proposal.applicant_id,
        client_name=proposal.client,
    )


def mismatch_reply(proposal: Proposal, *, observed: str, job_id: str) -> str:
    del observed
    client = proposal.client or proposal.applicant_id or "that client"
    return status_reply(
        headline=(
            f"I could not confirm that change for {client}, I am not calling this a success, "
            "and a person needs to look at it."
        ),
        job_id=job_id,
        applicant_id=proposal.applicant_id,
        client_name=proposal.client,
    )


def cancelled_reply(*, reason: str, job_id: str) -> str:
    return status_reply(headline=reason, job_id=job_id)


def sop_reply(*, answer: str, source: str, job_id: str, freshness: str = "") -> str:
    if not answer:
        return status_reply(
            headline="I don't have that in the procedures I can read.",
            job_id=job_id,
        )
    note = str(freshness or "").strip()
    body = answer.rstrip()
    if note:
        body = f"{body} {note}"
    if source:
        body = f"{body} ({source})"
    return status_reply(headline=body, job_id=job_id)


def memory_saved_reply(
    *,
    fact: str,
    scope: str,
    job_id: str,
    team: str = "",
    client: str = "",
    tax_note: str = "",
) -> str:
    if scope == "team":
        who = f"the {team} team" if team else "that team"
    elif scope == "agency":
        who = "the whole agency"
    elif scope == "client":
        who = f"{client or 'that client'}, for the whole agency"
    else:
        who = "you"
    lead = line("memory_ssn_removed_headline") if tax_note else line("memory_saved_headline")
    lead = lead.rstrip(".")
    team_bit = f" for the {team} team" if scope == "team" and team else f" for {who}"
    extra = f" {tax_note.strip()}" if tax_note else ""
    return status_reply(
        headline=f"{lead}{team_bit}: {fact}.{extra}",
        job_id=job_id,
    )


def memory_ssn_refused_reply(*, job_id: str) -> str:
    return status_reply(
        headline="Social Security numbers can't be remembered, and I did not save it.",
        job_id=job_id,
    )


def memory_refused_reply(*, job_id: str) -> str:
    return status_reply(
        headline=line("memory_refused_headline"),
        job_id=job_id,
    )


def memory_forgotten_reply(*, facts: list[str], job_id: str) -> str:
    if not facts:
        return status_reply(
            headline="Nothing stored matched that.",
            job_id=job_id,
        )
    shown = "; ".join(facts)
    return status_reply(
        headline=f"I forgot that: {shown}.",
        job_id=job_id,
    )


def memory_list_reply(*, about: str, facts: list[str], job_id: str) -> str:
    if not facts:
        headline = "Nothing stored matched that." if about else "I don't have any notes yet."
        return status_reply(headline=headline, job_id=job_id)
    base = line("memory_list_headline").rstrip(".")
    target = f" about {about}" if about else ""
    shown = "; ".join(facts)
    return status_reply(
        headline=f"{base}{target}: {shown}.",
        job_id=job_id,
    )


def lookup_reply(*, found: str, job_id: str) -> str:
    if not found:
        return status_reply(headline="I couldn't look that up.", job_id=job_id)
    return status_reply(headline=found, job_id=job_id)
