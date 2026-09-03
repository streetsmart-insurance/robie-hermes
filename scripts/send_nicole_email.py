import sys
import logging
from src.email_outreach.gmail_client import GmailRenewalClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("send_nicole_email")

def send():
    client = GmailRenewalClient()
    to_email = "Nicole@streetsmart.insurance"
    subject = "Action Required: Carrier Portal Logins for Robie (Automated Renewal System)"
    
    body_text = """Hi Nicole,

Carlo asked me to reach out to you regarding carrier portal access for Robie (our autonomous renewal automation system).

To enable Robie to automatically retrieve renewal documents and proposals every morning, we need dedicated user accounts created for Robie on our key carrier portals and added into Google Cloud Secret Manager (GCP).

==================================================
1. CARRIER PORTALS NEEDED FOR ROBIE:
==================================================
Priority 1 (Currently Active):
• Coterie Insurance (https://dashboard.coterieinsurance.com)
• The Hartford (https://ebusiness.thehartford.com)
• TAPCO Underwriters (https://www.gotapco.com)

Additional Carriers for Full Daily Automation:
• Appalachian Underwriters (AUI) (https://www.appund.com)
• BTIS (https://www.btisinc.com)
• Burns & Wilcox (https://www.burnsandwilcox.com)
• Attune / Coalition (https://portal.attuneinsurance.com)
• Next Insurance (https://agents.nextinsurance.com)
• Foremost / Farmers (https://www.foremoststar.com)
• Progressive Commercial (https://www.foragentsonly.com)
• Liberty Mutual / Safeco (https://agent.libertymutual.com)
• Travelers (https://www.travelers.com/foragents)

==================================================
2. USER ACCOUNT SETUP DETAILS:
==================================================
• User / Email: robie@streetsmart.insurance
• Role / Permissions: Read/View Policies, Download Policy Documents, Quotes & Proposals
• 2FA / Verification: Set verification delivery method to Email (robie@streetsmart.insurance) so Robie can automatically resolve OTP codes via API.

==================================================
3. ADDING CREDENTIALS TO GCP SECRET MANAGER:
==================================================
In Google Cloud Secret Manager, please add the credentials using either format:

Format A (Individual Secrets):
- coterie_username / coterie_password
- hartford_username / hartford_password
- tapco_username / tapco_password
(and so on for other carriers)

Format B (JSON Secret):
- coterie_login: {"username": "robie@streetsmart.insurance", "password": "<password>"}

==================================================
When you have finished adding the logins to GCP, please chat / message Carlo to let him know!

Thank you,
Robie & The StreetSmart Automation Team
"""

    res = client.send_email(to_email=to_email, subject=subject, body_text=body_text)
    logger.info(f"Email Dispatch Result: {res}")
    print("SUCCESS: Email sent successfully!")

if __name__ == "__main__":
    send()
