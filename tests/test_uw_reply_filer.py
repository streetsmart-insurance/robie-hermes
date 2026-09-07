"""Focused tests for underwriter-reply → titled EZLynx discussion filing."""

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.database.models import (
    ActionType,
    AuditNoteLog,
    Base,
    OutreachThread,
    PolicyRenewal,
    RenewalStatus,
    ThreadStatus,
)
from src.email_outreach.gmail_client import (
    GmailRenewalClient,
    filter_renewal_poll_inboxes,
)
from src.email_outreach.intent_classifier import (
    EmailClassification,
    UnderwriterIntentClassifier,
)
from src.email_outreach.uw_reply_filer import (
    doc_type_for_intent,
    extract_renewal_req_id,
    file_inbox_replies,
    folder_and_label_for_doc_type,
    is_allowed_filing_inbox,
    match_policy_for_reply,
)
from src.ezlynx.document_uploader import FOLDER_ROUTING, LABEL_ROUTING
from src.email_outreach.robie_inbox_cleaner import categorize_message, NOISE_QUERIES
from src.ezlynx.note_builder import EZLynxNoteBuilder
from src.reporting.email_handoff import notify_csr_of_underwriter_reply


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture
def policy(db):
    pol = PolicyRenewal(
        policy_number="COMM-AUTO-7788",
        insured_name="Metro Freight Logistics",
        applicant_id="21588091",
        carrier_name="Progressive Commercial",
        line_of_business="Commercial Auto",
        expiration_date=date(2026, 10, 15),
        underwriter_email="uw@progressive.com",
        assigned_agent="Sandy Santana",
        status=RenewalStatus.EMAIL_SENT_AWAITING_REPLY,
        discussion_title="Manual Commercial Auto Renewal",
        source="Manual",
    )
    db.add(pol)
    db.commit()
    db.add(
        OutreachThread(
            policy_id=pol.id,
            tracking_code=f"RENEWAL-REQ-{pol.id}",
            gmail_thread_id="th_7788",
            last_message_id="msg_out_1",
            recipient_email="uw@progressive.com",
            subject_line=f"[RENEWAL-REQ-{pol.id}] Renewal Terms: Metro Freight Logistics",
            status=ThreadStatus.ACTIVE,
        )
    )
    db.commit()
    return pol


def _uw_reply(policy_id, inbox="robie@streetsmart.insurance", message_id="gmail_uw_1"):
    return {
        "message_id": message_id,
        "thread_id": "th_7788",
        "inbox_source": inbox,
        "sender": "uw@progressive.com",
        "subject": f"Re: [RENEWAL-REQ-{policy_id}] Renewal Terms: Metro Freight Logistics",
        "body": "Please find attached the renewal quote for COMM-AUTO-7788.",
        "clean_reply_text": "Please find attached the renewal quote for COMM-AUTO-7788.",
        "attachments": [{"filename": "Renewal_COMM-AUTO-7788.pdf", "path": "/tmp/quote.pdf"}],
    }


def test_extract_renewal_req_id_from_subject_and_body():
    assert extract_renewal_req_id("Re: [RENEWAL-REQ-42] Renewal Terms") == 42
    assert extract_renewal_req_id("No tag here", "See [RENEWAL-REQ-99] in body") == 99
    assert extract_renewal_req_id("Hello", "No tracking") is None


def test_inbox_scope_robie_and_hello_only():
    assert is_allowed_filing_inbox("robie@streetsmart.insurance") is True
    assert is_allowed_filing_inbox("hello@streetsmart.insurance") is True
    assert is_allowed_filing_inbox("ROBIE@streetsmart.insurance") is True
    assert is_allowed_filing_inbox("carlo@streetsmart.insurance") is False
    assert is_allowed_filing_inbox("sandy@streetsmart.insurance") is False
    assert is_allowed_filing_inbox("jake@streetsmart.insurance") is False

    filtered = filter_renewal_poll_inboxes(
        {
            "robie@streetsmart.insurance": "robie_svc",
            "hello@streetsmart.insurance": "hello_svc",
            "carlo@streetsmart.insurance": "carlo_svc",
            "sandy@streetsmart.insurance": "sandy_svc",
        }
    )
    assert set(filtered) == {
        "robie@streetsmart.insurance",
        "hello@streetsmart.insurance",
    }


def test_poll_matching_replies_never_queries_carlo_inbox():
    robie = MagicMock(name="robie")
    hello = MagicMock(name="hello")
    carlo = MagicMock(name="carlo")

    robie.users().messages().list().execute.return_value = {"messages": []}
    hello.users().messages().list().execute.return_value = {"messages": []}

    client = GmailRenewalClient(
        service=robie,
        inbox_services={
            "robie@streetsmart.insurance": robie,
            "hello@streetsmart.insurance": hello,
            "carlo@streetsmart.insurance": carlo,
        },
    )
    client.poll_matching_replies()
    carlo.assert_not_called()
    robie.users.assert_called()
    hello.users.assert_called()


def test_match_policy_by_renewal_req_tag(db, policy):
    reply = _uw_reply(policy.id)
    matched = match_policy_for_reply(db, reply, [policy])
    assert matched is not None
    assert matched.id == policy.id


def test_match_policy_by_policy_number_without_tag(db, policy):
    reply = {
        "subject": "Upcoming Renewal Quote - Metro Freight Logistics - COMM-AUTO-7788",
        "body": "Please find attached the renewal quote.",
        "attachments": [{"filename": "Renewal_COMM-AUTO-7788.pdf"}],
        "inbox_source": "hello@streetsmart.insurance",
    }
    matched = match_policy_for_reply(db, reply, [policy])
    assert matched is not None
    assert matched.id == policy.id


def test_file_inbox_replies_posts_formatted_note_and_skips_disqualified_card(db, policy):
    mock_gmail = MagicMock()
    mock_ezlynx = MagicMock()
    mock_ezlynx.find_matching_discussion.return_value = {
        "discussionId": 825365065,
        "title": "Commercial Auto Renewal (2026-2027)",
    }
    mock_ezlynx.add_note_to_discussion.return_value = {
        "status": "success",
        "discussion_title": "Commercial Auto Renewal (2026-2027)",
        "note_id": 998877,
    }
    mock_ezlynx.create_user_task.return_value = {"status": "success"}

    summary = file_inbox_replies(
        db=db,
        gmail=mock_gmail,
        ezlynx=mock_ezlynx,
        classifier=UnderwriterIntentClassifier(),
        replies=[_uw_reply(policy.id, inbox="hello@streetsmart.insurance")],
        dry_run=False,
        alert_csr=True,
    )

    assert summary["filed"] == 1
    mock_ezlynx.find_matching_discussion.assert_called_once()
    mock_ezlynx.add_note_to_discussion.assert_called_once()
    kwargs = mock_ezlynx.add_note_to_discussion.call_args.kwargs
    assert kwargs["require_existing_discussion"] is True
    assert kwargs["policy_number"] == "COMM-AUTO-7788"
    assert kwargs["line_of_business"] == "Commercial Auto"
    assert kwargs["carrier_name"] == "Progressive Commercial"
    assert kwargs["discussion_title"] == "Commercial Auto Renewal (2026-2027)"
    note = kwargs["note_text"]
    assert note.startswith("Policy: #COMM-AUTO-7788 (Commercial Auto - Progressive Commercial)")
    assert note.rstrip().endswith("Robie was here")
    assert f"[RENEWAL-REQ-{policy.id}]" in note
    assert "Inbox: hello@streetsmart.insurance" in note
    assert "Inbox Message ID: gmail_uw_1" in note

    audit = db.query(AuditNoteLog).filter(AuditNoteLog.policy_id == policy.id).one()
    assert audit.action_type == ActionType.UNDERWRITER_REPLIED
    assert audit.discussion_title == "Commercial Auto Renewal (2026-2027)"


def test_file_inbox_replies_falls_back_to_email_recieved_by_robie_when_no_matching_discussion(db, policy):
    mock_gmail = MagicMock()
    mock_ezlynx = MagicMock()
    mock_ezlynx.find_matching_discussion.return_value = None
    mock_ezlynx.add_note_to_discussion.return_value = {
        "status": "created",
        "discussion_id": "disc_fallback_99",
        "discussion_title": "Email recieved by Robie",
        "note_id": "note_fallback_1",
    }

    summary = file_inbox_replies(
        db=db,
        gmail=mock_gmail,
        ezlynx=mock_ezlynx,
        classifier=UnderwriterIntentClassifier(),
        replies=[_uw_reply(policy.id)],
        dry_run=False,
        alert_csr=False,
    )

    assert summary["filed"] == 1
    assert summary["skipped"] == 0
    mock_ezlynx.add_note_to_discussion.assert_called_once()
    call_kwargs = mock_ezlynx.add_note_to_discussion.call_args[1]
    assert call_kwargs["discussion_title"] == "Email recieved by Robie"
    assert call_kwargs["require_existing_discussion"] is False
    assert call_kwargs["honor_explicit_title"] is True


def test_file_inbox_replies_skips_carlo_inbox_even_if_injected(db, policy):
    mock_gmail = MagicMock()
    mock_ezlynx = MagicMock()

    summary = file_inbox_replies(
        db=db,
        gmail=mock_gmail,
        ezlynx=mock_ezlynx,
        classifier=UnderwriterIntentClassifier(),
        replies=[_uw_reply(policy.id, inbox="carlo@streetsmart.insurance")],
        dry_run=False,
        alert_csr=False,
    )

    assert summary["filed"] == 0
    assert summary["results"][0]["reason"] == "inbox_out_of_scope"
    mock_ezlynx.find_matching_discussion.assert_not_called()
    mock_ezlynx.add_note_to_discussion.assert_not_called()


def test_file_inbox_replies_dedups_already_filed_message(db, policy):
    reply = _uw_reply(policy.id, message_id="gmail_dup_9")
    db.add(
        AuditNoteLog(
            policy_id=policy.id,
            applicant_id=policy.applicant_id,
            discussion_title="Commercial Auto Renewal (2026-2027)",
            action_type=ActionType.UNDERWRITER_REPLIED,
            note_text="Inbox Message ID: gmail_dup_9\n\nRobie was here",
        )
    )
    db.commit()

    mock_ezlynx = MagicMock()
    mock_ezlynx.find_matching_discussion.return_value = {
        "discussionId": 1,
        "title": "Commercial Auto Renewal (2026-2027)",
    }

    summary = file_inbox_replies(
        db=db,
        gmail=MagicMock(),
        ezlynx=mock_ezlynx,
        classifier=UnderwriterIntentClassifier(),
        replies=[reply],
        dry_run=False,
        alert_csr=False,
    )
    assert summary["filed"] == 0
    assert summary["results"][0]["reason"] == "already_filed"
    mock_ezlynx.add_note_to_discussion.assert_not_called()


def test_cleaner_does_not_treat_uw_renewal_req_as_noise():
    """Inbox cleaner must keep legitimate underwriter replies (filing is additive)."""
    category = categorize_message(
        "Barb McCanney <bmc@asiaworkerscomp.com>",
        "Re: [RENEWAL-REQ-74] Renewal & Loss Runs Request: Yes We Do LLC",
    )
    assert category == "Active Renewals & Underwriter Quotes"
    noise_queries = " ".join(q for _label, q in NOISE_QUERIES)
    assert "RENEWAL-REQ" not in noise_queries
    assert "underwrit" not in noise_queries.lower()


def test_note_builder_reply_includes_policy_header_signature_and_inbox():
    policy = PolicyRenewal(
        policy_number="CCP35165-01",
        insured_name="Kodomo Education Services LLC",
        carrier_name="Markel Insurance",
        line_of_business="Commercial",
        expiration_date=date.today() + timedelta(days=40),
    )
    note = EZLynxNoteBuilder.format_reply_received_note(
        policy=policy,
        tracking_code="RENEWAL-REQ-62",
        sender_email="lossruns@markel.com",
        intent="QUOTE_ATTACHED",
        summary="Markel sent 5-year loss run report",
        has_attachment=True,
        attachment_name="CCP35165.pdf",
        clean_reply_text="Please find attached the Loss Run Report.",
        inbox_source="robie@streetsmart.insurance",
        gmail_message_id="abc123",
    )
    assert note.startswith("Policy: #CCP35165-01 (Commercial - Markel Insurance)")
    assert note.rstrip().endswith("Robie was here")
    assert "[RENEWAL-REQ-62]" in note
    assert "Inbox: robie@streetsmart.insurance" in note
    assert "Inbox Message ID: abc123" in note


def test_notify_csr_alerts_assigned_csr_and_carlo():
    policy = PolicyRenewal(
        policy_number="COMM-AUTO-7788",
        insured_name="Metro Freight",
        applicant_id="1",
        carrier_name="Progressive",
        expiration_date=date(2026, 10, 15),
        assigned_agent="Sandy Santana",
    )
    classification = MagicMock()
    classification.intent = "QUOTE_ATTACHED"
    classification.summary = "Quote PDF received"
    client = MagicMock()
    client.send_email.return_value = {"id": "alert_1"}

    notify_csr_of_underwriter_reply(
        policy=policy,
        classification=classification,
        sender="uw@progressive.com",
        attachments=[],
        client=client,
        note_synced=True,
    )

    kwargs = client.send_email.call_args.kwargs
    assert kwargs["to_email"] == "sandy@streetsmart.insurance"
    assert "carlo@streetsmart.insurance" in (kwargs.get("cc") or [])


def test_attachment_routing_reuses_document_uploader_tables():
    assert doc_type_for_intent(EmailClassification(
        intent="QUOTE_ATTACHED", confidence=0.9, summary="q", action_needed="review"
    )) == "renewal"
    assert doc_type_for_intent(EmailClassification(
        intent="LOSS_RUNS_ATTACHED", confidence=0.9, summary="l", action_needed="upload"
    )) == "loss runs"
    assert doc_type_for_intent(EmailClassification(
        intent="NON_RENEWAL_DECLINED", confidence=0.9, summary="n", action_needed="remarket"
    )) == "non renewal"

    folder, label = folder_and_label_for_doc_type("renewal")
    assert folder == FOLDER_ROUTING["renewal"][0]
    assert label == LABEL_ROUTING["renewal"]
    folder, label = folder_and_label_for_doc_type("loss runs")
    assert folder == FOLDER_ROUTING["loss runs"][0]
    assert label == LABEL_ROUTING["loss runs"]
    folder, label = folder_and_label_for_doc_type("non renewal")
    assert folder == FOLDER_ROUTING["non renewal"][0]
    assert label == LABEL_ROUTING["non renewal"]


def test_file_inbox_replies_uploads_via_document_uploader_routing(db, policy):
    mock_gmail = MagicMock()
    mock_ezlynx = MagicMock()
    mock_ezlynx.find_matching_discussion.return_value = {
        "discussionId": 1,
        "title": "Commercial Auto Renewal (2026-2027)",
    }
    mock_ezlynx.add_note_to_discussion.return_value = {
        "status": "success",
        "discussion_title": "Commercial Auto Renewal (2026-2027)",
        "note_id": 1,
    }
    mock_ezlynx.create_user_task.return_value = {"status": "success"}

    file_inbox_replies(
        db=db,
        gmail=mock_gmail,
        ezlynx=mock_ezlynx,
        classifier=UnderwriterIntentClassifier(),
        replies=[_uw_reply(policy.id)],
        dry_run=False,
        alert_csr=False,
    )

    upload_kwargs = mock_ezlynx.upload_document.call_args.kwargs
    assert upload_kwargs["folder_name"] == FOLDER_ROUTING["renewal"][0]
    assert upload_kwargs["label_to_apply"] == LABEL_ROUTING["renewal"]
    assert upload_kwargs["doc_type"] == "renewal"
    assert upload_kwargs["policy_number"] == "COMM-AUTO-7788"
