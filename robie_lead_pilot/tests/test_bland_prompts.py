"""Unit tests for Bland AI Voice Prompt Generation, Grounding & Escalation Rules."""

import unittest
from datetime import date

from src.models.cadence_models import (
    ApplicantLead,
    CadenceType,
    Opportunity,
    OpportunityStage,
    QuoteSummary,
)
from src.scripts.escalation_rules import EscalationClassifier, EscalationType
from src.scripts.voice_scripts import (
    AGENCY_MAIN_CALLBACK_DISPLAY,
    AGENCY_MAIN_CALLBACK_SPOKEN,
    VOICE_CALLER_ID_E164,
    VoiceScriptBuilder,
)


class TestBlandPromptsAndEscalations(unittest.TestCase):
    def setUp(self):
        self.lead = ApplicantLead(
            applicant_id="AP-301",
            first_name="Michael",
            last_name="Scott",
            phone="+17325557777",
            assigned_producer="Jake Ferrara",
            assigned_producer_phone="+17324812520",
        )
        self.opp = Opportunity(
            opportunity_id="OPP-301",
            applicant_id="AP-301",
            line_of_business="Personal Auto",
            stage=OpportunityStage.NEW,
        )

    def test_inbound_lead_prompt_structure_and_callbacks(self):
        pkg = VoiceScriptBuilder.build_script(
            cadence_type=CadenceType.INBOUND_LEAD,
            touch_number=1,
            lead=self.lead,
            opportunity=self.opp,
        )
        self.assertIn("Michael", pkg.first_sentence)
        self.assertIn("StreetSmart Insurance", pkg.first_sentence)
        self.assertIn("Jake Ferrara", pkg.first_sentence)

        # Grounding: Verify agency callback is main office, not personal DID
        self.assertIn(AGENCY_MAIN_CALLBACK_DISPLAY, pkg.voicemail_message)
        self.assertNotIn("+17324812520", pkg.voicemail_message)
        self.assertIn(AGENCY_MAIN_CALLBACK_SPOKEN, pkg.task_prompt)

        # Transfer configuration
        self.assertEqual(pkg.transfer_phone, "+17324812520")

    def test_strict_grounding_prevents_fake_rate_increase_or_expiration(self):
        # Quote with NO verified expiration or rate lock date
        quote = QuoteSummary(
            quote_id="Q-301",
            opportunity_id="OPP-301",
            applicant_id="AP-301",
            carrier_name="Progressive",
            line_of_business="Personal Auto",
            quoted_premium=850.00,
        )
        pkg = VoiceScriptBuilder.build_script(
            cadence_type=CadenceType.QUOTED_PROSPECT,
            touch_number=3,
            lead=self.lead,
            opportunity=self.opp,
            quote=quote,
        )
        # Verify strict prohibition on rate increases/expirations
        self.assertIn("Do NOT mention quote expiration or rate increases unless explicitly asked", pkg.task_prompt)
        self.assertNotIn("rate is expiring", pkg.first_sentence.lower())
        self.assertNotIn("expires today", pkg.first_sentence.lower())
        self.assertNotIn("rate will increase", pkg.first_sentence.lower())

    def test_grounding_with_verified_date(self):
        # Quote WITH verified expiration date
        quote = QuoteSummary(
            quote_id="Q-301",
            opportunity_id="OPP-301",
            applicant_id="AP-301",
            carrier_name="Progressive",
            line_of_business="Personal Auto",
            verified_expiration_date=date(2026, 9, 30),
        )
        pkg = VoiceScriptBuilder.build_script(
            cadence_type=CadenceType.QUOTED_PROSPECT,
            touch_number=1,
            lead=self.lead,
            opportunity=self.opp,
            quote=quote,
        )
        self.assertIn("verified as September 30", pkg.task_prompt)

    def test_escalation_classifier_bind_request(self):
        dec = EscalationClassifier.evaluate("I looked at the numbers and I'm ready to bind right now")
        self.assertEqual(dec.escalation_type, EscalationType.BIND_REQUEST)
        self.assertTrue(dec.requires_producer_transfer)
        self.assertEqual(dec.action, "WARM_TRANSFER")

    def test_escalation_classifier_coverage_advice_eo_guard(self):
        dec = EscalationClassifier.evaluate("What liability limits do you recommend for my car?")
        self.assertEqual(dec.escalation_type, EscalationType.COVERAGE_ADVICE)
        self.assertTrue(dec.requires_producer_transfer)
        self.assertEqual(dec.eo_risk_level, "HIGH")
        self.assertIn("can't advise on specific coverage limits", dec.spoken_transition)

    def test_escalation_classifier_human_request(self):
        dec = EscalationClassifier.evaluate("Are you a robot? Let me speak to a person.")
        self.assertEqual(dec.escalation_type, EscalationType.HUMAN_REQUEST)
        self.assertTrue(dec.requires_producer_transfer)

    def test_escalation_classifier_complaint(self):
        dec = EscalationClassifier.evaluate("Why are you spamming me? This is ridiculous service!")
        self.assertEqual(dec.escalation_type, EscalationType.COMPLAINT)
        self.assertTrue(dec.requires_producer_transfer)

    def test_escalation_classifier_confusion(self):
        dec = EscalationClassifier.evaluate("Who is this again? I didn't ask for any quote.")
        self.assertEqual(dec.escalation_type, EscalationType.CONFUSION)
        self.assertFalse(dec.requires_producer_transfer)
        self.assertEqual(dec.action, "CLARIFY_AND_OFFER_CALLBACK")


if __name__ == "__main__":
    unittest.main()
