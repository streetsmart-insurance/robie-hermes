"""Prior-term vs renewal-term policy number aliases (e.g. Safe Man)."""

from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.database.models import Base, PolicyNumberAlias, PolicyRenewal, RenewalStatus
from src.database.policy_aliases import (
    association_policy_number,
    collect_policy_numbers,
    find_policy_by_any_number,
    harvest_policy_numbers,
    numbers_equivalent,
    register_aliases_from_texts,
    register_policy_alias,
    sibling_policy_for_intake,
)
from src.email_outreach.gmail_client import GmailRenewalClient
from src.email_outreach.intent_classifier import UnderwriterIntentClassifier
from src.email_outreach.uw_reply_filer import file_inbox_replies, match_policy_for_reply
from src.ezlynx.api_client import EZLynxApiClient
from src.intake.base_source import RawRenewalItem
from src.intake.report_ingestor import ReportIngestor


PRIOR = "R2WC681352"
RENEWAL = "R2WC771037"


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture
def safe_man(db):
    pol = PolicyRenewal(
        policy_number=PRIOR,
        insured_name="Safe Man",
        applicant_id="199000001",
        carrier_name="Associated Specialty",
        line_of_business="Workers Comp",
        expiration_date=date(2026, 10, 15),
        assigned_agent="Sandy Santana",
        status=RenewalStatus.EMAIL_SENT_AWAITING_REPLY,
        discussion_title="Manual Workers Comp Renewal",
        source="Manual",
    )
    db.add(pol)
    db.commit()
    return pol


def test_register_and_find_either_number(db, safe_man):
    register_policy_alias(db, safe_man, RENEWAL, alias_kind="renewal_term")
    db.commit()

    assert find_policy_by_any_number(db, PRIOR).id == safe_man.id
    assert find_policy_by_any_number(db, RENEWAL).id == safe_man.id
    assert find_policy_by_any_number(db, "R2WC-771037").id == safe_man.id
    assert PRIOR in collect_policy_numbers(safe_man)
    assert RENEWAL in collect_policy_numbers(safe_man)
    assert numbers_equivalent("R2WC-681352", PRIOR)


def test_register_alias_skips_canonical_and_other_policy(db, safe_man):
    assert register_policy_alias(db, safe_man, PRIOR) is None
    other = PolicyRenewal(
        policy_number="OTHER-999",
        insured_name="Other LLC",
        applicant_id="2",
        carrier_name="Coterie",
        expiration_date=date(2026, 11, 1),
        source="Manual",
    )
    db.add(other)
    db.commit()
    assert register_policy_alias(db, safe_man, "OTHER-999") is None


def test_harvest_skips_renewal_req_and_dates():
    tokens = harvest_policy_numbers(
        "Re: [RENEWAL-REQ-62] offer for R2WC771037 effective 10/15/2026"
    )
    assert RENEWAL in tokens
    assert not any(t.startswith("RENEWAL-REQ") for t in tokens)
    assert "10/15/2026" not in tokens


def test_gmail_match_accepts_renewal_term_alias():
    client = GmailRenewalClient(service=None, inbox_services={})
    parsed = {
        "subject": f"Renewal Offer {RENEWAL}",
        "body": "Please find the attached quote.",
        "attachments": [],
    }
    policies = [
        {
            "id": 7,
            "policy_number": PRIOR,
            "policy_numbers": [PRIOR, RENEWAL],
            "insured_name": "Safe Man",
        }
    ]
    assert client._matches_any_policy(parsed, policies) is True
    assert parsed["matched_policy_id"] == 7


def test_match_policy_for_reply_by_renewal_number_alias(db, safe_man):
    register_policy_alias(db, safe_man, RENEWAL, alias_kind="renewal_term")
    db.commit()
    reply = {
        "subject": f"Workers Comp Renewal Offer {RENEWAL}",
        "body": f"Quote for policy {RENEWAL} is attached.",
        "attachments": [],
        "inbox_source": "robie@streetsmart.insurance",
    }
    matched = match_policy_for_reply(db, reply, [safe_man])
    assert matched is not None
    assert matched.id == safe_man.id


def test_find_matching_discussion_accepts_alias_in_title():
    client = EZLynxApiClient(
        base_url="https://app.ezlynx.com",
        client_id="t",
        client_secret="t",
        username="t",
        password="t",
        app_secret="t",
    )
    discussions = [
        {
            "discussionId": 1,
            "title": f"Loss Runs request | {PRIOR}",
            "noteCount": 9,
            "discussionNote": {"policyNumber": PRIOR},
        },
        {
            "discussionId": 2,
            "title": f"Workers Comp Renewal | {RENEWAL} Associated Specialty",
            "noteCount": 4,
            "lastModifiedByName": "Sandy Santana",
            "discussionNote": {"policyNumber": RENEWAL},
        },
    ]
    from unittest.mock import patch

    with patch.object(client, "get_applicant_discussions", return_value=discussions):
        match = client.find_matching_discussion(
            applicant_id="199000001",
            policy_number=PRIOR,
            policy_numbers=[PRIOR, RENEWAL],
            line_of_business="Workers Comp",
            carrier_name="Associated Specialty",
        )
    assert match is not None
    assert match["discussionId"] == 2
    assert RENEWAL in match["title"]


def test_uw_filing_does_not_fail_when_offer_uses_new_number(db, safe_man):
    register_policy_alias(db, safe_man, RENEWAL, alias_kind="renewal_term")
    db.commit()

    mock_ezlynx = MagicMock()
    mock_ezlynx.find_matching_discussion.return_value = {
        "discussionId": 2,
        "title": f"Workers Comp Renewal | {RENEWAL}",
        "discussionNote": {"policyNumber": RENEWAL},
    }
    mock_ezlynx.add_note_to_discussion.return_value = {
        "status": "success",
        "discussion_title": f"Workers Comp Renewal | {RENEWAL}",
        "note_id": 55,
    }
    mock_ezlynx.create_user_task.return_value = {"status": "success"}

    reply = {
        "message_id": "gmail_safe_man",
        "inbox_source": "hello@streetsmart.insurance",
        "sender": "uw@carrier.com",
        "subject": f"Renewal offer {RENEWAL} Safe Man",
        "body": f"Please find attached the renewal quote for {RENEWAL}.",
        "clean_reply_text": f"Please find attached the renewal quote for {RENEWAL}.",
        "attachments": [{"filename": f"{RENEWAL}_Offer.pdf", "path": "/tmp/offer.pdf"}],
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
    assert summary["filed"] == 1
    kwargs = mock_ezlynx.find_matching_discussion.call_args.kwargs
    assert PRIOR in kwargs["policy_numbers"]
    assert RENEWAL in kwargs["policy_numbers"]
    upload_kwargs = mock_ezlynx.upload_document.call_args.kwargs
    assert upload_kwargs["policy_number"] == RENEWAL


def test_file_single_reply_harvests_new_number_as_alias(db, safe_man):
    mock_ezlynx = MagicMock()
    mock_ezlynx.find_matching_discussion.return_value = {
        "discussionId": 2,
        "title": "Workers Comp Renewal",
    }
    mock_ezlynx.add_note_to_discussion.return_value = {
        "status": "success",
        "discussion_title": "Workers Comp Renewal",
        "note_id": 1,
    }

    reply = {
        "message_id": "gmail_harvest",
        "inbox_source": "robie@streetsmart.insurance",
        "sender": "uw@carrier.com",
        "subject": f"Re: [RENEWAL-REQ-{safe_man.id}] terms {RENEWAL}",
        "body": f"New policy number is {RENEWAL}.",
        "clean_reply_text": f"New policy number is {RENEWAL}.",
        "attachments": [],
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
    assert summary["filed"] == 1
    aliases = [a.alias_number for a in db.query(PolicyNumberAlias).all()]
    assert RENEWAL in aliases


def test_association_prefers_discussion_or_renewal_alias(db, safe_man):
    register_policy_alias(db, safe_man, RENEWAL, alias_kind="renewal_term")
    db.commit()
    assert association_policy_number(safe_man) == RENEWAL
    assert association_policy_number(
        safe_man,
        {"discussionNote": {"policyNumber": RENEWAL}, "title": f"WC Renewal {RENEWAL}"},
    ) == RENEWAL


def test_intake_aliases_term_flip_instead_of_duplicate_row(db, tmp_path, safe_man):
    today = date(2026, 9, 5)
    # expiration 40 days out so it is inside the default 25–45 window
    safe_man.expiration_date = today + timedelta(days=40)
    db.commit()

    ingestor = ReportIngestor(input_dir=tmp_path)
    flipped = RawRenewalItem(
        policy_number=RENEWAL,
        insured_name="Safe Man",
        applicant_id=safe_man.applicant_id,
        carrier_name=safe_man.carrier_name,
        expiration_date=safe_man.expiration_date,
        line_of_business=safe_man.line_of_business,
        discussion_title="Manual Workers Comp Renewal",
        source="Manual",
    )
    ingestor.fetch_renewals = lambda target_date=None: [flipped]
    new_count, in_win = ingestor.sync_to_database(db, reference_date=today)
    assert in_win == 1
    assert new_count == 0
    assert db.query(PolicyRenewal).count() == 1
    assert find_policy_by_any_number(db, RENEWAL).id == safe_man.id


def test_sibling_helper_same_applicant_exp_lob(db, safe_man):
    sib = sibling_policy_for_intake(
        db,
        applicant_id=safe_man.applicant_id,
        expiration_date=safe_man.expiration_date,
        line_of_business=safe_man.line_of_business,
        policy_number=RENEWAL,
    )
    assert sib is not None
    assert sib.id == safe_man.id
