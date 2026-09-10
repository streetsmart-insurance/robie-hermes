"""Script to email Jake with the deployed pilot status, Google Doc link, and live testing guide."""

import sys
import logging
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.email_outreach.gmail_client import GmailRenewalClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("send_jake_pilot_deployment")

def send_deployment_update(dry_run: bool = False):
    client = GmailRenewalClient()
    to_email = "jake@streetsmart.insurance"
    cc_list = ["carlo@streetsmart.insurance"]
    subject = "Robie Pilot Deployed: System Specification Google Doc & Live Testing Instructions"

    google_doc_url = "https://docs.google.com/document/d/1j_uB-OzipXikdMc_wx0RnXegek7lStl2oPvKzw6O-d8/edit?usp=drivesdk"

    body_text = f"""Hi Jake,

The Robie Autonomous Lead & Quote Outreach pilot is officially deployed to our Hermes production server (/opt/renewal-automation-system/robie_lead_pilot), and all 32 automated verification tests have passed.

As requested, we created a comprehensive pageless Google Doc detailing the entire system architecture, eligibility gates, stopping logic, E&O safeguards, and operations guide:

GOOGLE DOC SPECIFICATION & RUNBOOK:
{google_doc_url}
(You have direct editor access, and domain-wide reader access is enabled for streetsmart.insurance)

================================================================================
HOW YOU CAN TEST ROBIE LIVE RIGHT NOW
================================================================================
We configured a dedicated live telephony test dispatcher on Hermes with strict allowlist protection. Your mobile number (+1-732-481-2520) is verified on the dial allowlist.

We can trigger a live call to your phone right now so you can experience Robie's voice prompt, test rate grounding, and try out the warm-transfer back to yourself:

Test Scenarios to Try:

1. Warm-Transfer Test:
   - Answer when +1 (732) 298-6745 calls your mobile.
   - Robie will introduce itself on your behalf: "Hi Jake, this is Robie calling from StreetSmart Insurance on behalf of Jake Ferrara. I saw you reached out for an insurance quote on your Personal Auto — do you have two minutes to connect with Jake to review your options?"
   - Say: "Yes, I'd like to speak with an agent."
   - Robie will initiate an immediate live bridge/warm transfer to +1 (732) 481-2520.

2. E&O Coverage Advice Safeguard Test:
   - When Robie speaks, ask: "What liability limits do you recommend?"
   - Robie will adhere to its E&O guardrail: "As an automated assistant, I can't advise on specific coverage limits or legal options, but Jake is licensed and right here to advise you. Let me get him on the line." and initiate the warm transfer.

3. Verbal Opt-Out & Instant Stopping Test:
   - Say: "Stop calling me, I'm not interested."
   - Robie will politely acknowledge, immediately end the call, write an immutable SHA-256 hash to our Global Suppression Registry, and log the opt-out with "ROBIE was here" in EZLynx discussions.

Whenever you're ready for us to ring your phone for a live test, just let Carlo or me know, or Carlo can trigger it anytime via CLI.

Thanks,

Robie & Carlo Ferrara
StreetSmart Insurance
carlo@streetsmart.insurance | robie@streetsmart.insurance
"""

    if dry_run:
        logger.info(f"[DRY-RUN] Would send email to: {to_email} (CC: {cc_list})")
        logger.info(f"Subject: {subject}")
        print("\n--- EMAIL PREVIEW ---")
        print(f"To: {to_email}")
        print(f"CC: {cc_list}")
        print(f"Subject: {subject}\n")
        print(body_text)
        return {"status": "dry_run"}

    result = client.send_email(
        to_email=to_email,
        subject=subject,
        body_text=body_text,
        cc=cc_list
    )
    logger.info(f"Send result: {result}")
    return result

if __name__ == "__main__":
    is_dry = "--dry-run" in sys.argv
    send_deployment_update(dry_run=is_dry)
