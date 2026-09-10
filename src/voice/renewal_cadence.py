"""One carrier Robie Call from the live follow-up give-up branch.

Hook: ``OutreachCadenceManager.process_due_followups`` in
``src/email_outreach/thread_tracker.py`` (the EXHAUSTED / CSR-task branch).
Not a parallel daily-runner scan, cron, or Bland stack.

N=2 failed outreach checks **after** the initial UW email, then exactly one
``VoiceCallDispatcher().dispatch(policy_number=...)`` (``call_type=carrier``).
Do not post a Robie Call EZLynx label (avoids watcher loops).

Stop / skip if renewal is already in hand. Set ``carrier_voice_attempted``
so the call never re-fires. CSR escalate may still happen later. No client
autodial. Portal-only carriers with no UW email do not invent email attempts;
skip voice unless a carrier phone exists in the directory.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src.database.models import PolicyRenewal, RenewalStatus, ThreadStatus
from src.ezlynx.note_builder import EZLynxNoteBuilder
from src.voice.call_directory import lookup_carrier_phone, normalize_phone_e164
from src.voice.context_hydrator import CALL_TYPE_CARRIER
from src.voice.dispatcher import VoiceCallDispatcher

logger = logging.getLogger("renewal_carrier_voice_cadence")

# Drive/Hermes skill: follow up no more than twice after the initial email.
CARRIER_VOICE_AFTER_FOLLOWUPS = 2
QUIET_FOLLOWUP_BUDGET = CARRIER_VOICE_AFTER_FOLLOWUPS
CARRIER_VOICE_ATTEMPT_THRESHOLD = CARRIER_VOICE_AFTER_FOLLOWUPS

_OBTAINED_STATUSES = frozenset(
    {
        RenewalStatus.QUOTE_RECEIVED,
        RenewalStatus.READY_FOR_AGENT_REVIEW,
    }
)


def renewal_obtained_before_voice(
    policy: PolicyRenewal,
    thread: Optional[Any] = None,
) -> bool:
    """True when a dec/offer is already in hand — do not place the carrier call.

    Unused enum ``FOLLOWUP_SENT`` is never a signal (it is never written).
    """
    status = getattr(policy, "status", None)
    if status in _OBTAINED_STATUSES:
        return True
    if thread is not None and getattr(thread, "status", None) == ThreadStatus.RESOLVED:
        return True
    documents = getattr(policy, "documents", None) or []
    if documents:
        return True
    return False


def carrier_voice_already_attempted(policy: PolicyRenewal) -> bool:
    """Flag so the one voice attempt never re-fires."""
    if bool(getattr(policy, "carrier_voice_attempted", False)):
        return True
    for note in getattr(policy, "notes", None) or []:
        text = str(getattr(note, "note_text", "") or "").upper()
        if "ROBIE AUTONOMOUS CALL DISPATCHED" in text and "CARRIER" in text:
            if "CLIENT OUTREACH" in text or "CLIENT FOLLOW" in text:
                continue
            return True
        if "CARRIER ROBIE CALL" in text or "CARRIER VOICE CADENCE" in text:
            return True
    return False


def mark_carrier_voice_attempted(policy: PolicyRenewal) -> None:
    policy.carrier_voice_attempted = True


def resolve_directory_carrier_phone(policy: PolicyRenewal) -> Optional[str]:
    """Directory E.164 only. Never invent a number. Never invent email attempts."""
    return normalize_phone_e164(lookup_carrier_phone(getattr(policy, "carrier_name", None)))


def place_one_carrier_voice(
    policy: PolicyRenewal,
    *,
    thread: Optional[Any] = None,
    dispatcher: Optional[VoiceCallDispatcher] = None,
    ezlynx_client: Optional[Any] = None,
    db: Optional[Any] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Fire exactly one carrier Robie Call via VoiceCallDispatcher.

    Called from the process_due_followups give-up path after N=2. Skips when
    the renewal is in hand, the flag is set, or no directory phone exists.
    """
    base = {
        "policy_number": policy.policy_number,
        "applicant_id": policy.applicant_id,
        "call_type": CALL_TYPE_CARRIER,
    }
    if renewal_obtained_before_voice(policy, thread):
        return {**base, "status": "SKIPPED_RENEWAL_OBTAINED"}
    if carrier_voice_already_attempted(policy):
        return {**base, "status": "SKIPPED_ALREADY_CALLED"}

    phone = resolve_directory_carrier_phone(policy)
    if not phone:
        mark_carrier_voice_attempted(policy)
        note_text = EZLynxNoteBuilder.format_carrier_voice_phone_needed_note(policy)
        _post_note(policy, note_text, ezlynx_client=ezlynx_client, db=db)
        return {
            **base,
            "status": "CLARIFICATION_NEEDED",
            "reason": "MISSING_CARRIER_PHONE",
        }

    engine = dispatcher or VoiceCallDispatcher()
    result = engine.dispatch(
        policy_number=policy.policy_number,
        phone=phone,
        instructions=(
            f"Follow up on the upcoming renewal for {policy.insured_name}, "
            f"policy {policy.policy_number}. Ask if renewal terms or a quote "
            f"have been released, and where the packet was delivered."
        ),
        dry_run=dry_run,
        call_type=CALL_TYPE_CARRIER,
    )
    mark_carrier_voice_attempted(policy)
    status_label = result.get("status") or (
        "DISPATCHED" if result.get("success") else "FAILED"
    )
    note_text = EZLynxNoteBuilder.format_carrier_voice_cadence_note(
        policy,
        phone=phone,
        call_id=result.get("call_id"),
        status=status_label,
        attempts=CARRIER_VOICE_AFTER_FOLLOWUPS,
    )
    _post_note(policy, note_text, ezlynx_client=ezlynx_client, db=db)
    return {
        **base,
        "status": status_label,
        "success": bool(result.get("success")),
        "call_id": result.get("call_id"),
        "phone": phone,
        "attempts": CARRIER_VOICE_AFTER_FOLLOWUPS,
        "dry_run": dry_run,
    }


def _post_note(
    policy: PolicyRenewal,
    note_text: str,
    *,
    ezlynx_client: Optional[Any] = None,
    db: Optional[Any] = None,
) -> None:
    from src.database.models import ActionType, AuditNoteLog

    if ezlynx_client is not None:
        ezlynx_client.add_note_to_discussion(
            applicant_id=policy.applicant_id,
            discussion_title=policy.discussion_title,
            note_text=note_text,
            policy_number=policy.policy_number,
            line_of_business=policy.line_of_business,
            carrier_name=policy.carrier_name,
        )
    if db is None:
        return
    db.add(
        AuditNoteLog(
            policy_id=policy.id,
            applicant_id=policy.applicant_id,
            discussion_title=policy.discussion_title,
            action_type=ActionType.EZLYNX_NOTE_ADDED,
            note_text=note_text,
        )
    )
