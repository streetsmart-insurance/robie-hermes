"""Carrier Portal Sub-Account & Credential Requester.

Automates the mandate to email Nicole (nicole@streetsmart.insurance) to provision
dedicated sub-account logins for Robie (robie@streetsmart.insurance) across carrier portals.
"""

import sys
import argparse
import logging
from typing import Optional, List, Dict, Any
from src.email_outreach.gmail_client import GmailRenewalClient

logger = logging.getLogger("portal_credential_requester")

NICOLE_EMAIL = "nicole@streetsmart.insurance"
DEFAULT_CC = ["carlo@streetsmart.insurance", "jake@streetsmart.insurance"]

def build_login_request_body(
    carrier_name: str,
    portal_url: str,
    secret_key_prefix: str,
    additional_notes: Optional[str] = None,
    affected_policies: Optional[List[str]] = None
) -> str:
    """Builds a standardized, polite email requesting sub-account credentials for Robie."""
    pol_section = ""
    if affected_policies:
        pol_lines = "\n".join([f"• {p}" for p in affected_policies])
        pol_section = f"\nUpcoming Policies Requiring Renewal Packet Retrieval:\n{pol_lines}\n"

    notes_section = f"\nCarrier Notes: {additional_notes}\n" if additional_notes else ""

    return f"""Hi Nicole,

We are setting up autonomous carrier portal retrieval for {carrier_name} in our renewal system.

To allow Robie to crawl and retrieve renewal packets headlessly without interrupting team members for 2FA, could you please provision a dedicated agency sub-account for Robie?

Portal Details:
• Carrier / MGA: {carrier_name}
• Portal URL: {portal_url}{notes_section}{pol_section}
Sub-Account Provisioning Specifications:
• User / Login Email: robie@streetsmart.insurance
• First / Last Name: Robie Automation (StreetSmart)
• Role / Permissions: View Policies / Download Documents & Declarations
• 2FA / MFA Delivery Method: Set to EMAIL (robie@streetsmart.insurance) so Robie can automatically intercept verification codes.

GCP Secret Manager Storage:
Once created, please add the credentials to Google Cloud Secret Manager (or let Carlo know):
• Username Secret: {secret_key_prefix}_username
• Password Secret: {secret_key_prefix}_password

Thank you for your help!

Best regards,
Robie & The StreetSmart Automation Team
robie@streetsmart.insurance
"""

def request_portal_credentials(
    carrier_name: str,
    portal_url: str,
    additional_notes: Optional[str] = None,
    affected_policies: Optional[List[str]] = None,
    cc: Optional[List[str]] = None,
    dry_run: bool = False
) -> Dict[str, Any]:
    """Dispatches an email to Nicole requesting a dedicated sub-account for Robie."""
    prefix = carrier_name.lower().replace(" ", "_").replace("-", "_").replace(".", "")
    subject = f"Carrier Portal Sub-Account Needed for Robie: {carrier_name}"
    body = build_login_request_body(
        carrier_name=carrier_name,
        portal_url=portal_url,
        secret_key_prefix=prefix,
        additional_notes=additional_notes,
        affected_policies=affected_policies
    )

    cc_recipients = cc or DEFAULT_CC

    if dry_run:
        logger.info(f"[DRY RUN] Prepared portal login request to {NICOLE_EMAIL} (CC: {cc_recipients})")
        return {
            "dry_run": True,
            "to": NICOLE_EMAIL,
            "cc": cc_recipients,
            "subject": subject,
            "body": body
        }

    logger.info(f"Sending sub-account credential request to {NICOLE_EMAIL} for {carrier_name}...")
    client = GmailRenewalClient()
    result = client.send_email(
        to_email=NICOLE_EMAIL,
        subject=subject,
        body_text=body,
        cc=cc_recipients
    )
    logger.info(f"Login request email sent! Message ID: {result.get('id')}")
    return {
        "success": True,
        "message_id": result.get("id"),
        "thread_id": result.get("threadId"),
        "to": NICOLE_EMAIL,
        "carrier": carrier_name
    }

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="Request carrier portal sub-account from Nicole for Robie")
    parser.add_argument("--carrier", required=True, help="Name of carrier (e.g. 'Liberty Mutual Assigned Risk')")
    parser.add_argument("--portal-url", required=True, help="Portal URL (e.g. 'https://account.libertymutual.com/broker')")
    parser.add_argument("--notes", default="", help="Additional carrier or portal notes")
    parser.add_argument("--policy", action="append", default=[], help="Affected policy numbers")
    parser.add_argument("--send", action="store_true", help="Actually send the email (defaults to dry-run)")

    args = parser.parse_args()

    res = request_portal_credentials(
        carrier_name=args.carrier,
        portal_url=args.portal_url,
        additional_notes=args.notes,
        affected_policies=args.policy,
        dry_run=not args.send
    )

    if not args.send:
        print("\n--- [DRY RUN PREVIEW] ---")
        print(f"To: {res['to']}")
        print(f"CC: {res['cc']}")
        print(f"Subject: {res['subject']}")
        print("\n" + res["body"])
        print("------------------------\nRun with --send to dispatch email.")
    else:
        print(f"\n✅ Request sent successfully to {res['to']} (Message ID: {res.get('message_id')})")

if __name__ == '__main__':
    main()
