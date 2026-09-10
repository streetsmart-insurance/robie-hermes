import logging
from src.email_outreach.gmail_client import GmailRenewalClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("save_emails_to_ezlynx")

def main():
    client = GmailRenewalClient()
    applicant_id = "21587333"
    policy_number = "PAC00001215485"
    carrier_name = "Plymouth Rock Assurance Corp"
    lob = "Auto (Commercial)"
    target_discussion = "Commercial Auto Policy Change Request - CHANGE ME"

    carrier_body = """Hi Annmarie,

Please see the policy change request below for Green Lion Lawn Care LLC DBA Lawn Buddies:

POLICY INFORMATION:
- Named Insured: Green Lion Lawn Care LLC DBA Lawn Buddies
- Carrier: Plymouth Rock Assurance Corp (Palisades Insurance Company)
- Policy Number: PAC00001215485
- Policy Term: 11/26/2025 to 11/26/2026
- Line of Business: Commercial Auto ($1,000,000 CSL)

CHANGE REQUESTED: Commercial Driver Addition & Eligibility Review
- Full Legal Name: Jackson Christopher Cocozello
- Date of Birth: 11/20/1999
- Driver License Number: C6062 38063 11994
- State of Issuance: New Jersey (NJ)
- Residential Address: 1 Deer Ln, Lincroft, NJ 07738-1705
- License Class: Class D (Auto)
- Expiration Date: 11/20/2029
- Special Notation: INTERLOCK DEVICE (Scheduled for removal this week)

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
carlo@streetsmart.insurance"""

    logger.info("Saving Carrier Change Request Email to EZLynx via API...")
    res1 = client._save_sent_email_to_ezlynx(
        to_email="ABakhsh@plymouthrock.com",
        subject="Green Lion Lawn Care LLC DBA Lawn Buddies [PAC00001215485] - Policy Change Request - Driver Addition & Eligibility Inquiry",
        body_text=carrier_body,
        sent_msg_id="1a082b4d4750704a",
        applicant_id=applicant_id,
        policy_number=policy_number,
        discussion_title=target_discussion,
        carrier_name=carrier_name,
        line_of_business=lob,
        cc_emails=["commercialauto@plymouthrock.com", "carlo@streetsmart.insurance"]
    )
    logger.info(f"Carrier email saved result: {res1}")

    client_body = """Hi Taylor,

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
Certificates of Insurance: certs@streetsmart.insurance"""

    logger.info("Saving Client Update Email to EZLynx via API...")
    res2 = client._save_sent_email_to_ezlynx(
        to_email="lawnbuddyteam@gmail.com",
        subject="Re: Updates",
        body_text=client_body,
        sent_msg_id="1a082b4d62d7f8ab",
        applicant_id=applicant_id,
        policy_number=policy_number,
        discussion_title=target_discussion,
        carrier_name=carrier_name,
        line_of_business=lob,
        cc_emails=["lawnbuddyadam@gmail.com", "aguagenti1986@gmail.com", "chrisstevenson22@gmail.com", "holsey2@gmail.com"]
    )
    logger.info(f"Client email saved result: {res2}")

if __name__ == "__main__":
    main()
