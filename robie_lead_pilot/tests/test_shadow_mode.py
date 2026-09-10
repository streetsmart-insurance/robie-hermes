"""Unit tests for Shadow Mode Engine."""

import unittest
from datetime import datetime
from pathlib import Path

from src.gates.eligibility_gate import EligibilityGate
from src.gates.suppression_engine import SuppressionEngine
from src.models.cadence_models import (
    ApplicantLead,
    ApplicantStatus,
    CadenceType,
    Opportunity,
    OpportunityStage,
    StopReason,
)
from src.simulation.shadow_mode import ShadowModeEngine


class TestShadowMode(unittest.TestCase):
    def setUp(self):
        self.tmp_report = Path("data/test_shadow_report.json")
        self.supp_engine = SuppressionEngine()
        self.supp_engine.clear()
        self.gate = EligibilityGate(suppression_engine=self.supp_engine)
        self.shadow = ShadowModeEngine(
            suppression_engine=self.supp_engine,
            eligibility_gate=self.gate,
            report_output_path=self.tmp_report,
        )
        self.valid_time = datetime(2026, 9, 8, 14, 0, 0)

    def tearDown(self):
        if self.tmp_report.exists():
            self.tmp_report.unlink()

    def test_shadow_mode_evaluation_and_report_generation(self):
        # Candidate 1: Fully eligible
        lead_1 = ApplicantLead(
            applicant_id="SHADOW-1",
            first_name="Alice",
            last_name="Smith",
            phone="+17325551111",
            lead_source="StreetSmart Website",
            assigned_producer="Jake Ferrara",
        )
        opp_1 = Opportunity(
            opportunity_id="OPP-S1",
            applicant_id="SHADOW-1",
            line_of_business="Personal Auto",
            stage=OpportunityStage.NEW,
        )

        # Candidate 2: Ineligible (Suppressed)
        lead_2 = ApplicantLead(
            applicant_id="SHADOW-2",
            first_name="Bob",
            last_name="Jones",
            phone="+17325552222",
            lead_source="StreetSmart Website",
            assigned_producer="Jake Ferrara",
        )
        opp_2 = Opportunity(
            opportunity_id="OPP-S2",
            applicant_id="SHADOW-2",
            line_of_business="Personal Auto",
            stage=OpportunityStage.NEW,
        )
        self.supp_engine.add_suppression(phone=lead_2.phone, reason=StopReason.OPT_OUT_CALL)

        dec1 = self.shadow.evaluate_candidate(
            lead=lead_1,
            opportunity=opp_1,
            cadence_type=CadenceType.INBOUND_LEAD,
            touch_number=1,
            current_time=self.valid_time,
        )
        self.assertTrue(dec1.is_eligible)
        self.assertIn("WOULD_DISPATCH_VOICE_CALL", dec1.hypothetical_action)
        self.assertIsNotNone(dec1.first_sentence_preview)

        dec2 = self.shadow.evaluate_candidate(
            lead=lead_2,
            opportunity=opp_2,
            cadence_type=CadenceType.INBOUND_LEAD,
            touch_number=1,
            current_time=self.valid_time,
        )
        self.assertFalse(dec2.is_eligible)
        self.assertIn("RULE_4_GLOBAL_SUPPRESSION", dec2.hypothetical_action)

        report = self.shadow.generate_report()
        self.assertEqual(report["total_evaluated"], 2)
        self.assertEqual(report["would_have_dispatched"], 1)
        self.assertEqual(report["would_have_skipped"], 1)
        self.assertEqual(report["cost_incurred"], "$0.00")
        self.assertTrue(self.tmp_report.exists())


if __name__ == "__main__":
    unittest.main()
