from datetime import datetime
from src.database.session import SessionLocal
from src.database.models import PolicyRenewal, OutreachThread, AuditNoteLog, RenewalStatus, ThreadStatus, ActionType

db = SessionLocal()

records = [
    {
        "pol_id": 30,
        "msg_id": "1a0691aed86a08f2",
        "note_id": "1122366237",
        "uw_email": "arwc@travelers.com",
        "discussion": "Workers Compensation Manual Renewal"
    },
    {
        "pol_id": 43,
        "msg_id": "1a0691b0e308f16b",
        "note_id": "1122366311",
        "uw_email": "renewals@xptpartners.com",
        "discussion": "General Liability Renewal"
    },
    {
        "pol_id": 44,
        "msg_id": "1a0691b2fe0d0468",
        "note_id": "1122366375",
        "uw_email": "renewals@xptpartners.com",
        "discussion": "Umbrella (Commercial) Manual Renewal"
    },
    {
        "pol_id": 40,
        "msg_id": "1a0691b5170121ad",
        "note_id": "1122366450",
        "uw_email": "Angie_Brunetti@rpsins.com",
        "discussion": "General Liability Renewal"
    },
    {
        "pol_id": 41,
        "msg_id": "1a0691b72008440f",
        "note_id": "1122366505",
        "uw_email": "Angie_Brunetti@rpsins.com",
        "discussion": "Umbrella (Commercial) Manual Renewal"
    },
    {
        "pol_id": 28,
        "msg_id": "1a0691b92176de46",
        "note_id": "1122366562",
        "uw_email": "customerservice@scoutig.com",
        "discussion": "Commercial Auto Renewal 2026-2027 ROCKLAKE"
    },
    {
        "pol_id": 29,
        "msg_id": "1a0691b92176de46",
        "note_id": "1122366562",
        "uw_email": "customerservice@scoutig.com",
        "discussion": "Commercial Auto Renewal 2026-2027 ROCKLAKE"
    }
]

for r in records:
    pol = db.query(PolicyRenewal).filter(PolicyRenewal.id == r["pol_id"]).first()
    if not pol:
        continue
    pol.status = RenewalStatus.EMAIL_SENT_AWAITING_REPLY
    pol.discussion_title = r["discussion"]
    
    # Add OutreachThread
    thread = db.query(OutreachThread).filter(OutreachThread.policy_id == pol.id).first()
    if not thread:
        thread = OutreachThread(
            policy_id=pol.id,
            thread_id=r["msg_id"],
            message_id=r["msg_id"],
            recipient_email=r["uw_email"],
            subject=f"[RENEWAL-REQ-{pol.id}] Renewal Request: {pol.insured_name}",
            sent_at=datetime.utcnow(),
            status=ThreadStatus.ACTIVE,
            follow_up_count=0
        )
        db.add(thread)
    else:
        thread.message_id = r["msg_id"]
        thread.status = ThreadStatus.ACTIVE
        thread.sent_at = datetime.utcnow()

    # Add AuditNoteLog
    note_log = AuditNoteLog(
        policy_id=pol.id,
        applicant_id=pol.applicant_id,
        discussion_title=r["discussion"],
        action_type=ActionType.INITIAL_EMAIL_SENT,
        note_text=f"Outreach sent via robie@streetsmart.insurance to {r['uw_email']}. Note ID: {r['note_id']}. Robie was here",
        ezlynx_note_id=r["note_id"],
        created_at=datetime.utcnow()
    )
    db.add(note_log)

db.commit()
print("Successfully synced 7 policy records with outreach threads and audit logs in renewals.db!")
db.close()
