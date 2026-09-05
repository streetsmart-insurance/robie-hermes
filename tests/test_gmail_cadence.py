"""Tests for 5-7 day underwriter follow-up cadence."""

import pytest
from datetime import date, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.database.models import (
    Base, PolicyRenewal, OutreachThread, RenewalStatus, ThreadStatus
)
from src.email_outreach.thread_tracker import OutreachCadenceManager
from src.email_outreach.gmail_client import GmailRenewalClient
from src.ezlynx.api_client import EZLynxApiClient

@pytest.fixture
def test_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()

from unittest.mock import MagicMock

def test_outreach_initial_and_cadence_calculation(test_db):
    mock_gmail = MagicMock()
    mock_gmail.send_email.return_value = {"id": "mock_test_123", "threadId": "mock_th_123"}
    cadence_mgr = OutreachCadenceManager(
        gmail_client=mock_gmail,
        ezlynx_api=EZLynxApiClient()
    )
    today = date(2026, 9, 1)  # Tuesday

    policy = PolicyRenewal(
        policy_number="HO3-100",
        insured_name="David Clark",
        applicant_id="EZL-999",
        carrier_name="Travelers",
        expiration_date=today + timedelta(days=35),
        underwriter_email="david.uw@travelers.com",
        status=RenewalStatus.OUTREACH_PENDING,
        discussion_title="Manual Homeowners Renewal"
    )
    test_db.add(policy)
    test_db.commit()

    # 1. Process Initial Outreach
    sent_count = cadence_mgr.process_pending_outreach(test_db, current_date=today)
    assert sent_count == 1
    assert policy.status == RenewalStatus.EMAIL_SENT_AWAITING_REPLY

    thread = test_db.query(OutreachThread).filter(OutreachThread.policy_id == policy.id).first()
    assert thread is not None
    assert thread.followup_count == 0
    # Tuesday + 5 days = Sunday -> Moves to Monday (6 days out = Sept 7)
    assert (thread.next_followup_due - today).days in (5, 6, 7)

    # 2. Test Follow-up Trigger when date arrives
    future_date = thread.next_followup_due
    followups_sent = cadence_mgr.process_due_followups(test_db, current_date=future_date)
    assert followups_sent == 1
    assert thread.followup_count == 1
    assert (thread.next_followup_due - future_date).days in (5, 6, 7)

def test_dual_inbox_reply_and_policy_matching(test_db):
    cadence_mgr = OutreachCadenceManager(
        gmail_client=MagicMock(),
        ezlynx_api=EZLynxApiClient()
    )
    today = date(2026, 9, 1)

    policy = PolicyRenewal(
        id=42,
        policy_number="COMM-AUTO-7788",
        insured_name="Metro Freight Logistics",
        applicant_id="EZL-888",
        carrier_name="Progressive Commercial",
        expiration_date=today + timedelta(days=40),
        underwriter_email="uw@progressive.com",
        status=RenewalStatus.EMAIL_SENT_AWAITING_REPLY,
        discussion_title="Manual Commercial Auto Renewal"
    )
    test_db.add(policy)
    test_db.commit()

    # Simulate carrier emailing quote directly to hello@streetsmart.insurance
    simulated_carrier_email = {
        "subject": "Upcoming Renewal Quote - Metro Freight Logistics - COMM-AUTO-7788",
        "body": "Please find attached the renewal quote and loss runs.",
        "attachments": [{"filename": "Renewal_COMM-AUTO-7788.pdf", "path": "/fake/path/quote.pdf"}]
    }

    # Verify policy matcher recognizes it
    active_policies = [{"id": policy.id, "policy_number": policy.policy_number, "insured_name": policy.insured_name}]
    client = GmailRenewalClient(service=None)
    assert client._matches_any_policy(simulated_carrier_email, active_policies) is True
    assert simulated_carrier_email["matched_policy_id"] == 42
