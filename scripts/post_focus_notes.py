import sys
import datetime
sys.path.insert(0, '/opt/renewal-automation-system')
from src.ezlynx.api_client import EZLynxApiClient

client = EZLynxApiClient()
now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

focus_notes = [
    {
        'aid': '59695072',
        'name': 'Jeffrey Hill',
        'title': 'Flood Renewal 2026-2027',
        'policy': 'TNF4434785',
        'note': f"""=== [VERIFIED MORTGAGEE RENEWAL AUDIT] ===
Timestamp: {now_str}
Policy: #TNF4434785 (Flood - Neptune Flood Incorporated)
Mortgagee: JPMorgan Chase Bank, NA
Loan #: 4575507577
Renewal Term: 2026-10-11 to 2027-10-11
Renewal Premium: $1,653.62
Status: WAITING_MORTGAGEE_PAYMENT (7-Day Monitoring Queue)
Mortgagee Verification Evidence: Renewal invoice confirms 'NOTICE OF PREMIUM BILLED TO MORTGAGEE. Policy premium was submitted for payment to: JPMorgan Chase Bank, NA.'
Delivery Channel: Neptune renewal invoice routed & prepared for Chase MyCoverageInfo intake.
Renewal Document: Verified on file in Document Library ('TNF4434785 Renewal Dec Page.pdf' & 'TNF4434785 Renewal Invoice.pdf').

ROBIE was here"""
    },
    {
        'aid': '48137418',
        'name': 'Winston & Sandra James',
        'title': 'Flood Renewal 26- 27',
        'policy': '29 1152209111 05',
        'note': f"""=== [VERIFIED MORTGAGEE RENEWAL AUDIT] ===
Timestamp: {now_str}
Policy: #29 1152209111 05 (Flood - Wright National Flood Insurance Company)
Mortgagee: Lakeview Loan Servicing LLC ISAOA ATIMA
Loan #: 7441037477
Renewal Term: 2026-10-15 to 2027-10-15
Renewal Premium: $1,259.00
Status: WAITING_MORTGAGEE_PAYMENT (7-Day Monitoring Queue)
Mortgagee Verification Evidence: Renewal notice confirms 'Payor: First Mortgagee. THIS IS A COPY OF YOUR BILL.'
Delivery Channel: Wright Flood renewal package saved and queued for Lakeview / LoanCare intake.
Renewal Document: '29 1152209111 05 - Wright Flood Renewal Offer 2026-2027.pdf' verified uploaded in Document Library under Renewals (Labeled: Renewal Offer).

ROBIE was here"""
    },
    {
        'aid': '30142145',
        'name': 'Laura & Ahmet Sangiray',
        'title': 'Homeowners Renewal / Mortgage Verification',
        'policy': 'CVX0005466B',
        'note': f"""=== [VERIFIED MORTGAGEE RENEWAL AUDIT] ===
Timestamp: {now_str}
Policy: #CVX0005466B (Homeowners - Johnson & Johnson / Convex)
Mortgagee: Residential Credit Opportunities Trust IX-A (PO Box 7001, Troy, MI 48007)
Loan #: 9160096212
Renewal Term: 2026-10-11 to 2027-10-11
Renewal Premium: $4,317.20
Status: WAITING_MORTGAGEE_PAYMENT (7-Day Monitoring Queue)
Mortgagee Verification Evidence: Renewal Offer confirms Mortgagee: Residential Credit Opportunities Trust IX-A, PO Box 7001, Troy, MI 48007, Loan # 9160096212. Total Policy Premium: $4,317.20.
Delivery Channel: Renewal package saved and routed for ExpressInsuranceInfo / Fax mortgagee transmission.
Renewal Document: 'CVX0005466B - J&J Convex Homeowners Renewal Offer 2026-2027.pdf' verified uploaded in Document Library under Renewals (Labeled: Renewal Offer).

ROBIE was here"""
    }
]

for item in focus_notes:
    res = client.add_note_to_discussion(
        applicant_id=item['aid'],
        discussion_title=item['title'],
        note_text=item['note'],
        policy_number=item['policy']
    )
    note_id = (res.get('data') or {}).get('NoteId') or (res.get('data') or {}).get('id')
    print(f"[FOCUS NOTE POSTED] AID {item['aid']} ({item['name']}) -> Title: '{item['title']}' | Status: {res.get('status')} | NoteId: {note_id}")
