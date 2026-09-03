"""Builder for standardized EZLynx audit notes mapped to Discussion Titles."""

from datetime import datetime, date
from typing import Optional, Dict, Any
from src.database.models import PolicyRenewal, ActionType

class EZLynxNoteBuilder:
    """Formats clear, audit-ready notes for EZLynx discussions."""

    @staticmethod
    def format_portal_check_note(
        policy: PolicyRenewal,
        success: bool,
        details: str,
        downloaded_file: Optional[str] = None
    ) -> str:
        status_tag = "✅ RENEWAL QUOTE RETRIEVED" if success else "⏳ PORTAL CHECKED - NOT YET RELEASED"
        lines = [
            f"=== [CARRIER PORTAL AUTOMATION] ===",
            f"Status: {status_tag}",
            f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"Carrier: {policy.carrier_name}",
            f"Policy #: {policy.policy_number}",
            f"Expiration Date: {policy.expiration_date}",
            f"Details: {details}"
        ]
        if downloaded_file:
            lines.append(f"Saved Document: {downloaded_file}")
        return "\n".join(lines)

    @staticmethod
    def format_outreach_email_note(
        policy: PolicyRenewal,
        tracking_code: str,
        recipient_email: str,
        subject: str,
        is_followup: bool = False,
        followup_number: int = 1,
        next_followup_date: Optional[date] = None,
        cc_list: Optional[list] = None
    ) -> str:
        action_header = f"=== [OUTREACH FOLLOW-UP #{followup_number} SENT] ===" if is_followup else "=== [INITIAL RENEWAL OUTREACH SENT] ==="
        lines = [
            action_header,
            f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"Tracking Reference: [{tracking_code}]",
            f"Underwriter / Recipient: {recipient_email}",
        ]
        if cc_list:
            lines.append(f"CC: {', '.join(cc_list)}")
        lines.extend([
            f"Subject: {subject}",
            f"Carrier: {policy.carrier_name}",
            f"Policy #: {policy.policy_number}",
            f"Expiration Date: {policy.expiration_date}"
        ])
        if next_followup_date:
            lines.append(f"Next Automated Follow-up Cadence Due: {next_followup_date} (5-7 day window)")
        return "\n".join(lines)

    @staticmethod
    def format_reply_received_note(
        policy: PolicyRenewal,
        tracking_code: str,
        sender_email: str,
        intent: str,
        summary: str,
        has_attachment: bool = False,
        attachment_name: Optional[str] = None
    ) -> str:
        lines = [
            f"=== [UNDERWRITER EMAIL RESPONSE RECEIVED] ===",
            f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"Tracking Reference: [{tracking_code}]",
            f"From: {sender_email}",
            f"Intent Classification: {intent}",
            f"Summary: {summary}"
        ]
        if has_attachment and attachment_name:
            lines.append(f"Attachment Received: {attachment_name} (Saved to Inbox Archive)")
        return "\n".join(lines)

    @staticmethod
    def format_quote_ready_task_note(
        policy: PolicyRenewal,
        renewal_premium: Optional[float] = None,
        expiring_premium: Optional[float] = None,
        document_path: Optional[str] = None
    ) -> str:
        diff_str = "N/A"
        if renewal_premium and expiring_premium:
            diff = renewal_premium - expiring_premium
            pct = (diff / expiring_premium) * 100
            diff_str = f"${renewal_premium:,.2f} vs Expiring ${expiring_premium:,.2f} ({'+' if diff >= 0 else ''}{pct:.1f}%)"
        elif renewal_premium:
            diff_str = f"${renewal_premium:,.2f}"

        lines = [
            f"🎉 === [RENEWAL QUOTE READY FOR USER REVIEW] ===",
            f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"Policy #: {policy.policy_number}",
            f"Named Insured: {policy.insured_name}",
            f"Carrier: {policy.carrier_name}",
            f"Premium Comparison: {diff_str}",
            f"Attached Quote: {document_path or 'Uploaded to Applicant Documents'}",
            f"Action Required: Account Manager review & customer presentation/binding."
        ]
        return "\n".join(lines)

    @staticmethod
    def format_csr_escalation_note(
        policy: PolicyRenewal,
        days_to_expiration: int,
        reason: str = "No renewal quote received by 20-25 days prior to expiration."
    ) -> str:
        lines = [
            f"⚠️ === [CSR ESCALATION - URGENT RENEWAL REVIEW] ===",
            f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"Policy #: {policy.policy_number}",
            f"Named Insured: {policy.insured_name}",
            f"Carrier / MGA: {policy.carrier_name}",
            f"Expiration Date: {policy.expiration_date} ({days_to_expiration} days remaining)",
            f"Assigned CSR: {policy.assigned_agent or 'Unassigned'}",
            f"Reason: {reason}",
            f"Action Required: High-priority CSR follow-up with carrier underwriter / portal."
        ]
        return "\n".join(lines)

