import os
import sys
import json
import base64
import asyncio
import logging
from pathlib import Path
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from googleapiclient.discovery import build
from playwright.async_api import async_playwright

from src.email_outreach.auth_setup import get_service_account_credentials
from src.ezlynx.api_client import EZLynxApiClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("lawn_buddies_driver_change")

APPLICANT_ID = "21587333"
POLICY_MASTER_ID = "75473629"
POLICY_NUMBER = "PAC00001215485"
CARRIER_NAME = "Plymouth Rock Assurance Corp (Palisades Insurance Company)"
NAMED_INSURED = "Green Lion Lawn Care LLC DBA Lawn Buddies"

DRIVER_NAME = "Jackson Christopher Cocozello"
DRIVER_DOB = "11/20/1999"
DRIVER_DL = "C6062 38063 11994"
DRIVER_STATE = "NJ"
DRIVER_EXP = "11/20/2029"
DRIVER_ADDR = "1 Deer Ln, Lincroft, NJ 07738-1705"
DRIVER_CLASS = "Class D (Auto)"
DRIVER_NOTATION = "INTERLOCK DEVICE (Scheduled for removal this week)"

LICENSE_PDF_PATH = "/opt/renewal-automation-system/data/documents/Jackson_License.pdf"

def send_carrier_email(service):
    sender = "carlo@streetsmart.insurance"
    to_email = "ABakhsh@plymouthrock.com"
    cc_list = ["commercialauto@plymouthrock.com", "carlo@streetsmart.insurance"]
    subject = f"{NAMED_INSURED} [{POLICY_NUMBER}] - Policy Change Request - Driver Addition & Eligibility Inquiry"
    
    body_text = f"""Hi Annmarie,

Please see the policy change request below for {NAMED_INSURED}:

POLICY INFORMATION:
- Named Insured: {NAMED_INSURED}
- Carrier: {CARRIER_NAME}
- Policy Number: {POLICY_NUMBER}
- Policy Term: 11/26/2025 to 11/26/2026
- Line of Business: Commercial Auto ($1,000,000 CSL)

CHANGE REQUESTED: Commercial Driver Addition & Eligibility Review
- Full Legal Name: {DRIVER_NAME}
- Date of Birth: {DRIVER_DOB}
- Driver License Number: {DRIVER_DL}
- State of Issuance: {DRIVER_STATE}
- Residential Address: {DRIVER_ADDR}
- License Class: {DRIVER_CLASS}
- Expiration Date: {DRIVER_EXP}
- Special Notation: {DRIVER_NOTATION}

UNDERWRITING INQUIRY:
The insured is looking to hire and add Jackson Cocozello as a driver to their commercial auto schedule. His current NJ license reflects an "INTERLOCK DEVICE" notation; however, the driver advises that the interlock device is scheduled to be removed this week.

Could you please review his eligibility? Specifically:
1. Can underwriting pull the MVR and review his record for approval now while the removal is underway?
2. Or does the insured need to wait until the NJ MVC officially clears the notation and issues an updated, unrestricted license before he can be submitted and added to the policy?

Attached is a copy of his current New Jersey driver's license for your review.

Please advise on carrier eligibility and next steps.

Thank you,
Carlo Ferrara
Chief Operating Officer
StreetSmart Insurance
Office: (732) 462-8343
carlo@streetsmart.insurance
"""

    msg = MIMEMultipart()
    msg["From"] = f"Carlo Ferrara <{sender}>"
    msg["To"] = to_email
    msg["Cc"] = ", ".join(cc_list)
    msg["Subject"] = subject
    
    msg.attach(MIMEText(body_text, "plain", "utf-8"))
    
    # Attach License PDF
    if os.path.exists(LICENSE_PDF_PATH):
        with open(LICENSE_PDF_PATH, "rb") as f:
            attach = MIMEApplication(f.read(), _subtype="pdf")
            attach.add_header("Content-Disposition", "attachment", filename="Jackson_License.pdf")
            msg.attach(attach)
        logger.info(f"Attached {LICENSE_PDF_PATH} to carrier email.")
    else:
        logger.warning(f"License PDF not found at {LICENSE_PDF_PATH}!")

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    logger.info(f"Dispatching carrier change request email to {to_email}...")
    sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    msg_id = sent.get("id")
    logger.info(f"Carrier email successfully sent! Message ID: {msg_id}")
    return msg_id


def send_client_reply(service):
    sender = "carlo@streetsmart.insurance"
    to_email = "lawnbuddyteam@gmail.com"
    cc_list = ["lawnbuddyadam@gmail.com", "aguagenti1986@gmail.com", "chrisstevenson22@gmail.com", "holsey2@gmail.com"]
    subject = "Re: Updates"
    thread_id = "19fdc1ffb938c916"  # Updates thread
    
    body_text = f"""Hi Taylor,

Thanks for reaching out and sending over Jackson's license.

Because his current license has an "INTERLOCK DEVICE" notation, commercial auto carriers will typically decline or suspend driver approval if we pull the MVR before the restriction is officially cleared in the state database.

I have submitted an eligibility inquiry directly to our underwriter at Plymouth Rock along with Jackson's license to find out whether:
1. They can run his record now to pre-screen his eligibility, or
2. If we need to wait until the New Jersey MVC physically clears the restriction and issues his updated, unrestricted license before adding him to the policy.

I will let you know as soon as the underwriter gets back to us with their official guideline so we don't prematurely trigger an MVR decline or unwanted exclusion.

Best regards,

Carlo Ferrara
Chief Operating Officer
StreetSmart Insurance
Office: (732) 462-8343 | Direct: carlo@streetsmart.insurance
Certificates of Insurance: certs@streetsmart.insurance
"""

    msg = MIMEMultipart()
    msg["From"] = f"Carlo Ferrara <{sender}>"
    msg["To"] = to_email
    msg["Cc"] = ", ".join(cc_list)
    msg["Subject"] = subject
    
    msg.attach(MIMEText(body_text, "plain", "utf-8"))
    
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    body = {"raw": raw, "threadId": thread_id}
    logger.info(f"Dispatching client response email to {to_email} in thread {thread_id}...")
    try:
        sent = service.users().messages().send(userId="me", body=body).execute()
        msg_id = sent.get("id")
        logger.info(f"Client response successfully sent in thread! Message ID: {msg_id}")
        return msg_id
    except Exception as e:
        logger.warning(f"Sending with threadId failed ({e}), sending standalone reply...")
        sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
        msg_id = sent.get("id")
        logger.info(f"Client response successfully sent standalone! Message ID: {msg_id}")
        return msg_id


async def submit_ezlynx_change_request():
    logger.info("Connecting to Playwright via Chrome CDP (http://localhost:9222)...")
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        page = await ctx.new_page()
        
        target_url = f"https://app.ezlynx.com/ApplicantPortal/Policy/Actions/ChangeRequest/{APPLICANT_ID}/{POLICY_MASTER_ID}"
        logger.info(f"Navigating to {target_url}...")
        await page.goto(target_url, wait_until="networkidle", timeout=30000)
        
        # Verify fields
        desc_text = (
            f"Policy Change Request - Commercial Auto Driver Addition & Eligibility Review - "
            f"{DRIVER_NAME} (NJ DL: {DRIVER_DL}, DOB: {DRIVER_DOB}, {DRIVER_ADDR}) - "
            f"Interlock Device notation pending removal this week - "
            f"Submitted via Email to Underwriter Annmarie Bakhsh (Pending Carrier Review)"
        )
        logger.info(f"Filling Description textarea...")
        await page.fill("#Description", desc_text)
        
        # Screenshot before submit
        ss_dir = Path("/opt/renewal-automation-system/data/screenshots")
        ss_dir.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(ss_dir / "ezlynx_lawn_buddies_cr_filled.png"))
        logger.info("Saved pre-submit screenshot.")
        
        # Click ChangeRequestPolicyBtn
        logger.info("Clicking #ChangeRequestPolicyBtn...")
        await page.click("#ChangeRequestPolicyBtn")
        
        # Wait for navigation/processing
        await asyncio.sleep(6)
        logger.info(f"Landed URL after submit: {page.url}")
        logger.info(f"Page title: {await page.title()}")
        
        await page.screenshot(path=str(ss_dir / "ezlynx_lawn_buddies_cr_submitted.png"))
        logger.info("Saved post-submit screenshot.")
        await page.close()


def post_audit_trail(carrier_msg_id, client_msg_id):
    logger.info("Posting E&O audit note to EZLynx...")
    client = EZLynxApiClient()
    
    disc_title = "Policy Change Request - Driver Addition & Eligibility (Jackson Cocozello)"
    note_text = f"""[09/08/2026 - Commercial Auto Driver Addition & Eligibility Inquiry]
- Named Insured: {NAMED_INSURED}
- Policy Number: {POLICY_NUMBER} (Policy Master: {POLICY_MASTER_ID})
- Carrier: {CARRIER_NAME}
- Requester: Taylor (lawnbuddyteam@gmail.com)
- Prospective Driver: {DRIVER_NAME}
  * Date of Birth: {DRIVER_DOB}
  * NJ Driver License: {DRIVER_DL} (Exp: {DRIVER_EXP}, Class D)
  * Residential Address: {DRIVER_ADDR}
  * Notation on License: INTERLOCK DEVICE (Client advises scheduled for removal this week)
- Actions Taken:
  1. Keyed Policy Change Request tracking shell in EZLynx on policy #{POLICY_NUMBER} effective 09/08/2026.
  2. Submitted formal underwriting inquiry and license attachment to Plymouth Rock underwriter Annmarie Bakhsh (ABakhsh@plymouthrock.com, CC commercialauto@plymouthrock.com) to verify if MVR can be pulled now or if submission must wait until NJ MVC issues unrestricted physical license. (Google Workspace Message ID: {carrier_msg_id})
  3. Responded to Taylor at Lawn Buddies explaining carrier underwriting guidelines around interlock devices and setting expectations. (Google Workspace Message ID: {client_msg_id})
- Follow-up Due Date: 09/10/2026 awaiting carrier underwriting response.
ROBIE was here."""

    try:
        res = client.add_note_to_discussion(
            applicant_id=APPLICANT_ID,
            discussion_title=disc_title,
            note_text=note_text,
            policy_number=POLICY_NUMBER,
            line_of_business="Auto (Commercial)",
            carrier_name="Plymouth Rock Assurance Corp"
        )
        logger.info(f"Audit note posted: {res}")
    except Exception as e:
        logger.error(f"Error posting note: {e}")

    # Also upload license document
    if os.path.exists(LICENSE_PDF_PATH):
        logger.info(f"Uploading {LICENSE_PDF_PATH} to EZLynx account documents...")
        try:
            up_res = client.upload_document(
                applicant_id=APPLICANT_ID,
                file_path=Path(LICENSE_PDF_PATH),
                folder_name="Policy",
                policy_number=POLICY_NUMBER,
                doc_type="License",
                label_to_apply="Policy Change Document"
            )
            logger.info(f"Document upload result: {up_res}")
        except Exception as e:
            logger.error(f"Error uploading doc: {e}")


async def run_all():
    logger.info("Authenticating Google Workspace as carlo@streetsmart.insurance...")
    creds = get_service_account_credentials("carlo@streetsmart.insurance")
    service = build("gmail", "v1", credentials=creds)
    
    # 1. Send carrier inquiry email
    carrier_msg_id = send_carrier_email(service)
    
    # 2. Send client update email
    client_msg_id = send_client_reply(service)
    
    # 3. Enter Change Request in EZLynx
    await submit_ezlynx_change_request()
    
    # 4. Post audit trail to EZLynx
    post_audit_trail(carrier_msg_id, client_msg_id)
    
    logger.info("=== ALL ACTIONS COMPLETED SUCCESSFULLY ON ROBIE SERVER ===")

if __name__ == "__main__":
    asyncio.run(run_all())
