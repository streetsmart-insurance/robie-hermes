#!/usr/bin/env python3
import sys
from pathlib import Path
REPO_ROOT = Path("/opt/renewal-automation-system")
sys.path.insert(0, str(REPO_ROOT))

import logging
from src.email_outreach.gmail_client import GmailRenewalClient
from src.voice.voice_client import CarrierVoiceClient
from src.voice.context_hydrator import CallingDossier
from src.ezlynx.api_client import EZLynxApiClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("HartfordAuditPull")

ACCOUNTS = [
    {
        "applicant_id": "141557790",
        "insured_name": "Jay Vijay C Store LLC dba 6-12 Convenience Store",
        "policy_number": "13WECAZ4ZNZ",
        "csr_email": "jackie@streetsmart.insurance",
        "csr_name": "Jackie Arriola",
        "term": "08/10/2025 - 08/10/2026",
    },
    {
        "applicant_id": "21588003",
        "insured_name": "Cocoa Beach Tanning Lounge Inc.",
        "policy_number": "13WECAD7UT5",
        "csr_email": "erika@streetsmart.insurance",
        "csr_name": "Erika Palacios",
        "term": "08/10/2025 - 08/10/2026",
    }
]

def main():
    logger.info("Starting Hartford Audit Pull Workflow...")
    gmail_client = GmailRenewalClient()
    voice_client = CarrierVoiceClient()
    ezlynx_client = EZLynxApiClient()
    ezlynx_client.authenticate_classic()

    for acc in ACCOUNTS:
        logger.info(f"Processing: {acc['insured_name']} ({acc['policy_number']})")
        subject = f"{acc['insured_name']} - Policy # {acc['policy_number']} - Final Audit Statement Request"
        body_text = (
            "Dear The Hartford Audit & Agency Services Team,\n\n"
            f"StreetSmart Insurance represents the insured referenced below:\n\n"
            f"Insured Name: {acc['insured_name']}\n"
            f"Policy Number: {acc['policy_number']}\n"
            f"Line of Business: Workers Compensation\n"
            f"Policy Term: {acc['term']}\n\n"
            "Please email the completed Final Payroll Audit Billing Statement, Audit Settlement, and Workpapers "
            "for this policy term directly to robie@streetsmart.insurance.\n\n"
            "If additional payroll reports, 941s, or NY/NJ WR-30s are needed from the insured to close out this audit, "
            "please reply with the specific outstanding items so our service team can promptly deliver them.\n\n"
            "Thank you,\n"
            "Robie - Autonomous Insurance Operations\n"
            "StreetSmart Insurance Agency\n"
            "robie@streetsmart.insurance | (732) 440-9799\n"
        )
        cc_list = ["carlo@streetsmart.insurance", acc['csr_email']]
        email_res = gmail_client.send_email(
            to_email="agency.service@thehartford.com",
            subject=subject,
            body_text=body_text,
            cc=cc_list
        )
        msg_id = email_res.get("id") or email_res.get("message_id")
        logger.info(f"Channel 2 Email sent: {msg_id}")

        custom_instructions = (
            "You are Robie, an autonomous insurance operations assistant calling from StreetSmart Insurance agency. "
            f"Your objective is to speak with an agent servicing or audit representative at The Hartford regarding "
            f"Workers Compensation policy #{acc['policy_number']} for {acc['insured_name']}. "
            f"Introduce yourself as Robie from StreetSmart Insurance and state that you are inquiring about the final payroll audit statement for term {acc['term']}. "
            "Request that the completed final audit billing statement and settlement workpapers be emailed directly to robie@streetsmart.insurance. "
            "If the audit is pending or documents are needed, ask what specific documents are required so our service team can submit them. "
            "Be professional, courteous, and ask for the representative name or reference ID before concluding."
        )
        dossier = CallingDossier(
            policy_number=acc['policy_number'],
            insured_name=acc['insured_name'],
            carrier_name="The Hartford",
            line_of_business="Workers Compensation",
            carrier_phone="800-842-8868",
            applicant_id=acc['applicant_id'],
            assigned_csr_email=acc['csr_email'],
            custom_instructions=custom_instructions
        )
        call_res = voice_client.dispatch_call(dossier=dossier, dry_run=False)
        call_id = call_res.get("call_id")
        logger.info(f"Channel 3 Voice AI call dispatched: {call_id}")

        note_text = (
            "AUTONOMOUS AUDIT RETRIEVAL DISPATCHED (The Hartford)\n"
            f"Policy: {acc['policy_number']} (Workers Compensation | Term: {acc['term']})\n"
            f"Assigned CSR: {acc['csr_name']} ({acc['csr_email']})\n"
            "--------------------------------------------------\n"
            "1. Channel 1 (Carrier Portal EBC): Blocked by carrier edge WAF on GCP IP; automatically transitioned to Tri-Channel sequence.\n"
            f"2. Channel 2 (Carrier Email): Sent official audit statement & workpaper request to agency.service@thehartford.com "
            f"(CC: {acc['csr_email']}, carlo@streetsmart.insurance). Gmail ID: {msg_id}.\n"
            f"3. Channel 3 (Voice AI Phone Call): Dispatched live outbound call via Bland AI to The Hartford Commercial Agency/Audit Desk at (800) 842-8868. "
            f"Bland Call ID: {call_id}.\n"
            "Next Action: Ingest final statement upon arrival at robie@streetsmart.insurance, verify against ledger, and deliver via Audit SOP.\n\n"
            "ROBIE was here"
        )
        note_res = ezlynx_client.add_note_to_discussion(
            applicant_id=acc['applicant_id'],
            note_text=note_text,
            discussion_title=f"{acc['insured_name']} - Workers Comp Audit Verification",
            
        )
        logger.info(f"EZLynx note logged: {note_res.get('status')}")

if __name__ == '__main__':
    main()
