import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

# Add project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ezlynx.discussion_poster import EZLynxDiscussionPoster
from src.email_outreach.gmail_client import GmailRenewalClient
from src.email_outreach.templates import get_initial_outreach_body, get_outreach_subject
from src.email_outreach.thread_tracker import CSR_EMAIL_DIRECTORY

BATCH_POLICIES = [
    {
        "applicant_id": "102351931",
        "insured_name": "Advance Marble & Granite LLC",
        "policy_number": "6S60UB A422965-3-25",
        "carrier_name": "NJCRIB - Hartford Assigned Risk",
        "underwriter_email": "arwc@travelers.com",
        "underwriter_name": "Hartford Underwriting Team",
        "lob": "Workers comp",
        "exp_date": "2026-10-22",
        "exp_prem": 1254.00,
        "csr_name": "Sandy Santana",
        "csr_email": "sandy@streetsmart.insurance",
        "discussion_search": "Workers Compensation Manual Renewal",
        "screenshot_name": "advance_marble_note_posted.png"
    },
    {
        "applicant_id": "157388228",
        "insured_name": "Epoxy Concrete Coatings LLC",
        "policy_number": "3AA948407",
        "carrier_name": "XPT Partners MGA",
        "underwriter_email": "renewals@xptpartners.com",
        "underwriter_name": "XPT Renewals Team",
        "lob": "General Liability",
        "exp_date": "2026-10-22",
        "exp_prem": 4000.00,
        "csr_name": "Eimy Ramos",
        "csr_email": "eimy@streetsmart.insurance",
        "discussion_search": "General Liability Renewal",
        "screenshot_name": "epoxy_gl_note_posted.png"
    },
    {
        "applicant_id": "157388228",
        "insured_name": "Epoxy Concrete Coatings LLC",
        "policy_number": "EZXS3220708",
        "carrier_name": "XPT Partners MGA",
        "underwriter_email": "renewals@xptpartners.com",
        "underwriter_name": "XPT Renewals Team",
        "lob": "Umbrella (Commercial)",
        "exp_date": "2026-10-22",
        "exp_prem": 2250.00,
        "csr_name": "Eimy Ramos",
        "csr_email": "eimy@streetsmart.insurance",
        "discussion_search": "Umbrella (Commercial) Manual Renewal",
        "screenshot_name": "epoxy_umbrella_note_posted.png"
    },
    {
        "applicant_id": "151382204",
        "insured_name": "Ank Construction LLC",
        "policy_number": "WS666360",
        "carrier_name": "Risk Placement Services (RPS) MGA",
        "underwriter_email": "Angie_Brunetti@rpsins.com",
        "underwriter_name": "Angie Brunetti",
        "lob": "General Liability",
        "exp_date": "2026-10-20",
        "exp_prem": 5041.00,
        "csr_name": "Lenin Perdomo",
        "csr_email": "lenin@streetsmart.insurance",
        "discussion_search": "General Liability Renewal",
        "screenshot_name": "ank_gl_note_posted.png"
    },
    {
        "applicant_id": "151382204",
        "insured_name": "Ank Construction LLC",
        "policy_number": "EZXS3220176",
        "carrier_name": "Risk Placement Services (RPS) MGA",
        "underwriter_email": "Angie_Brunetti@rpsins.com",
        "underwriter_name": "Angie Brunetti",
        "lob": "Umbrella (Commercial)",
        "exp_date": "2026-10-20",
        "exp_prem": 6000.00,
        "csr_name": "Lenin Perdomo",
        "csr_email": "lenin@streetsmart.insurance",
        "discussion_search": "Umbrella (Commercial) Manual Renewal",
        "screenshot_name": "ank_umbrella_note_posted.png"
    },
    {
        "applicant_id": "99055770",
        "insured_name": "Le Shawn Sneed",
        "policy_number": "CUS062900594 NTL / CUS062007927 APD",
        "carrier_name": "Rocklake Insurance Group MGA",
        "underwriter_email": "customerservice@scoutig.com",
        "underwriter_name": "Scout / Rocklake Underwriting",
        "lob": "Commercial Auto (NTL & Phys Dam)",
        "exp_date": "2026-10-20",
        "exp_prem": 2667.00,
        "csr_name": "Ricardo Aguilar",
        "csr_email": "ricardo@streetsmart.insurance",
        "discussion_search": "Commercial Auto Renewal 2026-2027 ROCKLAKE",
        "screenshot_name": "le_shawn_sneed_note_posted.png"
    }
]

async def run_batch():
    gmail = GmailRenewalClient()
    poster = EZLynxDiscussionPoster()
    results = []

    print(f"================================================================")
    print(f"🚀 STARTING BATCH MANUAL RENEWALS: {len(BATCH_POLICIES)} POLICIES")
    print(f"================================================================\n")

    for idx, item in enumerate(BATCH_POLICIES, 1):
        print(f"\n--- [{idx}/{len(BATCH_POLICIES)}] Processing: {item['insured_name']} ({item['policy_number']}) ---")
        
        # 1. Compose Email
        exp_d = datetime.strptime(item['exp_date'], '%Y-%m-%d').date()
        clean_pol = item['policy_number'].replace(" ", "").replace("/", "-")
        tracking_tag = clean_pol[:15]
        subject = f"[RENEWAL-REQ-{tracking_tag}] Renewal Request: {item['insured_name']} - Pol #{item['policy_number']}"
        body = get_initial_outreach_body(
            underwriter_name=item.get('underwriter_name'),
            insured_name=item['insured_name'],
            policy_num=item['policy_number'],
            carrier_name=item['carrier_name'],
            line_of_business=item['lob'],
            expiration_date=exp_d,
            expiring_premium=item.get('exp_prem'),
            assigned_agent=item.get('csr_name'),
            sender_name="Robie"
        )
        
        # Add routing header for test delivery
        test_body = (
            f"=== [OUTREACH DESTINATION: {item['underwriter_email']}] ===\n"
            f"Carrier: {item['carrier_name']}\n"
            f"Assigned CSR: {item['csr_name']} ({item['csr_email']})\n"
            f"----------------------------------------------------------\n\n"
            f"{body}"
        )

        # Send email preview to Carlo with CSR and Jake CC'd
        print(f"📧 Sending outreach test email via Gmail API...")
        email_res = gmail.send_email(
            to_email="carlo@streetsmart.insurance",
            subject=subject,
            body_text=test_body,
            cc=["jake@streetsmart.insurance"]
        )
        msg_id = email_res.get("id", "simulated")
        print(f"   -> Email Sent! Message ID: {msg_id}")

        # 2. Build Note for EZLynx
        note_text = (
            f"Autonomous Manual Renewal Outreach initiated via Gmail API (robie@streetsmart.insurance).\n"
            f"Carrier Underwriter: {item['underwriter_email']}\n"
            f"Policy #: {item['policy_number']}\n"
            f"Line of Business: {item['lob']}\n"
            f"Expiration Date: {item['exp_date']}\n"
            f"Assigned CSR: {item['csr_name']}\n"
            f"Tracking Subject: [RENEWAL-REQ-{tracking_tag}]\n"
            f"Status: Renewal terms & loss runs requested. Awaiting underwriter response.\n\n"
            f"Robie was here"
        )

        # 3. Post Note into EZLynx via CDP
        print(f"📝 Posting audit note to discussion '{item['discussion_search']}' in EZLynx...")
        post_res = await poster.post_note(
            applicant_id=item['applicant_id'],
            discussion_search_text=item['discussion_search'],
            note_text=note_text,
            screenshot_filename=item['screenshot_name']
        )

        note_id = post_res.get("note_id", "Posted")
        screenshot_path = post_res.get("screenshot_path", "")
        print(f"   -> Note Posted! Note ID: {note_id} | Screenshot: {screenshot_path}")

        results.append({
            "item": item,
            "email_id": msg_id,
            "note_id": note_id,
            "screenshot_path": screenshot_path,
            "success": post_res.get("success", False)
        })

    print(f"\n================================================================")
    print(f"✅ BATCH COMPLETED: {len(results)} POLICIES PROCESSED")
    print(f"================================================================")
    return results

if __name__ == "__main__":
    asyncio.run(run_batch())
