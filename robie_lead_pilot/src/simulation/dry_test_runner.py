"""
Internal Dry Test Runner for Robie Bland AI Integration.

Executes full-cycle simulations using internal test accounts (Carlo Ferrara & Jake Ferrara),
verifying:
1. Multi-channel touch generation
2. Bland AI prompt syntax & callback grounding
3. Warm-transfer destination configuration
4. Verbal & text opt-out stopping triggers
5. Global suppression creation & cross-workflow enforcement
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List

from src.gates.eligibility_gate import EligibilityGate
from src.gates.stopping_logic import StoppingLogicEngine
from src.gates.suppression_engine import SuppressionEngine
from src.models.cadence_models import (
    ApplicantLead,
    ApplicantStatus,
    ContactConsent,
    Opportunity,
    OpportunityStage,
    QuoteSummary,
)
from src.workflows.inbound_lead_workflow import InboundLeadWorkflow
from src.workflows.quoted_prospect_workflow import QuotedProspectWorkflow
from src.workflows.xdate_workflow import XDateWorkflow

logger = logging.getLogger("dry_test_runner")


def run_internal_dry_test() -> Dict[str, Any]:
    """Runs a complete internal dry-run test suite."""
    results: Dict[str, Any] = {"tests": [], "passed": True}
    suppression_engine = SuppressionEngine()
    suppression_engine.clear()  # Ensure test isolation

    eligibility_gate = EligibilityGate(suppression_engine=suppression_engine)
    stopping_logic = StoppingLogicEngine(suppression_engine=suppression_engine)

    # Test Subject 1: Carlo Ferrara (Internal Test Account)
    carlo_lead = ApplicantLead(
        applicant_id="TEST-CARLO-001",
        first_name="Carlo",
        last_name="Ferrara",
        phone="+17324812520",  # Allowlisted internal number
        email="carlo@streetsmart.insurance",
        lead_source="StreetSmart Website",
        assigned_producer="Jake Ferrara",
        assigned_producer_phone="+17324812520",
        client_status=ApplicantStatus.PROSPECT_LEAD,
        consent=ContactConsent(voice_consent=True, sms_consent=True, email_consent=True),
    )

    carlo_opp = Opportunity(
        opportunity_id="OPP-TEST-001",
        applicant_id="TEST-CARLO-001",
        line_of_business="Personal Auto",
        stage=OpportunityStage.NEW,
    )

    # Business hour test timestamp: Tuesday at 2:00 PM
    sim_time = datetime(2026, 9, 8, 14, 0, 0)

    # TEST 1: Inbound Lead Workflow Enrollment & Immediate Touch 0
    inbound_wf = InboundLeadWorkflow(
        eligibility_gate=eligibility_gate,
        stopping_logic=stopping_logic,
    )
    enrollment = inbound_wf.enroll(carlo_lead, carlo_opp, start_time=sim_time)
    adv_0 = inbound_wf.evaluate_and_advance(
        lead=carlo_lead,
        opportunity=carlo_opp,
        enrollment=enrollment,
        current_time=sim_time + timedelta(minutes=5),
        dry_run=True,
    )

    t1_pass = adv_0["action"] == "TOUCH_EXECUTED" and "SMS" in adv_0["channel_results"]
    results["tests"].append({
        "name": "TEST_1_INBOUND_TOUCH_0_IMMEDIATE_ACK",
        "passed": t1_pass,
        "details": adv_0,
    })

    # TEST 2: Day 1 Voice Dispatch & Bland AI Prompt Grounding
    sim_day_1 = sim_time + timedelta(days=1, hours=1)
    adv_1 = inbound_wf.evaluate_and_advance(
        lead=carlo_lead,
        opportunity=carlo_opp,
        enrollment=enrollment,
        current_time=sim_day_1,
        dry_run=True,
    )

    voice_res = adv_1.get("channel_results", {}).get("VOICE", {})
    payload = voice_res.get("payload", {})
    t2_pass = (
        adv_1["action"] == "TOUCH_EXECUTED"
        and voice_res.get("success") is True
        and payload.get("from") == "+17322986745"
        and payload.get("transfer_phone_number") == "+17324812520"
        and "(732) 462-8343" in payload.get("voicemail_message", "")
    )
    results["tests"].append({
        "name": "TEST_2_INBOUND_DAY_1_VOICE_PAYLOAD",
        "passed": t2_pass,
        "details": {
            "first_sentence": payload.get("first_sentence"),
            "caller_id": payload.get("from"),
            "transfer_did": payload.get("transfer_phone_number"),
            "voicemail": payload.get("voicemail_message"),
        },
    })

    # TEST 3: Verbal Opt-Out Halts Cadence & Creates Global Suppression Record
    opt_out_event = {
        "type": "voice_call",
        "transcript": "I am not interested, please stop calling me and take me off your list.",
        "disposition": "not_interested",
    }
    adv_opt = inbound_wf.evaluate_and_advance(
        lead=carlo_lead,
        opportunity=carlo_opp,
        enrollment=enrollment,
        inbound_event=opt_out_event,
        current_time=sim_day_1 + timedelta(minutes=10),
        dry_run=True,
    )

    suppressed, supp_rec = suppression_engine.is_suppressed(phone=carlo_lead.phone)
    t3_pass = (
        adv_opt["action"] == "HALTED"
        and suppressed is True
        and supp_rec is not None
        and "ROBIE was here" in adv_opt.get("audit_note", "")
    )
    results["tests"].append({
        "name": "TEST_3_VERBAL_OPT_OUT_AND_GLOBAL_SUPPRESSION",
        "passed": t3_pass,
        "suppression_id": supp_rec.record_id if supp_rec else None,
        "audit_note": adv_opt.get("audit_note"),
    })

    # TEST 4: Cross-Workflow Blocking (Quoted Prospect Cadence Blocked by Prior Suppression)
    quoted_wf = QuotedProspectWorkflow(
        eligibility_gate=eligibility_gate,
        stopping_logic=stopping_logic,
    )
    quote_summary = QuoteSummary(
        quote_id="Q-9912",
        opportunity_id="OPP-TEST-001",
        applicant_id="TEST-CARLO-001",
        carrier_name="Travelers",
        line_of_business="Personal Auto",
        quoted_premium=1240.00,
    )
    enrollment_quoted = quoted_wf.enroll(carlo_lead, carlo_opp, quote=quote_summary, start_time=sim_time)
    adv_quoted = quoted_wf.evaluate_and_advance(
        lead=carlo_lead,
        opportunity=carlo_opp,
        enrollment=enrollment_quoted,
        quote=quote_summary,
        current_time=sim_day_1 + timedelta(days=2),
        dry_run=True,
    )

    t4_pass = (
        adv_quoted["action"] == "SKIPPED_INELIGIBLE"
        and "PERMANENT_ABORT_SUPPRESSED" in str(adv_quoted)
    )
    results["tests"].append({
        "name": "TEST_4_CROSS_WORKFLOW_SUPPRESSION_ENFORCEMENT",
        "passed": t4_pass,
        "details": adv_quoted,
    })

    results["passed"] = all(t["passed"] for t in results["tests"])
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    res = run_internal_dry_test()
    print("\n=== INTERNAL DRY TEST RESULTS ===")
    print(f"Overall Status: {'PASSED' if res['passed'] else 'FAILED'}")
    for t in res["tests"]:
        print(f" - [{ 'PASS' if t['passed'] else 'FAIL' }] {t['name']}")
