"""
Stopping Logic Engine for Robie Lead & Quote Cadences.

Monitors account status changes, opportunity outcomes, inbound opt-out signals,
and employee note commands, instantly halting outreach and updating the
Global Suppression Registry.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from src.gates.suppression_engine import SuppressionEngine
from src.models.cadence_models import (
    ApplicantLead,
    ApplicantStatus,
    CadenceEnrollment,
    CadenceStatus,
    CadenceType,
    ChannelType,
    GlobalSuppressionRecord,
    Opportunity,
    OpportunityStage,
    StopReason,
)

logger = logging.getLogger("stopping_logic")

# Verbal opt-out patterns from Bland AI call transcripts
VOICE_OPT_OUT_PATTERNS = [
    r"\b(?:stop\s+calling(?:\s+me)?)\b",
    r"\b(?:don'?t\s+call(?:\s+me)?(?:\s+again)?)\b",
    r"\b(?:remove\s+(?:me|my\s+number)(?:\s+from(?:\s+your)?\s+list)?)\b",
    r"\b(?:take\s+me\s+off(?:\s+your)?\s+list)\b",
    r"\b(?:do\s+not\s+call)\b",
    r"\b(?:not\s+interested)\b",
    r"\b(?:wrong\s+number)\b",
    r"\b(?:unsubscribe)\b",
]

# SMS opt-out keywords
SMS_STOP_KEYWORDS = frozenset({"stop", "unsubscribe", "cancel", "quit", "end", "stopall"})

# EZLynx employee note triggers
EZLYNX_STOP_TRIGGERS = [
    r"\b(?:robie\s+stop)\b",
    r"\b(?:stop\s+robie)\b",
    r"\b(?:robie\s+cancel)\b",
    r"\b(?:cancel\s+robie)\b",
    r"\b(?:robie\s+pause)\b",
    r"\b(?:dnc|do\s+not\s+call)\b",
    r"\b(?:opt\s*out)\b",
]


@dataclass
class StopEvaluationResult:
    should_stop: bool
    reason: Optional[StopReason] = None
    trigger_source: Optional[str] = None
    created_suppression: Optional[GlobalSuppressionRecord] = None
    ezlynx_audit_note: Optional[str] = None


class StoppingLogicEngine:
    def __init__(self, suppression_engine: Optional[SuppressionEngine] = None):
        self.suppression_engine = suppression_engine or SuppressionEngine()

    def evaluate(
        self,
        lead: ApplicantLead,
        opportunity: Opportunity,
        enrollment: CadenceEnrollment,
        inbound_event: Optional[Dict[str, Any]] = None,
    ) -> StopEvaluationResult:
        """
        Evaluates whether an active cadence must halt immediately.
        """
        if enrollment.status == CadenceStatus.STOPPED:
            return StopEvaluationResult(
                should_stop=True,
                reason=enrollment.stop_reason or StopReason.EMPLOYEE_PAUSE,
                trigger_source="ALREADY_STOPPED",
            )

        # 1. Account Status Changed to ActiveClient (Bound / Won)
        if lead.client_status == ApplicantStatus.ACTIVE_CLIENT:
            return self._halt_enrollment(
                enrollment,
                lead,
                reason=StopReason.WON_BOUND,
                trigger_source="APPLICANT_STATUS_ACTIVE_CLIENT",
                create_suppression=False,  # Client is now active, not an opt-out
            )

        # 2. Opportunity Stage Moved to Won
        if opportunity.stage == OpportunityStage.WON:
            return self._halt_enrollment(
                enrollment,
                lead,
                reason=StopReason.WON_BOUND,
                trigger_source="OPPORTUNITY_STAGE_WON",
                create_suppression=False,
            )

        # 3. Opportunity Stage Moved to Lost, Dead, or Closed
        if opportunity.stage in (OpportunityStage.LOST, OpportunityStage.DEAD, OpportunityStage.CLOSED):
            reason = StopReason.DEAD if opportunity.stage == OpportunityStage.DEAD else StopReason.LOST_CLOSED
            return self._halt_enrollment(
                enrollment,
                lead,
                reason=reason,
                trigger_source=f"OPPORTUNITY_STAGE_{opportunity.stage.value.upper()}",
                create_suppression=False,
            )

        # 4. Inbound Event Inspection (Voice, SMS, Email, EZLynx Note)
        if inbound_event:
            event_type = inbound_event.get("type", "").lower()

            # 4a. Voice Call Completed / Transcript
            if event_type == "voice_call":
                transcript = (inbound_event.get("transcript") or "").lower()
                disposition = (inbound_event.get("disposition") or "").lower()
                if disposition in ("not_interested", "wrong_number", "dnc"):
                    return self._halt_enrollment(
                        enrollment,
                        lead,
                        reason=StopReason.OPT_OUT_CALL,
                        trigger_source=f"VOICE_DISPOSITION_{disposition.upper()}",
                        create_suppression=True,
                    )
                for pattern in VOICE_OPT_OUT_PATTERNS:
                    if re.search(pattern, transcript):
                        return self._halt_enrollment(
                            enrollment,
                            lead,
                            reason=StopReason.OPT_OUT_CALL,
                            trigger_source=f"VOICE_TRANSCRIPT_MATCH: {pattern}",
                            create_suppression=True,
                        )

            # 4b. SMS Inbound Reply
            elif event_type == "sms_inbound":
                text = (inbound_event.get("body") or "").strip().lower()
                if text in SMS_STOP_KEYWORDS or any(kw in text.split() for kw in SMS_STOP_KEYWORDS):
                    return self._halt_enrollment(
                        enrollment,
                        lead,
                        reason=StopReason.OPT_OUT_SMS_STOP,
                        trigger_source=f"SMS_STOP_KEYWORD: {text}",
                        create_suppression=True,
                    )

            # 4c. Email Inbound Reply
            elif event_type == "email_inbound":
                body = (inbound_event.get("body") or "").lower()
                subject = (inbound_event.get("subject") or "").lower()
                combined = f"{subject} {body}"
                if any(re.search(pat, combined) for pat in VOICE_OPT_OUT_PATTERNS) or "unsubscribe" in combined:
                    return self._halt_enrollment(
                        enrollment,
                        lead,
                        reason=StopReason.OPT_OUT_EMAIL,
                        trigger_source="EMAIL_UNSUBSCRIBE_INTENT",
                        create_suppression=True,
                    )

            # 4d. EZLynx Note or Discussion Added
            elif event_type == "ezlynx_note":
                note_text = (inbound_event.get("note_text") or "").lower()
                for pattern in EZLYNX_STOP_TRIGGERS:
                    if re.search(pattern, note_text):
                        return self._halt_enrollment(
                            enrollment,
                            lead,
                            reason=StopReason.EZLYNX_NOTE_KEYWORD,
                            trigger_source=f"EZLYNX_NOTE_MATCH: {pattern}",
                            create_suppression=True,
                        )

            # 4e. Manual Employee Pause / Cancel Command
            elif event_type == "employee_action":
                action = inbound_event.get("action", "").lower()
                if action in ("pause", "cancel", "stop"):
                    return self._halt_enrollment(
                        enrollment,
                        lead,
                        reason=StopReason.EMPLOYEE_PAUSE,
                        trigger_source=f"EMPLOYEE_ACTION_{action.upper()}",
                        create_suppression=False,
                    )

        return StopEvaluationResult(should_stop=False)

    def _halt_enrollment(
        self,
        enrollment: CadenceEnrollment,
        lead: ApplicantLead,
        reason: StopReason,
        trigger_source: str,
        create_suppression: bool = False,
    ) -> StopEvaluationResult:
        enrollment.status = CadenceStatus.STOPPED
        enrollment.stop_reason = reason
        enrollment.next_touch_due = None

        supp_rec = None
        if create_suppression:
            supp_rec = self.suppression_engine.add_suppression(
                phone=lead.phone,
                email=lead.email,
                applicant_id=lead.applicant_id,
                channel=ChannelType.ALL,
                reason=reason,
                source_workflow=enrollment.cadence_type,
                notes=f"Triggered by {trigger_source} at {datetime.utcnow().isoformat()}",
            )

        audit_note = (
            f"🛑 ROBIE NOTICE: Cadence '{enrollment.cadence_type.value}' halted immediately.\n"
            f"Reason: {reason.value} ({trigger_source})\n"
        )
        if supp_rec:
            audit_note += (
                f"Global Suppression ID: {supp_rec.record_id} (All future automated outreach blocked).\n"
            )
        audit_note += "\nROBIE was here"

        logger.info(
            "🛑 Halting enrollment %s for Applicant %s: reason=%s source=%s suppression=%s",
            enrollment.enrollment_id,
            lead.applicant_id,
            reason.value,
            trigger_source,
            supp_rec.record_id if supp_rec else "None",
        )

        return StopEvaluationResult(
            should_stop=True,
            reason=reason,
            trigger_source=trigger_source,
            created_suppression=supp_rec,
            ezlynx_audit_note=audit_note,
        )
