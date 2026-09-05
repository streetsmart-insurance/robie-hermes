import os
import sys
from datetime import date, datetime
from pathlib import Path

# Add project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.reporting.daily_handoff import DailyHandoffReporter
from src.reporting.email_handoff import send_daily_handoff_email

active_processed = [
    {
        "applicant_id": "166548777",
        "insured_name": "Edwin Lema",
        "policy_number": "TAR0010427-APD-93375-SSRM",
        "carrier_name": "Trinity Underwriters",
        "line_of_business": "Auto (Commercial)",
        "expiration_date": "2026-09-24",
        "expiring_premium": 2862.00,
        "assigned_csr": "Sandy Santana",
        "status": "Email Sent & Note Posted",
        "discussion_title": "Commercial Auto Renewal",
        "tracking_ref": "RENEWAL-REQ-166548777",
        "underwriter_email": "submissions@trinityunderwriters.com",
        "cc_list": ["sandy@streetsmart.insurance", "jake@streetsmart.insurance"],
        "screenshot_path": "data/screenshots/edwin_lema_note_posted.png"
    },
    {
        "applicant_id": "154331454",
        "insured_name": "THAA Hand & Stone",
        "policy_number": "TAR0010426-APD-93375-SSRM",
        "carrier_name": "Trinity Underwriters",
        "line_of_business": "Auto (Commercial)",
        "expiration_date": "2026-09-24",
        "expiring_premium": 2175.00,
        "assigned_csr": "Sandy Santana",
        "status": "Email Sent & Note Posted",
        "discussion_title": "Commercial Auto Renewal",
        "tracking_ref": "RENEWAL-REQ-154331454",
        "underwriter_email": "submissions@trinityunderwriters.com",
        "cc_list": ["sandy@streetsmart.insurance", "jake@streetsmart.insurance"],
        "screenshot_path": "data/screenshots/thaa_note_posted.png"
    },
    {
        "applicant_id": "152431698",
        "insured_name": "George Chafos",
        "policy_number": "0100414169-0",
        "carrier_name": "AmWINS MGA",
        "line_of_business": "Commercial Inland Marine",
        "expiration_date": "2026-10-02",
        "expiring_premium": 850.00,
        "assigned_csr": "Sandy Santana",
        "status": "Email Sent & Note Posted",
        "discussion_title": "Commercial Inland Marine Renewal",
        "tracking_ref": "RENEWAL-REQ-152431698",
        "underwriter_email": "support@amwins.com",
        "cc_list": ["sandy@streetsmart.insurance", "jake@streetsmart.insurance"],
        "screenshot_path": "data/screenshots/chafos_note_posted.png"
    },
    {
        "applicant_id": "154881881",
        "insured_name": "Maier Solar LLC DBA Solar Me",
        "policy_number": "BDG-3128281-01",
        "carrier_name": "AmWINS MGA",
        "line_of_business": "Commercial Package",
        "expiration_date": "2026-10-02",
        "expiring_premium": 1820.00,
        "assigned_csr": "Sandy Santana",
        "status": "Email Sent & Note Posted",
        "discussion_title": "Commercial Package Renewal",
        "tracking_ref": "RENEWAL-REQ-154881881",
        "underwriter_email": "support@amwins.com",
        "cc_list": ["sandy@streetsmart.insurance", "jake@streetsmart.insurance"],
        "screenshot_path": "data/screenshots/solar_note_posted.png"
    },
    {
        "applicant_id": "154881881",
        "insured_name": "Maier Solar LLC DBA Solar Me",
        "policy_number": "0100414172-0",
        "carrier_name": "AmWINS MGA",
        "line_of_business": "General Liability",
        "expiration_date": "2026-10-02",
        "expiring_premium": 2450.00,
        "assigned_csr": "Sandy Santana",
        "status": "Email Sent & Note Posted",
        "discussion_title": "Manual Commercial General Liability Renewal",
        "tracking_ref": "RENEWAL-REQ-154881881-GL",
        "underwriter_email": "support@amwins.com",
        "cc_list": ["sandy@streetsmart.insurance", "jake@streetsmart.insurance"],
        "screenshot_path": "data/screenshots/solar_gl_note_posted.png"
    },
    {
        "applicant_id": "102351931",
        "insured_name": "Advance Marble & Granite LLC",
        "policy_number": "6S60UB A422965-3-25",
        "carrier_name": "NJCRIB - Hartford Assigned Risk",
        "line_of_business": "Workers comp",
        "expiration_date": "2026-10-22",
        "expiring_premium": 1254.00,
        "assigned_csr": "Sandy Santana",
        "status": "Email Sent & Note Posted",
        "discussion_title": "Workers Compensation Manual Renewal",
        "tracking_ref": "RENEWAL-REQ-6S60UB-A422965",
        "underwriter_email": "arwc@travelers.com",
        "cc_list": ["sandy@streetsmart.insurance", "jake@streetsmart.insurance"],
        "screenshot_path": "data/screenshots/advance_marble_note_posted.png"
    },
    {
        "applicant_id": "157388228",
        "insured_name": "Epoxy Concrete Coatings LLC",
        "policy_number": "3AA948407",
        "carrier_name": "XPT Partners MGA",
        "line_of_business": "General Liability",
        "expiration_date": "2026-10-22",
        "expiring_premium": 4000.00,
        "assigned_csr": "Eimy Ramos",
        "status": "Email Sent & Note Posted",
        "discussion_title": "General Liability Renewal",
        "tracking_ref": "RENEWAL-REQ-3AA948407",
        "underwriter_email": "renewals@xptpartners.com",
        "cc_list": ["eimy@streetsmart.insurance", "jake@streetsmart.insurance"],
        "screenshot_path": "data/screenshots/epoxy_gl_note_posted.png"
    },
    {
        "applicant_id": "157388228",
        "insured_name": "Epoxy Concrete Coatings LLC",
        "policy_number": "EZXS3220708",
        "carrier_name": "XPT Partners MGA",
        "line_of_business": "Umbrella (Commercial)",
        "expiration_date": "2026-10-22",
        "expiring_premium": 2250.00,
        "assigned_csr": "Eimy Ramos",
        "status": "Email Sent & Note Posted",
        "discussion_title": "Umbrella (Commercial) Manual Renewal",
        "tracking_ref": "RENEWAL-REQ-EZXS3220708",
        "underwriter_email": "renewals@xptpartners.com",
        "cc_list": ["eimy@streetsmart.insurance", "jake@streetsmart.insurance"],
        "screenshot_path": "data/screenshots/epoxy_umbrella_note_posted.png"
    },
    {
        "applicant_id": "151382204",
        "insured_name": "Ank Construction LLC",
        "policy_number": "WS666360",
        "carrier_name": "Risk Placement Services (RPS) MGA",
        "line_of_business": "General Liability",
        "expiration_date": "2026-10-20",
        "expiring_premium": 5041.00,
        "assigned_csr": "Lenin Perdomo",
        "status": "Email Sent & Note Posted",
        "discussion_title": "General Liability Renewal",
        "tracking_ref": "RENEWAL-REQ-WS666360",
        "underwriter_email": "Angie_Brunetti@rpsins.com",
        "cc_list": ["lenin@streetsmart.insurance", "jake@streetsmart.insurance"],
        "screenshot_path": "data/screenshots/ank_gl_note_posted.png"
    },
    {
        "applicant_id": "151382204",
        "insured_name": "Ank Construction LLC",
        "policy_number": "EZXS3220176",
        "carrier_name": "Risk Placement Services (RPS) MGA",
        "line_of_business": "Umbrella (Commercial)",
        "expiration_date": "2026-10-20",
        "expiring_premium": 6000.00,
        "assigned_csr": "Lenin Perdomo",
        "status": "Email Sent & Note Posted",
        "discussion_title": "Umbrella (Commercial) Manual Renewal",
        "tracking_ref": "RENEWAL-REQ-EZXS3220176",
        "underwriter_email": "Angie_Brunetti@rpsins.com",
        "cc_list": ["lenin@streetsmart.insurance", "jake@streetsmart.insurance"],
        "screenshot_path": "data/screenshots/ank_umbrella_note_posted.png"
    },
    {
        "applicant_id": "99055770",
        "insured_name": "Le Shawn Sneed",
        "policy_number": "CUS062900594 NTL / CUS062007927 APD",
        "carrier_name": "Rocklake Insurance Group MGA",
        "line_of_business": "Commercial Auto (NTL & Phys Dam)",
        "expiration_date": "2026-10-20",
        "expiring_premium": 2667.00,
        "assigned_csr": "Ricardo Aguilar",
        "status": "Email Sent & Note Posted",
        "discussion_title": "Commercial Auto Renewal 2026-2027 ROCKLAKE",
        "tracking_ref": "RENEWAL-REQ-CUS062900594",
        "underwriter_email": "customerservice@scoutig.com",
        "cc_list": ["ricardo@streetsmart.insurance", "jake@streetsmart.insurance"],
        "screenshot_path": "data/screenshots/le_shawn_sneed_note_posted.png"
    }
]

excluded_inactive = [
    {
        "insured_name": "Varsity Moving LLC",
        "policy_number": "VARS-2025-01",
        "carrier_name": "Cover Whale MGA",
        "applicant_id": "32941812",
        "reason": "Policy expired on 02/15/2026 prior to automated renewal window; inactive in EZLynx."
    },
    {
        "insured_name": "Manjushri Enterprises LLC",
        "policy_number": "TAR0010425-APD-93375-SSRM",
        "carrier_name": "Trinity Underwriters",
        "applicant_id": "169788491",
        "reason": "Mid-term cancelled on 02/15/2026 per EZLynx PolicyAPI; zero active policies on applicant context."
    },
    {
        "insured_name": "The Tree Guy Service LLC",
        "policy_number": "TREE-JJ-2025-01",
        "carrier_name": "Johnson & Johnson, Inc. MGA",
        "applicant_id": "58135101",
        "reason": "Policy expired on 04/28/2026; applicant confirmed inactive in EZLynx."
    },
    {
        "insured_name": "Imperio Enterprises I LLC",
        "policy_number": "6S60UB-A374754-A-24 / NPP8993646",
        "carrier_name": "XPT Partners / RPS / Hartford",
        "applicant_id": "150751441",
        "reason": "Non-Renewal on GL policy NPP8993646 (commercial submission added to move to Utica shared quote); prior WC policy cancelled."
    }
]

portal_logins_needed = [
    {"carrier": "Coterie Insurance", "portal_url": "https://dashboard.coterieinsurance.com/", "policy_count": 3, "lob": "Commercial Package / BOP"},
    {"carrier": "The Hartford Portal", "portal_url": "https://service.thehartford.com/", "policy_count": 5, "lob": "Workers Comp & BOP"},
    {"carrier": "TAPCO Underwriters", "portal_url": "https://www.gotapco.com/", "policy_count": 2, "lob": "Commercial Property & GL"},
    {"carrier": "Hyundai Portal", "portal_url": "Carrier Portal", "policy_count": 1, "lob": "Commercial Auto"},
    {"carrier": "Selective Insurance", "portal_url": "https://www.selective.com/", "policy_count": 4, "lob": "Flood & Commercial"},
    {"carrier": "Johnson & Johnson, Inc. MGA", "portal_url": "https://www.jjins.com/", "policy_count": 2, "lob": "General Liability"},
    {"carrier": "Swyfft", "portal_url": "https://www.swyfft.com/", "policy_count": 3, "lob": "Homeowners & Commercial Property"},
    {"carrier": "Orchid Insurance", "portal_url": "https://orchidinsurance.com/", "policy_count": 2, "lob": "Coastal Property & Flood"},
    {"carrier": "Pathpoint", "portal_url": "https://www.pathpoint.com/", "policy_count": 2, "lob": "Excess & Surplus Lines"},
    {"carrier": "MGA Resource", "portal_url": "MGA Portal", "policy_count": 1, "lob": "Commercial Specialty"},
    {"carrier": "Utica First", "portal_url": "https://www.uticafirst.com/", "policy_count": 3, "lob": "Artisan Contractors & BOP"},
    {"carrier": "Universal Property", "portal_url": "https://universalproperty.com/", "policy_count": 2, "lob": "Property & Homeowners"}
]

upcoming_accounts = [
    {
        "applicant_id": "173838508",
        "insured_name": "The Laundorist Llc",
        "policy_number": "9300220617-00",
        "carrier_name": "Geico",
        "line_of_business": "Auto (Commercial)",
        "expiration_date": "2026-10-08",
        "expiring_premium": 3735.03,
        "assigned_csr": "Taylor Cimei",
        "planned_action": "Day 45 Outreach (CSR CC'd)"
    },
    {
        "applicant_id": "145834411",
        "insured_name": "Newline Logistics Trucking LLC",
        "policy_number": "CW5342160-00 MTC / TPM5334908-00 GL",
        "carrier_name": "Cover Whale MGA",
        "line_of_business": "Auto / GL (Commercial)",
        "expiration_date": "2026-10-12",
        "expiring_premium": 4025.03,
        "assigned_csr": "Maria Bara",
        "planned_action": "Day 45 Outreach (CSR CC'd)"
    },
    {
        "applicant_id": "193438339",
        "insured_name": "ABC TRANSPIRATION LLC",
        "policy_number": "02TRM066190-01",
        "carrier_name": "Berkshire Hathaway Inc",
        "line_of_business": "Auto (Commercial)",
        "expiration_date": "2026-10-15",
        "expiring_premium": 12638.00,
        "assigned_csr": "Ricardo Aguilar",
        "planned_action": "Day 45 Outreach (CSR CC'd)"
    },
    {
        "applicant_id": "199797602",
        "insured_name": "Kodomo Education Services LLC",
        "policy_number": "CCP35165-01",
        "carrier_name": "Markel Insurance Company",
        "line_of_business": "Commercial Package",
        "expiration_date": "2026-10-15",
        "expiring_premium": 3913.71,
        "assigned_csr": "Sandy Santana",
        "planned_action": "Day 45 Outreach (CSR CC'd)"
    },
    {
        "applicant_id": "76858853",
        "insured_name": "OTL General Contractor LLC",
        "policy_number": "67664405",
        "carrier_name": "CNA Surety",
        "line_of_business": "Bonds Miscellaneous",
        "expiration_date": "2026-10-20",
        "expiring_premium": 625.00,
        "assigned_csr": "Angie Valladarez",
        "planned_action": "Day 45 Outreach (CSR CC'd)"
    },
    {
        "applicant_id": "99340932",
        "insured_name": "Larry Gonzales",
        "policy_number": "ACT4034556",
        "carrier_name": "Neptune Insurance",
        "line_of_business": "Flood",
        "expiration_date": "2026-10-23",
        "expiring_premium": 554.63,
        "assigned_csr": "Daniela Aguilar",
        "planned_action": "Day 45 Outreach (CSR CC'd)"
    }
]

report_md = DailyHandoffReporter.generate_report_markdown(
    report_date=date.today(),
    active_processed=active_processed,
    excluded_inactive=excluded_inactive,
    portal_logins_needed=portal_logins_needed,
    upcoming_accounts=upcoming_accounts,
    output_filepath="reports/daily_handoff_2026_09_03.md"
)

# Also write to brain artifact
brain_artifact_path = "/Users/carloferrara/.gemini/antigravity/brain/54749f43-ff0c-4a5a-bd30-1701cbb01ec0/daily_handoff_2026_09_03.md"
with open(brain_artifact_path, "w") as f:
    f.write(report_md)

print("✅ Daily Handoff Report written to reports/daily_handoff_2026_09_03.md and brain artifact!")

# Send to Carlo, Jake, Gabriela, Sandy, Ashley
print("📤 Sending email distribution to team...")
email_res = send_daily_handoff_email(
    report_markdown=report_md,
    report_date=date.today()
)
print(f"✅ Daily Handoff Email Sent! Message ID: {email_res.get('id')}")
