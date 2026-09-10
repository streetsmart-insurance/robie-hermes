"""
Base Cadence Workflow State Machine for Robie.
"""

from __future__ import annotations

import logging
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from src.channels.bland_voice import BlandVoiceClient
from src.channels.email_dispatcher import EmailDispatcher
from src.channels.sms_dispatcher import SMSDispatcher
from src.gates.eligibility_gate import EligibilityGate
from src.gates.stopping_logic import StoppingLogicEngine
from src.models.cadence_models import (
    ApplicantLead,
    CadenceEnrollment,
    CadenceStatus,
    CadenceType,
    ChannelType,
    Opportunity,
    QuoteSummary,
    StopReason,
)

logger = logging.getLogger("base_workflow")


class BaseWorkflow(ABC):
    def __init__(
        self,
        cadence_type: CadenceType,
        eligibility_gate: Optional[EligibilityGate] = None,
        stopping_logic: Optional[StoppingLogicEngine] = None,
        voice_client: Optional[BlandVoiceClient] = None,
        email_dispatcher: Optional[EmailDispatcher] = None,
        sms_dispatcher: Optional[SMSDispatcher] = None,
    ):
        self.cadence_type = cadence_type
        self.eligibility_gate = eligibility_gate or EligibilityGate()
        self.stopping_logic = stopping_logic or StoppingLogicEngine()
        self.voice_client = voice_client or BlandVoiceClient()
        self.email_dispatcher = email_dispatcher or EmailDispatcher()
        self.sms_dispatcher = sms_dispatcher or SMSDispatcher()

    def enroll(
        self,
        lead: ApplicantLead,
        opportunity: Opportunity,
        quote: Optional[QuoteSummary] = None,
        start_time: Optional[datetime] = None,
    ) -> CadenceEnrollment:
        enrollment_id = f"ENROLL-{self.cadence_type.value[:3]}-{uuid.uuid4().hex[:8].upper()}"
        now = start_time or datetime.now()
        enrollment = CadenceEnrollment(
            enrollment_id=enrollment_id,
            applicant_id=lead.applicant_id,
            opportunity_id=opportunity.opportunity_id,
            cadence_type=self.cadence_type,
            current_touch=0,
            status=CadenceStatus.ACTIVE,
            last_touch_at=None,
            next_touch_due=self.calculate_first_touch_due(now, opportunity),
            history=[],
        )
        logger.info(
            "Enrolled applicant %s into %s (Enrollment: %s, Next Due: %s)",
            lead.applicant_id,
            self.cadence_type.value,
            enrollment.enrollment_id,
            enrollment.next_touch_due,
        )
        return enrollment

    @abstractmethod
    def calculate_first_touch_due(self, now: datetime, opportunity: Opportunity) -> datetime:
        pass

    @abstractmethod
    def calculate_next_touch_due(self, completed_touch: int, now: datetime, opportunity: Opportunity) -> Optional[datetime]:
        pass

    @abstractmethod
    def get_touch_channels(self, touch_number: int) -> List[ChannelType]:
        pass

    def evaluate_and_advance(
        self,
        lead: ApplicantLead,
        opportunity: Opportunity,
        enrollment: CadenceEnrollment,
        quote: Optional[QuoteSummary] = None,
        inbound_event: Optional[Dict[str, Any]] = None,
        current_time: Optional[datetime] = None,
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        now = current_time or datetime.now()

        # Step 1: Stopping Logic Evaluation
        stop_eval = self.stopping_logic.evaluate(
            lead=lead,
            opportunity=opportunity,
            enrollment=enrollment,
            inbound_event=inbound_event,
        )
        if stop_eval.should_stop:
            return {
                "action": "HALTED",
                "reason": stop_eval.reason.value if stop_eval.reason else "UNKNOWN",
                "trigger_source": stop_eval.trigger_source,
                "suppression_record": stop_eval.created_suppression,
                "audit_note": stop_eval.ezlynx_audit_note,
            }

        # Step 2: Check if Touch is Due
        target_touch = enrollment.current_touch
        if enrollment.next_touch_due and now < enrollment.next_touch_due:
            return {
                "action": "PENDING_SCHEDULE",
                "next_due": enrollment.next_touch_due.isoformat(),
                "hours_remaining": (enrollment.next_touch_due - now).total_seconds() / 3600,
            }

        channels = self.get_touch_channels(target_touch)
        if not channels:
            enrollment.status = CadenceStatus.COMPLETED
            enrollment.next_touch_due = None
            return {"action": "CADENCE_COMPLETED", "touch": target_touch}

        # Step 3: Eligibility Pre-Flight Gate
        results: Dict[str, Any] = {}
        permanently_suppressed = False
        for ch in channels:
            eligibility = self.eligibility_gate.verify_outreach_eligibility(
                lead=lead,
                opportunity=opportunity,
                channel=ch,
                enrollment=enrollment,
                current_time=now,
            )
            if not eligibility.is_eligible:
                logger.info(
                    "Touch %d channel %s skipped for applicant %s: %s",
                    target_touch,
                    ch.value,
                    lead.applicant_id,
                    eligibility.reason,
                )
                results[ch.value] = {
                    "dispatched": False,
                    "reason": eligibility.reason,
                    "failed_rule": eligibility.failed_rule,
                    "recommended_action": eligibility.recommended_action,
                }
                if eligibility.recommended_action == "PERMANENT_ABORT_SUPPRESSED":
                    permanently_suppressed = True
                continue

            # Step 4: Dispatch Channel
            if ch == ChannelType.VOICE:
                resp = self.voice_client.dispatch(
                    lead=lead,
                    opportunity=opportunity,
                    cadence_type=self.cadence_type,
                    touch_number=target_touch,
                    quote=quote,
                    dry_run=dry_run,
                )
                results["VOICE"] = resp
            elif ch == ChannelType.EMAIL:
                resp = self.email_dispatcher.dispatch(
                    lead=lead,
                    opportunity=opportunity,
                    cadence_type=self.cadence_type,
                    touch_number=target_touch,
                    quote=quote,
                    dry_run=dry_run,
                )
                results["EMAIL"] = resp
            elif ch == ChannelType.SMS:
                resp = self.sms_dispatcher.dispatch(
                    lead=lead,
                    opportunity=opportunity,
                    cadence_type=self.cadence_type,
                    touch_number=target_touch,
                    dry_run=dry_run,
                )
                results["SMS"] = resp

        # Step 5: Advance State or Handle Permanent Suppression
        any_dispatched = any(r.get("success") for r in results.values() if isinstance(r, dict))
        if permanently_suppressed:
            enrollment.status = CadenceStatus.STOPPED
            enrollment.stop_reason = StopReason.SUPPRESSION_MATCH
            enrollment.next_touch_due = None

        if any_dispatched:
            enrollment.last_touch_at = now
            enrollment.current_touch += 1
            next_due = self.calculate_next_touch_due(enrollment.current_touch, now, opportunity)
            enrollment.next_touch_due = next_due
            if next_due is None:
                enrollment.status = CadenceStatus.COMPLETED

            enrollment.history.append({
                "touch": target_touch,
                "timestamp": now.isoformat(),
                "channels": [c.value for c in channels],
                "results": results,
            })

        return {
            "action": "TOUCH_EXECUTED" if any_dispatched else "SKIPPED_INELIGIBLE",
            "touch": target_touch,
            "next_touch": enrollment.current_touch,
            "next_due": enrollment.next_touch_due.isoformat() if enrollment.next_touch_due else None,
            "status": enrollment.status.value,
            "channel_results": results,
        }
