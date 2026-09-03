"""Manages underwriter outreach lifecycle, 5-7 day follow-up cadence, and status updates."""

import re
import logging
from pathlib import Path
from datetime import date, datetime, timedelta
from typing import List, Tuple, Optional
from sqlalchemy.orm import Session

from src.config import settings
from src.database.models import (
    PolicyRenewal, OutreachThread, AuditNoteLog, DocumentRecord,
    RenewalStatus, ThreadStatus, ActionType
)
from src.email_outreach.gmail_client import GmailRenewalClient
from src.email_outreach.templates import get_outreach_subject, get_initial_outreach_body, get_followup_body
from src.email_outreach.intent_classifier import UnderwriterIntentClassifier, EmailClassification
from src.ezlynx.note_builder import EZLynxNoteBuilder
from src.ezlynx.api_client import EZLynxApiClient

logger = logging.getLogger("thread_tracker")

CSR_EMAIL_DIRECTORY = {
    "Bara, Maria": "maria@streetsmart.insurance",
    "Maria Bara": "maria@streetsmart.insurance",
    "Aguilar, Ricardo": "ricardo@streetsmart.insurance",
    "Ricardo Aguilar": "ricardo@streetsmart.insurance",
    "Aguilar, Daniela": "daniela@streetsmart.insurance",
    "Daniela Aguilar": "daniela@streetsmart.insurance",
    "Molina, Jazmin": "jazmin@streetsmart.insurance",
    "Jazmin Molina": "jazmin@streetsmart.insurance",
    "Ferrara, Jake": "jake@streetsmart.insurance",
    "Jake Ferrara": "jake@streetsmart.insurance",
    "Illanes, Andrea": "andrea@streetsmart.insurance",
    "Andrea Illanes": "andrea@streetsmart.insurance",
    "Santana, Sandy": "sandy@streetsmart.insurance",
    "Sandy Mara": "sandy@streetsmart.insurance",
    "Cimei, Taylor": "taylor@streetsmart.insurance",
    "Taylor Cimei": "taylor@streetsmart.insurance",
    "Valladarez, Angie": "angie@streetsmart.insurance",
    "Angie Valladarez": "angie@streetsmart.insurance",
}

def resolve_outreach_cc_list(assigned_agent: Optional[str], always_cc_jake: bool = True) -> List[str]:
    """Builds CC recipient list including the assigned CSR and Jake Ferrara."""
    cc_recipients = []
    if assigned_agent:
        csr_email = CSR_EMAIL_DIRECTORY.get(assigned_agent.strip())
        if not csr_email:
            name_parts = assigned_agent.replace(",", " ").split()
            for part in name_parts:
                part_clean = part.strip().lower()
                for key, email in CSR_EMAIL_DIRECTORY.items():
                    if part_clean in key.lower():
                        csr_email = email
                        break
                if csr_email:
                    break
        if csr_email and csr_email not in cc_recipients:
            cc_recipients.append(csr_email)

    if always_cc_jake and "jake@streetsmart.insurance" not in cc_recipients:
        cc_recipients.append("jake@streetsmart.insurance")

    return cc_recipients

class OutreachCadenceManager:
    """Orchestrates underwriter email outreach, follow-up timing (5-7 days), and reply processing."""

    def __init__(
        self,
        gmail_client: Optional[GmailRenewalClient] = None,
        ezlynx_api: Optional[EZLynxApiClient] = None,
        classifier: Optional[UnderwriterIntentClassifier] = None
    ):
        self.gmail = gmail_client or GmailRenewalClient()
        self.ezlynx = ezlynx_api or EZLynxApiClient()
        self.classifier = classifier or UnderwriterIntentClassifier()

    def _calc_next_followup(self, from_date: date, min_days: int = 5, max_days: int = 7) -> date:
        """Calculates next follow-up date (skipping weekends)."""
        target = from_date + timedelta(days=min_days)
        # If Saturday (5) or Sunday (6), move to Monday
        if target.weekday() == 5:
            target += timedelta(days=2)
        elif target.weekday() == 6:
            target += timedelta(days=1)
        return target

    def process_pending_outreach(self, db: Session, current_date: Optional[date] = None) -> int:
        """Sends initial renewal quote request emails for policies pending outreach."""
        from src.portals.carrier_routing import CarrierRoutingMatrix

        curr_date = current_date or date.today()
        policies_to_email = db.query(PolicyRenewal).filter(
            PolicyRenewal.status.in_([RenewalStatus.PENDING_EVALUATION, RenewalStatus.OUTREACH_PENDING])
        ).all()

        sent_count = 0
        for pol in policies_to_email:
            carrier_conf = CarrierRoutingMatrix.get_carrier_config(pol.carrier_name)
            target_email = pol.underwriter_email or carrier_conf.get("underwriter_email")
            
            # If carrier is strictly portal and no email is configured, skip email outreach
            if carrier_conf.get("channel") == "PORTAL" and not target_email:
                continue

            if not target_email:
                continue

            tracking_code = f"RENEWAL-REQ-{pol.id}"
            subject = get_outreach_subject(pol.id, pol.insured_name, pol.policy_number, pol.expiration_date)
            ask_portal = carrier_conf.get("channel") == "EMAIL_ASK_PORTAL"

            body = get_initial_outreach_body(
                underwriter_name=pol.underwriter_name,
                insured_name=pol.insured_name,
                policy_num=pol.policy_number,
                carrier_name=pol.carrier_name,
                line_of_business=pol.line_of_business,
                expiration_date=pol.expiration_date,
                expiring_premium=pol.expiring_premium,
                assigned_agent=pol.assigned_agent,
                ask_portal=ask_portal
            )

            # Send Email
            cc_list = resolve_outreach_cc_list(pol.assigned_agent, always_cc_jake=True)
            res = self.gmail.send_email(
                to_email=target_email,
                subject=subject,
                body_text=body,
                cc=cc_list
            )

            gmail_thread_id = res.get("threadId")
            next_followup = self._calc_next_followup(curr_date, settings.followup_cadence_min_days, settings.followup_cadence_max_days)

            # Record thread
            thread = db.query(OutreachThread).filter(
                (OutreachThread.tracking_code == tracking_code) | (OutreachThread.policy_id == pol.id)
            ).first()

            if thread:
                thread.gmail_thread_id = gmail_thread_id
                thread.last_message_id = res.get("id")
                thread.recipient_email = target_email
                thread.subject_line = subject
                thread.last_followup_at = datetime.utcnow()
                thread.next_followup_due = next_followup
                thread.status = ThreadStatus.ACTIVE
            else:
                thread = OutreachThread(
                    policy_id=pol.id,
                    tracking_code=tracking_code,
                    gmail_thread_id=gmail_thread_id,
                    last_message_id=res.get("id"),
                    recipient_email=target_email,
                    subject_line=subject,
                    initial_sent_at=datetime.utcnow(),
                    last_followup_at=datetime.utcnow(),
                    followup_count=0,
                    next_followup_due=next_followup,
                    status=ThreadStatus.ACTIVE
                )
                db.add(thread)

            # Update Policy Status
            pol.status = RenewalStatus.EMAIL_SENT_AWAITING_REPLY

            # Log EZLynx note
            note_text = EZLynxNoteBuilder.format_outreach_email_note(
                policy=pol,
                tracking_code=tracking_code,
                recipient_email=target_email,
                subject=subject,
                is_followup=False,
                next_followup_date=next_followup,
                cc_list=cc_list
            )
            note_log = AuditNoteLog(
                policy_id=pol.id,
                applicant_id=pol.applicant_id,
                discussion_title=pol.discussion_title,
                action_type=ActionType.INITIAL_EMAIL_SENT,
                note_text=note_text
            )
            db.add(note_log)
            self.ezlynx.add_note_to_discussion(
                applicant_id=pol.applicant_id,
                discussion_title=pol.discussion_title,
                note_text=note_text,
                policy_number=pol.policy_number
            )

            sent_count += 1

        db.commit()
        return sent_count

    def process_due_followups(self, db: Session, current_date: Optional[date] = None) -> int:
        """Checks active threads and sends 5-7 day follow-up emails where due."""
        curr_date = current_date or date.today()
        due_threads = db.query(OutreachThread).join(PolicyRenewal).filter(
            OutreachThread.status == ThreadStatus.ACTIVE,
            OutreachThread.next_followup_due <= curr_date,
            PolicyRenewal.status == RenewalStatus.EMAIL_SENT_AWAITING_REPLY
        ).all()

        followups_sent = 0
        for thread in due_threads:
            pol = thread.policy
            next_count = thread.followup_count + 1

            if next_count > settings.max_followups:
                # Exceeded max follow-ups -> Escalate to human
                thread.status = ThreadStatus.EXHAUSTED
                pol.status = RenewalStatus.ESCALATED_MANUAL

                esc_note = (
                    f"⚠️ === [MAX UNDERWRITER FOLLOW-UPS EXCEEDED] ===\n"
                    f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"Carrier: {pol.carrier_name} | Underwriter: {pol.underwriter_email}\n"
                    f"Outreach attempts: {settings.max_followups} follow-ups sent with no reply.\n"
                    f"Action Required: Account Manager direct phone call / manual escalation."
                )
                db.add(AuditNoteLog(
                    policy_id=pol.id,
                    applicant_id=pol.applicant_id,
                    discussion_title=pol.discussion_title,
                    action_type=ActionType.STATUS_CHANGE,
                    note_text=esc_note
                ))
                self.ezlynx.add_note_to_discussion(
                    applicant_id=pol.applicant_id,
                    discussion_title=pol.discussion_title,
                    note_text=esc_note,
                    policy_number=pol.policy_number
                )
                self.ezlynx.create_user_task(
                    applicant_id=pol.applicant_id,
                    title=f"URGENT: Call Underwriter for {pol.insured_name} Renewal",
                    description=f"Automated email follow-ups ({settings.max_followups} attempts) received no response for Pol #{pol.policy_number}."
                )
                continue

            days_to_exp = (pol.expiration_date - curr_date).days
            body = get_followup_body(
                followup_number=next_count,
                underwriter_name=pol.underwriter_name,
                insured_name=pol.insured_name,
                policy_num=pol.policy_number,
                expiration_date=pol.expiration_date,
                days_to_expiration=days_to_exp,
                assigned_agent=pol.assigned_agent
            )

            # Send Follow-up Email in same thread
            cc_list = resolve_outreach_cc_list(pol.assigned_agent, always_cc_jake=True)
            res = self.gmail.send_email(
                to_email=thread.recipient_email,
                subject=f"Re: {thread.subject_line}",
                body_text=body,
                thread_id=thread.gmail_thread_id,
                in_reply_to=thread.last_message_id,
                cc=cc_list
            )

            next_due = self._calc_next_followup(curr_date, settings.followup_cadence_min_days, settings.followup_cadence_max_days)
            thread.followup_count = next_count
            thread.last_followup_at = datetime.utcnow()
            thread.next_followup_due = next_due
            thread.last_message_id = res.get("id")

            # Log EZLynx Note
            note_text = EZLynxNoteBuilder.format_outreach_email_note(
                policy=pol,
                tracking_code=thread.tracking_code,
                recipient_email=thread.recipient_email,
                subject=f"Re: {thread.subject_line}",
                is_followup=True,
                followup_number=next_count,
                next_followup_date=next_due,
                cc_list=cc_list
            )
            db.add(AuditNoteLog(
                policy_id=pol.id,
                applicant_id=pol.applicant_id,
                discussion_title=pol.discussion_title,
                action_type=ActionType.FOLLOWUP_EMAIL_SENT,
                note_text=note_text
            ))
            self.ezlynx.add_note_to_discussion(
                applicant_id=pol.applicant_id,
                discussion_title=pol.discussion_title,
                note_text=note_text,
                policy_number=pol.policy_number
            )
            followups_sent += 1

        db.commit()
        return followups_sent

    def process_incoming_inbox_replies(self, db: Session) -> int:
        """Polls connected inboxes (Robie + Hello) for replies, classifies them, and updates EZLynx."""
        # Query active pending policies to match against incoming carrier emails
        pending_policies = db.query(PolicyRenewal).filter(
            PolicyRenewal.status.in_([
                RenewalStatus.PENDING_EVALUATION,
                RenewalStatus.EMAIL_SENT_AWAITING_REPLY,
                RenewalStatus.INFO_REQUESTED
            ])
        ).all()
        policy_dicts = [
            {"id": p.id, "policy_number": p.policy_number, "insured_name": p.insured_name}
            for p in pending_policies
        ]

        replies = self.gmail.poll_matching_replies(active_policies=policy_dicts)
        processed_count = 0

        for reply in replies:
            subject = reply.get("subject", "")
            policy_id = reply.get("matched_policy_id")
            inbox_source = reply.get("inbox_source", "inbox")

            if not policy_id:
                match = re.search(r"\[RENEWAL-REQ-(\d+)\]", subject)
                if match:
                    policy_id = int(match.group(1))

            if not policy_id:
                continue

            pol = db.query(PolicyRenewal).filter(PolicyRenewal.id == policy_id).first()
            if not pol:
                continue

            thread = db.query(OutreachThread).filter(OutreachThread.policy_id == policy_id).first()
            tracking_code = thread.tracking_code if thread else f"[RENEWAL-INBOX-{pol.id}]"

            att_paths = [a["path"] for a in reply.get("attachments", [])]
            att_names = [a["filename"] for a in reply.get("attachments", [])]

            # Classify underwriter reply intent
            classification: EmailClassification = self.classifier.classify(
                subject=subject,
                body_text=reply.get("body", ""),
                attachment_filenames=att_names
            )

            if thread:
                thread.latest_reply_summary = f"[{inbox_source} | {classification.intent}] {classification.summary}"
                thread.status = ThreadStatus.REPLIED

            # Handle attachments
            attached_file_path = None
            if att_paths:
                attached_file_path = str(att_paths[0])
                doc = DocumentRecord(
                    policy_id=pol.id,
                    file_name=att_names[0],
                    file_path=attached_file_path,
                    source=f"GMAIL_{inbox_source.upper()}"
                )
                db.add(doc)

            # Update Policy Status based on classification
            if classification.intent == "QUOTE_ATTACHED":
                pol.status = RenewalStatus.QUOTE_RECEIVED
                if thread:
                    thread.status = ThreadStatus.RESOLVED
            elif classification.intent == "INFO_REQUESTED":
                pol.status = RenewalStatus.INFO_REQUESTED
            elif classification.intent == "NON_RENEWAL_DECLINED":
                pol.status = RenewalStatus.NON_RENEWAL_DECLINED
                if thread:
                    thread.status = ThreadStatus.RESOLVED

            # Log EZLynx Note
            note_text = EZLynxNoteBuilder.format_reply_received_note(
                policy=pol,
                tracking_code=thread.tracking_code,
                sender_email=reply.get("sender", ""),
                intent=classification.intent,
                summary=classification.summary,
                has_attachment=bool(att_paths),
                attachment_name=att_names[0] if att_names else None
            )
            db.add(AuditNoteLog(
                policy_id=pol.id,
                applicant_id=pol.applicant_id,
                discussion_title=pol.discussion_title,
                action_type=ActionType.UNDERWRITER_REPLIED,
                note_text=note_text
            ))
            self.ezlynx.add_note_to_discussion(
                applicant_id=pol.applicant_id,
                discussion_title=pol.discussion_title,
                note_text=note_text,
                policy_number=pol.policy_number
            )

            # If quote attached, upload to EZLynx & create review task
            if attached_file_path and classification.intent == "QUOTE_ATTACHED":
                self.ezlynx.upload_document(
                    applicant_id=pol.applicant_id,
                    file_path=Path(attached_file_path),
                    folder_name="Renewals"
                )
                self.ezlynx.create_user_task(
                    applicant_id=pol.applicant_id,
                    title=f"Review Renewal Quote: {pol.insured_name} ({pol.carrier_name})",
                    description=f"Underwriter emailed renewal proposal for Pol #{pol.policy_number}. Document uploaded to EZLynx.",
                    assigned_user=pol.assigned_agent
                )

            processed_count += 1

        db.commit()
        return processed_count
