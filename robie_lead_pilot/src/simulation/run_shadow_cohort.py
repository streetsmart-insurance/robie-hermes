"""
Cohort Shadow Mode Runner.

Simulates running Robie in shadow mode against a cohort of 35 candidate leads,
validating pre-flight gating, stopping logic, and hypothetical prompt generation
with ZERO network dials ($0.00 API cost).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from src.gates.eligibility_gate import EligibilityGate
from src.gates.suppression_engine import SuppressionEngine
from src.models.cadence_models import (
    ApplicantLead,
    ApplicantStatus,
    CadenceType,
    ContactConsent,
    Opportunity,
    OpportunityStage,
    QuoteSummary,
    StopReason,
)
from src.simulation.shadow_mode import ShadowModeEngine

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def run_shadow_pilot_cohort():
    supp_engine = SuppressionEngine(persistence_file=Path("data/pilot_suppression.json"))
    gate = EligibilityGate(suppression_engine=supp_engine)
    shadow = ShadowModeEngine(
        suppression_engine=supp_engine,
        eligibility_gate=gate,
        report_output_path=Path("data/shadow_pilot_report.json"),
    )

    # 1. Pre-seed a known suppression
    supp_engine.add_suppression(phone="+17325550099", reason=StopReason.OPT_OUT_SMS_STOP)

    candidates = [
        # Approved leads for Jake Ferrara - Personal Auto
        (
            ApplicantLead("LD-001", "Marcus", "Vance", "+17325550101", "marcus@example.com", lead_source="StreetSmart Website", assigned_producer="Jake Ferrara"),
            Opportunity("OPP-001", "LD-001", "Personal Auto", stage=OpportunityStage.NEW),
            CadenceType.INBOUND_LEAD, 1, None
        ),
        (
            ApplicantLead("LD-002", "Elena", "Rostova", "+17325550102", "elena@example.com", lead_source="Website Inbound", assigned_producer="Jake Ferrara"),
            Opportunity("OPP-002", "LD-002", "Personal Auto", stage=OpportunityStage.NEW),
            CadenceType.INBOUND_LEAD, 1, None
        ),
        (
            ApplicantLead("LD-003", "David", "Miller", "+17325550103", "david@example.com", lead_source="EverQuote", assigned_producer="Jake Ferrara"),
            Opportunity("OPP-003", "LD-003", "Personal Auto", stage=OpportunityStage.QUOTED),
            CadenceType.QUOTED_PROSPECT, 1,
            QuoteSummary("Q-103", "OPP-003", "LD-003", "Travelers", "Personal Auto", 1250.00)
        ),
        (
            ApplicantLead("LD-004", "Sophia", "Chen", "+17325550104", "sophia@example.com", lead_source="QuoteWizard", assigned_producer="Jake Ferrara"),
            Opportunity("OPP-004", "LD-004", "Homeowners", stage=OpportunityStage.QUOTED),
            CadenceType.QUOTED_PROSPECT, 1,
            QuoteSummary("Q-104", "OPP-004", "LD-004", "Plymouth Rock", "Homeowners", 890.00)
        ),
        (
            ApplicantLead("LD-005", "Brian", "O'Connor", "+17325550105", "brian@example.com", lead_source="StreetSmart Website", assigned_producer="Jake Ferrara"),
            Opportunity("OPP-005", "LD-005", "Personal Auto", stage=OpportunityStage.NEW),
            CadenceType.INBOUND_LEAD, 3, None
        ),
        # Test Case: Suppressed Lead
        (
            ApplicantLead("LD-006", "Karen", "Walker", "+17325550099", "karen@example.com", lead_source="StreetSmart Website", assigned_producer="Jake Ferrara"),
            Opportunity("OPP-006", "LD-006", "Personal Auto", stage=OpportunityStage.NEW),
            CadenceType.INBOUND_LEAD, 1, None
        ),
        # Test Case: Unapproved Lead Source
        (
            ApplicantLead("LD-007", "Tom", "Hanks", "+17325550107", "tom@example.com", lead_source="Unvetted Scraping List", assigned_producer="Jake Ferrara"),
            Opportunity("OPP-007", "LD-007", "Personal Auto", stage=OpportunityStage.NEW),
            CadenceType.INBOUND_LEAD, 1, None
        ),
        # Test Case: Already Active Client
        (
            ApplicantLead("LD-008", "Sarah", "Connor", "+17325550108", "sarah@example.com", lead_source="StreetSmart Website", assigned_producer="Jake Ferrara", client_status=ApplicantStatus.ACTIVE_CLIENT),
            Opportunity("OPP-008", "LD-008", "Personal Auto", stage=OpportunityStage.NEW),
            CadenceType.INBOUND_LEAD, 1, None
        ),
        # Test Case: Opportunity Already Won
        (
            ApplicantLead("LD-009", "Arthur", "Dent", "+17325550109", "arthur@example.com", lead_source="StreetSmart Website", assigned_producer="Jake Ferrara"),
            Opportunity("OPP-009", "LD-009", "Personal Auto", stage=OpportunityStage.WON),
            CadenceType.INBOUND_LEAD, 1, None
        ),
        # Test Case: Missing Voice Consent
        (
            ApplicantLead("LD-010", "Ford", "Prefect", "+17325550110", "ford@example.com", lead_source="StreetSmart Website", assigned_producer="Jake Ferrara", consent=ContactConsent(voice_consent=False)),
            Opportunity("OPP-010", "LD-010", "Personal Auto", stage=OpportunityStage.NEW),
            CadenceType.INBOUND_LEAD, 1, None
        ),
    ]

    # Add 20 more approved leads to make a full cohort of 30
    for i in range(11, 31):
        lead = ApplicantLead(
            f"LD-{i:03d}", f"Customer_{i}", f"LastName_{i}", f"+173255501{i:02d}",
            f"cust{i}@example.com", lead_source="StreetSmart Website", assigned_producer="Jake Ferrara"
        )
        opp = Opportunity(f"OPP-{i:03d}", f"LD-{i:03d}", "Personal Auto", stage=OpportunityStage.NEW)
        candidates.append((lead, opp, CadenceType.INBOUND_LEAD, 1, None))

    sim_time = datetime(2026, 9, 8, 14, 30, 0)  # Tuesday 2:30 PM ET
    for lead, opp, ctype, touch, quote in candidates:
        shadow.evaluate_candidate(
            lead=lead,
            opportunity=opp,
            cadence_type=ctype,
            touch_number=touch,
            quote=quote,
            current_time=sim_time,
        )

    report = shadow.generate_report()
    print("\n================ SHADOW MODE COHORT AUDIT ================")
    print(f"Total Cohort Candidates Evaluated: {report['total_evaluated']}")
    print(f"Candidates Approved for Outreach:   {report['would_have_dispatched']}")
    print(f"Candidates Blocked by Safety Gate: {report['would_have_skipped']}")
    print(f"Total Telephony Cost Incurred:    {report['cost_incurred']}")
    print("==========================================================")
    print("\nSample Approved Candidate Preview:")
    for d in report["decisions"][:3]:
        if d["is_eligible"]:
            print(f" • [{d['lead_id']}] {d['lead_name']} ({d['phone']})")
            print(f"   First Sentence: \"{d['first_sentence_preview']}\"\n")

    print("Sample Blocked Candidate Reasons:")
    for d in report["decisions"]:
        if not d["is_eligible"]:
            print(f" ✕ [{d['lead_id']}] {d['lead_name']} -> {d['hypothetical_action']}")
            print(f"   Reason: {d['eligibility_reason']}")

    return report


if __name__ == "__main__":
    run_shadow_pilot_cohort()
