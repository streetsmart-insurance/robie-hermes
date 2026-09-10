"""Unit tests for Robie Stopping Logic & Global Suppression Triggers."""

import unittest

from src.gates.stopping_logic import StoppingLogicEngine
from src.gates.suppression_engine import SuppressionEngine
from src.models.cadence_models import (
    ApplicantLead,
    ApplicantStatus,
    CadenceEnrollment,
    CadenceStatus,
    CadenceType,
    Opportunity,
    OpportunityStage,
    StopReason,
)


class TestStoppingLogic(unittest.TestCase):
    def setUp(self):
        self.suppression_engine = SuppressionEngine()
        self.suppression_engine.clear()
        self.engine = StoppingLogicEngine(suppression_engine=self.suppression_engine)

        self.lead = ApplicantLead(
            applicant_id="AP-201",
            first_name="Jane",
            last_name="Doe",
            phone="+17325559876",
            email="jane.doe@example.com",
            client_status=ApplicantStatus.PROSPECT_LEAD,
        )
        self.opp = Opportunity(
            opportunity_id="OPP-201",
            applicant_id="AP-201",
            line_of_business="Homeowners",
            stage=OpportunityStage.NEW,
        )
        self.enrollment = CadenceEnrollment(
            enrollment_id="ENR-201",
            applicant_id="AP-201",
            opportunity_id="OPP-201",
            cadence_type=CadenceType.INBOUND_LEAD,
            status=CadenceStatus.ACTIVE,
        )

    def test_stop_on_applicant_becoming_active_client(self):
        self.lead.client_status = ApplicantStatus.ACTIVE_CLIENT
        res = self.engine.evaluate(self.lead, self.opp, self.enrollment)
        self.assertTrue(res.should_stop)
        self.assertEqual(res.reason, StopReason.WON_BOUND)
        self.assertEqual(self.enrollment.status, CadenceStatus.STOPPED)
        # Active client should NOT be added to opt-out suppression ledger
        self.assertIsNone(res.created_suppression)

    def test_stop_on_opportunity_won(self):
        self.opp.stage = OpportunityStage.WON
        res = self.engine.evaluate(self.lead, self.opp, self.enrollment)
        self.assertTrue(res.should_stop)
        self.assertEqual(res.reason, StopReason.WON_BOUND)
        self.assertEqual(self.enrollment.status, CadenceStatus.STOPPED)

    def test_stop_on_opportunity_lost_or_dead(self):
        self.opp.stage = OpportunityStage.LOST
        res = self.engine.evaluate(self.lead, self.opp, self.enrollment)
        self.assertTrue(res.should_stop)
        self.assertEqual(res.reason, StopReason.LOST_CLOSED)

        self.enrollment.status = CadenceStatus.ACTIVE
        self.opp.stage = OpportunityStage.DEAD
        res_dead = self.engine.evaluate(self.lead, self.opp, self.enrollment)
        self.assertTrue(res_dead.should_stop)
        self.assertEqual(res_dead.reason, StopReason.DEAD)

    def test_stop_on_verbal_call_opt_out_creates_suppression(self):
        event = {
            "type": "voice_call",
            "transcript": "Hello? Please take me off your list and do not call again.",
            "disposition": "not_interested",
        }
        res = self.engine.evaluate(self.lead, self.opp, self.enrollment, inbound_event=event)
        self.assertTrue(res.should_stop)
        self.assertEqual(res.reason, StopReason.OPT_OUT_CALL)
        self.assertIsNotNone(res.created_suppression)
        self.assertIn("ROBIE was here", res.ezlynx_audit_note)

        # Confirm global ledger blocks phone and email
        suppressed_phone, _ = self.suppression_engine.is_suppressed(phone=self.lead.phone)
        suppressed_email, _ = self.suppression_engine.is_suppressed(email=self.lead.email)
        self.assertTrue(suppressed_phone)
        self.assertTrue(suppressed_email)

    def test_stop_on_sms_keyword_stop(self):
        for kw in ["STOP", "Stop", "unsubscribe", "CANCEL", "quit"]:
            self.enrollment.status = CadenceStatus.ACTIVE
            event = {"type": "sms_inbound", "body": kw}
            res = self.engine.evaluate(self.lead, self.opp, self.enrollment, inbound_event=event)
            self.assertTrue(res.should_stop)
            self.assertEqual(res.reason, StopReason.OPT_OUT_SMS_STOP)
            self.assertIsNotNone(res.created_suppression)

    def test_stop_on_ezlynx_note_trigger(self):
        event = {
            "type": "ezlynx_note",
            "note_text": "Spoke to customer on phone. Robie Stop.",
        }
        res = self.engine.evaluate(self.lead, self.opp, self.enrollment, inbound_event=event)
        self.assertTrue(res.should_stop)
        self.assertEqual(res.reason, StopReason.EZLYNX_NOTE_KEYWORD)
        self.assertIsNotNone(res.created_suppression)

    def test_stop_on_employee_manual_cancel(self):
        event = {"type": "employee_action", "action": "cancel"}
        res = self.engine.evaluate(self.lead, self.opp, self.enrollment, inbound_event=event)
        self.assertTrue(res.should_stop)
        self.assertEqual(res.reason, StopReason.EMPLOYEE_PAUSE)


if __name__ == "__main__":
    unittest.main()
