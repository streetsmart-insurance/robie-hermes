import sys
import datetime
sys.path.insert(0, '/opt/renewal-automation-system')
from src.ezlynx.api_client import EZLynxApiClient

client = EZLynxApiClient()
now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

corrective_notes = [
    {
        'aid': '37113156',
        'name': 'Philip Koehler',
        'title': 'High Risk Renewal Review',
        'policy': 'HCPC-HO6-852902-5',
        'note': f"""=== [AUDIT CORRECTION & MORTGAGEE VERIFICATION] ===
Timestamp: {now_str}
Policy: #HCPC-HO6-852902-5 (Homeowners - Homeowners Choice Property & Casualty)
Mortgagee: Rocket Mortgage LLC ISAOA
Loan #: 3461231661
Renewal Term: 2026-10-18 to 2027-10-18
Renewal Premium: $1,446.00
Submission Channel: MyCoverageInfo / Rocket Mortgage Lender Portal
Portal Tracking / Confirmation: 3866510
Status: WAITING_MORTGAGEE_PAYMENT (7-Day Monitoring Queue)

AUDIT CORRECTION NOTE:
Correcting previous note (Note ID 1123409787) which was posted to non-canonical thread 841938709 citing carrier 'UPCIC' and loan # '0421374778' in error. Active carrier is Homeowners Choice Property & Casualty (HCPC) and verified loan number is 3461231661. This card (Discussion ID 841126647 - 'High Risk Renewal Review') is the canonical renewal task.

ROBIE was here"""
    },
    {
        'aid': '37113156',
        'name': 'Philip Koehler',
        'title': 'Homeowners Renewal / Mortgage Verification',
        'policy': 'HCPC-HO6-852902-5',
        'note': f"""=== [AUDIT NOTICE: THREAD REDIRECTION] ===
Timestamp: {now_str}
AUDIT NOTICE:
Previous note (Note ID 1123409787) posted to this thread contained clerical errors (carrier cited as UPCIC instead of Homeowners Choice; loan cited as 0421374778 instead of 3461231661). The canonical renewal review task for this policy (HCPC-HO6-852902-5) is active under Discussion ID 841126647 ('High Risk Renewal Review') where the verified mortgagee submission (Tracking # 3866510, Loan # 3461231661) is formally documented.

ROBIE was here"""
    },
    {
        'aid': '199797187',
        'name': 'Sunil Dixit',
        'title': 'Homeowners Renewal / Mortgage Verification',
        'policy': '4236565',
        'note': f"""=== [AUDIT CORRECTION: LOAN NUMBER CLARIFICATION] ===
Timestamp: {now_str}
Policy: #4236565 (Homeowners - Farmers Mutual Fire Ins. Co of Salem)
Mortgagee: Nationstar Mortgage LLC / Mr. Cooper
Verified Loan #: 0429484256
Portal Tracking: 3866489
Status: WAITING_MORTGAGEE_PAYMENT (7-Day Monitoring Queue)

AUDIT CORRECTION NOTE:
Clarifying previous note (Note ID 1123409789) which erroneously cited loan # 0644321045. Verified loan number in agency database and mortgagee schedule is 0429484256. Tracking # 3866489 confirmed under correct loan.

ROBIE was here"""
    },
    {
        'aid': '21586719',
        'name': 'Blanca Tipan',
        'title': 'Homeowners Renewal / Mortgage Verification',
        'policy': 'HONJ026092',
        'note': f"""=== [AUDIT CORRECTION: CARRIER CLARIFICATION] ===
Timestamp: {now_str}
Policy: #HONJ026092 (Homeowners - Farmers Mutual Fire Ins. Co of Salem)
Mortgagee: US Bank Home Mortgage
Loan #: 0114008272
Submission Channel: MyCoverageInfo (Status: MCI_SUBMISSION_UNDER_REVIEW)
Status: WAITING_MORTGAGEE_PAYMENT (7-Day Monitoring Queue)

AUDIT CORRECTION NOTE:
Correcting carrier attribution on previous note (Note ID 1123409798) which cited 'Kingstone'. Active writing carrier for policy HONJ026092 is Farmers Mutual Fire Ins. Co of Salem.

ROBIE was here"""
    },
    {
        'aid': '22234682',
        'name': 'Vasiliy Shafar',
        'title': 'Homeowners Renewal',
        'policy': 'HONJ2015070057-26',
        'note': f"""=== [AUDIT CORRECTION: CARRIER CLARIFICATION] ===
Timestamp: {now_str}
Policy: #HONJ2015070057-26 (Homeowners - Hyundai Marine & Fire Insurance Company)
Mortgagee: Mr. Cooper
Loan #: 0418520857
Portal Tracking: 3866500
Status: WAITING_MORTGAGEE_PAYMENT (7-Day Monitoring Queue)

AUDIT CORRECTION NOTE:
Correcting carrier attribution on previous note (Note ID 1123409795) which cited 'Kingstone'. Active writing carrier for policy HONJ2015070057-26 is Hyundai Marine & Fire Insurance Company.

ROBIE was here"""
    },
    {
        'aid': '54893172',
        'name': 'Hua Li',
        'title': 'Mortgage Verification',
        'policy': 'DPNJ2024100008-25',
        'note': f"""=== [AUDIT CORRECTION: CARRIER CLARIFICATION] ===
Timestamp: {now_str}
Policy: #DPNJ2024100008-25 (Dwelling Fire - Hyundai Marine & Fire Insurance Company)
Mortgagee: ServiceMac LLC
Loan #: 0015525049
Portal Tracking: 3866467
Status: WAITING_MORTGAGEE_PAYMENT (7-Day Monitoring Queue)

AUDIT CORRECTION NOTE:
Correcting carrier attribution on previous note (Note ID 1123409792) which cited 'Kingstone'. Active writing carrier for policy DPNJ2024100008-25 is Hyundai Marine & Fire Insurance Company.

ROBIE was here"""
    }
]

for item in corrective_notes:
    res = client.add_note_to_discussion(
        applicant_id=item['aid'],
        discussion_title=item['title'],
        note_text=item['note'],
        policy_number=item['policy']
    )
    note_id = (res.get('data') or {}).get('NoteId') or (res.get('data') or {}).get('id')
    print(f"[CORRECTED] AID {item['aid']} ({item['name']}) -> Title: '{item['title']}' | Status: {res.get('status')} | NoteId: {note_id}")
