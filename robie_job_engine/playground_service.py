"""Playground turn handling for Chat and for robie@ email.

A message in the configured space (or an email while the flag is on) is
classified here. Writes wait for go. Blocked actions never call a writer.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

logger = logging.getLogger(__name__)

ONLY_REQUESTER = "Only the person who asked can approve this."
MISSING_THREAD = "I can't approve that without the original thread."

from .models import VERIFIER_AUTHORITY, JobStatus, VerificationEvidence
from .playground_config import (
    confirm_timeout_minutes,
    email_guardrails_enabled,
    message_in_playground,
)
from .playground_execute import (
    ApplyResult,
    compare_readback,
    policy_change_discussion,
    policy_change_note,
    prepare_carrier_delivery,
    refuse_if_not_allowlisted,
)
from .playground_guardrails import (
    CARRIER_EMAIL,
    FORGET,
    NOTE,
    RECALL,
    REMEMBER,
    SIMPLE_EDIT,
    Decision,
    Proposal,
    _is_reply_request,
    classify_playground_request,
    mentioned_client,
)
from .playground_memory import (
    forget_matching,
    list_memories,
    memory_contains_secret,
    plan_preference,
    prepare_memory_text,
    recall_for_turn,
    record_job,
    safe_text,
    store_preference,
    team_discussion_title,
)
from .playground_reply import (
    blocked_reply,
    cancelled_reply,
    clarify_reply,
    confirmation_reply,
    help_reply,
    lookup_reply,
    matched_reply,
    memory_forgotten_reply,
    memory_list_reply,
    memory_refused_reply,
    memory_saved_reply,
    memory_ssn_refused_reply,
    mismatch_reply,
    sop_reply,
)
from .playground_voice import render_turn_prompt
from .playground_sop import retrieve_sop, ambiguous_sop_hits
from .playground_undo import record_write
from .store import JobStore, utc_now

PLAYGROUND_ACTION = "playground.task"
CONFIRM_KIND = "playground_confirmation"
REPLY_KIND = "playground_reply"
PROMPT_KIND = "playground_prompt"
CANCEL_NO_GO = "Cancelled. I didn't get a go within 30 minutes, so I didn't change anything."
STOPPED = "Stopped. That job is cancelled."

ApplyFn = Callable[[Proposal], ApplyResult]
ReadFn = Callable[[Proposal], str | None]
DiscussFn = Callable[[Proposal], list[dict[str, Any]]]
NoteFn = Callable[[Proposal, str, str], str]


def handle_playground_chat(
    db_path: str,
    text: str,
    *,
    conversation_id: str | None,
    thread_id: str | None = None,
    message_id: str | None = None,
    requested_by: str = "",
    requester_user_id: str | None = None,
    now: datetime | None = None,
    apply: ApplyFn | None = None,
    read: ReadFn | None = None,
    discussions: DiscussFn | None = None,
    file_note: NoteFn | None = None,
    sop_docs: list[dict[str, Any]] | None = None,
) -> list[str] | None:
    """Replies for a Playground space message, or None when this is not that space."""
    if not message_in_playground(conversation_id):
        return None
    return _dispatch(
        db_path,
        text,
        conversation_id=str(conversation_id or ""),
        thread_id=thread_id,
        message_id=str(message_id or ""),
        requested_by=requested_by or "Google Chat user",
        requester_user_id=requester_user_id,
        now=now,
        apply=apply,
        read=read,
        discussions=discussions,
        file_note=file_note,
        sop_docs=sop_docs,
    )


def handle_playground_email(
    db_path: str,
    text: str,
    *,
    sender: str = "",
    thread_id: str = "",
    message_id: str = "",
    now: datetime | None = None,
    apply: ApplyFn | None = None,
    read: ReadFn | None = None,
    discussions: DiscussFn | None = None,
    file_note: NoteFn | None = None,
    sop_docs: list[dict[str, Any]] | None = None,
) -> str | None:
    """Same guardrails for robie@. None lets the existing email path run."""
    if not email_guardrails_enabled():
        return None
    # Ascend/PFA requests bypass playground - they have their own workflow
    _tl = text.lower()
    if any(k in _tl for k in ["ascend", "finance agreement", "financing agreement", "payment agreement", "premium finance"]):
        return None
    conversation = f"email:{thread_id or message_id or 'inbox'}"
    replies = _dispatch(
        db_path,
        text,
        conversation_id=conversation,
        thread_id=thread_id or message_id or None,
        message_id=message_id,
        requested_by=sender or "email requester",
        requester_user_id=str(sender or "").strip(),
        now=now,
        apply=apply,
        read=read,
        discussions=discussions,
        file_note=file_note,
        sop_docs=sop_docs,
    )
    return "\n\n".join(replies)


def expire_due_confirmations(
    store: JobStore,
    *,
    now: datetime | None = None,
    poster: Callable[..., Any] | None = None,
) -> list[str]:
    """Cancel Playground jobs that never got a go. Plain message, no write."""
    moment = _aware(now)
    expired: list[str] = []
    for job in store.list_jobs_by_status({JobStatus.AWAITING_HUMAN_INPUT}):
        if job.get("action_type") != PLAYGROUND_ACTION:
            continue
        note = store.get_checkpoint(job["id"], CONFIRM_KIND) or {}
        deadline = _parse_time(str(note.get("expires_at") or ""))
        if deadline is None or deadline > moment:
            continue
        reply = cancelled_reply(reason=CANCEL_NO_GO, job_id=job["id"])
        store.transition(
            job["id"],
            JobStatus.FAILED,
            expected={JobStatus.AWAITING_HUMAN_INPUT},
            error=CANCEL_NO_GO,
            release_lease=True,
        )
        store.checkpoint(job["id"], REPLY_KIND, {"text": reply, "reason": "timeout"})
        payload = dict(job.get("payload") or {})
        _remember_job(
            store,
            job["id"],
            requested_by=str(payload.get("requested_by") or ""),
            text=str(payload.get("text") or ""),
            client=str(payload.get("client") or ""),
            outcome="cancelled",
            now=moment,
            applicant_id=str(payload.get("applicant_id") or ""),
        )
        expired.append(job["id"])
        if poster is None:
            continue
        payload = dict(job.get("payload") or {})
        space = str(payload.get("conversation_id") or "")
        if not space.startswith("spaces/"):
            continue
        try:
            poster(space, reply, thread_name=payload.get("thread_id") or None)
        except Exception:
            continue
    return expired


def _approval_identity(requester_user_id: str | None, requested_by: str) -> str:
    """Chat user id when the caller passed one. Otherwise the legacy name.

    An explicit empty id fails closed. It does not fall back to a display name.
    """
    if requester_user_id is not None:
        return str(requester_user_id).strip()
    return str(requested_by or "").strip()


def _thread_matches(incoming: str | None, stored: str | None) -> bool:
    """Exact, non-empty thread names only. Missing or blank never matches."""
    left = str(incoming).strip() if incoming is not None else ""
    right = str(stored).strip() if stored is not None else ""
    return bool(left) and left == right


def _dispatch(
    db_path: str,
    text: str,
    *,
    conversation_id: str,
    thread_id: str | None,
    message_id: str,
    requested_by: str,
    requester_user_id: str | None,
    now: datetime | None,
    apply: ApplyFn | None,
    read: ReadFn | None,
    discussions: DiscussFn | None,
    file_note: NoteFn | None,
    sop_docs: list[dict[str, Any]] | None,
) -> list[str]:
    store = JobStore(db_path)
    moment = _aware(now)
    expire_due_confirmations(store, now=moment)
    decision = classify_playground_request(text)
    if decision.intent == "stop":
        return [_stop(store, conversation_id, str(thread_id or ""), requested_by=requested_by, now=moment)]
    if decision.intent == "go":
        return _approve(
            store,
            conversation_id=conversation_id,
            thread_id=thread_id,
            approver_user_id=_approval_identity(requester_user_id, requested_by),
            now=moment,
            apply=apply,
            read=read,
            discussions=discussions,
            file_note=file_note,
        )
    if decision.intent in {REMEMBER, FORGET, RECALL}:
        return [
            _memory_turn(
                store,
                decision,
                text=text,
                conversation_id=conversation_id,
                thread_id=thread_id,
                message_id=message_id,
                requested_by=requested_by,
                requester_user_id=requester_user_id,
                now=moment,
            )
        ]
    _cancel_pending(store, conversation_id, str(thread_id or ""), reason="Replaced by a new request.")
    return [
        _start(
            store,
            decision,
            text=text,
            conversation_id=conversation_id,
            thread_id=thread_id,
            message_id=message_id,
            requested_by=requested_by,
            requester_user_id=requester_user_id,
            now=moment,
            read=read,
            sop_docs=sop_docs,
        )
    ]


def _start(
    store: JobStore,
    decision: Decision,
    *,
    text: str,
    conversation_id: str,
    thread_id: str | None,
    message_id: str,
    requested_by: str,
    requester_user_id: str | None,
    now: datetime,
    read: ReadFn | None,
    sop_docs: list[dict[str, Any]] | None,
) -> str:
    stored = safe_text(text)
    channel = "email" if conversation_id.startswith("email:") else "chat"
    stored_thread = str(thread_id).strip() if thread_id is not None else ""
    job = store.create_job(
        PLAYGROUND_ACTION,
        {
            "playground": True,
            "text": stored,
            "conversation_id": conversation_id,
            "thread_id": stored_thread,
            "requested_by": requested_by,
            "requester_user_id": _approval_identity(requester_user_id, requested_by),
            "channel": channel,
        },
        idempotency_key=f"playground:{message_id or stored}:{conversation_id}"[:180],
    )
    job_id = job["id"]
    if JobStatus(job["status"]) != JobStatus.PENDING:
        saved = store.get_checkpoint(job_id, REPLY_KIND) or {}
        return str(saved.get("text") or "I already have that message.")
    client = ""
    applicant_id = ""
    if decision.proposal is not None:
        client = decision.proposal.client
        applicant_id = decision.proposal.applicant_id
    client = client or mentioned_client(text)
    memories = recall_for_turn(
        store.path,
        requested_by=requested_by,
        text=stored,
        client=client,
        applicant_id=applicant_id,
        now=now,
    )
    decision = _with_standing_discussion(text, decision, memories)
    if decision.proposal is not None and decision.proposal.client:
        client = decision.proposal.client
    _checkpoint_prompt(
        store,
        job_id,
        channel=channel,
        requested_by=requested_by,
        text=stored,
        memories=memories,
    )
    remembered = _preference_lines(memories)

    def _note(outcome: str) -> None:
        _remember_job(
            store,
            job_id,
            requested_by=requested_by,
            text=stored,
            client=client,
            outcome=outcome,
            now=now,
            applicant_id=applicant_id,
        )

    if decision.intent == "help":
        reply = _finish_reply(store, job_id, help_reply(job_id=job_id), terminal=JobStatus.COMPLETE, answer_only=True)
        _note("answered")
        return reply
    if decision.blocked:
        reply = blocked_reply(
            reason=decision.reason,
            job_id=job_id,
            applicant_id=decision.proposal.applicant_id if decision.proposal else "",
            client_name=mentioned_client(text) or (decision.proposal.client if decision.proposal else ""),
        )
        _note("blocked")
        return _fail(store, job_id, reply, error=decision.reason)
    if decision.intent == "vague" or decision.question:
        question = decision.question or "What should I do? Name the client and the task."
        reply = clarify_reply(question=question, job_id=job_id, memory_lines=remembered)
        store.checkpoint(job_id, "clarification", {"question": question, "needs_clarification": True})
        store.update_payload(
            job_id,
            {
                **dict(store.get_job(job_id).get("payload") or {}),
                "needs_clarification": True,
            },
        )
        store.transition(
            job_id,
            JobStatus.NEEDS_CLARIFICATION,
            expected={JobStatus.PENDING},
            error=question,
            release_lease=True,
        )
        store.checkpoint(job_id, REPLY_KIND, {"text": reply})
        _note("needs_clarification")
        return reply
    if decision.intent == "sop":
        hits = retrieve_sop(text, sop_docs, limit=2, include_ties=True)
        if ambiguous_sop_hits(hits):
            tied = [hit for hit in hits if hit.match_score == hits[0].match_score]
            sources = "; ".join(hit.citation for hit in tied[:3])
            remaining = f"; and {len(tied) - 3} more" if len(tied) > 3 else ""
            question = (f"{len(tied)} procedure sources match this question with different text: "
                        + sources + remaining
                        + ". Which approved source should I use?")
            reply = clarify_reply(question=question, job_id=job_id)
            store.checkpoint(job_id, "sop_ambiguity", {"source_ids": [hit.doc_id for hit in tied],
                                                      "needs_review": True})
            store.transition(job_id, JobStatus.NEEDS_CLARIFICATION,
                             expected={JobStatus.PENDING}, error="SOP source ambiguity",
                             release_lease=True)
            store.checkpoint(job_id, REPLY_KIND, {"text": reply})
            _note("needs_clarification")
            return reply
        if hits:
            hit = hits[0]
            reply = sop_reply(
                answer=hit.excerpt,
                source=hit.citation,
                job_id=job_id,
                freshness=hit.freshness_note,
            )
        elif _is_reply_request(text.lower()):
            # Simple reply request (2026-10-02): user just wants an
            # acknowledgment, not a procedure lookup.
            reply = sop_reply(
                answer="Got it.",
                source="",
                job_id=job_id,
            )
        else:
            reply = sop_reply(answer="", source="", job_id=job_id)
        _note("answered")
        return _finish_reply(store, job_id, reply, terminal=JobStatus.COMPLETE, answer_only=True)
    if decision.intent == "lookup":
        found = ""
        if read is not None and decision.proposal is not None:
            try:
                found = str(read(decision.proposal) or "")
            except Exception:
                logger.info("playground lookup unavailable job=%s", job_id)
        reply = lookup_reply(found=found, job_id=job_id)
        _note("answered")
        return _finish_reply(store, job_id, reply, terminal=JobStatus.COMPLETE, answer_only=True)
    proposal = decision.proposal or Proposal(kind=decision.intent)
    proposal.requested_by = requested_by
    proposal.requested_at = now.isoformat()
    if proposal.kind == CARRIER_EMAIL:
        proposal = _present_carrier(proposal)
    if proposal.kind == SIMPLE_EDIT and not proposal.old_value:
        current = read(proposal) if read else None
        if not current:
            question = (
                f"I don't have the current {proposal.field} for "
                f"{proposal.client or 'that client'}. What is it now?"
            )
            reply = clarify_reply(question=question, job_id=job_id, memory_lines=remembered)
            store.checkpoint(job_id, "clarification", {"question": question, "needs_clarification": True})
            store.update_payload(
                job_id,
                {
                    **dict(store.get_job(job_id).get("payload") or {}),
                    "needs_clarification": True,
                },
            )
            store.transition(
                job_id,
                JobStatus.NEEDS_CLARIFICATION,
                expected={JobStatus.PENDING},
                error=question,
                release_lease=True,
            )
            store.checkpoint(job_id, REPLY_KIND, {"text": reply})
            _note("needs_clarification")
            return reply
        proposal.old_value = current
    refusal = refuse_if_not_allowlisted(proposal)
    if refusal:
        reply = blocked_reply(
            reason=refusal,
            job_id=job_id,
            applicant_id=proposal.applicant_id,
            client_name=proposal.client,
        )
        _note("blocked")
        return _fail(store, job_id, reply, error=refusal)
    expires = now + timedelta(minutes=confirm_timeout_minutes())
    store.checkpoint(
        job_id,
        CONFIRM_KIND,
        {
            "status": "awaiting_go",
            "expires_at": expires.isoformat(),
            "proposal": proposal.as_dict(),
            "thread_id": str(thread_id).strip() if thread_id is not None else "",
            "conversation_id": conversation_id,
        },
    )
    store.update_payload(
        job_id,
        {
            **dict(store.get_job(job_id).get("payload") or {}),
            "applicant_id": proposal.applicant_id,
            "playground_kind": proposal.kind,
        },
    )
    reply = confirmation_reply(proposal, job_id=job_id, memory_lines=remembered)
    store.transition(
        job_id,
        JobStatus.AWAITING_HUMAN_INPUT,
        expected={JobStatus.PENDING},
        release_lease=True,
    )
    store.checkpoint(job_id, REPLY_KIND, {"text": reply})
    _note("awaiting_go")
    return reply


_NEW_DISCUSSION = re.compile(
    r"\b(?:untitled|new discussion|create a discussion|start a discussion)\b",
    re.IGNORECASE,
)


def _with_standing_discussion(text: str, decision: Decision, memories: list) -> Decision:
    """Use a stored discussion title. Never turns a block into a write."""
    if decision.code != "untitled_note":
        return decision
    if _NEW_DISCUSSION.search(text):
        return decision
    title = team_discussion_title(memories)
    if not title:
        return decision
    rewritten = f"File a note on the existing {title} discussion. {text}"
    revised = classify_playground_request(rewritten)
    if revised.blocked or revised.intent not in {NOTE, "vague"}:
        return decision
    return revised


def _preference_lines(memories: list) -> list[str]:
    return [item.body for item in memories if item.kind in {"preference", "note"}][:3]


def _checkpoint_prompt(
    store: JobStore,
    job_id: str,
    *,
    channel: str,
    requested_by: str,
    text: str,
    memories: list,
) -> None:
    store.checkpoint(
        job_id,
        PROMPT_KIND,
        {
            "text": render_turn_prompt(
                channel=channel,
                requested_by=requested_by,
                text=text,
                memories=memories,
            )
        },
    )


def _remember_job(
    store: JobStore,
    job_id: str,
    *,
    requested_by: str,
    text: str,
    client: str,
    outcome: str,
    now: datetime,
    applicant_id: str = "",
) -> None:
    record_job(
        store.path,
        job_id=job_id,
        requested_by=requested_by,
        text=safe_text(text),
        client_name="" if memory_contains_secret(text) else client,
        outcome=safe_text(outcome),
        now=now,
        applicant_id="" if memory_contains_secret(text) else applicant_id,
    )


def _sync_job_outcome(
    store: JobStore,
    job_id: str,
    *,
    outcome: str,
    client: str,
    now: datetime,
    requested_by: str = "",
) -> None:
    payload = dict(store.get_job(job_id).get("payload") or {})
    _remember_job(
        store,
        job_id,
        requested_by=requested_by or str(payload.get("requested_by") or ""),
        text=str(payload.get("text") or ""),
        client=client,
        outcome=outcome,
        now=now,
        applicant_id=str(payload.get("applicant_id") or ""),
    )


def _tax_note(prepared: Any) -> str:
    if getattr(prepared, "action", "") != "tax_redacted":
        return ""
    if prepared.removed_ssn and prepared.removed_itin:
        return "I took out the Social Security number and the ITIN."
    if prepared.removed_itin:
        return "I took out the ITIN."
    return "I took out the Social Security number."


def _format_memory(item: Any) -> str:
    if item.kind == "job":
        client = f" for {item.client_name}" if item.client_name else ""
        return f"Past job{client}: {item.outcome}. {item.body}"
    if item.scope == "team":
        who = f"{item.team} team" if item.team else "Team"
    elif item.scope == "agency":
        who = "Agency"
    elif item.scope == "client":
        who = item.client_name or "Client"
    else:
        who = item.author
    return f"{who}: {item.body}"


def _memory_turn(
    store: JobStore,
    decision: Decision,
    *,
    text: str,
    conversation_id: str,
    thread_id: str | None,
    message_id: str,
    requested_by: str,
    requester_user_id: str | None = None,
    now: datetime,
) -> str:
    stored = safe_text(text)
    channel = "email" if conversation_id.startswith("email:") else "chat"
    job = store.create_job(
        PLAYGROUND_ACTION,
        {
            "playground": True,
            "text": stored,
            "conversation_id": conversation_id,
            "thread_id": str(thread_id).strip() if thread_id is not None else "",
            "requested_by": requested_by,
            "requester_user_id": _approval_identity(requester_user_id, requested_by),
            "channel": channel,
        },
        idempotency_key=f"playground:{message_id or stored}:{conversation_id}"[:180],
    )
    job_id = job["id"]
    if JobStatus(job["status"]) != JobStatus.PENDING:
        saved = store.get_checkpoint(job_id, REPLY_KIND) or {}
        return str(saved.get("text") or "I already have that message.")
    proposal = decision.proposal or Proposal(kind=decision.intent)
    memories = recall_for_turn(
        store.path,
        requested_by=requested_by,
        text=stored,
        client=proposal.client or mentioned_client(stored),
        applicant_id=proposal.applicant_id,
        now=now,
    )
    _checkpoint_prompt(
        store,
        job_id,
        channel=channel,
        requested_by=requested_by,
        text=stored,
        memories=memories,
    )
    def _note(outcome: str) -> None:
        _remember_job(
            store,
            job_id,
            requested_by=requested_by,
            text=stored,
            client=proposal.client,
            outcome=outcome,
            now=now,
            applicant_id=proposal.applicant_id,
        )

    if decision.question:
        reply = clarify_reply(question=decision.question, job_id=job_id)
        store.transition(
            job_id,
            JobStatus.NEEDS_CLARIFICATION,
            expected={JobStatus.PENDING},
            error=decision.question,
            release_lease=True,
        )
        store.checkpoint(job_id, REPLY_KIND, {"text": reply})
        _note("needs_clarification")
        return reply
    fact = proposal.body
    if decision.intent == REMEMBER:
        prepared = prepare_memory_text(fact)
        if prepared.action == "secret" or prepare_memory_text(text).action == "secret":
            reply = memory_refused_reply(job_id=job_id)
            _note("refused")
            return _fail(store, job_id, reply, error="Not stored.")
        if prepared.action == "tax_refused":
            reply = memory_ssn_refused_reply(job_id=job_id)
            _note("refused")
            return _fail(store, job_id, reply, error="Not stored.")
        fact = prepared.text
        scope_hint = proposal.field if proposal.field in {"person", "team", "client", "agency"} else ""
        planned = plan_preference(
            fact,
            requested_by=requested_by,
            now=now,
            force_team=scope_hint == "team",
            scope_hint=scope_hint,
            team_hint=proposal.subject if scope_hint == "team" else "",
            applicant_id=proposal.applicant_id,
            client_name=proposal.client,
        )
        if planned is None:
            reply = memory_refused_reply(job_id=job_id)
            _note("refused")
            return _fail(store, job_id, reply, error="Not stored.")
        if planned.question:
            reply = clarify_reply(question=planned.question, job_id=job_id)
            store.transition(
                job_id,
                JobStatus.NEEDS_CLARIFICATION,
                expected={JobStatus.PENDING},
                error=planned.question,
                release_lease=True,
            )
            store.checkpoint(job_id, REPLY_KIND, {"text": reply})
            _note("needs_clarification")
            return reply
        saved = store_preference(
            store.path,
            planned,
            requested_by=requested_by,
            now=now,
            source_job_id=job_id,
        )
        reply = memory_saved_reply(
            fact=saved.text,
            scope=saved.scope,
            job_id=job_id,
            team=saved.team,
            client=saved.client_name,
            tax_note=_tax_note(prepared),
        )
        _note("remembered")
        return _finish_reply(store, job_id, reply, terminal=JobStatus.COMPLETE, answer_only=True)
    if decision.intent == FORGET:
        prepared = prepare_memory_text(fact)
        if prepared.action == "secret":
            reply = memory_refused_reply(job_id=job_id)
            _note("refused")
            return _fail(store, job_id, reply, error="Not stored.")
        if prepared.action == "tax_refused":
            reply = memory_ssn_refused_reply(job_id=job_id)
            _note("refused")
            return _fail(store, job_id, reply, error="Not stored.")
        fact = prepared.text
        forgotten = forget_matching(
            store.path,
            requested_by=requested_by,
            query=fact,
            now=now,
        )
        reply = memory_forgotten_reply(
            facts=[item.body for item in forgotten],
            job_id=job_id,
        )
        _note("forgotten")
        return _finish_reply(store, job_id, reply, terminal=JobStatus.COMPLETE, answer_only=True)
    prepared = prepare_memory_text(fact)
    if prepared.action == "secret":
        reply = memory_refused_reply(job_id=job_id)
        _note("refused")
        return _fail(store, job_id, reply, error="Not stored.")
    if prepared.action == "tax_refused":
        reply = memory_ssn_refused_reply(job_id=job_id)
        _note("refused")
        return _fail(store, job_id, reply, error="Not stored.")
    fact = prepared.text
    rows = list_memories(store.path, requested_by=requested_by, about=fact)
    reply = memory_list_reply(
        about=fact,
        facts=[_format_memory(item) for item in rows],
        job_id=job_id,
    )
    _note("listed")
    return _finish_reply(store, job_id, reply, terminal=JobStatus.COMPLETE, answer_only=True)


def _present_carrier(proposal: Proposal) -> Proposal:
    prepared = prepare_carrier_delivery(proposal)
    if prepared.extra.get("redirected"):
        original = prepared.extra.get("original_to") or proposal.carrier_address
        sink = prepared.new_value
        prepared.extra["display_new"] = f"{sink} (practice; would have gone to {original})"
        prepared.extra["delivered_to"] = sink
    return prepared


def _approve(
    store: JobStore,
    *,
    conversation_id: str,
    thread_id: str | None,
    approver_user_id: str,
    now: datetime,
    apply: ApplyFn | None,
    read: ReadFn | None,
    discussions: DiscussFn | None,
    file_note: NoteFn | None,
) -> list[str]:
    if not str(thread_id or "").strip():
        logger.info("playground go refused: missing thread conversation=%s", conversation_id)
        return [MISSING_THREAD]
    pending = _find_exact(store, conversation_id, thread_id, {JobStatus.AWAITING_HUMAN_INPUT})
    if pending is not None:
        payload = dict(pending.get("payload") or {})
        stored_user = str(payload.get("requester_user_id") or "").strip()
        approver = str(approver_user_id or "").strip()
        if not stored_user or stored_user != approver:
            logger.info(
                "playground go refused: requester mismatch job=%s",
                pending["id"],
            )
            return [ONLY_REQUESTER]
    if pending is None:
        latest = _find_exact(
            store,
            conversation_id,
            thread_id,
            {JobStatus.FAILED, JobStatus.UNVERIFIED, JobStatus.COMPLETE},
        )
        if latest and CANCEL_NO_GO in str(latest.get("last_error") or ""):
            saved = store.get_checkpoint(latest["id"], REPLY_KIND) or {}
            return [str(saved.get("text") or cancelled_reply(reason=CANCEL_NO_GO, job_id=latest["id"]))]
        opened = store.create_job(
            PLAYGROUND_ACTION,
            {
                "playground": True,
                "conversation_id": conversation_id,
                "thread_id": thread_id,
                "text": "go",
            },
            idempotency_key=f"playground-go-empty:{conversation_id}:{thread_id}:{now.isoformat()}",
        )
        question = "I don't have a change waiting. Tell me the client and what should change."
        reply = clarify_reply(question=question, job_id=opened["id"])
        store.transition(
            opened["id"],
            JobStatus.NEEDS_CLARIFICATION,
            expected={JobStatus.PENDING},
            error=question,
            release_lease=True,
        )
        store.checkpoint(opened["id"], REPLY_KIND, {"text": reply})
        return [reply]
    note = store.get_checkpoint(pending["id"], CONFIRM_KIND) or {}
    deadline = _parse_time(str(note.get("expires_at") or ""))
    if deadline is not None and deadline <= now:
        expire_due_confirmations(store, now=now)
        saved = store.get_checkpoint(pending["id"], REPLY_KIND) or {}
        return [str(saved.get("text") or cancelled_reply(reason=CANCEL_NO_GO, job_id=pending["id"]))]
    proposal = Proposal.from_dict(note.get("proposal"))
    refusal = refuse_if_not_allowlisted(proposal)
    if refusal:
        reply = blocked_reply(
            reason=refusal,
            job_id=pending["id"],
            applicant_id=proposal.applicant_id,
            client_name=proposal.client,
        )
        _fail(store, pending["id"], reply, error=refusal)
        _sync_job_outcome(store, pending["id"], outcome="blocked", client=proposal.client, now=now)
        return [reply]
    logger.info("playground applying job=%s kind=%s", pending["id"], proposal.kind)
    store.transition(
        pending["id"],
        JobStatus.RUNNING,
        expected={JobStatus.AWAITING_HUMAN_INPUT},
        release_lease=True,
    )
    try:
        result = apply(proposal) if apply is not None else _default_apply(proposal)
    except Exception as exc:
        reply = mismatch_reply(proposal, observed="", job_id=pending["id"])
        _fail(
            store,
            pending["id"],
            reply,
            error=f"The change did not finish ({type(exc).__name__}).",
        )
        _sync_job_outcome(store, pending["id"], outcome="not_confirmed", client=proposal.client, now=now)
        logger.info("playground apply failed job=%s", pending["id"])
        return [reply]
    if not result.applied:
        reply = mismatch_reply(
            proposal,
            observed=result.detail or "the change was not made",
            job_id=pending["id"],
        )
        _fail(store, pending["id"], reply, error=result.detail or "not applied")
        _sync_job_outcome(store, pending["id"], outcome="not_confirmed", client=proposal.client, now=now)
        return [reply]
    observed = result.observed
    if read is not None:
        looked_up = read(proposal)
        if looked_up is not None:
            observed = looked_up
    expected = proposal.new_value
    if proposal.kind == CARRIER_EMAIL:
        expected = str(proposal.extra.get("delivered_to") or proposal.new_value)
    readback = compare_readback(expected=expected, observed=observed)
    record_write(
        store,
        job_id=pending["id"],
        requested_by=proposal.requested_by,
        requested_at=proposal.requested_at,
        client_name=proposal.client or proposal.applicant_id or "unknown client",
        applicant_id=proposal.applicant_id,
        field_name=proposal.field,
        before_value=proposal.old_value,
        after_value=proposal.new_value,
        write_kind=proposal.kind,
        readback_result=readback.result,
        readback_detail=readback.detail,
        created_at=now.isoformat(),
    )
    note_line = _maybe_file_note(
        store,
        proposal,
        job_id=pending["id"],
        observed=readback.observed or observed or "",
        discussions=discussions,
        file_note=file_note,
        now=now,
    )
    logger.info(
        "playground readback job=%s matched=%s detail=%s",
        pending["id"],
        readback.matched,
        readback.detail,
    )
    if readback.matched:
        reply = matched_reply(
            proposal,
            observed=readback.observed,
            job_id=pending["id"],
            note=note_line,
        )
        _mark_confirmed(store, pending["id"], proposal, readback.observed, reply)
        _sync_job_outcome(store, pending["id"], outcome="confirmed", client=proposal.client, now=now)
        return [reply]
    reply = mismatch_reply(proposal, observed=readback.observed, job_id=pending["id"])
    store.transition(
        pending["id"],
        JobStatus.UNVERIFIED,
        expected={JobStatus.RUNNING},
        error=readback.detail,
        release_lease=True,
    )
    store.checkpoint(pending["id"], REPLY_KIND, {"text": reply, "human_required": True})
    _sync_job_outcome(store, pending["id"], outcome="not_confirmed", client=proposal.client, now=now)
    return [reply]


def _maybe_file_note(
    store: JobStore,
    proposal: Proposal,
    *,
    job_id: str,
    observed: str,
    discussions: DiscussFn | None,
    file_note: NoteFn | None,
    now: datetime,
) -> str:
    if file_note is None or discussions is None:
        return ""
    if proposal.kind == NOTE:
        return ""
    found = policy_change_discussion(discussions(proposal))
    if not found:
        return ""
    title = str(found.get("title") or found.get("Title") or "").strip()
    if not title:
        return ""
    body = policy_change_note(proposal, observed=observed)
    try:
        note_id = file_note(proposal, title, body)
    except Exception:
        return "I found the Policy Change Request discussion, but the note did not file."
    if not note_id:
        return "I found the Policy Change Request discussion, but the note did not file."
    record_write(
        store,
        job_id=job_id,
        requested_by=proposal.requested_by,
        requested_at=proposal.requested_at,
        client_name=proposal.client or proposal.applicant_id or "unknown client",
        applicant_id=proposal.applicant_id,
        field_name="note",
        before_value="none",
        after_value=body,
        write_kind="note",
        readback_result="matched" if note_id else "mismatch",
        readback_detail=f"note_id {note_id}",
        created_at=now.isoformat(),
    )
    return f"I added a short note on {title}."


def _default_apply(proposal: Proposal) -> ApplyResult:
    from .playground_execute import default_apply

    return default_apply(proposal)


def _mark_confirmed(store: JobStore, job_id: str, proposal: Proposal, observed: str, reply: str) -> None:
    locator = f"ezlynx:applicant:{proposal.applicant_id}"
    expected = {
        "applicant_id": proposal.applicant_id,
        "status": "confirmed",
        "outcome": observed,
    }
    store.checkpoint(
        job_id,
        "action",
        {
            "action": PLAYGROUND_ACTION,
            "destination": {"applicant_id": proposal.applicant_id, "locator": locator},
        },
    )
    store.update_payload(
        job_id,
        {
            **dict(store.get_job(job_id).get("payload") or {}),
            "applicant_id": proposal.applicant_id,
            "locator": {"applicant_id": proposal.applicant_id, "locator": locator},
        },
    )
    captured = utc_now()
    store.add_evidence(
        job_id,
        True,
        VerificationEvidence(
            method="ezlynx-readback",
            source="playground",
            expected=expected,
            observed=dict(expected),
            authoritative=True,
            captured_at=captured,
            locator=locator,
        ),
    )
    store.transition(job_id, JobStatus.VERIFYING, expected={JobStatus.RUNNING}, release_lease=True)
    try:
        store.transition(
            job_id,
            JobStatus.COMPLETE,
            expected={JobStatus.VERIFYING},
            authority=VERIFIER_AUTHORITY,
            release_lease=True,
        )
    except (PermissionError, RuntimeError):
        store.transition(
            job_id,
            JobStatus.UNVERIFIED,
            expected={JobStatus.VERIFYING},
            error="Readback matched. The job ledger did not accept COMPLETE.",
            release_lease=True,
        )
    store.checkpoint(job_id, REPLY_KIND, {"text": reply})


def _stop(
    store: JobStore,
    conversation_id: str,
    thread_id: str,
    *,
    requested_by: str = "",
    now: datetime | None = None,
) -> str:
    pending = _find(
        store,
        conversation_id,
        thread_id,
        {JobStatus.AWAITING_HUMAN_INPUT, JobStatus.RUNNING, JobStatus.PENDING, JobStatus.NEEDS_CLARIFICATION},
    )
    if pending is None:
        from .chat_thread import job_for_chat_thread
        from .chat_turn_control import ALREADY_FINISHED_REPLY, fail_cancelled_chat_job
        from .models import TERMINAL_STATUSES
        from .user_reply import format_user_reply

        owner = job_for_chat_thread(store, thread_id) if thread_id else None
        if owner is not None:
            if JobStatus(owner["status"]) in TERMINAL_STATUSES:
                return ALREADY_FINISHED_REPLY
            return format_user_reply(fail_cancelled_chat_job(store, str(owner["id"])))
        opened = store.create_job(
            PLAYGROUND_ACTION,
            {"playground": True, "conversation_id": conversation_id, "thread_id": thread_id, "text": "stop"},
            idempotency_key=f"playground-stop-empty:{conversation_id}:{thread_id}:{utc_now()}",
        )
        reply = cancelled_reply(
            reason="Stopped. There isn't a running job in this thread.",
            job_id=opened["id"],
        )
        _fail(store, opened["id"], reply, error="No running job.")
        return reply
    reply = cancelled_reply(reason=STOPPED, job_id=pending["id"])
    status = JobStatus(pending["status"])
    if status not in {JobStatus.FAILED, JobStatus.COMPLETE, JobStatus.UNVERIFIED}:
        store.transition(
            pending["id"],
            JobStatus.FAILED,
            expected={status},
            error="Cancelled.",
            release_lease=True,
        )
    store.checkpoint(pending["id"], "cancelled", {"by": "stop", "reason": "Cancelled."})
    store.checkpoint(pending["id"], REPLY_KIND, {"text": reply})
    _sync_job_outcome(
        store,
        pending["id"],
        outcome="cancelled",
        client="",
        now=now or _aware(None),
        requested_by=requested_by,
    )
    return reply


def _cancel_pending(store: JobStore, conversation_id: str, thread_id: str, *, reason: str) -> None:
    pending = _find(store, conversation_id, thread_id, {JobStatus.AWAITING_HUMAN_INPUT})
    if pending is None:
        return
    store.transition(
        pending["id"],
        JobStatus.FAILED,
        expected={JobStatus.AWAITING_HUMAN_INPUT},
        error=reason,
        release_lease=True,
    )


def _find_exact(
    store: JobStore,
    conversation_id: str,
    thread_id: str | None,
    statuses: set[JobStatus],
) -> dict[str, Any] | None:
    """Match one job only when both thread names are non-empty and equal."""
    incoming = str(thread_id or "").strip()
    if not incoming:
        return None
    found: list[dict[str, Any]] = []
    for job in store.list_jobs_by_status(statuses):
        if job.get("action_type") != PLAYGROUND_ACTION:
            continue
        payload = dict(job.get("payload") or {})
        if str(payload.get("conversation_id") or "") != conversation_id:
            continue
        if not _thread_matches(incoming, payload.get("thread_id")):
            continue
        found.append(job)
    if not found:
        return None
    return found[-1]


def _find(
    store: JobStore,
    conversation_id: str,
    thread_id: str,
    statuses: set[JobStatus],
) -> dict[str, Any] | None:
    found: list[dict[str, Any]] = []
    for job in store.list_jobs_by_status(statuses):
        if job.get("action_type") != PLAYGROUND_ACTION:
            continue
        payload = dict(job.get("payload") or {})
        if str(payload.get("conversation_id") or "") != conversation_id:
            continue
        job_thread = str(payload.get("thread_id") or "")
        if thread_id and job_thread and job_thread != thread_id:
            continue
        found.append(job)
    if not found:
        return None
    return found[-1]


def _fail(
    store: JobStore,
    job_id: str,
    reply: str,
    *,
    error: str,
) -> str:
    current = JobStatus(store.get_job(job_id)["status"])
    if current not in {JobStatus.FAILED, JobStatus.COMPLETE, JobStatus.UNVERIFIED}:
        store.transition(
            job_id,
            JobStatus.FAILED,
            expected={current},
            error=error,
            release_lease=True,
        )
    store.checkpoint(job_id, REPLY_KIND, {"text": reply})
    return reply


def _finish_reply(
    store: JobStore,
    job_id: str,
    reply: str,
    *,
    terminal: JobStatus,
    answer_only: bool,
) -> str:
    """A question or lookup did not write. Count it as answered, not unverified."""
    del terminal
    if answer_only:
        from .answer_only import mark_answered_question

        mark_answered_question(store, job_id, reply)
    store.checkpoint(job_id, REPLY_KIND, {"text": reply})
    return reply


def _aware(now: datetime | None) -> datetime:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _parse_time(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
