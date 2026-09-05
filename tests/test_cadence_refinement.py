"""Unit tests for cadence refinements, 25-day auto-escalation, and CSR CC guarantee."""

import pytest
from datetime import date, timedelta
from unittest.mock import MagicMock, patch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.database.models import Base, PolicyRenewal, OutreachThread, AuditNoteLog, RenewalStatus, ThreadStatus
from src.email_outreach.thread_tracker import (
    OutreachCadenceManager,
    resolve_outreach_cc_list,
    CSR_EMAIL_DIRECTORY
)
from src.config import settings

@pytest.fixture
def mock_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()

def test_resolve_outreach_cc_list():
    # Direct match
    cc = resolve_outreach_cc_list("Sandy Santana")
    assert "sandy@streetsmart.insurance" in cc
    assert "jake@streetsmart.insurance" in cc

    # Reverse format "Last, First"
    cc_perdomo = resolve_outreach_cc_list("Perdomo, Lenin")
    assert "lenin@streetsmart.insurance" in cc_perdomo
    assert "jake@streetsmart.insurance" in cc_perdomo

    # Unknown CSR falls back to sandy@
    cc_unknown = resolve_outreach_cc_list("Nonexistent Agent")
    assert "sandy@streetsmart.insurance" in cc_unknown
    assert "jake@streetsmart.insurance" in cc_unknown

    # None falls back to sandy@
    cc_none = resolve_outreach_cc_list(None)
    assert "sandy@streetsmart.insurance" in cc_none
    assert "jake@streetsmart.insurance" in cc_none

def test_process_pending_outreach_window_and_escalation(mock_db):
    today = date(2026, 9, 3)

    # 1. Policy > 45 days out (e.g. 50 days) -> Should be skipped (upcoming)
    p_far = PolicyRenewal(
        policy_number="POL-FAR-50",
        insured_name="Far LLC",
        applicant_id="11111",
        carrier_name="Trinity Underwriters",
        expiration_date=today + timedelta(days=50),
        status=RenewalStatus.PENDING_EVALUATION,
        source="Manual",
        assigned_agent="Sandy Santana"
    )

    # 2. Policy in window (e.g. 35 days) -> Should be sent
    p_window = PolicyRenewal(
        policy_number="POL-WIN-35",
        insured_name="Window LLC",
        applicant_id="22222",
        carrier_name="Trinity Underwriters",
        underwriter_email="uw@trinity.com",
        expiration_date=today + timedelta(days=35),
        status=RenewalStatus.PENDING_EVALUATION,
        source="Manual",
        assigned_agent="Sandy Santana"
    )

    # 3. Policy <= 25 days out with no prior outreach (e.g. 20 days) -> Immediate CSR escalation
    p_late = PolicyRenewal(
        policy_number="POL-LATE-20",
        insured_name="Late LLC",
        applicant_id="33333",
        carrier_name="Trinity Underwriters",
        underwriter_email="uw@trinity.com",
        expiration_date=today + timedelta(days=20),
        status=RenewalStatus.PENDING_EVALUATION,
        source="Manual",
        assigned_agent="Eimy Ramos"
    )

    mock_db.add_all([p_far, p_window, p_late])
    mock_db.commit()

    mock_gmail = MagicMock()
    mock_gmail.send_email.return_value = {"id": "msg_win_123", "threadId": "th_win_123"}
    mock_ezlynx = MagicMock()

    tracker = OutreachCadenceManager(gmail_client=mock_gmail, ezlynx_api=mock_ezlynx)
    sent_count = tracker.process_pending_outreach(mock_db, current_date=today)

    assert sent_count == 1
    assert p_far.status == RenewalStatus.PENDING_EVALUATION
    assert p_window.status == RenewalStatus.EMAIL_SENT_AWAITING_REPLY
    assert p_late.status == RenewalStatus.ESCALATED_MANUAL

    # Verify task created for late policy
    mock_ezlynx.create_user_task.assert_called_with(
        applicant_id="33333",
        title="URGENT: Renewal Review for Late LLC (20 Days Remaining)",
        description=f"Policy #POL-LATE-20 is 20 days from expiration ({today + timedelta(days=20)}). Assigned to Eimy Ramos.",
        assigned_user="Eimy Ramos"
    )

    # Verify audit note ended with Robie was here
    notes = mock_db.query(AuditNoteLog).filter(AuditNoteLog.policy_id == p_late.id).all()
    assert len(notes) == 1
    assert notes[0].note_text.endswith("Robie was here")

def test_process_due_followups_max_and_escalation(mock_db):
    today = date(2026, 9, 3)

    # Policy with 3 prior followups -> Exceeds max 3 -> Auto-escalate
    p_exhausted = PolicyRenewal(
        policy_number="POL-EXHAUST-3",
        insured_name="Exhausted LLC",
        applicant_id="44444",
        carrier_name="Trinity Underwriters",
        underwriter_email="uw@trinity.com",
        expiration_date=today + timedelta(days=28),
        status=RenewalStatus.EMAIL_SENT_AWAITING_REPLY,
        assigned_agent="Sandy Santana"
    )
    mock_db.add(p_exhausted)
    mock_db.commit()

    th_exhausted = OutreachThread(
        policy_id=p_exhausted.id,
        tracking_code="RENEWAL-REQ-44444",
        gmail_thread_id="th_44444",
        last_message_id="msg_44444",
        recipient_email="uw@trinity.com",
        subject_line="[RENEWAL-REQ-44444] Test",
        followup_count=3, # Already sent 3
        next_followup_due=today - timedelta(days=1),
        status=ThreadStatus.ACTIVE
    )
    mock_db.add(th_exhausted)

    # Policy reaching <= 25 days before expiration -> Auto-escalate regardless of follow-up count
    p_25day = PolicyRenewal(
        policy_number="POL-25DAY",
        insured_name="Urgent 25 Day LLC",
        applicant_id="55555",
        carrier_name="Trinity Underwriters",
        underwriter_email="uw@trinity.com",
        expiration_date=today + timedelta(days=24), # 24 days left <= 25 threshold
        status=RenewalStatus.EMAIL_SENT_AWAITING_REPLY,
        assigned_agent="Lenin Perdomo"
    )
    mock_db.add(p_25day)
    mock_db.commit()

    th_25day = OutreachThread(
        policy_id=p_25day.id,
        tracking_code="RENEWAL-REQ-55555",
        gmail_thread_id="th_55555",
        last_message_id="msg_55555",
        recipient_email="uw@trinity.com",
        subject_line="[RENEWAL-REQ-55555] Test",
        followup_count=1,
        next_followup_due=today - timedelta(days=1),
        status=ThreadStatus.ACTIVE
    )
    mock_db.add(th_25day)
    mock_db.commit()

    mock_gmail = MagicMock()
    mock_ezlynx = MagicMock()

    tracker = OutreachCadenceManager(
        gmail_client=mock_gmail,
        ezlynx_api=mock_ezlynx,
        voice_dispatcher=MagicMock(),
    )
    followups_sent = tracker.process_due_followups(mock_db, current_date=today)

    assert followups_sent == 0 # Both escalated, none sent as ordinary follow-up
    assert th_exhausted.status == ThreadStatus.EXHAUSTED
    assert p_exhausted.status == RenewalStatus.ESCALATED_MANUAL
    assert th_25day.status == ThreadStatus.EXHAUSTED
    assert p_25day.status == RenewalStatus.ESCALATED_MANUAL

    # Verify both notes end with Robie was here
    notes = mock_db.query(AuditNoteLog).all()
    for n in notes:
        assert n.note_text.endswith("Robie was here")
