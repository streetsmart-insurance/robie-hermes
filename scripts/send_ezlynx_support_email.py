"""Script to email EZLynx Support regarding filtering by Policy Source (Manual) in EZLynx 5.0 reports."""

import os
import sys
import logging
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.email_outreach.gmail_client import GmailRenewalClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("send_ezlynx_support_email")

def send_support_inquiry(dry_run: bool = False):
    client = GmailRenewalClient()
    to_email = "support@ezlynx.com"
    cc_list = ["carlo@streetsmart.insurance"]
    subject = "Inquiry: Filtering by Policy Source (Manual Non-Download) in EZLynx 5.0 Policy Expiration Reports"

    body_text = """Hi EZLynx Support Team,

Agency: StreetSmart Insurance
Contact: Carlo Ferrara (carlo@streetsmart.insurance)
EZLynx System: EZLynx 5.0 (Five [B09])

We are currently managing our upcoming renewal pipeline for non-download manual policies (30 to 50 days out from expiration).

In the Classic / Legacy EZLynx Report Portal (Report #28: BOB_PolicyExpiration_Detail), we rely on the "Manage Columns" feature to include the "Source" column, allowing us to easily distinguish and isolate "Manual" policies from automated carrier "Download" (IVANS/eDocs) policies.

We are looking to transition this workflow to the new EZLynx 5.0 Looker-based reports (Policy Expiration Detail, report/3471 under All Categories > Book of Business > Reports). However, in the standard filter view, we only see filters for:
- Policy Expiration Master Company
- Policy Expiration Line Of Business
- Policy Expiration Premium (Annualized / Written)
- Applicant Data Branch
- Policy Expiration Department
- Serviceteam Service Team

We do not see a filter or visible column for "Source" (Manual vs. Download) in this EZLynx 5.0 report.

Could you please assist us with the following:
1. How can we filter the EZLynx 5.0 Policy Expiration Detail report so that we can view and export ONLY manual (non-download) policies?
2. Is there a dimension or field for Policy Source / Download Status that can be added into our custom filter sets or Looker views?
3. If this specific report does not support filtering by policy source, what is the recommended EZLynx 5.0 report or dashboard for isolating expiring manual policies?

In the meantime, we are continuing to utilize the classic BOB_PolicyExpiration_Detail report (Report #28).

Thank you for your guidance,

Carlo Ferrara & StreetSmart Insurance Automation Team
StreetSmart Insurance
carlo@streetsmart.insurance
"""

    if dry_run:
        logger.info(f"[DRY-RUN] Would send email to: {to_email} (CC: {cc_list})")
        logger.info(f"Subject: {subject}")
        print("\n--- EMAIL PREVIEW ---")
        print(f"To: {to_email}")
        print(f"CC: {', '.join(cc_list)}")
        print(f"Subject: {subject}\n")
        print(body_text)
        return {"status": "dry_run"}

    logger.info(f"Sending inquiry email to {to_email} with CC {cc_list}...")
    res = client.send_email(
        to_email=to_email,
        subject=subject,
        body_text=body_text,
        cc=cc_list
    )
    logger.info(f"Email Dispatch Result: {res}")
    return res

if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    res = send_support_inquiry(dry_run=dry)
    if not dry and res.get("id"):
        print(f"\nSUCCESS: Email dispatched to EZLynx Support (Message ID: {res.get('id')})")
    elif not dry and res.get("status") == "simulated":
        print(f"\nSIMULATED: Sent simulated email (ID: {res.get('id')})")
