#!/usr/bin/env python3
"""
Live Bland AI Test Call Dispatcher for StreetSmart Lead Pilot.

Permits Jake or Carlo to dispatch an authorized test call to their verified mobile
devices (+17324812520 or +17326540947) to experience Robie's voice prompt,
rate-grounding safeguards, and warm-transfer logic firsthand.
"""

import argparse
import logging
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# Also add parent renewal-automation-system root for secrets access if available
sys.path.insert(0, "/opt/renewal-automation-system")

from src.channels.bland_voice import BlandVoiceClient, normalize_phone_e164
from src.models.cadence_models import (
    ApplicantLead,
    CadenceType,
    Opportunity,
    OpportunityStage,
    QuoteSummary,
)
from src.scripts.voice_scripts import VoiceScriptBuilder

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("run_live_test_call")


def main():
    parser = argparse.ArgumentParser(description="Dispatch a live Bland AI voice test call.")
    parser.add_argument(
        "--phone",
        default="+17324812520",
        help="Target test phone number (must be in dial_allowlist.txt). Default: Jake Ferrara (+17324812520)",
    )
    parser.add_argument(
        "--cadence",
        choices=["inbound", "quoted", "xdate"],
        default="inbound",
        help="Cadence scenario to test (inbound, quoted, xdate). Default: inbound",
    )
    parser.add_argument(
        "--touch",
        type=int,
        default=1,
        help="Touch number to simulate (1, 2, or 3). Default: 1",
    )
    parser.add_argument(
        "--name",
        default="Jake",
        help="Recipient first name for greeting personalization. Default: Jake",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Actually place the live call via Bland AI (default is dry-run simulation).",
    )

    args = parser.parse_args()
    target_phone = normalize_phone_e164(args.phone)
    if not target_phone:
        logger.error("Invalid phone format: %s", args.phone)
        sys.exit(1)

    lead = ApplicantLead(
        applicant_id="TEST-LIVE-001",
        first_name=args.name,
        last_name="Ferrara",
        phone=target_phone,
        email="jake@streetsmart.insurance",
        lead_source="StreetSmart Website",
        assigned_producer="Jake Ferrara",
        assigned_producer_phone="+17324812520",
    )

    opp = Opportunity(
        opportunity_id="OPP-TEST-001",
        applicant_id="TEST-LIVE-001",
        line_of_business="Personal Auto",
        stage=OpportunityStage.NEW,
        producer_name="Jake Ferrara",
    )

    quote = None
    cadence_enum = CadenceType.INBOUND_LEAD
    if args.cadence == "quoted":
        cadence_enum = CadenceType.QUOTED_PROSPECT
        opp.stage = OpportunityStage.QUOTED
        quote = QuoteSummary(
            quote_id="Q-TEST-001",
            opportunity_id="OPP-TEST-001",
            applicant_id="TEST-LIVE-001",
            carrier_name="Progressive",
            line_of_business="Personal Auto",
            quoted_premium=950.00,
        )
    elif args.cadence == "xdate":
        cadence_enum = CadenceType.XDATE_OPPORTUNITY

    script_pkg = VoiceScriptBuilder.build_script(
        cadence_type=cadence_enum,
        touch_number=args.touch,
        lead=lead,
        opportunity=opp,
        quote=quote,
    )

    client = BlandVoiceClient(
        allowlist_path=Path("config/dial_allowlist.txt"),
    )

    print("\n=======================================================")
    print("      ROBIE LIVE TELEPHONY TEST CALL DISPATCHER        ")
    print("=======================================================")
    print(f"Target Phone:     {target_phone}")
    print(f"Scenario:         {cadence_enum.value} (Touch {args.touch})")
    print(f"Recipient Name:   {args.name}")
    print(f"Mode:             {'LIVE BLAND AI CALL' if args.live else 'DRY-RUN (Simulated)'}")
    print(f"Caller ID:        {client.caller_id}")
    print(f"Transfer Target:  {script_pkg.transfer_phone}")
    print("-------------------------------------------------------")
    print("First Sentence Spoken by Robie:")
    print(f"  \"{script_pkg.first_sentence}\"")
    print("-------------------------------------------------------")
    print("Voicemail Message (if unanswered):")
    print(f"  \"{script_pkg.voicemail_message}\"")
    print("=======================================================\n")

    if not client.is_number_allowed(target_phone):
        logger.error(
            "SAFETY ERROR: Phone number %s is NOT in the dial allowlist (config/dial_allowlist.txt). "
            "Refusing to dispatch call.",
            target_phone,
        )
        sys.exit(1)

    res = client.dispatch(
        lead=lead,
        opportunity=opp,
        cadence_type=cadence_enum,
        touch_number=args.touch,
        quote=quote,
        dry_run=not args.live,
    )

    print("Call Dispatch Result:")
    print(f"  Success: {res.get('success')}")
    print(f"  Call ID: {res.get('call_id')}")
    print(f"  Mode:    {res.get('mode')}")
    if res.get('error'):
        print(f"  Error:   {res.get('error')}")
    print("\nIf testing live: Please answer your phone when +1 (732) 298-6745 calls!\n")


if __name__ == "__main__":
    main()
