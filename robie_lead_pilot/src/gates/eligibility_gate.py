"""
Eligibility Gate: 6-Factor Atomic Pre-Flight Verification for Robie Outreach.

Every single outreach touch must pass all 6 checks before any voice call, SMS,
or email can be dispatched.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from src.gates.suppression_engine import SuppressionEngine
from src.models.cadence_models import (
    ApplicantLead,
    ApplicantStatus,
    CadenceEnrollment,
    ChannelType,
    Opportunity,
    OpportunityStage,
)

logger = logging.getLogger("eligibility_gate")

# NJ & Tri-state area codes (Eastern Time)
EASTERN_AREA_CODES = {
    "201", "551", "609", "732", "848", "856", "862", "908", "973",  # NJ
    "212", "332", "646", "718", "917", "347", "929", "516", "631", "914", "845",  # NY
    "215", "267", "445", "484", "610", "570", "717", "814",  # PA
}

# Central, Mountain, Pacific mapping prefixes
CENTRAL_AREA_CODES = {"312", "773", "847", "630", "815", "214", "469", "972", "713", "281", "832", "512", "210"}
MOUNTAIN_AREA_CODES = {"303", "720", "970", "719", "602", "480", "623", "520", "801", "385", "435", "505"}
PACIFIC_AREA_CODES = {"206", "425", "253", "503", "971", "415", "628", "510", "341", "408", "669", "213", "323", "310", "424", "818", "747", "619", "858"}


@dataclass
class EligibilityResult:
    is_eligible: bool
    passed_rules: List[str]
    failed_rule: Optional[str] = None
    reason: Optional[str] = None
    recommended_action: Optional[str] = None


class EligibilityGate:
    def __init__(
        self,
        suppression_engine: Optional[SuppressionEngine] = None,
        config_path: Optional[Path] = None,
    ):
        self.suppression_engine = suppression_engine or SuppressionEngine()
        self.config_path = config_path or Path("config/pilot_config.json")
        self.config = self._load_config()

    def _load_config(self) -> Dict:
        if self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning("Failed to load config from %s: %s", self.config_path, e)
        return {
            "approved_producer": {"name": "Jake Ferrara", "transfer_did": "+17324812520"},
            "approved_lines_of_business": ["Personal Auto", "Homeowners"],
            "approved_lead_sources": ["StreetSmart Website", "Website Inbound", "EverQuote", "QuoteWizard", "Sales Center X-Date"],
            "contact_hours": {"start_hour": 9, "end_hour": 18, "allowed_days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]},
        }

    def verify_outreach_eligibility(
        self,
        lead: ApplicantLead,
        opportunity: Opportunity,
        channel: ChannelType,
        enrollment: Optional[CadenceEnrollment] = None,
        current_time: Optional[datetime] = None,
    ) -> EligibilityResult:
        """
        Executes the 6-factor atomic pre-flight verification.
        """
        passed: List[str] = []
        now = current_time or datetime.now()

        # Rule 1: Lead Source & Assigned Producer Check
        rule1_ok, rule1_reason = self._check_lead_source_and_producer(lead, opportunity)
        if not rule1_ok:
            return EligibilityResult(
                is_eligible=False,
                passed_rules=passed,
                failed_rule="RULE_1_SOURCE_AND_PRODUCER",
                reason=rule1_reason,
                recommended_action="SKIP_UNAPPROVED_SOURCE_OR_PRODUCER",
            )
        passed.append("RULE_1_SOURCE_AND_PRODUCER")

        # Rule 2: Client & Opportunity Status Check
        rule2_ok, rule2_reason = self._check_client_and_opportunity_status(lead, opportunity)
        if not rule2_ok:
            return EligibilityResult(
                is_eligible=False,
                passed_rules=passed,
                failed_rule="RULE_2_STATUS_CHECK",
                reason=rule2_reason,
                recommended_action="HALT_CADENCE_STATUS_CLOSED",
            )
        passed.append("RULE_2_STATUS_CHECK")

        # Rule 3: Contact Consent Check
        rule3_ok, rule3_reason = self._check_contact_consent(lead, channel)
        if not rule3_ok:
            return EligibilityResult(
                is_eligible=False,
                passed_rules=passed,
                failed_rule="RULE_3_CONTACT_CONSENT",
                reason=rule3_reason,
                recommended_action="BLOCK_CHANNEL_LACKS_CONSENT",
            )
        passed.append("RULE_3_CONTACT_CONSENT")

        # Rule 4: Do-Not-Call (DNC) & Global Suppression Status
        rule4_ok, rule4_reason = self._check_suppression_and_dnc(lead, channel)
        if not rule4_ok:
            return EligibilityResult(
                is_eligible=False,
                passed_rules=passed,
                failed_rule="RULE_4_GLOBAL_SUPPRESSION",
                reason=rule4_reason,
                recommended_action="PERMANENT_ABORT_SUPPRESSED",
            )
        passed.append("RULE_4_GLOBAL_SUPPRESSION")

        # Rule 5: Recent / Duplicate Outreach Cooldown
        rule5_ok, rule5_reason = self._check_duplicate_cooldown(enrollment, channel, now)
        if not rule5_ok:
            return EligibilityResult(
                is_eligible=False,
                passed_rules=passed,
                failed_rule="RULE_5_COOLDOWN_AND_DUPLICATE",
                reason=rule5_reason,
                recommended_action="POSTPONE_TO_NEXT_WINDOW",
            )
        passed.append("RULE_5_COOLDOWN_AND_DUPLICATE")

        # Rule 6: Appropriate Contact Hours (TCPA / Business Window)
        rule6_ok, rule6_reason = self._check_contact_hours(lead, now)
        if not rule6_ok:
            return EligibilityResult(
                is_eligible=False,
                passed_rules=passed,
                failed_rule="RULE_6_CONTACT_HOURS",
                reason=rule6_reason,
                recommended_action="RESCHEDULE_TO_BUSINESS_HOURS",
            )
        passed.append("RULE_6_CONTACT_HOURS")

        return EligibilityResult(
            is_eligible=True,
            passed_rules=passed,
            recommended_action="PROCEED_WITH_DISPATCH",
        )

    def _check_lead_source_and_producer(
        self, lead: ApplicantLead, opportunity: Opportunity
    ) -> Tuple[bool, Optional[str]]:
        approved_sources = self.config.get("approved_lead_sources", [])
        if lead.lead_source not in approved_sources:
            return False, f"Lead source '{lead.lead_source}' is not in approved list: {approved_sources}"

        approved_producer = self.config.get("approved_producer", {})
        expected_producer = approved_producer.get("name")
        if expected_producer and lead.assigned_producer.lower() != expected_producer.lower():
            # In pilot, verify lead is assigned to approved producer
            return False, f"Assigned producer '{lead.assigned_producer}' does not match pilot producer '{expected_producer}'"

        if not approved_producer.get("transfer_did"):
            return False, f"Pilot producer '{expected_producer}' has no transfer DID configured"

        return True, None

    def _check_client_and_opportunity_status(
        self, lead: ApplicantLead, opportunity: Opportunity
    ) -> Tuple[bool, Optional[str]]:
        if lead.client_status != ApplicantStatus.PROSPECT_LEAD:
            return False, f"Applicant status is '{lead.client_status.value}', expected '{ApplicantStatus.PROSPECT_LEAD.value}'"

        closed_stages = {
            OpportunityStage.WON,
            OpportunityStage.LOST,
            OpportunityStage.DEAD,
            OpportunityStage.CLOSED,
        }
        if opportunity.stage in closed_stages:
            return False, f"Opportunity stage is '{opportunity.stage.value}', cannot reach out to closed/won/lost opportunities"

        return True, None

    def _check_contact_consent(
        self, lead: ApplicantLead, channel: ChannelType
    ) -> Tuple[bool, Optional[str]]:
        if not lead.consent.has_consent(channel):
            return False, f"Explicit consent missing for channel '{channel.value}' on applicant {lead.applicant_id}"
        return True, None

    def _check_suppression_and_dnc(
        self, lead: ApplicantLead, channel: ChannelType
    ) -> Tuple[bool, Optional[str]]:
        suppressed, record = self.suppression_engine.is_suppressed(
            phone=lead.phone,
            email=lead.email,
            applicant_id=lead.applicant_id,
            channel=channel,
        )
        if suppressed and record:
            return False, f"Contact suppressed under record {record.record_id}: reason={record.reason.value}"
        return True, None

    def _check_duplicate_cooldown(
        self, enrollment: Optional[CadenceEnrollment], channel: ChannelType, now: datetime
    ) -> Tuple[bool, Optional[str]]:
        if not enrollment or not enrollment.last_touch_at:
            return True, None

        elapsed = now - enrollment.last_touch_at

        # Voice cooldown: strictly minimum 24 hours between phone calls
        if channel == ChannelType.VOICE:
            if elapsed < timedelta(hours=24):
                return False, f"Voice cooldown in effect: only {elapsed.total_seconds() / 3600:.1f} hours elapsed since last touch (min 24h)"

        # Overall channel cooldown: minimum 4 hours between any touches unless touch 0
        if elapsed < timedelta(hours=4) and enrollment.current_touch > 0:
            return False, f"Rapid touch cooldown in effect: {elapsed.total_seconds() / 3600:.1f} hours elapsed"

        return True, None

    def _check_contact_hours(
        self, lead: ApplicantLead, now: datetime
    ) -> Tuple[bool, Optional[str]]:
        # 1. Day of week check
        hours_cfg = self.config.get("contact_hours", {})
        allowed_days = hours_cfg.get("allowed_days", ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"])
        weekday_name = now.strftime("%A")
        if weekday_name not in allowed_days:
            return False, f"Outreach blocked on {weekday_name}; allowed days are {allowed_days}"

        # 2. Timezone offset inference from phone area code
        phone_digits = "".join(filter(str.isdigit, lead.phone))
        area_code = phone_digits[1:4] if phone_digits.startswith("1") and len(phone_digits) == 11 else phone_digits[:3]

        local_hour = now.hour
        # Area code offset adjustments relative to Eastern
        if area_code in CENTRAL_AREA_CODES:
            local_hour = (now.hour - 1) % 24
        elif area_code in MOUNTAIN_AREA_CODES:
            local_hour = (now.hour - 2) % 24
        elif area_code in PACIFIC_AREA_CODES:
            local_hour = (now.hour - 3) % 24

        start_hour = hours_cfg.get("start_hour", 9)
        end_hour = hours_cfg.get("end_hour", 18)

        if not (start_hour <= local_hour < end_hour):
            return False, f"Current recipient local hour {local_hour}:00 is outside allowable window ({start_hour}:00 - {end_hour}:00)"

        return True, None
