#!/usr/bin/env python3
import sys
import argparse
import logging
from src.email_outreach.gmail_client import GmailRenewalClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("request_nicole_smordoni_logins")

def main():
    parser = argparse.ArgumentParser(description="Send portal login request to Nicole for NatGen and JJIns")
    parser.add_argument("--send", action="store_true", help="Actually send the email via Gmail API")
    args = parser.parse_args()

    to_email = "Nicole@streetsmart.insurance"
    cc = ["carlo@streetsmart.insurance"]
    subject = "Carrier Portal Logins Needed: National General & Johnson & Johnson (jjins.com) for Smordoni Policies"
    
    body_text = """Hi Nicole,

Per Carlo's request, we need carrier portal logins for Robie (our autonomous renewal system) on two carrier portals in order to crawl and retrieve upcoming renewal documents for David & Carmen Smordoni (Applicant #186333470):

1. National General (RLI Umbrella Policy #PUP2437998)
   - Portal: https://agent.nationalgeneral.com (or https://devpl.nationalgeneral.com)
   - Policy: PUP2437998 (Exp 10/01/2026)

2. Johnson & Johnson Insurance (Scottsdale Dwelling Fire Policies #DFS3996870 & #DFS3999475)
   - Portal: https://www.jjins.com
   - Policies: DFS3996870 & DFS3999475 (Exp 10/01/2026)

Could you please create sub-account user access for robie@streetsmart.insurance (or provide the agency credentials) and add them to Google Cloud Secret Manager:
• User / Email: robie@streetsmart.insurance
• Role: View Policies / Download Documents
• 2FA / Verification: Set delivery method to Email (robie@streetsmart.insurance) so Robie can automatically intercept OTP codes.

Secret Manager Keys:
• natgen_username / natgen_password
• jjins_username / jjins_password

When added, please let Carlo know!

Thank you,
Robie & The StreetSmart Automation Team
"""

    if not args.send:
        logger.info("[DRY RUN] Email prepared but not sent. Run with --send to dispatch.")
        print(f"TO: {to_email}")
        print(f"CC: {cc}")
        print(f"SUBJECT: {subject}")
        print("-" * 50)
        print(body_text)
        print("-" * 50)
        return

    logger.info(f"Sending portal login request email to {to_email} (CC: {cc})...")
    client = GmailRenewalClient()
    result = client.send_email(
        to_email=to_email,
        subject=subject,
        body_text=body_text,
        cc=cc
    )
    logger.info(f"Email sent successfully! Message ID: {result.get('id')}, Thread ID: {result.get('threadId')}")
    print(f"SENT: Message ID: {result.get('id')}")

if __name__ == "__main__":
    main()
