"""Unit tests for the Cadence State Machines (Inbound, Quoted, X-Date)."""

import unittest
from datetime import date, datetime, timedelta

from src.gates.eligibility_gate import EligibilityGate
from src.gates.stopping_logic import StoppingLogicEngine
from src.gates.suppression_engine import SuppressionEngine
from src.models.cadence_models import (
    ApplicantLead,
    ApplicantStatus,
    CadenceStatus,
    ContactConsent,
    Opportunity,
    OpportunityStage,
    QuoteSummary,
)
from src.workflows.inbound_lead_workflow import InboundLeadWorkflow
from src.workflows.quoted_prospect_workflow import QuotedProspectWorkflow
from src.workflows.xdate_workflow import XDateWorkflow


class TestCadenceStateMachine(unittest.TestCase):
    def setUp(self):
        self.suppression_engine = SuppressionEngine()
        self.suppression_engine.clear()
        self.gate = EligibilityGate(suppression_engine=self.suppression_engine)
        self.stopping_logic = StoppingLogicEngine(suppression_engine=self.suppression_engine)

        self.lead = ApplicantLead(
            applicant_id="AP-401",
            first_name="Dwight",
            last_name="Schrute",
            phone="+17325553333",
            email="dwight@example.com",
            lead_source="StreetSmart Website",
            assigned_producer="Jake Ferrara",
            assigned_producer_phone="+17324812520",
            client_status=ApplicantStatus.PROSPECT_LEAD,
            consent=ContactConsent(voice_consent=True, sms_consent=True, email_consent=True),
        )

        self.opp = Opportunity(
            opportunity_id="OPP-401",
            applicant_id="AP-401",
            line_of_business="Personal Auto",
            stage=OpportunityStage.NEW,
            producer_name="Jake Ferrara",
        )
        self.t0 = datetime(2026, 9, 8, 10, 0, 0)  # Tuesday 10:00 AM

    def test_inbound_workflow_multi_day_progression(self):
        wf = InboundLeadWorkflow(
            eligibility_gate=self.gate,
            stopping_logic=self.stopping_logic,
        )
        enrollment = wf.enroll(self.lead, self.opp, start_time=self.t0)
        self.assertEqual(enrollment.status, CadenceStatus.ACTIVE)
        self.assertEqual(enrollment.current_touch, 0)

        # Touch 0: Immediate Ack (SMS + Email)
        adv_0 = wf.evaluate_and_advance(self.lead, self.opp, enrollment, current_time=self.t0 + timedelta(minutes=5))
        self.assertEqual(adv_0["action"], "TOUCH_EXECUTED")
        self.assertEqual(enrollment.current_touch, 1)

        # Attempting Touch 1 too early (before 24h) returns PENDING_SCHEDULE
        adv_early = wf.evaluate_and_advance(self.lead, self.opp, enrollment, current_time=self.t0 + timedelta(hours=2))
        self.assertEqual(adv_early["action"], "PENDING_SCHEDULE")

        # Touch 1: Day 1 Voice Call + Email (at T0 + 25h)
        t1_time = self.t0 + timedelta(days=1, hours=1)
        adv_1 = wf.evaluate_and_advance(self.lead, self.opp, enrollment, current_time=t1_time)
        self.assertEqual(adv_1["action"], "TOUCH_EXECUTED")
        self.assertEqual(enrollment.current_touch, 2)

        # Touch 2: Day 3 Email + SMS (at T1 + 49h)
        t2_time = t1_time + timedelta(days=2, hours=1)
        adv_2 = wf.evaluate_and_advance(self.lead, self.opp, enrollment, current_time=t2_time)
        self.assertEqual(adv_2["action"], "TOUCH_EXECUTED")
        self.assertEqual(enrollment.current_touch, 3)

        # Touch 3: Day 7 Voice Call + Email (at T2 + 97h)
        t3_time = t2_time + timedelta(days=4, hours=1)
        adv_3 = wf.evaluate_and_advance(self.lead, self.opp, enrollment, current_time=t3_time)
        self.assertEqual(adv_3["action"], "TOUCH_EXECUTED")
        self.assertEqual(enrollment.status, CadenceStatus.COMPLETED)
        self.assertIsNone(enrollment.next_touch_due)

    def test_quoted_prospect_workflow_completion(self):
        wf = QuotedProspectWorkflow(
            eligibility_gate=self.gate,
            stopping_logic=self.stopping_logic,
        )
        quote = QuoteSummary(
            quote_id="Q-401",
            opportunity_id="OPP-401",
            applicant_id="AP-401",
            carrier_name="Travelers",
            line_of_business="Personal Auto",
            quoted_premium=1150.00,
        )
        enrollment = wf.enroll(self.lead, self.opp, quote=quote, start_time=self.t0)

        # Day 1: Touch 0/1 review call
        adv_1 = wf.evaluate_and_advance(self.lead, self.opp, enrollment, quote=quote, current_time=self.t0 + timedelta(days=1, hours=1))
        self.assertEqual(adv_1["action"], "TOUCH_EXECUTED")

        # Day 3: Touch 1/2 coverage email + sms
        adv_2 = wf.evaluate_and_advance(self.lead, self.opp, enrollment, quote=quote, current_time=self.t0 + timedelta(days=3, hours=2))
        self.assertEqual(adv_2["action"], "TOUCH_EXECUTED")

        # Day 7: Touch 2/3 final call
        adv_3 = wf.evaluate_and_advance(self.lead, self.opp, enrollment, quote=quote, current_time=self.t0 + timedelta(days=7, hours=3))
        self.assertEqual(adv_3["action"], "TOUCH_EXECUTED")
        self.assertEqual(enrollment.status, CadenceStatus.COMPLETED)

    def test_xdate_workflow_calculation(self):
        self.opp.expiration_date = date(2026, 10, 23)  # 45 days after Sept 8
        wf = XDateWorkflow(
            eligibility_gate=self.gate,
            stopping_logic=self.stopping_logic,
        )
        enrollment = wf.enroll(self.lead, self.opp, start_time=self.t0)
        self.assertIsNotNone(enrollment.next_touch_due)


if __name__ == "__main__":
    unittest.main()
