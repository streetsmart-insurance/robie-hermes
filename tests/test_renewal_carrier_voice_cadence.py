"""Carrier Robie Call from process_due_followups give-up (N=2), not a second path."""

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.config import settings
from src.database.models import (
    Base,
    DocumentRecord,
    OutreachThread,
    PolicyRenewal,
    RenewalStatus,
    ThreadStatus,
)
from src.email_outreach.thread_tracker import OutreachCadenceManager
from src.voice.renewal_cadence import (
    CARRIER_VOICE_AFTER_FOLLOWUPS,
    carrier_voice_already_attempted,
    place_one_carrier_voice,
    renewal_obtained_before_voice,
)


@pytest.fixture
def test_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


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
        underwriter_email="uw@thehartford.com",
        carrier_voice_attempted=False,
    )
    data.update(overrides)
    return PolicyRenewal(**data)


def _thread(policy, followup_count, **overrides) -> OutreachThread:
    today = date(2026, 9, 5)
    data = dict(
        policy_id=policy.id,
        tracking_code=f"RENEWAL-REQ-{policy.id or 1}",
        gmail_thread_id="th_1",
        last_message_id="msg_1",
        recipient_email="uw@thehartford.com",
        subject_line="[RENEWAL-REQ-1] Test",
        initial_sent_at=datetime(2026, 8, 20),
        followup_count=followup_count,
        next_followup_due=today - timedelta(days=1),
        status=ThreadStatus.ACTIVE,
    )
    data.update(overrides)
    return OutreachThread(**data)


def _tracker(voice=None):
    mock_voice = voice if voice is not None else MagicMock()
    mock_voice.dispatch.return_value = {
        "success": True,
        "call_id": "sim_carrier_001",
        "status": "DISPATCHED_SIMULATED",
    }
    return OutreachCadenceManager(
        gmail_client=MagicMock(
            send_email=MagicMock(return_value={"id": "msg_f", "threadId": "th_1"})
        ),
        ezlynx_api=MagicMock(),
        voice_dispatcher=mock_voice,
    ), mock_voice


def test_n_is_two_failed_checks_after_initial():
    assert CARRIER_VOICE_AFTER_FOLLOWUPS == 2
    assert settings.carrier_voice_after_followups == 2
    assert settings.max_followups == 3


def test_renewal_obtained_only_uses_specified_signals():
    assert renewal_obtained_before_voice(
        _policy(status=RenewalStatus.QUOTE_RECEIVED)
    )
    assert renewal_obtained_before_voice(
        _policy(status=RenewalStatus.READY_FOR_AGENT_REVIEW)
    )
    # Unused enum is never a signal.
    assert not renewal_obtained_before_voice(
        _policy(status=RenewalStatus.FOLLOWUP_SENT)
    )
    thread = OutreachThread(
        policy_id=1,
        tracking_code="x",
        recipient_email="a@b.c",
        subject_line="s",
        status=ThreadStatus.RESOLVED,
    )
    assert renewal_obtained_before_voice(_policy(), thread)
    with_doc = _policy()
    with_doc.documents = [
        DocumentRecord(
            policy_id=1,
            file_name="renewal.pdf",
            file_path="/tmp/renewal.pdf",
            source="UNDERWRITER_EMAIL",
        )
    ]
    assert renewal_obtained_before_voice(with_doc)
    assert not renewal_obtained_before_voice(
        _policy(status=RenewalStatus.EMAIL_SENT_AWAITING_REPLY)
    )


def test_giveup_after_two_followups_places_one_carrier_voice(test_db):
    policy = _policy()
    test_db.add(policy)
    test_db.commit()
    test_db.add(_thread(policy, followup_count=2))
    test_db.commit()
    test_db.refresh(policy)

    tracker, mock_voice = _tracker()
    sent = tracker.process_due_followups(test_db, current_date=date(2026, 9, 5))

    assert sent == 0
    mock_voice.dispatch.assert_called_once()
    kwargs = mock_voice.dispatch.call_args.kwargs
    assert kwargs["policy_number"] == policy.policy_number
    assert kwargs["call_type"] == "carrier"
    assert policy.carrier_voice_attempted is True
    assert policy.status == RenewalStatus.EMAIL_SENT_AWAITING_REPLY
    assert carrier_voice_already_attempted(policy) is True


def test_giveup_voice_never_refires(test_db):
    policy = _policy(carrier_voice_attempted=True)
    test_db.add(policy)
    test_db.commit()
    test_db.add(_thread(policy, followup_count=2))
    test_db.commit()

    tracker, mock_voice = _tracker()
    tracker.process_due_followups(test_db, current_date=date(2026, 9, 5))
    mock_voice.dispatch.assert_not_called()


def test_skip_voice_when_quote_received(test_db):
    policy = _policy(status=RenewalStatus.QUOTE_RECEIVED)
    test_db.add(policy)
    test_db.commit()
    # Due-followups query requires EMAIL_SENT_AWAITING_REPLY, so call helper.
    result = place_one_carrier_voice(
        policy,
        thread=_thread(policy, followup_count=2),
        dispatcher=MagicMock(),
        ezlynx_client=MagicMock(),
        db=test_db,
        dry_run=True,
    )
    assert result["status"] == "SKIPPED_RENEWAL_OBTAINED"


def test_skip_voice_when_document_filed(test_db):
    policy = _policy()
    test_db.add(policy)
    test_db.commit()
    test_db.add(
        DocumentRecord(
            policy_id=policy.id,
            file_name="2026-27 Renewal Offer.pdf",
            file_path="/tmp/renewal.pdf",
            source="UNDERWRITER_EMAIL",
        )
    )
    test_db.commit()
    test_db.refresh(policy)

    mock_voice = MagicMock()
    result = place_one_carrier_voice(
        policy,
        dispatcher=mock_voice,
        ezlynx_client=MagicMock(),
        db=test_db,
        dry_run=True,
    )
    assert result["status"] == "SKIPPED_RENEWAL_OBTAINED"
    mock_voice.dispatch.assert_not_called()


def test_first_two_followups_do_not_autodial(test_db):
    policy = _policy()
    test_db.add(policy)
    test_db.commit()
    test_db.add(_thread(policy, followup_count=1))
    test_db.commit()

    tracker, mock_voice = _tracker()
    sent = tracker.process_due_followups(test_db, current_date=date(2026, 9, 5))
    assert sent == 1
    mock_voice.dispatch.assert_not_called()
    assert policy.carrier_voice_attempted is False


def test_escalate_at_25d_without_n2_does_not_voice(test_db):
    policy = _policy(expiration_date=date(2026, 9, 5) + timedelta(days=24))
    test_db.add(policy)
    test_db.commit()
    test_db.add(_thread(policy, followup_count=1))
    test_db.commit()

    tracker, mock_voice = _tracker()
    sent = tracker.process_due_followups(test_db, current_date=date(2026, 9, 5))
    assert sent == 0
    assert policy.status == RenewalStatus.ESCALATED_MANUAL
    mock_voice.dispatch.assert_not_called()


def test_max_followups_escalate_still_fires_voice_once(test_db):
    policy = _policy()
    test_db.add(policy)
    test_db.commit()
    test_db.add(_thread(policy, followup_count=3))
    test_db.commit()

    tracker, mock_voice = _tracker()
    tracker.process_due_followups(test_db, current_date=date(2026, 9, 5))
    mock_voice.dispatch.assert_called_once()
    assert policy.status == RenewalStatus.ESCALATED_MANUAL
    assert policy.carrier_voice_attempted is True


def test_skip_voice_without_directory_phone_does_not_invent_email(test_db, monkeypatch):
    monkeypatch.setattr(
        "src.voice.renewal_cadence.lookup_carrier_phone", lambda *_a, **_k: None
    )
    policy = _policy(carrier_name="Unknown Boutique MGA With No Phone")
    test_db.add(policy)
    test_db.commit()
    mock_voice = MagicMock()
    mock_ezlynx = MagicMock()
    result = place_one_carrier_voice(
        policy,
        dispatcher=mock_voice,
        ezlynx_client=mock_ezlynx,
        db=test_db,
        dry_run=True,
    )
    assert result["status"] == "CLARIFICATION_NEEDED"
    mock_voice.dispatch.assert_not_called()
    assert policy.carrier_voice_attempted is True
    note = mock_ezlynx.add_note_to_discussion.call_args[1]["note_text"]
    assert "PHONE NUMBER NEEDED" in note
    assert "never invented" in note.lower()
    assert "Robie was here" in note
    assert "Robie Call" not in note or "label" not in note.lower()


def test_voice_uses_dispatcher_not_ezlynx_label(test_db):
    policy = _policy()
    test_db.add(policy)
    test_db.commit()
    test_db.add(_thread(policy, followup_count=2))
    test_db.commit()

    tracker, mock_voice = _tracker()
    tracker.process_due_followups(test_db, current_date=date(2026, 9, 5))
    ack = tracker.ezlynx.add_note_to_discussion.call_args[1]["note_text"]
    assert "Call type: carrier" in ack
    assert "client_outreach" not in ack
    assert "Robie was here" in ack
    # Must not apply a Robie Call org label (watcher loop).
    assert "noteLabels" not in str(tracker.ezlynx.add_note_to_discussion.call_args)


def test_portal_only_without_uw_email_does_not_invent_followup_thread(test_db):
    policy = _policy(
        carrier_name="Coterie",
        underwriter_email=None,
        status=RenewalStatus.PENDING_EVALUATION,
        portal_supported=True,
    )
    test_db.add(policy)
    test_db.commit()

    tracker, mock_voice = _tracker()
    sent = tracker.process_pending_outreach(test_db, current_date=date(2026, 9, 5))
    assert sent == 0
    assert test_db.query(OutreachThread).count() == 0
    followups = tracker.process_due_followups(test_db, current_date=date(2026, 9, 5))
    assert followups == 0
    mock_voice.dispatch.assert_not_called()
    assert policy.carrier_voice_attempted is False
