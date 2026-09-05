"""Manual-renewal carrier voice sprinkle after two unsuccessful channel attempts.

Carlo lock 2026-09-05:
1. Email or portal first (existing channels).
2. After two unsuccessful email/portal attempts with no renewal in hand,
   place exactly one outbound Robie Call (``call_type=carrier``).
3. If the renewal is obtained (PDF filed, UW reply matched, or pipeline
   status says we have the dec/offer) → STOP. No more carrier calls.
   Do not auto-dial the client. Never fire ``renewal_reachout`` or any
   ``client_outreach`` pathway from this pipeline — those require an
   explicit CSR label/note.

Never invent a carrier phone. If no E.164 underwriter/carrier number is on
file, post an EZLynx note and skip the dial.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from src.database.models import (
    ActionType,
    AuditNoteLog,
    PolicyRenewal,
    RenewalStatus,
    ThreadStatus,
)
from src.ezlynx.api_client import EZLynxApiClient
from src.ezlynx.note_builder import EZLynxNoteBuilder
from src.voice.call_directory import lookup_carrier_phone, normalize_phone_e164
from src.voice.context_hydrator import CALL_TYPE_CARRIER, ContextHydrator
from src.voice.processed_robie_notes import ProcessedRobieCallStore
from src.voice.voice_client import CarrierVoiceClient

logger = logging.getLogger("renewal_carrier_voice_cadence")

CARRIER_VOICE_ATTEMPT_THRESHOLD = 2

# Pipeline statuses that mean we have (or no longer need) a renewal offer.
RENEWAL_IN_HAND_STATUSES = frozenset(
    {
        RenewalStatus.PORTAL_QUOTE_FOUND,
        RenewalStatus.REPLY_RECEIVED,
        RenewalStatus.QUOTE_RECEIVED,
        RenewalStatus.UPLOADED_TO_EZLYNX,
        RenewalStatus.READY_FOR_AGENT_REVIEW,
        RenewalStatus.COMPLETED,
        RenewalStatus.RENEWAL_OFFER_RECEIVED,
        RenewalStatus.NON_RENEWAL_DECLINED,
        RenewalStatus.NON_RENEWAL_CONFIRMED_DOCS_UPLOADED,
    }
)

# Still waiting — eligible for the one carrier Robie Call after 2 misses.
ELIGIBLE_WAITING_STATUSES = frozenset(
    {
        RenewalStatus.PENDING_EVALUATION,
        RenewalStatus.CHECKING_PORTAL,
        RenewalStatus.PORTAL_UNAVAILABLE,
        RenewalStatus.OUTREACH_PENDING,
        RenewalStatus.EMAIL_SENT_AWAITING_REPLY,
        RenewalStatus.FOLLOWUP_SENT,
        RenewalStatus.ESCALATED_MANUAL,
    }
)

_CHANNEL_ATTEMPT_ACTIONS = frozenset(
    {
        ActionType.PORTAL_CHECK,
        ActionType.INITIAL_EMAIL_SENT,
        ActionType.FOLLOWUP_EMAIL_SENT,
        ActionType.OUTREACH_SENT,
    }
)


def cadence_voice_identity(policy: PolicyRenewal) -> str:
    """Stable per policy / applicant / term identity for the one-call guard."""
    term = ""
    if getattr(policy, "expiration_date", None):
        term = policy.expiration_date.isoformat()
    term = term or "unknown-term"
    applicant = str(getattr(policy, "applicant_id", "") or "unknown")
    number = str(getattr(policy, "policy_number", "") or "unknown")
    return f"renewal-cadence:{applicant}:{number}:{term}"


def renewal_already_obtained(policy: PolicyRenewal) -> bool:
    """True when a renewal PDF, UW reply, or in-hand status is already present."""
    status = getattr(policy, "status", None)
    if status in RENEWAL_IN_HAND_STATUSES:
        return True
    if getattr(policy, "renewal_premium", None):
        return True
    documents = getattr(policy, "documents", None) or []
    if documents:
        return True
    for thread in getattr(policy, "threads", None) or []:
        if getattr(thread, "status", None) in (ThreadStatus.REPLIED, ThreadStatus.RESOLVED):
            return True
        if getattr(thread, "latest_reply_summary", None):
            return True
    return False


def count_unsuccessful_channel_attempts(policy: PolicyRenewal) -> int:
    """Count prior email + unsuccessful portal attempts for this policy/term."""
    count = 0
    for note in getattr(policy, "notes", None) or []:
        action = getattr(note, "action_type", None)
        if action not in _CHANNEL_ATTEMPT_ACTIONS:
            continue
        text = str(getattr(note, "note_text", "") or "").upper()
        if action == ActionType.PORTAL_CHECK and "RENEWAL QUOTE RETRIEVED" in text:
            continue
        count += 1
    if count:
        return count
    # Legacy rows that have a thread but no audit notes yet.
    for thread in getattr(policy, "threads", None) or []:
        sent = 0
        if getattr(thread, "initial_sent_at", None):
            sent += 1
        sent += int(getattr(thread, "followup_count", 0) or 0)
        if sent:
            return sent
    return 0


def carrier_voice_already_placed(
    policy: PolicyRenewal,
    store: Optional[ProcessedRobieCallStore] = None,
) -> bool:
    """True when the one carrier Robie Call for this policy/term already ran."""
    identity = cadence_voice_identity(policy)
    if store is not None and store.has(identity):
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


def resolve_carrier_e164(
    policy: PolicyRenewal,
    hydrator: Optional[ContextHydrator] = None,
    phone_override: Optional[str] = None,
) -> Optional[str]:
    """Resolve an existing E.164 carrier/UW phone. Never invent a number."""
    if phone_override:
        return normalize_phone_e164(phone_override)
    carrier = getattr(policy, "carrier_name", None)
    direct = lookup_carrier_phone(carrier)
    if direct:
        return normalize_phone_e164(direct)
    if hydrator is None:
        return None
    try:
        dossier = hydrator.hydrate(policy_number=getattr(policy, "policy_number", None))
    except Exception as exc:
        logger.debug("Carrier phone hydrate skipped for %s: %s", policy.policy_number, exc)
        return None
    if not dossier:
        return None
    return normalize_phone_e164(dossier.carrier_phone)


class RenewalCarrierVoiceCadence:
    """Enqueue exactly one carrier voice call after two unsuccessful attempts."""

    def __init__(
        self,
        ezlynx_client: Optional[EZLynxApiClient] = None,
        voice_client: Optional[CarrierVoiceClient] = None,
        hydrator: Optional[ContextHydrator] = None,
        processed_store: Optional[ProcessedRobieCallStore] = None,
    ):
        self.ezlynx = ezlynx_client or EZLynxApiClient()
        self.voice = voice_client or CarrierVoiceClient()
        self.hydrator = hydrator or ContextHydrator()
        self.processed_store = processed_store or ProcessedRobieCallStore()

    def process_policy(
        self,
        policy: PolicyRenewal,
        *,
        db=None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Evaluate one policy. Never dials the client."""
        identity = cadence_voice_identity(policy)
        base = {
            "policy_number": policy.policy_number,
            "applicant_id": policy.applicant_id,
            "identity": identity,
            "call_type": CALL_TYPE_CARRIER,
        }

        if renewal_already_obtained(policy):
            logger.info(
                "Carrier voice cadence skip %s: renewal already obtained (%s).",
                policy.policy_number,
                getattr(policy.status, "value", policy.status),
            )
            return {**base, "status": "SKIPPED_RENEWAL_OBTAINED"}

        status = getattr(policy, "status", None)
        if status not in ELIGIBLE_WAITING_STATUSES:
            return {**base, "status": "SKIPPED_NOT_WAITING"}

        if carrier_voice_already_placed(policy, self.processed_store):
            logger.info(
                "Carrier voice cadence skip %s: call already placed for this term.",
                policy.policy_number,
            )
            return {**base, "status": "SKIPPED_ALREADY_CALLED"}

        attempts = count_unsuccessful_channel_attempts(policy)
        if attempts < CARRIER_VOICE_ATTEMPT_THRESHOLD:
            return {
                **base,
                "status": "SKIPPED_UNDER_BUDGET",
                "attempts": attempts,
            }

        phone = resolve_carrier_e164(policy, hydrator=self.hydrator)
        if not phone:
            note_text = EZLynxNoteBuilder.format_carrier_voice_phone_needed_note(policy)
            self._post_and_log(policy, note_text, db=db)
            self.processed_store.mark(
                identity,
                applicant_id=policy.applicant_id,
                status="CLARIFICATION_NEEDED",
                dry_run=dry_run,
            )
            return {
                **base,
                "status": "CLARIFICATION_NEEDED",
                "reason": "MISSING_CARRIER_PHONE",
                "attempts": attempts,
            }

        instructions = (
            f"Follow up on the upcoming renewal for {policy.insured_name}, "
            f"policy {policy.policy_number}. Ask if renewal terms or a quote "
            f"have been released, and where the packet was delivered."
        )
        dossier = self.hydrator.hydrate(
            policy_number=policy.policy_number,
            phone_override=phone,
            instructions=instructions,
            call_type=CALL_TYPE_CARRIER,
        )
        if dossier is None:
            return {**base, "status": "HYDRATE_FAILED", "attempts": attempts}
        dossier.call_type = CALL_TYPE_CARRIER
        dossier.carrier_phone = phone

        result = self.voice.dispatch_call(dossier=dossier, dry_run=dry_run)
        call_id = result.get("call_id")
        status_label = result.get("status") or (
            "DISPATCHED" if result.get("success") else "FAILED"
        )
        note_text = EZLynxNoteBuilder.format_carrier_voice_cadence_note(
            policy,
            phone=phone,
            call_id=call_id,
            status=status_label,
            attempts=attempts,
        )
        self._post_and_log(policy, note_text, db=db)
        self.processed_store.mark(
            identity,
            applicant_id=policy.applicant_id,
            status=status_label,
            dry_run=dry_run,
        )
        return {
            **base,
            "status": status_label,
            "success": bool(result.get("success")),
            "call_id": call_id,
            "phone": phone,
            "attempts": attempts,
            "dry_run": dry_run,
        }

    def process_due_policies(
        self,
        db,
        *,
        dry_run: bool = False,
        policies: Optional[List[PolicyRenewal]] = None,
    ) -> Dict[str, Any]:
        """Scan waiting renewals and enqueue at most one carrier call each."""
        rows = policies
        if rows is None:
            rows = (
                db.query(PolicyRenewal)
                .filter(PolicyRenewal.status.in_(list(ELIGIBLE_WAITING_STATUSES)))
                .all()
            )
        dispatched: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []
        for policy in rows:
            outcome = self.process_policy(policy, db=db, dry_run=dry_run)
            status = outcome.get("status") or ""
            if status.startswith("SKIPPED"):
                skipped.append(outcome)
            else:
                dispatched.append(outcome)
        if db is not None:
            try:
                db.commit()
            except Exception as exc:
                logger.debug("Cadence commit skipped: %s", exc)
        return {
            "dispatched": dispatched,
            "skipped": skipped,
            "dry_run": dry_run,
        }

    def _post_and_log(
        self,
        policy: PolicyRenewal,
        note_text: str,
        *,
        db=None,
    ) -> None:
        header_pol = policy.policy_number
        if not note_text.startswith("Policy:"):
            note_text = (
                f"Policy: #{header_pol} "
                f"({policy.line_of_business or 'Commercial'} - {policy.carrier_name})\n\n"
                f"{note_text}"
            )
        self.ezlynx.add_note_to_discussion(
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


def process_carrier_voice_cadence(
    db,
    *,
    dry_run: bool = False,
    cadence: Optional[RenewalCarrierVoiceCadence] = None,
) -> Dict[str, Any]:
    """Daily-pipeline entry: one carrier Robie Call after two misses."""
    engine = cadence or RenewalCarrierVoiceCadence()
    return engine.process_due_policies(db, dry_run=dry_run)
