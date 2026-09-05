"""Email outreach module exports."""

from src.email_outreach.auth_setup import get_gmail_service, get_gmail_credentials
from src.email_outreach.gmail_client import GmailRenewalClient, filter_renewal_poll_inboxes
from src.email_outreach.thread_tracker import OutreachCadenceManager
from src.email_outreach.intent_classifier import UnderwriterIntentClassifier, EmailClassification
from src.email_outreach.templates import get_outreach_subject, get_initial_outreach_body, get_followup_body
from src.email_outreach.uw_reply_filer import (
    extract_renewal_req_id,
    file_inbox_replies,
    run_uw_reply_filing,
)

__all__ = [
    "get_gmail_service",
    "get_gmail_credentials",
    "GmailRenewalClient",
    "filter_renewal_poll_inboxes",
    "OutreachCadenceManager",
    "UnderwriterIntentClassifier",
    "EmailClassification",
    "get_outreach_subject",
    "get_initial_outreach_body",
    "get_followup_body",
    "extract_renewal_req_id",
    "file_inbox_replies",
    "run_uw_reply_filing",
]
