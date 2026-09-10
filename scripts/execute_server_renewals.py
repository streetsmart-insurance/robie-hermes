import base64
import email
from email.mime.text import MIMEText
from pathlib import Path
import sqlite3

from src.email_outreach.auth_setup import get_robie_gmail_service
from src.ezlynx.api_client import EZLynxApiClient

ez = EZLynxApiClient()
svc = get_robie_gmail_service()
conn = sqlite3.connect('data/renewals.db')
c = conn.cursor()

# ---------------------------------------------------------
# 1. Sharon's Way 2 Move LLC (One80 / Diesel Insurance Solutions)
# ---------------------------------------------------------
print('=== 1. Sharon\'s Way 2 Move LLC ===')
sharon_to = 'elizabeth.rivera@one80.com'
sharon_cc = 'maria@streetsmart.insurance, jake@streetsmart.insurance'
sharon_sub = '[RENEWAL-REQ-54] Renewal Terms Request: Sharon\'s Way 2 Move LLC - Pol #CM92DC00437-001 MTC (Exp: 10/06/2026)'
sharon_body = '''Good morning Elizabeth,

We are requesting the renewal quotation terms and currently valued loss runs for the upcoming commercial auto renewal for Sharon's Way 2 Move LLC:

- Insured: Sharon's Way 2 Move LLC
- Policy Number: CM92DC00437-001 MTC
- Line of Business: Commercial Auto (MTC)
- Expiration Date: 10/06/2026
- Servicing Underwriter: Elizabeth Rivera (One80 Intermediaries / Diesel Insurance Solutions)

Please let us know if any renewal application, updated driver list/MVRs, or vehicle schedules are needed to release the renewal proposal.

Thank you,
Streetsmart Insurance Renewal Desk
'''
msg1 = MIMEText(sharon_body)
msg1['to'] = sharon_to
msg1['cc'] = sharon_cc
msg1['subject'] = sharon_sub
raw1 = base64.urlsafe_b64encode(msg1.as_bytes()).decode('utf-8')
sent1 = svc.users().messages().send(userId='me', body={'raw': raw1}).execute()
print(f'Sharon outreach sent. ID: {sent1["id"]}')

c.execute("""
    UPDATE policy_renewals 
    SET underwriter_email = ?, status = 'EMAIL_SENT_AWAITING_REPLY'
    WHERE id = 54
""", (sharon_to,))
conn.commit()

ez.add_note_to_discussion(
    applicant_id='197389903',
    discussion_title='Renewal Manual Auto (Commercial) | CM92DC00437-001 MTC Diesel Insurance Solutions',
    note_text='''Policy: #CM92DC00437-001 MTC (Auto (Commercial) - Diesel Insurance Solutions)

Direct renewal quotation & loss runs request emailed to underwriter Elizabeth Rivera (elizabeth.rivera@one80.com) at One80 Intermediaries / Diesel Insurance Solutions (CC: maria@, jake@).
Awaiting renewal terms or subjectivity requirements.

Robie was here''',
    policy_number='CM92DC00437-001 MTC',
    line_of_business='Auto (Commercial)',
    carrier_name='Diesel Insurance Solutions',
    require_existing_discussion=False
)
print('Sharon discussion note posted.')

# ---------------------------------------------------------
# 2. Edwin Lema & Marek PKS Transportation (Trinity Underwriters)
# ---------------------------------------------------------
print('=== 2. Edwin Lema & Marek PKS Transportation ===')
# Download / find loss run files
inbox_dir = Path('data/downloads/inbox_attachments')
lema_lr = inbox_dir / '1a082da98263e5b7_NT Loss Runs 2025-26.pdf'
marek_lr = inbox_dir / '1a082da78d5e2400_PDNT Loss Runs 2025-26.pdf'

# If files don't exist under those names, find any NT / PDNT loss runs
if not lema_lr.exists():
    for f in inbox_dir.glob('*NT Loss Runs*.pdf'):
        lema_lr = f
        break

if not marek_lr.exists():
    for f in inbox_dir.glob('*PDNT Loss Runs*.pdf'):
        marek_lr = f
        break

print(f'Lema LR: {lema_lr} (exists: {lema_lr.exists()})')
print(f'Marek LR: {marek_lr} (exists: {marek_lr.exists()})')

# Upload Edwin Lema loss runs
if lema_lr.exists():
    up_lema = ez.upload_document(
        applicant_id='145217363',
        file_path=lema_lr,
        folder_name='Renewal Offers/Declarations',
        description='Trinity Underwriters Currently Valued Loss Runs 2025-2026',
        policy_number='A23B8960-78760-SSRM NTL',
        doc_type='loss_runs',
        label_to_apply='Loss Runs'
    )
    print('Lema upload:', up_lema)

# Upload Marek loss runs
if marek_lr.exists():
    up_marek = ez.upload_document(
        applicant_id='84705043',
        file_path=marek_lr,
        folder_name='Renewal Offers/Declarations',
        description='Trinity Underwriters Currently Valued Loss Runs 2025-2026',
        policy_number='FINFR17078371-94344-SSRM NTL',
        doc_type='loss_runs',
        label_to_apply='Loss Runs'
    )
    print('Marek upload:', up_marek)

# Send Reply to Trinity Underwriters asking for renewal terms
trinity_to = 'lossruns@trinityunderwriters.net'
trinity_cc = 'quotes@trinityunderwriters.net, maria@streetsmart.insurance, jake@streetsmart.insurance'
trinity_sub = 'Re: Policy # A23B8960-78760-SSRM NTL & FINFR17078371-94344-SSRM NTL - Loss Runs Received / Renewal Terms Request'
trinity_body = '''Good morning Trinity Underwriting Team,

Thank you for providing the currently valued loss runs for:
1. Edwin Lema - Pol # A23B8960-78760-SSRM NTL (Exp: 10/05/2026)
2. Marek PKS Transportation Inc - Pol # FINFR17078371-94344-SSRM NTL & 248394-001APD-94344-SSRM APD (Exp: 10/16/2026)

The loss runs have been filed into the client files.

Could you please release the upcoming renewal quotations / proposals for both accounts, or let us know if any renewal questionnaires, updated driver lists, or vehicle schedules are required?

Thank you,
Streetsmart Insurance Renewal Desk
'''
msg2 = MIMEText(trinity_body)
msg2['to'] = trinity_to
msg2['cc'] = trinity_cc
msg2['subject'] = trinity_sub
raw2 = base64.urlsafe_b64encode(msg2.as_bytes()).decode('utf-8')
sent2 = svc.users().messages().send(userId='me', body={'raw': raw2}).execute()
print(f'Trinity reply sent. ID: {sent2["id"]}')

# Post notes and create tasks
ez.add_note_to_discussion(
    applicant_id='145217363',
    discussion_title='Renewal Manual Auto (Commercial) | A23B8960-78760-SSRM NTL Trinity Underwriters',
    note_text='''Policy: #A23B8960-78760-SSRM NTL (Auto (Commercial) - Trinity Underwriters)

Currently valued loss runs received from Trinity Underwriters and uploaded to Documents tab.
Follow-up sent to Trinity (lossruns@trinityunderwriters.net, CC: quotes@, maria@, jake@) formally requesting renewal terms and proposals.
Task assigned to Maria Bara.

Robie was here''',
    policy_number='A23B8960-78760-SSRM NTL',
    line_of_business='Auto (Commercial)',
    carrier_name='Trinity Underwriters',
    require_existing_discussion=False
)

ez.create_user_task(
    applicant_id='145217363',
    title='Follow Up: Edwin Lema Trinity Renewal Terms (Pol #A23B8960-78760-SSRM NTL)',
    description='Trinity loss runs received and uploaded to Documents tab. Renewal proposals requested from carrier. Follow up with underwriter for renewal terms.',
    assigned_user='Maria Bara',
    due_days_out=2,
    policy_number='A23B8960-78760-SSRM NTL',
    line_of_business='Auto (Commercial)',
    carrier_name='Trinity Underwriters',
    high_priority=False
)
print('Edwin Lema note and task created.')

ez.add_note_to_discussion(
    applicant_id='84705043',
    discussion_title='Renewal Manual Auto (Commercial) | FINFR17078371-94344-SSRM NTL Trinity Underwriters',
    note_text='''Policy: #FINFR17078371-94344-SSRM NTL (Auto (Commercial) - Trinity Underwriters)

Currently valued loss runs received from Trinity Underwriters and uploaded to Documents tab.
Follow-up sent to Trinity (lossruns@trinityunderwriters.net, CC: quotes@, maria@, jake@) formally requesting renewal terms and proposals.
Task assigned to Maria Bara.

Robie was here''',
    policy_number='FINFR17078371-94344-SSRM NTL',
    line_of_business='Auto (Commercial)',
    carrier_name='Trinity Underwriters',
    require_existing_discussion=False
)

ez.create_user_task(
    applicant_id='84705043',
    title='Follow Up: Marek PKS Transportation Trinity Renewal Terms (Pol #FINFR17078371-94344-SSRM NTL)',
    description='Trinity loss runs received and uploaded to Documents tab. Renewal proposals requested from carrier. Follow up with underwriter for renewal terms.',
    assigned_user='Maria Bara',
    due_days_out=2,
    policy_number='FINFR17078371-94344-SSRM NTL',
    line_of_business='Auto (Commercial)',
    carrier_name='Trinity Underwriters',
    high_priority=False
)
print('Marek note and task created.')

conn.close()
print('=== ALL SERVER ACTIONS FINISHED SUCCESSFULLY ===')
