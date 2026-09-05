"""Unit tests for the manual-renewal carrier voice cadence hook."""

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.database.models import (
    ActionType,
    AuditNoteLog,
    Base,
    DocumentRecord,
    OutreachThread,
    PolicyRenewal,
    RenewalStatus,
    ThreadStatus,
)
from src.voice.processed_robie_notes import ProcessedRobieCallStore
from src.voice.renewal_cadence import (
    QUIET_FOLLOWUP_BUDGET,
    RenewalCarrierVoiceCadence,
    carrier_voice_already_placed,
    count_quiet_followup_checks,
    in_late_csr_escalation_window,
    renewal_already_obtained,
)


@pytest.fixture
def test_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture
def processed_store(tmp_path):
    return ProcessedRobieCallStore(tmp_path / "robie_call_processed_notes.sqlite")


def _policy(**overrides) -> PolicyRenewal:
    today = date(2026, 9, 5)
    data = dict(
        policy_number="UB-6N448514-25-42-V",
        insured_name="Yes We Do LLC",
        applicant_id="21588091",
        carrier_name="The Hartford",
        line_of_business="Workers Comp",
        expiration_date=today + timedelta(days=35),
        status=RenewalStatus.EMAIL_SENT_AWAITING_REPLY,
        discussion_title="Manual Workers Compensation Renewal",
        assigned_agent="Carlo Ferrara",
    )
    data.update(overrides)
    return PolicyRenewal(**data)


def _add_attempt(db, policy, action, text="Attempt"):
    db.add(
        AuditNoteLog(
            policy_id=policy.id,
            applicant_id=policy.applicant_id,
            discussion_title=policy.discussion_title,
            action_type=action,
            note_text=text,
        )
    )


def test_cadence_does_not_fire_when_renewal_already_obtained(test_db, processed_store):
    policy = _policy(status=RenewalStatus.QUOTE_RECEIVED, renewal_premium=1200.0)
    test_db.add(policy)
    test_db.commit()
    _add_attempt(test_db, policy, ActionType.INITIAL_EMAIL_SENT, "Emailed UW")
    _add_attempt(test_db, policy, ActionType.FOLLOWUP_EMAIL_SENT, "Follow-up #1")
    test_db.commit()

    mock_voice = MagicMock()
    mock_ezlynx = MagicMock()
    cadence = RenewalCarrierVoiceCadence(
        ezlynx_client=mock_ezlynx,
        voice_client=mock_voice,
        processed_store=processed_store,
    )
    result = cadence.process_policy(policy, db=test_db, dry_run=True)

    assert renewal_already_obtained(policy) is True
    assert result["status"] == "SKIPPED_RENEWAL_OBTAINED"
    mock_voice.dispatch_call.assert_not_called()


def test_cadence_does_not_fire_when_uw_reply_or_pdf_already_filed(test_db, processed_store):
    policy = _policy(status=RenewalStatus.EMAIL_SENT_AWAITING_REPLY)
    test_db.add(policy)
    test_db.commit()
    test_db.add(
        DocumentRecord(
            policy_id=policy.id,
            file_name="2026-27 Renewal Offer - Hartford UB.pdf",
            file_path="/tmp/renewal.pdf",
            source="UNDERWRITER_EMAIL",
        )
    )
    _add_attempt(test_db, policy, ActionType.INITIAL_EMAIL_SENT)
    _add_attempt(test_db, policy, ActionType.FOLLOWUP_EMAIL_SENT)
    test_db.commit()
    test_db.refresh(policy)

    cadence = RenewalCarrierVoiceCadence(
        ezlynx_client=MagicMock(),
        voice_client=MagicMock(),
        processed_store=processed_store,
    )
    result = cadence.process_policy(policy, db=test_db, dry_run=True)
    assert result["status"] == "SKIPPED_RENEWAL_OBTAINED"


def test_cadence_does_not_fire_on_same_day_portal_and_initial_email(test_db, processed_store):
    """Steps 2–3 are not the 5–7d follow-up budget. No autodial that day."""
    policy = _policy()
    test_db.add(policy)
    test_db.commit()
    _add_attempt(test_db, policy, ActionType.PORTAL_CHECK, "⏳ PORTAL CHECKED - NOT YET RELEASED")
    _add_attempt(test_db, policy, ActionType.INITIAL_EMAIL_SENT, "Emailed UW")
    test_db.commit()
    test_db.refresh(policy)

    assert count_quiet_followup_checks(policy) == 0
    mock_voice = MagicMock()
    cadence = RenewalCarrierVoiceCadence(
        ezlynx_client=MagicMock(),
        voice_client=mock_voice,
        processed_store=processed_store,
    )
    result = cadence.process_policy(policy, db=test_db, dry_run=True)
    assert result["status"] == "SKIPPED_UNDER_BUDGET"
    mock_voice.dispatch_call.assert_not_called()


def test_cadence_fires_once_after_two_quiet_followups(test_db, processed_store):
    policy = _policy()
    test_db.add(policy)
    test_db.commit()
    _add_attempt(test_db, policy, ActionType.INITIAL_EMAIL_SENT, "Emailed UW")
    _add_attempt(test_db, policy, ActionType.FOLLOWUP_EMAIL_SENT, "Follow-up #1")
    _add_attempt(test_db, policy, ActionType.FOLLOWUP_EMAIL_SENT, "Follow-up #2")
    test_db.commit()
    test_db.refresh(policy)

    assert count_quiet_followup_checks(policy) == QUIET_FOLLOWUP_BUDGET

    mock_voice = MagicMock()
    mock_voice.dispatch_call.return_value = {
        "success": True,
        "call_id": "sim_carrier_001",
        "status": "DISPATCHED_SIMULATED",
    }
    mock_ezlynx = MagicMock()
    mock_hydrator = MagicMock()
    dossier = MagicMock()
    dossier.carrier_phone = "+18005551234"
    mock_hydrator.hydrate.return_value = dossier

    cadence = RenewalCarrierVoiceCadence(
        ezlynx_client=mock_ezlynx,
        voice_client=mock_voice,
        hydrator=mock_hydrator,
        processed_store=processed_store,
    )
    first = cadence.process_policy(
        policy, db=test_db, dry_run=True, reference_date=date(2026, 9, 5)
    )
    second = cadence.process_policy(
        policy, db=test_db, dry_run=True, reference_date=date(2026, 9, 5)
    )

    assert first["status"] == "DISPATCHED_SIMULATED"
    assert first["call_id"] == "sim_carrier_001"
    assert first["attempts"] == 2
    assert first["call_type"] == "carrier"
    mock_voice.dispatch_call.assert_called_once()
    dossier_arg = mock_voice.dispatch_call.call_args.kwargs["dossier"]
    assert dossier_arg.call_type == "carrier"
    assert second["status"] == "SKIPPED_ALREADY_CALLED"
    ack = mock_ezlynx.add_note_to_discussion.call_args[1]["note_text"]
    assert "Robie was here" in ack
    assert "Call type: carrier" in ack
    assert "client_outreach" not in ack
    assert "renewal_reachout" not in ack
    assert "Policy: #" in ack


def test_cadence_skips_under_two_attempts(test_db, processed_store):
    policy = _policy()
    test_db.add(policy)
    test_db.commit()
    _add_attempt(test_db, policy, ActionType.FOLLOWUP_EMAIL_SENT, "Follow-up #1")
    test_db.commit()
    test_db.refresh(policy)

    mock_voice = MagicMock()
    cadence = RenewalCarrierVoiceCadence(
        ezlynx_client=MagicMock(),
        voice_client=mock_voice,
        processed_store=processed_store,
    )
    result = cadence.process_policy(policy, db=test_db, dry_run=True)
    assert result["status"] == "SKIPPED_UNDER_BUDGET"
    assert result["attempts"] == 1
    mock_voice.dispatch_call.assert_not_called()


def test_cadence_posts_note_and_skips_when_no_carrier_phone(test_db, processed_store):
    policy = _policy(carrier_name="Unknown Boutique MGA With No Phone")
    test_db.add(policy)
    test_db.commit()
    _add_attempt(test_db, policy, ActionType.FOLLOWUP_EMAIL_SENT, "Follow-up #1")
    _add_attempt(test_db, policy, ActionType.FOLLOWUP_EMAIL_SENT, "Follow-up #2")
    test_db.commit()
    test_db.refresh(policy)

    mock_voice = MagicMock()
    mock_ezlynx = MagicMock()
    mock_hydrator = MagicMock()
    mock_hydrator.hydrate.return_value = None

    cadence = RenewalCarrierVoiceCadence(
        ezlynx_client=mock_ezlynx,
        voice_client=mock_voice,
        hydrator=mock_hydrator,
        processed_store=processed_store,
    )
    result = cadence.process_policy(policy, db=test_db, dry_run=True)

    assert result["status"] == "CLARIFICATION_NEEDED"
    assert result["reason"] == "MISSING_CARRIER_PHONE"
    mock_voice.dispatch_call.assert_not_called()
    note = mock_ezlynx.add_note_to_discussion.call_args[1]["note_text"]
    assert "PHONE NUMBER NEEDED" in note
    assert "never invented" in note.lower()
    assert "Robie was here" in note


def test_cadence_does_not_count_portal_or_initial_email_as_quiet_checks():
    policy = _policy()
    policy.notes = [
        AuditNoteLog(
            policy_id=1,
            applicant_id="1",
            discussion_title="t",
            action_type=ActionType.PORTAL_CHECK,
            note_text="Status: ✅ RENEWAL QUOTE RETRIEVED",
        ),
        AuditNoteLog(
            policy_id=1,
            applicant_id="1",
            discussion_title="t",
            action_type=ActionType.INITIAL_EMAIL_SENT,
            note_text="Emailed UW",
        ),
    ]
    assert count_quiet_followup_checks(policy) == 0
    assert carrier_voice_already_placed(policy) is False


def test_cadence_counts_thread_followups_when_notes_missing():
    policy = _policy()
    policy.notes = []
    policy.threads = [
        OutreachThread(
            policy_id=1,
            tracking_code="RENEWAL-REQ-1",
            recipient_email="uw@example.com",
            subject_line="test",
            initial_sent_at=datetime(2026, 9, 1),
            followup_count=2,
            status=ThreadStatus.ACTIVE,
        )
    ]
    assert count_quiet_followup_checks(policy) == 2


def test_cadence_skips_escalated_manual_non_autodial(test_db, processed_store):
    policy = _policy(status=RenewalStatus.ESCALATED_MANUAL)
    test_db.add(policy)
    test_db.commit()
    _add_attempt(test_db, policy, ActionType.FOLLOWUP_EMAIL_SENT, "Follow-up #1")
    _add_attempt(test_db, policy, ActionType.FOLLOWUP_EMAIL_SENT, "Follow-up #2")
    test_db.commit()
    test_db.refresh(policy)

    mock_voice = MagicMock()
    cadence = RenewalCarrierVoiceCadence(
        ezlynx_client=MagicMock(),
        voice_client=mock_voice,
        processed_store=processed_store,
    )
    result = cadence.process_policy(policy, db=test_db, dry_run=True)
    assert result["status"] == "SKIPPED_NOT_WAITING"
    mock_voice.dispatch_call.assert_not_called()


def test_cadence_skips_late_25d_csr_window(test_db, processed_store):
    policy = _policy(expiration_date=date(2026, 9, 5) + timedelta(days=22))
    test_db.add(policy)
    test_db.commit()
    _add_attempt(test_db, policy, ActionType.FOLLOWUP_EMAIL_SENT, "Follow-up #1")
    _add_attempt(test_db, policy, ActionType.FOLLOWUP_EMAIL_SENT, "Follow-up #2")
    test_db.commit()
    test_db.refresh(policy)

    assert in_late_csr_escalation_window(policy, date(2026, 9, 5)) is True
    mock_voice = MagicMock()
    cadence = RenewalCarrierVoiceCadence(
        ezlynx_client=MagicMock(),
        voice_client=mock_voice,
        processed_store=processed_store,
    )
    result = cadence.process_policy(
        policy, db=test_db, dry_run=True, reference_date=date(2026, 9, 5)
    )
    assert result["status"] == "SKIPPED_LATE_CSR_ESCALATION"
    mock_voice.dispatch_call.assert_not_called()
