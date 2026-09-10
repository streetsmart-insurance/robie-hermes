"""
Shadow Mode Engine for Robie Lead & Quote Outreach.

Safely evaluates candidates from Sales Center / EZLynx without sending any
external network requests. Logs exact hypothetical actions, prompts, and
timings for executive review.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.gates.eligibility_gate import EligibilityGate
from src.gates.stopping_logic import StoppingLogicEngine
from src.gates.suppression_engine import SuppressionEngine
from src.models.cadence_models import (
    ApplicantLead,
    CadenceEnrollment,
    CadenceType,
    ChannelType,
    Opportunity,
    QuoteSummary,
)
from src.scripts.voice_scripts import VoiceScriptBuilder

logger = logging.getLogger("shadow_mode")


@dataclass
class ShadowDecision:
    lead_id: str
    lead_name: str
    phone: str
    channel: str
    is_eligible: bool
    eligibility_reason: Optional[str]
    hypothetical_action: str
    first_sentence_preview: Optional[str]
    timestamp: str


class ShadowModeEngine:
    def __init__(
        self,
        suppression_engine: Optional[SuppressionEngine] = None,
        eligibility_gate: Optional[EligibilityGate] = None,
        report_output_path: Optional[Path] = None,
    ):
        self.suppression_engine = suppression_engine or SuppressionEngine()
        self.eligibility_gate = eligibility_gate or EligibilityGate(suppression_engine=self.suppression_engine)
        self.stopping_logic = StoppingLogicEngine(suppression_engine=self.suppression_engine)
        self.report_output_path = report_output_path or Path("data/shadow_mode_report.json")
        self.decisions: List[ShadowDecision] = []

    def evaluate_candidate(
        self,
        lead: ApplicantLead,
        opportunity: Opportunity,
        cadence_type: CadenceType,
        touch_number: int,
        quote: Optional[QuoteSummary] = None,
        current_time: Optional[datetime] = None,
    ) -> ShadowDecision:
        now = current_time or datetime.now()
        channel = ChannelType.VOICE

        eligibility = self.eligibility_gate.verify_outreach_eligibility(
            lead=lead,
            opportunity=opportunity,
            channel=channel,
            current_time=now,
        )

        preview = None
        if eligibility.is_eligible:
            pkg = VoiceScriptBuilder.build_script(
                cadence_type=cadence_type,
                touch_number=touch_number,
                lead=lead,
                opportunity=opportunity,
                quote=quote,
                producer_transfer_did=lead.assigned_producer_phone,
            )
            preview = pkg.first_sentence
            action = f"WOULD_DISPATCH_VOICE_CALL (Touch {touch_number})"
        else:
            action = f"WOULD_SKIP ({eligibility.failed_rule})"

        decision = ShadowDecision(
            lead_id=lead.applicant_id,
            lead_name=lead.full_name,
            phone=lead.phone,
            channel=channel.value,
            is_eligible=eligibility.is_eligible,
            eligibility_reason=eligibility.reason,
            hypothetical_action=action,
            first_sentence_preview=preview,
            timestamp=now.isoformat(),
        )
        self.decisions.append(decision)
        return decision

    def generate_report(self) -> Dict[str, Any]:
        eligible_count = sum(1 for d in self.decisions if d.is_eligible)
        skipped_count = len(self.decisions) - eligible_count
        report = {
            "mode": "SHADOW_SIMULATION",
            "total_evaluated": len(self.decisions),
            "would_have_dispatched": eligible_count,
            "would_have_skipped": skipped_count,
            "cost_incurred": "$0.00",
            "decisions": [
                {
                    "lead_id": d.lead_id,
                    "lead_name": d.lead_name,
                    "phone": d.phone,
                    "channel": d.channel,
                    "is_eligible": d.is_eligible,
                    "eligibility_reason": d.eligibility_reason,
                    "hypothetical_action": d.hypothetical_action,
                    "first_sentence_preview": d.first_sentence_preview,
                    "timestamp": d.timestamp,
                }
                for d in self.decisions
            ],
        }

        try:
            self.report_output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.report_output_path, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2)
            logger.info("Shadow mode report saved to %s", self.report_output_path)
        except Exception as e:
            logger.error("Failed to save shadow mode report: %s", e)

        return report
