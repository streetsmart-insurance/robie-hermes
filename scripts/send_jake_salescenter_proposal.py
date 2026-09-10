"""Script to email Jake regarding autonomous lead intake, X-date sequences, and quote follow-ups."""

import sys
import logging
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.email_outreach.gmail_client import GmailRenewalClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("send_jake_salescenter_proposal")

def send_proposal(dry_run: bool = False):
    client = GmailRenewalClient()
    to_email = "jake@streetsmart.insurance"
    cc_list = ["carlo@streetsmart.insurance"]
    subject = "Proposal: Autonomous Sales Center Lead Intake, X-Date Sequences & Quote Follow-Up Cadences"

    body_text = """Hi Jake,

Carlo asked me to put together and share these ideas with you regarding expanding Robie (our autonomous operations and voice AI engine) into Sales Center leads, unreached opportunities, and quote follow-ups.

Here is what we discovered from testing the live EZLynx API and how we can set this up:

================================================================================
1. NEW LEAD INTAKE: LEAD SOURCE & CLIENT STATUS VIA DIRECT API
================================================================================
We verified live against EZLynx that Robie can extract both Lead Source and Client Status via direct API without manual entry:
• Lead Source: Pulls the exact channel name (e.g., "StreetSmart Website", "EverQuote", "QuoteWizard", "Google", "Sales Center X-Date" [Channel ID 500]) via the EZLynx Lead and Channel APIs.
• Client Status: Real-time tracking of the account type:
  - "ProspectLead" (New unreached lead)
  - "ActiveClient" (Bound active customer)
  - "InactiveClient" (Prior/expired client)
• Assigned Producer: We also pull who the lead is assigned to (e.g., "Carlo Ferrara (Carlo1)", "Jake", etc.) so Robie personalizes outreach on behalf of the specific agent.

================================================================================
2. UNREACHED OPPORTUNITIES & X-DATE FOLLOW-UP SEQUENCES
================================================================================
For leads and opportunities that haven't been reached:
• Automated Intake: Can be triggered via API when a new prospect lands or via a scheduled daily Sales Center / X-Date CSV report emailed directly to robie@streetsmart.insurance (mirroring our daily renewal and endorsement workflows).
• X-Date Sequences: For prospects whose expiration date / renewal window is upcoming (e.g., 30–45 days out), Robie initiates proactive outreach to introduce StreetSmart, verify current coverage details, and schedule an appointment with you or the assigned producer.

================================================================================
3. PROSPECTS WHO RECEIVED A QUOTE: MULTI-TOUCH CADENCE
================================================================================
When an agent or rater generates a quote in EZLynx:
• Quote Detection: Robie queries the EZLynx Quote API, which automatically extracts the quoted premium (e.g. $1,250.00), Line of Business (Auto, Home, GL), and quote date.
• Multi-Touch Follow-Up Cadence:
  - Touch 1 (Day 1 / 24h post-quote): Courteous phone call or email checking if they received the proposal and answering any initial questions.
  - Touch 2 (Day 3 post-quote): Coverage comparison & value check-in.
  - Touch 3 (Day 7 post-quote): Final bind reminder before rates update or quote lock expires.

================================================================================
4. HOW TO TELL ROBIE TO STOP VIA EZLYNX (AUTOMATIC & ZERO-CODE)
================================================================================
You asked how we stop Robie from continuing to follow up. We designed 3 completely frictionless ways to kill or stop the sequence directly inside EZLynx:

1. Sales Center Status Change (Moved to "Won" or "ActiveClient"):
   - When you sell and bind the policy, EZLynx automatically transitions the account from "ProspectLead" to "ActiveClient", and the opportunity moves to "Won".
   - Robie performs a live API pre-check before every single call or email. The moment Robie sees "ActiveClient" or "Won", the sequence terminates immediately!
   - Similarly, if the opportunity is marked "Dead" or "Lost", Robie immediately halts.

2. Simple EZLynx Note / Label Keyword ("Robie Stop"):
   - If you or any CSR writes a quick note or creates a card on the applicant with the words "Robie Stop", "Stop Robie", "Robie Cancel", or "DNC", Robie catches the keyword and kills the sequence instantly.

3. Prospect Opt-Out:
   - If the prospect answers Robie's call and indicates they went with another carrier or are no longer interested, Bland AI detects the negative response, halts the cadence, and posts an audit note to your EZLynx discussion card so you know the outcome.

================================================================================
NEXT STEPS & REVIEW
================================================================================
Please review these use cases and let Carlo and me know:
1. Do you like the proposed 3-touch cadence timing (Day 1, Day 3, Day 7), or would you prefer a different cadence interval?
2. Would you like us to proceed with implementing this for Robie to begin automated testing?

Thanks,

Robie (Autonomous Operations Specialist) & Carlo Ferrara
StreetSmart Insurance
robie@streetsmart.insurance | +1 (732) 298-6745
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
    send_proposal(dry_run=is_dry)
