"""Email outreach module exports."""

from src.email_outreach.auth_setup import get_gmail_service, get_gmail_credentials
from src.email_outreach.gmail_client import GmailRenewalClient
from src.email_outreach.thread_tracker import OutreachCadenceManager
from src.email_outreach.intent_classifier import UnderwriterIntentClassifier, EmailClassification
from src.email_outreach.templates import get_outreach_subject, get_initial_outreach_body, get_followup_body

__all__ = [
    "get_gmail_service",
    "get_gmail_credentials",
    "GmailRenewalClient",
    "OutreachCadenceManager",
    "UnderwriterIntentClassifier",
    "EmailClassification",
    "get_outreach_subject",
    "get_initial_outreach_body",
    "get_followup_body"
]
