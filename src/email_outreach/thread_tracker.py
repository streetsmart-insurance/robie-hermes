"""Manages underwriter outreach lifecycle, 5-7 day follow-up cadence, and status updates."""

import logging
from datetime import date, datetime, timedelta
from typing import List, Optional
from sqlalchemy.orm import Session

from src.config import settings
from src.database.models import (
    PolicyRenewal, OutreachThread, AuditNoteLog,
    RenewalStatus, ThreadStatus, ActionType
)
from src.email_outreach.gmail_client import GmailRenewalClient
from src.email_outreach.templates import get_outreach_subject, get_initial_outreach_body, get_followup_body
from src.email_outreach.intent_classifier import UnderwriterIntentClassifier
from src.ezlynx.note_builder import EZLynxNoteBuilder
from src.ezlynx.api_client import EZLynxApiClient
from src.intake.safety_gate import RenewalSafetyGate

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
    "Sandy Santana": "sandy@streetsmart.insurance",
    "Sandy Mara": "sandy@streetsmart.insurance",
    "Cimei, Taylor": "taylor@streetsmart.insurance",
    "Taylor Cimei": "taylor@streetsmart.insurance",
    "Valladarez, Angie": "angie@streetsmart.insurance",
    "Angie Valladarez": "angie@streetsmart.insurance",
    "Ramos, Eimy": "eimy@streetsmart.insurance",
    "Eimy Ramos": "eimy@streetsmart.insurance",
    "Perdomo, Lenin": "lenin@streetsmart.insurance",
    "Gabriela": "gabrielac@streetsmart.insurance",
    "Gabriela C": "gabrielac@streetsmart.insurance",
    "Ashley": "ashley@streetsmart.insurance",
    "Cabrera, Diana": "dianac@streetsmart.insurance",
    "Diana Cabrera": "dianac@streetsmart.insurance",
    "Dani": "dianac@streetsmart.insurance",
    "Flores, Ana": "ana@streetsmart.insurance",
    "Ana Flores": "ana@streetsmart.insurance",
    "Ana": "ana@streetsmart.insurance",
}

def resolve_outreach_cc_list(assigned_agent: Optional[str], always_cc_jake: bool = True) -> List[str]:
    """Builds CC recipient list ensuring the assigned CSR (or fallback) and Jake Ferrara are ALWAYS CC'd."""
    cc_recipients = []
    csr_email = None
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
    
    # If CSR could not be resolved, fallback to sandy@streetsmart.insurance to guarantee CSR coverage
    if not csr_email:
        csr_email = "sandy@streetsmart.insurance"

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
        classifier: Optional[UnderwriterIntentClassifier] = None,
        safety_gate: Optional[RenewalSafetyGate] = None
    ):
        self.gmail = gmail_client or GmailRenewalClient()
        self.ezlynx = ezlynx_api or EZLynxApiClient()
        self.classifier = classifier or UnderwriterIntentClassifier()
        self.safety_gate = safety_gate or RenewalSafetyGate(ezlynx_client=self.ezlynx)

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
            PolicyRenewal.status.in_([RenewalStatus.PENDING_EVALUATION, RenewalStatus.OUTREACH_PENDING]),
            PolicyRenewal.status != RenewalStatus.EXCLUDED_INACTIVE_ACCOUNT,
            (PolicyRenewal.source == "Manual") | (PolicyRenewal.source == None),
            PolicyRenewal.applicant_id != "169788491"
        ).all()

        sent_count = 0
        for pol in policies_to_email:
            days_to_exp = (pol.expiration_date - curr_date).days

            # 0. Pre-flight Cancellation & Non-Renewal Safety Gate
            safety_res = self.safety_gate.check_cancellation_risk(
                applicant_id=pol.applicant_id,
                policy_number=pol.policy_number
            )
            if safety_res.risk_detected:
                logger.warning(
                    f"Skipping email outreach for {pol.policy_number} ({pol.insured_name}): {safety_res.reason}"
                )
                pol.status = RenewalStatus.ESCALATED_CSR_REVIEW
                esc_note = (
                    f"⚠️ === [PRE-FLIGHT ESCALATION: CANCELLATION/NON-RENEWAL SIGNAL] ===\n"
                    f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"Policy #: {pol.policy_number}\n"
                    f"Named Insured: {pol.insured_name}\n"
                    f"Carrier / MGA: {pol.carrier_name}\n"
                    f"Reason: {safety_res.reason}\n"
                    f"Action Required: High-priority CSR review. Automated email outreach blocked.\n\n"
                    f"Robie was here"
                )
                db.add(AuditNoteLog(
                    policy_id=pol.id,
                    applicant_id=pol.applicant_id,
                    discussion_title=pol.discussion_title,
                    action_type=ActionType.STATUS_CHANGE,
                    note_text=esc_note
                ))
                self.ezlynx.create_user_task(
                    applicant_id=pol.applicant_id,
                    title=f"URGENT: Review Cancellation Signal for {pol.insured_name}",
                    description=esc_note,
                    assigned_user=pol.assigned_agent,
                    due_days_out=1,
                    policy_number=pol.policy_number,
                    line_of_business=pol.line_of_business,
                    carrier_name=pol.carrier_name,
                    high_priority=True
                )
                continue

            # 1. Skip if outside renewal window (> 45 days)
            if days_to_exp > settings.renewal_window_max_days:
                logger.debug(f"Skipping {pol.policy_number}: {days_to_exp} days to expiration (window max: {settings.renewal_window_max_days})")
                continue

            # 2. If already <= 25 days with no prior outreach, escalate immediately to CSR
            if days_to_exp <= settings.csr_escalation_threshold_days:
                pol.status = RenewalStatus.ESCALATED_MANUAL
                esc_note = (
                    f"⚠️ === [CSR ESCALATION - URGENT RENEWAL REVIEW] ===\n"
                    f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"Policy #: {pol.policy_number}\n"
                    f"Named Insured: {pol.insured_name}\n"
                    f"Carrier / MGA: {pol.carrier_name}\n"
                    f"Expiration Date: {pol.expiration_date} ({days_to_exp} days remaining)\n"
                    f"Assigned CSR: {pol.assigned_agent}\n"
                    f"Reason: Policy is {days_to_exp} days prior to expiration (threshold: {settings.csr_escalation_threshold_days} days) with no renewal terms.\n"
                    f"Action Required: High-priority CSR follow-up with carrier underwriter / portal.\n\n"
                    f"Robie was here"
                )
                db.add(AuditNoteLog(
                    policy_id=pol.id,
                    applicant_id=pol.applicant_id,
                    discussion_title=pol.discussion_title,
                    action_type=ActionType.STATUS_CHANGE,
                    note_text=esc_note
                ))
                res = self.ezlynx.add_note_to_discussion(
                    applicant_id=pol.applicant_id,
                    discussion_title=pol.discussion_title,
                    note_text=esc_note,
                    policy_number=pol.policy_number,
                    line_of_business=pol.line_of_business,
                    carrier_name=pol.carrier_name
                )
                if isinstance(res, dict) and isinstance(res.get("discussion_title"), str):
                    pol.discussion_title = res.get("discussion_title")
                self.ezlynx.create_user_task(
                    applicant_id=pol.applicant_id,
                    title=f"URGENT: Renewal Review for {pol.insured_name} ({days_to_exp} Days Remaining)",
                    description=f"Policy #{pol.policy_number} is {days_to_exp} days from expiration ({pol.expiration_date}). Assigned to {pol.assigned_agent}.",
                    assigned_user=pol.assigned_agent
                )
                continue

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
            res = self.ezlynx.add_note_to_discussion(
                applicant_id=pol.applicant_id,
                discussion_title=pol.discussion_title,
                note_text=note_text,
                policy_number=pol.policy_number,
                line_of_business=pol.line_of_business,
                carrier_name=pol.carrier_name
            )
            if isinstance(res, dict) and isinstance(res.get("discussion_title"), str):
                pol.discussion_title = res.get("discussion_title")

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
            days_to_exp = (pol.expiration_date - curr_date).days
            next_count = thread.followup_count + 1

            if next_count > settings.max_followups or days_to_exp <= settings.csr_escalation_threshold_days:
                # Exceeded max follow-ups (3) OR reached 25 days before expiration -> Escalate to CSR in EZLynx
                thread.status = ThreadStatus.EXHAUSTED
                pol.status = RenewalStatus.ESCALATED_MANUAL

                esc_note = (
                    f"⚠️ === [CSR ESCALATION - URGENT RENEWAL REVIEW] ===\n"
                    f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"Policy #: {pol.policy_number}\n"
                    f"Named Insured: {pol.insured_name}\n"
                    f"Carrier / MGA: {pol.carrier_name} | Underwriter: {pol.underwriter_email}\n"
                    f"Expiration Date: {pol.expiration_date} ({days_to_exp} days remaining)\n"
                    f"Assigned CSR: {pol.assigned_agent}\n"
                    f"Outreach Attempts: {thread.followup_count} email follow-ups sent with no reply.\n"
                    f"Reason: No renewal quote received by {days_to_exp} days prior to expiration (threshold: {settings.csr_escalation_threshold_days} days).\n"
                    f"Action Required: High-priority CSR follow-up with carrier underwriter / portal.\n\n"
                    f"Robie was here"
                )
                db.add(AuditNoteLog(
                    policy_id=pol.id,
                    applicant_id=pol.applicant_id,
                    discussion_title=pol.discussion_title,
                    action_type=ActionType.STATUS_CHANGE,
                    note_text=esc_note
                ))
                res = self.ezlynx.add_note_to_discussion(
                    applicant_id=pol.applicant_id,
                    discussion_title=pol.discussion_title,
                    note_text=esc_note,
                    policy_number=pol.policy_number,
                    line_of_business=pol.line_of_business,
                    carrier_name=pol.carrier_name
                )
                if isinstance(res, dict) and isinstance(res.get("discussion_title"), str):
                    pol.discussion_title = res.get("discussion_title")
                self.ezlynx.create_user_task(
                    applicant_id=pol.applicant_id,
                    title=f"URGENT: Review / Call Carrier for {pol.insured_name} Renewal",
                    description=f"Automated email follow-ups ({thread.followup_count} attempts) received no response for Pol #{pol.policy_number} ({days_to_exp} days remaining).",
                    assigned_user=pol.assigned_agent
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
            res = self.ezlynx.add_note_to_discussion(
                applicant_id=pol.applicant_id,
                discussion_title=pol.discussion_title,
                note_text=note_text,
                policy_number=pol.policy_number,
                line_of_business=pol.line_of_business,
                carrier_name=pol.carrier_name
            )
            if isinstance(res, dict) and isinstance(res.get("discussion_title"), str):
                pol.discussion_title = res.get("discussion_title")
            followups_sent += 1

        db.commit()
        return followups_sent

    def process_incoming_inbox_replies(self, db: Session) -> int:
        """Polls robie@ + hello@ for underwriter replies and files them onto titled EZLynx cards.

        Delegates to ``uw_reply_filer`` so cadence, cleaner, and the standalone cron
        share one matching / no-orphan posting path.
        """
        from src.email_outreach.uw_reply_filer import file_inbox_replies

        result = file_inbox_replies(
            db=db,
            gmail=self.gmail,
            ezlynx=self.ezlynx,
            classifier=self.classifier,
        )
        return int(result.get("filed", 0))

