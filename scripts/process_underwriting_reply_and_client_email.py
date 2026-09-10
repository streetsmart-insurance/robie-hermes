import asyncio
import base64
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from googleapiclient.discovery import build

from src.email_outreach.auth_setup import get_service_account_credentials
from src.email_outreach.gmail_client import GmailRenewalClient
from src.ezlynx.api_client import EZLynxApiClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("lawn_buddies_uw_reply")

APPLICANT_ID = "21587333"
POLICY_NUMBER = "PAC00001215485"
DISCUSSION_TITLE = "Commercial Auto Policy Change Request - CHANGE ME"

async def main():
    ezlynx = EZLynxApiClient()
    gmail_client = GmailRenewalClient()

    # ---------------------------------------------------------
    # 1. DISPATCH CLIENT NOTIFICATION EMAIL AS CARLO
    # ---------------------------------------------------------
    sender_email = "carlo@streetsmart.insurance"
    thread_id = "19fdc1ffb938c916"
    client_to = "lawnbuddyteam@gmail.com"
    client_cc = [
        "lawnbuddyadam@gmail.com",
        "aguagenti1986@gmail.com",
        "chrisstevenson22@gmail.com",
        "holsey2@gmail.com"
    ]
    client_subject = "Re: Updates"
    
    client_body = """Hi Taylor,

Following up on our review with Plymouth Rock regarding Jackson Cocozello:

Our commercial auto underwriter reviewed his driving credentials and confirmed that he does not meet underwriting guidelines due to the major violation associated with the interlock device. 

Plymouth Rock's commercial auto guidelines require a minimum of 5 years to have elapsed from the date of a major violation before a driver can qualify for their program. Consequently, we cannot add Jackson as an approved driver to your commercial auto policy at this time, and we have cancelled the pending policy change request.

Please let us know if you have any questions or if you are considering any other prospective drivers you would like us to pre-screen.

Best regards,

Carlo Ferrara
Chief Operating Officer
StreetSmart Insurance
Office: (732) 462-8343 | Direct: carlo@streetsmart.insurance
Certificates of Insurance: certs@streetsmart.insurance"""

    logger.info(f"Authenticating as {sender_email} via Service Account DWD...")
    creds = get_service_account_credentials(sender_email)
    service = build("gmail", "v1", credentials=creds)

    msg = MIMEMultipart()
    msg["To"] = client_to
    msg["Cc"] = ", ".join(client_cc)
    msg["From"] = f"Carlo Ferrara <{sender_email}>"
    msg["Subject"] = client_subject
    msg.add_header("In-Reply-To", "<CALP_GZq_G-X62gNn3zP7Z6-B81b5h7Wp2N3z=L=4@mail.gmail.com>")
    msg.add_header("References", "<CALP_GZq_G-X62gNn3zP7Z6-B81b5h7Wp2N3z=L=4@mail.gmail.com>")
    msg.attach(MIMEText(client_body, "plain"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    send_payload = {
        "raw": raw,
        "threadId": thread_id
    }

    logger.info("Sending client declination update email in thread 19fdc1ffb938c916...")
    sent_res = service.users().messages().send(userId="me", body=send_payload).execute()
    sent_msg_id = sent_res.get("id")
    logger.info(f"Client email sent successfully! Message ID: {sent_msg_id}")

    # ---------------------------------------------------------
    # 2. SAVE CLIENT SENT EMAIL VIA EZLYNX API (_save_sent_email_to_ezlynx)
    # ---------------------------------------------------------
    logger.info("Saving client declination email to EZLynx discussion card via API...")
    sync_res = gmail_client._save_sent_email_to_ezlynx(
        to_email=client_to,
        subject=client_subject,
        body_text=client_body,
        sent_msg_id=sent_msg_id,
        applicant_id=APPLICANT_ID,
        policy_number=POLICY_NUMBER,
        discussion_title=DISCUSSION_TITLE,
        carrier_name="Plymouth Rock Assurance Corp",
        line_of_business="Commercial Auto",
        cc_emails=client_cc
    )
    logger.info(f"EZLynx email sync result: {sync_res}")

    # ---------------------------------------------------------
    # 3. POST FORMAL CANCELLATION & AUDIT NOTE TO DISCUSSION
    # ---------------------------------------------------------
    logger.info("Posting comprehensive cancellation & underwriting closure audit note...")
    closure_note = f"""POLICY CHANGE REQUEST CANCELLED - DRIVER INELIGIBLE (UNDERWRITING DECLINATION)

Carrier: Plymouth Rock Assurance Corp (Palisades Insurance Company)
Policy Number: {POLICY_NUMBER}
Named Insured: Green Lion Lawn Care LLC DBA Lawn Buddies
Driver Reviewed: Jackson Christopher Cocozello (DOB: 11/20/1999, NJ DL: C6062 38063 11994)

UNDERWRITING DECISION:
- Underwriter Annmarie Bakhsh (ABakhsh@plymouthrock.com) reviewed Jackson Cocozello's driver license and interlock device notation.
- Official Underwriting Response (09/08/2026 4:36 PM):
  "Jackson Cocozello does not meet underwriting guidelines due to that major violation. After 5 years then he will be qualified for our program."
- Result: Driver addition CANCELLED / VOIDED. Prospective driver cannot be added to policy.

ACTIONS TAKEN:
1. Underwriter Decision EML & Correspondence PDF compiled and uploaded to EZLynx Document Library under Commercial Auto folder (Document: Plymouth Rock - Underwriting Ineligibility Decision (Jackson Cocozello).pdf).
2. Ineligible driver notice dispatched to insured (Taylor at lawnbuddyteam@gmail.com, cc: Adam, Ant, Chris, Patrick) via carlo@streetsmart.insurance (Gmail ID: {sent_msg_id}).
3. Sent client email archived to EZLynx via REST API.
4. Pending Change Request closed/cancelled. No changes made to active vehicle/driver schedule.

Status: CLOSED / INELIGIBLE DRIVER - NO COVERAGE BOUND
ROBIE was here."""

    ez_res = ezlynx.add_note_to_discussion(
        applicant_id=APPLICANT_ID,
        discussion_title=DISCUSSION_TITLE,
        note_text=closure_note,
        policy_number=POLICY_NUMBER,
        line_of_business="Commercial Auto",
        carrier_name="Plymouth Rock Assurance Corp",
        honor_explicit_title=True
    )
    logger.info(f"Closure audit note posted: {ez_res}")

    print("\n================ SUCCESS ================")
    print(f"1. Client Email Sent: ID {sent_msg_id}")
    print(f"2. Email Saved to EZLynx: {sync_res}")
    print(f"3. Discussion Note ID: {ez_res.get('note_id')}")
    print("=========================================\n")

if __name__ == "__main__":
    asyncio.run(main())
