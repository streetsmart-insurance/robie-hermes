"""Unit tests for the 6-factor Eligibility Gate."""

import unittest
from datetime import datetime, timedelta

from src.gates.eligibility_gate import EligibilityGate
from src.gates.suppression_engine import SuppressionEngine
from src.models.cadence_models import (
    ApplicantLead,
    ApplicantStatus,
    CadenceEnrollment,
    CadenceType,
    ChannelType,
    ContactConsent,
    Opportunity,
    OpportunityStage,
    StopReason,
)


class TestEligibilityGate(unittest.TestCase):
    def setUp(self):
        self.suppression_engine = SuppressionEngine()
        self.suppression_engine.clear()
        self.gate = EligibilityGate(suppression_engine=self.suppression_engine)
        self.valid_time = datetime(2026, 9, 8, 14, 0, 0)  # Tuesday 2:00 PM ET

    def _sample_lead(self, **overrides) -> ApplicantLead:
        data = {
            "applicant_id": "AP-101",
            "first_name": "John",
            "last_name": "Smith",
            "phone": "+17325551234",
            "email": "john.smith@example.com",
            "lead_source": "StreetSmart Website",
            "assigned_producer": "Jake Ferrara",
            "assigned_producer_phone": "+17324812520",
            "client_status": ApplicantStatus.PROSPECT_LEAD,
            "consent": ContactConsent(voice_consent=True, sms_consent=True, email_consent=True),
        }
        data.update(overrides)
        return ApplicantLead(**data)

    def _sample_opp(self, **overrides) -> Opportunity:
        data = {
            "opportunity_id": "OPP-101",
            "applicant_id": "AP-101",
            "line_of_business": "Personal Auto",
            "stage": OpportunityStage.NEW,
            "producer_name": "Jake Ferrara",
        }
        data.update(overrides)
        return Opportunity(**data)

    def test_all_rules_pass_on_valid_prospect(self):
        lead = self._sample_lead()
        opp = self._sample_opp()
        res = self.gate.verify_outreach_eligibility(lead, opp, ChannelType.VOICE, current_time=self.valid_time)
        self.assertTrue(res.is_eligible)
        self.assertEqual(len(res.passed_rules), 6)
        self.assertIsNone(res.failed_rule)

    def test_rule_1_fails_on_unapproved_lead_source(self):
        lead = self._sample_lead(lead_source="Random Craigslist Lead")
        opp = self._sample_opp()
        res = self.gate.verify_outreach_eligibility(lead, opp, ChannelType.VOICE, current_time=self.valid_time)
        self.assertFalse(res.is_eligible)
        self.assertEqual(res.failed_rule, "RULE_1_SOURCE_AND_PRODUCER")

    def test_rule_1_fails_on_mismatched_pilot_producer(self):
        lead = self._sample_lead(assigned_producer="Unapproved Producer")
        opp = self._sample_opp()
        res = self.gate.verify_outreach_eligibility(lead, opp, ChannelType.VOICE, current_time=self.valid_time)
        self.assertFalse(res.is_eligible)
        self.assertEqual(res.failed_rule, "RULE_1_SOURCE_AND_PRODUCER")

    def test_rule_2_fails_if_applicant_is_active_client(self):
        lead = self._sample_lead(client_status=ApplicantStatus.ACTIVE_CLIENT)
        opp = self._sample_opp()
        res = self.gate.verify_outreach_eligibility(lead, opp, ChannelType.VOICE, current_time=self.valid_time)
        self.assertFalse(res.is_eligible)
        self.assertEqual(res.failed_rule, "RULE_2_STATUS_CHECK")

    def test_rule_2_fails_if_opportunity_is_won_or_lost(self):
        lead = self._sample_lead()
        opp_won = self._sample_opp(stage=OpportunityStage.WON)
        res_won = self.gate.verify_outreach_eligibility(lead, opp_won, ChannelType.VOICE, current_time=self.valid_time)
        self.assertFalse(res_won.is_eligible)
        self.assertEqual(res_won.failed_rule, "RULE_2_STATUS_CHECK")

        opp_lost = self._sample_opp(stage=OpportunityStage.LOST)
        res_lost = self.gate.verify_outreach_eligibility(lead, opp_lost, ChannelType.VOICE, current_time=self.valid_time)
        self.assertFalse(res_lost.is_eligible)
        self.assertEqual(res_lost.failed_rule, "RULE_2_STATUS_CHECK")

    def test_rule_3_fails_if_channel_consent_missing(self):
        lead = self._sample_lead(consent=ContactConsent(voice_consent=False, sms_consent=True, email_consent=True))
        opp = self._sample_opp()
        res = self.gate.verify_outreach_eligibility(lead, opp, ChannelType.VOICE, current_time=self.valid_time)
        self.assertFalse(res.is_eligible)
        self.assertEqual(res.failed_rule, "RULE_3_CONTACT_CONSENT")

        # But SMS should still pass
        res_sms = self.gate.verify_outreach_eligibility(lead, opp, ChannelType.SMS, current_time=self.valid_time)
        self.assertTrue(res_sms.is_eligible)

    def test_rule_4_fails_if_suppressed_in_global_ledger(self):
        lead = self._sample_lead()
        opp = self._sample_opp()
        self.suppression_engine.add_suppression(
            phone=lead.phone,
            applicant_id=lead.applicant_id,
            reason=StopReason.OPT_OUT_CALL,
        )
        res = self.gate.verify_outreach_eligibility(lead, opp, ChannelType.VOICE, current_time=self.valid_time)
        self.assertFalse(res.is_eligible)
        self.assertEqual(res.failed_rule, "RULE_4_GLOBAL_SUPPRESSION")
        self.assertEqual(res.recommended_action, "PERMANENT_ABORT_SUPPRESSED")

    def test_rule_5_fails_if_voice_cooldown_under_24_hours(self):
        lead = self._sample_lead()
        opp = self._sample_opp()
        enrollment = CadenceEnrollment(
            enrollment_id="ENR-1",
            applicant_id=lead.applicant_id,
            opportunity_id=opp.opportunity_id,
            cadence_type=CadenceType.INBOUND_LEAD,
            current_touch=1,
            last_touch_at=self.valid_time - timedelta(hours=6),  # Only 6h ago
        )
        res = self.gate.verify_outreach_eligibility(
            lead, opp, ChannelType.VOICE, enrollment=enrollment, current_time=self.valid_time
        )
        self.assertFalse(res.is_eligible)
        self.assertEqual(res.failed_rule, "RULE_5_COOLDOWN_AND_DUPLICATE")

    def test_rule_6_fails_outside_business_hours_and_weekends(self):
        lead = self._sample_lead()
        opp = self._sample_opp()

        # Sunday 2:00 PM
        sunday_time = datetime(2026, 9, 6, 14, 0, 0)
        res_sunday = self.gate.verify_outreach_eligibility(lead, opp, ChannelType.VOICE, current_time=sunday_time)
        self.assertFalse(res_sunday.is_eligible)
        self.assertEqual(res_sunday.failed_rule, "RULE_6_CONTACT_HOURS")

        # Tuesday 3:00 AM
        night_time = datetime(2026, 9, 8, 3, 0, 0)
        res_night = self.gate.verify_outreach_eligibility(lead, opp, ChannelType.VOICE, current_time=night_time)
        self.assertFalse(res_night.is_eligible)
        self.assertEqual(res_night.failed_rule, "RULE_6_CONTACT_HOURS")


if __name__ == "__main__":
    unittest.main()
