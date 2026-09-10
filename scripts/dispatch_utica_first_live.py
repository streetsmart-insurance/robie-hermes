#!/usr/bin/env python3
import sys
import time
import json
import logging
from src.voice.context_hydrator import CallingDossier
from src.voice.voice_client import CarrierVoiceClient
from src.voice.call_completion import handle_completed_call, fetch_bland_call_details

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("dispatch_utica_first")

def main():
    dossier = CallingDossier(
        policy_number="ART3000133890",
        insured_name="B.W.E Custom Construction LLC",
        carrier_name="Utica First Insurance Company",
        line_of_business="Commercial",
        carrier_phone="+18004564556",
        applicant_id=36427615,
        assigned_csr_email="carlo@streetsmart.insurance",
        custom_instructions=(
            "Request reinstatement on policy ART3000133890 for B.W.E Custom Construction LLC. "
            "Insured Brett Embley (908-442-1579) urgently needs Certificate of Liability today for a job starting this week. "
            "Connect to Commercial Lines Underwriting (Heather Burgdoff: hburgdoff@uticafirst.com). "
            "Inquire what is required to reinstate the policy immediately. "
            "If underwriter is reached and requires Carlo Ferrara or agency authorization, "
            "or if any issues arise, warm transfer to Carlo Ferrara at +17324622360."
        ),
        ivr_instructions="Press 2 for Commercial Underwriting. Ask for Heather Burgdoff.",
        requestor_name="Carlo Ferrara",
        requestor_email="carlo@streetsmart.insurance",
        requestor_phone="+17324622360",
        call_type="carrier"
    )

    client = CarrierVoiceClient()
    logger.info("Dispatching LIVE Bland AI call to Utica First (+18004564556)...")
    res = client.dispatch_call(dossier, dry_run=False)
    logger.info("Dispatch result: %s", json.dumps(res))

    call_id = res.get("call_id")
    if not call_id:
        logger.error("No call_id returned from dispatch!")
        sys.exit(1)

    print(f"CALL_DISPATCHED:{call_id}")
    return call_id

if __name__ == "__main__":
    main()
