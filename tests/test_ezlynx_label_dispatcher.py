"""Unit tests for EZLynx Label and Note Call Dispatcher."""

from unittest.mock import MagicMock

import pytest

from src.voice.ezlynx_label_dispatcher import (
    PORTAL_DISCUSSIONS_PAGE_SIZE,
    EZLynxLabelCallDispatcher,
    discussion_is_client_outreach,
    discussion_is_lead_followup,
    discussion_is_robie_call,
    discussion_note_identity,
    extract_discussion_note_labels,
    extract_discussion_note_text,
    extract_discussion_requestor,
    infer_call_type,
    latest_activity_is_from_robie,
    parse_call_note_instructions,
    _text_matches_client_outreach_trigger,
    _text_matches_lead_followup_trigger,
)
from src.voice.outreach_pathways import (
    EZLYNX_ADMIN_OUTREACH_LABEL_PATHWAYS,
    EZLYNX_ADMIN_OUTREACH_LABELS,
    infer_outreach_pathway,
)
from src.voice.processed_robie_notes import ProcessedRobieCallStore


@pytest.fixture
def processed_store(tmp_path):
    return ProcessedRobieCallStore(tmp_path / "robie_call_processed_notes.sqlite")


def _dispatcher(mock_ezlynx, mock_voice, processed_store):
    return EZLynxLabelCallDispatcher(
        ezlynx_client=mock_ezlynx,
        voice_client=mock_voice,
        processed_store=processed_store,
    )

# Live GetPagedDiscussions card from hermes-poc-01 (applicant 26356199 / Buster Brown).
BUSTER_BROWN_LIVE_CARD = {
    "title": "Rest",
    "discussionId": 88001,
    "discussionNote": {
        "noteId": 99001,
        "note": "call carlo at 7329953409 and ask him if the renewal is ready for progressive 123456789 ",
        "noteLabels": [],
    },
}


def test_parse_call_note_with_explicit_fields():
    sample_note = """
    robie call
    Who to call: The Hartford (800-555-1234)
    Policy: PWC1239278
    What to say: Follow up on the upcoming workers comp renewal quote and request premium details.
    """
    parsed = parse_call_note_instructions(sample_note)
    assert parsed["is_robie_call"] is True
    assert parsed["phone_number"] == "+18005551234"
    assert "The Hartford" in parsed["target_name"]
    assert parsed["policy_number"] == "PWC1239278"
    assert "Follow up on the upcoming workers comp renewal" in parsed["instructions"]


def test_parse_call_note_with_alternate_formatting():
    sample_note = """
    [ROBIE CALL]
    Carrier: Coterie Insurance
    Phone: (855) 567-3421
    Policy#: COT-992144
    Instructions: Check if renewal terms are issued. If not, ask when underwriter will release them.
    Robie was here
    """
    parsed = parse_call_note_instructions(sample_note)
    assert parsed["is_robie_call"] is True
    assert parsed["phone_number"] == "+18555673421"
    assert parsed["target_name"] == "Coterie Insurance"
    assert parsed["policy_number"] == "COT-992144"
    assert "Check if renewal terms are issued" in parsed["instructions"]
    assert "Robie was here" not in parsed["instructions"]


def test_parse_non_robie_note():
    sample_note = """
    Spoke with insured regarding general liability endorsement. Added new location.
    """
    parsed = parse_call_note_instructions(sample_note)
    assert parsed["is_robie_call"] is False


def test_dispatcher_processes_robie_call(processed_store):
    mock_ezlynx = MagicMock()
    mock_voice = MagicMock()

    mock_ezlynx.get_applicant_discussions.return_value = [
        {
            "discussionId": 999111,
            "title": "Robie Call - Follow up with Travelers",
            "discussionNote": {
                "noteText": "Who to call: Travelers (800-238-6225)\nPolicy: TRV445566\nWhat to say: Ask for renewal quote."
            }
        }
    ]
    mock_ezlynx.get_applicant.return_value = {
        "status": "success",
        "applicant": {"BusinessName": "Acme Widgets LLC"}
    }
    mock_voice.from_phone = "+17322986745"
    mock_voice.build_call_prompt.return_value = "Test prompt"
    mock_voice.dispatch_call.return_value = {"call_id": "call_12345", "status": "DISPATCHED"}

    dispatcher = _dispatcher(mock_ezlynx, mock_voice, processed_store)
    results = dispatcher.process_applicant_notes_for_calls("123456", dry_run=True)

    assert len(results) == 1
    assert results[0]["call_id"] == "call_12345"
    assert results[0]["phone"] == "+18002386225"
    mock_voice.dispatch_call.assert_called_once()
    mock_ezlynx.add_note_to_discussion.assert_called_once()
    posted_kwargs = mock_ezlynx.add_note_to_discussion.call_args.kwargs
    assert posted_kwargs.get("label_to_apply") is None
    assert posted_kwargs.get("label") is None


def test_dispatcher_resolves_phone_from_carrier_directory(processed_store):
    """Tests that Robie auto-resolves phone if user only provided carrier name."""
    mock_ezlynx = MagicMock()
    mock_voice = MagicMock()

    mock_ezlynx.get_applicant_discussions.return_value = [
        {
            "discussionId": 999222,
            "title": "Robie Call - The Hartford",
            "discussionNote": {
                # Note has NO phone number, only carrier name
                "noteText": "Who to call: The Hartford\nPolicy: PWC998877\nWhat to say: Check if audit completed."
            }
        }
    ]
    mock_ezlynx.get_applicant.return_value = {
        "status": "success",
        "applicant": {"BusinessName": "Main Street Cafe"}
    }
    mock_voice.dispatch_call.return_value = {"call_id": "call_hartford_01", "status": "DISPATCHED"}

    dispatcher = _dispatcher(mock_ezlynx, mock_voice, processed_store)
    results = dispatcher.process_applicant_notes_for_calls("123456", dry_run=True)

    assert len(results) == 1
    # Auto-resolved Hartford phone +18005551234
    assert results[0]["phone"] == "+18005551234"
    assert results[0]["status"] == "DISPATCHED"
    mock_voice.dispatch_call.assert_called_once()


def test_dispatcher_posts_clarification_note_when_phone_unknown(processed_store):
    """Tests that Robie posts a clarification note via direct API (no Playwright) when phone cannot be resolved."""
    mock_ezlynx = MagicMock()
    mock_voice = MagicMock()

    mock_ezlynx.get_applicant_discussions.return_value = [
        {
            "discussionId": 999333,
            "title": "robie call",
            "discussionNote": {
                # Completely unknown carrier, no phone, no policy
                "noteText": "Who to call: Obscure Local Mutual\nWhat to say: Inquire about status."
            }
        }
    ]
    mock_ezlynx.get_applicant.return_value = {
        "status": "success",
        "applicant": {"BusinessName": "Unknown Corp"}
    }
    mock_ezlynx.get_applicant_policies.return_value = []

    dispatcher = _dispatcher(mock_ezlynx, mock_voice, processed_store)
    results = dispatcher.process_applicant_notes_for_calls("123456", dry_run=True)

    assert len(results) == 1
    assert results[0]["status"] == "CLARIFICATION_NEEDED"
    assert results[0]["reason"] == "MISSING_PHONE_NUMBER"
    # Verify no call was placed
    mock_voice.dispatch_call.assert_not_called()
    # Verify a clarification note was posted to EZLynx directly
    mock_ezlynx.add_note_to_discussion.assert_called_once()
    posted_text = mock_ezlynx.add_note_to_discussion.call_args[1]["note_text"]
    assert "⚠️ [ROBIE CALL - PHONE NUMBER NEEDED]" in posted_text


def test_parse_call_type_client_and_carrier_cues():
    client_note = """
    robie call
    Call type: client
    Who to call: the insured
    What to say: Review the quote.
    """
    parsed = parse_call_note_instructions(client_note)
    assert parsed["call_type"] == "client_followup"
    assert infer_call_type(parsed, insured_name="Garcia Landscaping LLC") == "client_followup"

    carrier_note = """
    robie call
    Call type: carrier
    Who to call: The Hartford
    What to say: Ask for terms.
    """
    parsed_carrier = parse_call_note_instructions(carrier_note)
    assert parsed_carrier["call_type"] == "carrier"
    assert infer_call_type(parsed_carrier) == "carrier"


def test_infer_call_type_from_who_to_call_insured_only():
    parsed = parse_call_note_instructions(
        "robie call\nWho to call: the insured\nWhat to say: Follow up on the quote."
    )
    assert infer_call_type(parsed, insured_name="Acme LLC") == "client_followup"
    named = parse_call_note_instructions(
        "robie call\nWho to call: Acme LLC\nWhat to say: Follow up on the quote."
    )
    assert infer_call_type(named, insured_name="Acme LLC") == "client_followup"
    carrier = parse_call_note_instructions(
        "robie call\nWho to call: Travelers\nWhat to say: Ask for terms."
    )
    assert infer_call_type(carrier, insured_name="Acme LLC") == "carrier"


@pytest.mark.parametrize(
    "phrase",
    [
        "Robie lead follow-up",
        "Robie Lead Follow-up",
        "robie lead follow up",
        "Robie lead followup",
        "ROBIE LEAD FOLLOW-UP",
        "[Robie lead follow-up]",
        "robie_lead_followup",
    ],
)
def test_lead_followup_trigger_variants(phrase):
    """Close hyphen/space/case variants of the org label must be recognized."""
    assert _text_matches_lead_followup_trigger(phrase) is True
    parsed = parse_call_note_instructions(
        f"{phrase}\nWho to call: Travelers\nWhat to say: Review the quote."
    )
    assert parsed["is_robie_call"] is True
    assert parsed["call_type"] == "client_followup"
    assert infer_call_type(parsed, insured_name="Acme LLC") == "client_followup"


def test_lead_followup_does_not_match_unrelated_robie_text():
    assert _text_matches_lead_followup_trigger("Robie Call") is False
    assert _text_matches_lead_followup_trigger("Please do a lead follow-up") is False
    assert _text_matches_lead_followup_trigger("robie call\nCall type: carrier") is False


def test_discussion_is_lead_followup_from_note_labels():
    """Org label Robie lead follow-up triggers even when title/body omit the phrase."""
    card = {
        "title": "Rest",
        "discussionNote": {
            "note": "Please review the quote with the insured.",
            "noteLabels": [
                {"labelName": "Robie lead follow-up", "organizationLabelId": 21, "applicantNoteId": 3}
            ],
        },
    }
    assert discussion_is_lead_followup(card) is True
    assert discussion_is_robie_call(card) is True


@pytest.mark.parametrize(
    "label_name",
    [
        "Robie lead follow-up",
        "Robie Lead Follow-up",
        "robie lead follow up",
        "Robie lead followup",
    ],
)
def test_discussion_is_lead_followup_label_name_variants(label_name):
    card = {
        "title": "Activity",
        "discussionNote": {
            "note": "ask the client about the quote",
            "noteLabels": [{"labelName": label_name}],
        },
    }
    assert discussion_is_lead_followup(card) is True
    assert discussion_is_robie_call(card) is True


def test_discussion_is_lead_followup_from_title_and_note():
    titled = {
        "title": "Robie lead follow-up - Garcia Landscaping",
        "discussionNote": {"note": "Review the quote.", "noteLabels": []},
    }
    assert discussion_is_lead_followup(titled) is True

    noted = {
        "title": "Rest",
        "discussionNote": {
            "note": "Robie lead followup — please call the insured about the quote.",
            "noteLabels": [],
        },
    }
    assert discussion_is_lead_followup(noted) is True
    assert discussion_is_robie_call(noted) is True


def test_infer_call_type_lead_followup_label_forces_client_without_call_type_cue():
    """Label-only lead follow-up is client_followup; no Call type: client required."""
    parsed = parse_call_note_instructions(
        "Who to call: Travelers\nWhat to say: Review the quote."
    )
    assert parsed["call_type"] is None
    assert infer_call_type(parsed, insured_name="Acme LLC") == "carrier"
    assert infer_call_type(parsed, insured_name="Acme LLC", lead_followup=True) == "client_followup"


def test_infer_call_type_both_labels_prefers_lead_followup():
    """When Robie Call and Robie lead follow-up both appear, client_followup wins."""
    parsed = parse_call_note_instructions(
        "robie call\nCall type: carrier\nWho to call: The Hartford\nWhat to say: Ask for terms."
    )
    assert parsed["call_type"] == "carrier"
    assert infer_call_type(parsed) == "carrier"
    assert infer_call_type(parsed, lead_followup=True) == "client_followup"

    both_in_text = parse_call_note_instructions(
        "Robie Call\nRobie lead follow-up\nCall type: carrier\nWho to call: The Hartford\n"
        "What to say: Ask for terms."
    )
    assert both_in_text["is_robie_call"] is True
    assert both_in_text["call_type"] == "client_followup"
    assert infer_call_type(both_in_text) == "client_followup"

    both_labels = {
        "title": "Rest",
        "discussionNote": {
            "note": "Who to call: The Hartford\nWhat to say: Ask for terms.",
            "noteLabels": [
                {"labelName": "Robie Call"},
                {"labelName": "Robie lead follow-up"},
            ],
        },
    }
    assert discussion_is_robie_call(both_labels) is True
    assert discussion_is_lead_followup(both_labels) is True
    parsed_labels = parse_call_note_instructions(
        f"{both_labels['title']}\n{extract_discussion_note_text(both_labels)}"
    )
    assert infer_call_type(
        parsed_labels,
        insured_name="Acme LLC",
        lead_followup=discussion_is_lead_followup(both_labels),
    ) == "client_followup"


def test_robie_call_alone_stays_carrier_by_default():
    """Existing Robie Call behavior: carrier unless Call type: client / who-to-call insured."""
    parsed = parse_call_note_instructions(
        "robie call\nWho to call: The Hartford\nWhat to say: Ask for terms."
    )
    assert parsed["is_robie_call"] is True
    assert parsed["call_type"] is None
    assert infer_call_type(parsed, insured_name="Acme LLC") == "carrier"
    assert infer_call_type(parsed, insured_name="Acme LLC", lead_followup=False) == "carrier"

    card = {
        "title": "Rest",
        "discussionNote": {
            "note": "Who to call: The Hartford\nWhat to say: Ask for terms.",
            "noteLabels": [{"labelName": "Robie Call"}],
        },
    }
    assert discussion_is_robie_call(card) is True
    assert discussion_is_lead_followup(card) is False


def test_dispatcher_client_followup_transfers_to_mike_not_producer(processed_store):
    """Mike applies Robie Call; Producer Jake is greeting-only; transfer is Mike's RC DID."""
    mock_ezlynx = MagicMock()
    mock_voice = MagicMock()
    mock_voice.from_phone = "+17322986745"
    mock_voice.build_call_prompt.return_value = "client prompt"
    mock_voice.dispatch_call.return_value = {"call_id": "call_client_01", "status": "DISPATCHED"}
    mock_ezlynx.get_applicant_discussions.return_value = [
        {
            "discussionId": 777,
            "title": "robie call",
            "discussionNote": {
                "noteId": 555001,
                "createdByName": "Mike Sosa",
                "createdByEmail": "mike@streetsmart.insurance",
                "noteText": (
                    "Call type: client\nWho to call: the insured\n"
                    "What to say: Review the quote Jake put together."
                ),
            },
        }
    ]
    mock_ezlynx.get_applicant.return_value = {
        "status": "success",
        "applicant": {
            "FirstName": "Maria",
            "LastName": "Garcia",
            "BusinessName": "Garcia Landscaping LLC",
            "Producer": "Brittni Example",
            "AssignedTo": "Carlo1",
            "CellPhone": "732-555-0100",
        },
    }
    mock_ezlynx.get_applicant_policies.return_value = [
        {
            "CommissionProducers": [{"Producer": {"ProducerName": "Brittni Example"}}],
        }
    ]
    mock_ezlynx.get_sales_center_opportunities.return_value = [
        {"producerName": "Jake Ferrara", "status": "Open"}
    ]

    dispatcher = _dispatcher(mock_ezlynx, mock_voice, processed_store)
    results = dispatcher.process_applicant_notes_for_calls("123456", dry_run=True)

    assert len(results) == 1
    assert results[0]["call_type"] == "client_followup"
    assert results[0]["phone"] == "+17325550100"
    assert results[0]["producer_name"] == "Jake Ferrara"
    assert results[0]["requestor_name"] == "Mike Sosa"
    assert results[0]["requestor_phone"] == "+17326540947"
    dossier = mock_voice.dispatch_call.call_args.kwargs["dossier"]
    assert dossier.client_first_name == "Maria"
    assert dossier.call_type == "client_followup"
    assert dossier.producer_name == "Jake Ferrara"
    assert dossier.requestor_phone == "+17326540947"
    assert dossier.assigned_csr_email == "carlo@streetsmart.insurance"
    ack = mock_ezlynx.add_note_to_discussion.call_args[1]["note_text"]
    assert "Warm transfer: enabled" in ack
    assert "Mike Sosa" in ack
    assert "+17326540947" in ack
    first = mock_voice.build_call_prompt.call_args.kwargs["dossier"]
    assert first.producer_name == "Jake Ferrara"


def test_dispatcher_missing_requestor_phone_does_not_fall_back_to_producer(processed_store):
    mock_ezlynx = MagicMock()
    mock_voice = MagicMock()
    mock_voice.from_phone = "+17322986745"
    mock_voice.build_call_prompt.return_value = "client prompt"
    mock_voice.dispatch_call.return_value = {"call_id": "call_client_02", "status": "DISPATCHED"}
    mock_ezlynx.get_applicant_discussions.return_value = [
        {
            "discussionId": 778,
            "title": "robie call",
            "discussionNote": {
                "noteId": 555002,
                "createdByName": "Pat Nobody",
                "noteText": (
                    "Call type: client\nWho to call: the insured\n"
                    "What to say: Review the quote."
                ),
            },
        }
    ]
    mock_ezlynx.get_applicant.return_value = {
        "status": "success",
        "applicant": {
            "FirstName": "Maria",
            "BusinessName": "Garcia Landscaping LLC",
            "Producer": "Brittni Example",
            "CellPhone": "732-555-0100",
        },
    }
    mock_ezlynx.get_applicant_policies.return_value = []
    mock_ezlynx.get_sales_center_opportunities.return_value = [
        {"producerName": "Jake Ferrara", "status": "Open"}
    ]

    dispatcher = _dispatcher(mock_ezlynx, mock_voice, processed_store)
    results = dispatcher.process_applicant_notes_for_calls("123456", dry_run=True)

    assert len(results) == 1
    assert results[0]["producer_name"] == "Jake Ferrara"
    assert results[0]["requestor_phone"] is None
    dossier = mock_voice.dispatch_call.call_args.kwargs["dossier"]
    assert dossier.producer_name == "Jake Ferrara"
    assert dossier.requestor_phone is None
    assert dossier.transfer_mode is None
    ack = mock_ezlynx.add_note_to_discussion.call_args[1]["note_text"]
    assert "Warm transfer: not available" in ack
    assert "will not fall back" in ack


def test_dispatcher_buster_greeting_is_sales_center_producer_not_commission(processed_store):
    """Buster Brown 26356199: Sales Center Carlo; commission Brittni ignored; transfer = Mike."""
    mock_ezlynx = MagicMock()
    mock_voice = MagicMock()
    mock_voice.from_phone = "+17322986745"
    mock_voice.build_call_prompt.return_value = "client prompt"
    mock_voice.dispatch_call.return_value = {"call_id": "call_buster_01", "status": "DISPATCHED"}
    mock_ezlynx.get_applicant_discussions.return_value = [
        {
            "discussionId": 88099,
            "title": "robie call",
            "discussionNote": {
                "noteId": 555099,
                "createdByName": "Mike Sosa",
                "createdByEmail": "mike@streetsmart.insurance",
                "note": (
                    "Call type: client\nWho to call: the insured\n"
                    "What to say: Review the quote."
                ),
            },
        }
    ]
    mock_ezlynx.get_applicant.return_value = {
        "status": "success",
        "applicant": {
            "FirstName": "Buster",
            "LastName": "Brown",
            "BusinessName": "Buster Brown",
            "AssignedTo": "Carlo1",
            "CsrUserModel": {"FullName": "Carlo Ferrara"},
            "CellPhone": "732-555-0199",
        },
    }
    mock_ezlynx.get_applicant_policies.return_value = [
        {
            "policyNumber": "HOP622388401",
            "carrierName": "Progressive",
            "CommissionProducers": [
                {"Producer": {"ProducerName": "Brittni Example"}}
            ],
        }
    ]
    mock_ezlynx.get_sales_center_opportunities.return_value = [
        {"producerName": "Carlo Ferrara", "status": "Open"}
    ]

    dispatcher = _dispatcher(mock_ezlynx, mock_voice, processed_store)
    results = dispatcher.process_applicant_notes_for_calls("26356199", dry_run=True)

    assert len(results) == 1
    assert results[0]["call_type"] == "client_followup"
    assert results[0]["producer_name"] == "Carlo Ferrara"
    assert results[0]["requestor_name"] == "Mike Sosa"
    assert results[0]["requestor_phone"] == "+17326540947"
    dossier = mock_voice.dispatch_call.call_args.kwargs["dossier"]
    assert dossier.producer_name == "Carlo Ferrara"
    assert dossier.client_first_name == "Buster"
    assert dossier.requestor_phone == "+17326540947"
    assert dossier.assigned_csr_email == "carlo@streetsmart.insurance"
    mock_ezlynx.get_sales_center_opportunities.assert_called_once_with("26356199")
    mock_ezlynx.get_applicant_sidebar.assert_not_called()


def test_dispatcher_lead_followup_label_forces_client_without_call_type(processed_store):
    """Org label Robie lead follow-up dispatches the client path; no Call type: client needed."""
    mock_ezlynx = MagicMock()
    mock_voice = MagicMock()
    mock_voice.from_phone = "+17322986745"
    mock_voice.build_call_prompt.return_value = "client prompt"
    mock_voice.dispatch_call.return_value = {"call_id": "call_lead_01", "status": "DISPATCHED"}
    mock_ezlynx.get_applicant_discussions.return_value = [
        {
            "discussionId": 88100,
            "title": "Rest",
            "discussionNote": {
                "noteId": 555200,
                "createdByName": "Mike Sosa",
                "createdByEmail": "mike@streetsmart.insurance",
                "note": "Please review the quote with the insured.",
                "noteLabels": [
                    {"labelName": "Robie lead follow-up", "organizationLabelId": 21, "applicantNoteId": 555200}
                ],
            },
        }
    ]
    mock_ezlynx.get_applicant.return_value = {
        "status": "success",
        "applicant": {
            "FirstName": "Maria",
            "LastName": "Garcia",
            "BusinessName": "Garcia Landscaping LLC",
            "CellPhone": "732-555-0100",
        },
    }
    mock_ezlynx.get_applicant_policies.return_value = []
    mock_ezlynx.get_sales_center_opportunities.return_value = [
        {"producerName": "Carlo Ferrara", "status": "Open"}
    ]

    dispatcher = _dispatcher(mock_ezlynx, mock_voice, processed_store)
    results = dispatcher.process_applicant_notes_for_calls("123456", dry_run=True)

    assert len(results) == 1
    assert results[0]["call_type"] == "client_followup"
    assert results[0]["phone"] == "+17325550100"
    assert results[0]["producer_name"] == "Carlo Ferrara"
    assert results[0]["requestor_name"] == "Mike Sosa"
    assert results[0]["requestor_phone"] == "+17326540947"
    dossier = mock_voice.dispatch_call.call_args.kwargs["dossier"]
    assert dossier.call_type == "client_followup"
    assert dossier.producer_name == "Carlo Ferrara"
    assert dossier.client_first_name == "Maria"
    assert dossier.requestor_phone == "+17326540947"
    ack = mock_ezlynx.add_note_to_discussion.call_args[1]["note_text"]
    assert "Call type: client_followup" in ack


def test_dispatcher_both_labels_prefers_lead_followup_client_path(processed_store):
    """Robie Call + Robie lead follow-up on the same note uses client_followup."""
    mock_ezlynx = MagicMock()
    mock_voice = MagicMock()
    mock_voice.from_phone = "+17322986745"
    mock_voice.dispatch_call.return_value = {"call_id": "call_both_01", "status": "DISPATCHED"}
    mock_ezlynx.get_applicant_discussions.return_value = [
        {
            "discussionId": 88101,
            "title": "Robie Call - Follow up with Travelers",
            "discussionNote": {
                "noteId": 555201,
                "createdByName": "Mike Sosa",
                "createdByEmail": "mike@streetsmart.insurance",
                "note": "Who to call: Travelers\nWhat to say: Ask for terms.",
                "noteLabels": [
                    {"labelName": "Robie Call"},
                    {"labelName": "Robie Lead Follow-up"},
                ],
            },
        }
    ]
    mock_ezlynx.get_applicant.return_value = {
        "status": "success",
        "applicant": {
            "FirstName": "Maria",
            "BusinessName": "Garcia Landscaping LLC",
            "CellPhone": "732-555-0100",
        },
    }
    mock_ezlynx.get_applicant_policies.return_value = []
    mock_ezlynx.get_sales_center_opportunities.return_value = [
        {"producerName": "Carlo Ferrara", "status": "Open"}
    ]

    dispatcher = _dispatcher(mock_ezlynx, mock_voice, processed_store)
    results = dispatcher.process_applicant_notes_for_calls("123456", dry_run=True)

    assert len(results) == 1
    assert results[0]["call_type"] == "client_followup"
    assert results[0]["phone"] == "+17325550100"
    dossier = mock_voice.dispatch_call.call_args.kwargs["dossier"]
    assert dossier.call_type == "client_followup"


def test_dispatcher_skips_when_latest_note_is_from_robie(processed_store):
    """Tests that Robie prevents loops by not re-triggering on its own notes."""
    mock_ezlynx = MagicMock()
    mock_voice = MagicMock()

    mock_ezlynx.get_applicant_discussions.return_value = [
        {
            "discussionId": 999444,
            "title": "robie call",
            "discussionNote": {
                "createdByName": "Robie AI",
                "noteText": "🤖 [ROBIE AUTONOMOUS CALL DISPATCHED]\nRobie has placed an outbound call..."
            }
        }
    ]

    dispatcher = _dispatcher(mock_ezlynx, mock_voice, processed_store)
    results = dispatcher.process_applicant_notes_for_calls("123456", dry_run=True)

    assert len(results) == 0
    mock_voice.dispatch_call.assert_not_called()
    mock_ezlynx.add_note_to_discussion.assert_not_called()


def test_extract_note_prefers_live_note_field_over_note_text():
    """Portal GetPagedDiscussions uses discussionNote.note; noteText is fallback only."""
    live = {
        "title": "Rest",
        "discussionNote": {
            "note": "call carlo at 7329953409",
            "noteText": "stale noteText should lose",
            "noteLabels": [],
        },
    }
    assert extract_discussion_note_text(live) == "call carlo at 7329953409"

    fallback = {
        "title": "Rest",
        "discussionNote": {"noteText": "Who to call: Travelers", "noteLabels": []},
    }
    assert extract_discussion_note_text(fallback) == "Who to call: Travelers"

    empty_note_uses_fallback = {
        "discussionNote": {"note": "", "noteText": "legacy noteText body"},
    }
    assert extract_discussion_note_text(empty_note_uses_fallback) == "legacy noteText body"


def test_extract_note_labels_from_discussion_note():
    labeled = {
        "discussionNote": {
            "note": "please call",
            "noteLabels": [
                {"labelName": "Robie Call", "organizationLabelId": 11, "applicantNoteId": 22},
                {"labelName": "CanopyConnect", "organizationLabelId": 12, "applicantNoteId": 22},
            ],
        }
    }
    assert extract_discussion_note_labels(labeled) == ["Robie Call", "CanopyConnect"]
    assert extract_discussion_note_labels(BUSTER_BROWN_LIVE_CARD) == []
    assert extract_discussion_note_labels({"discussionNote": {}}) == []
    assert extract_discussion_note_labels({"title": "Rest"}) == []


def test_discussion_is_robie_call_from_note_labels():
    """Org label Robie Call triggers even when title/body omit the phrase."""
    card = {
        "title": "Rest",
        "discussionNote": {
            "note": "call carlo at 7329953409 and ask him if the renewal is ready for progressive 123456789 ",
            "noteLabels": [{"labelName": "Robie Call", "organizationLabelId": 1, "applicantNoteId": 2}],
        },
    }
    assert discussion_is_robie_call(card) is True


def test_discussion_is_robie_call_from_note_text_field():
    """Writing 'Robie Call' in the live note body still triggers."""
    card = {
        "title": "Rest",
        "discussionNote": {
            "note": "Robie Call — call carlo at 7329953409 and ask about Progressive 123456789",
            "noteLabels": [],
        },
    }
    assert discussion_is_robie_call(card) is True


def test_discussion_is_robie_call_from_note_text_fallback_field():
    card = {
        "title": "Activity",
        "discussionNote": {
            "noteText": "Please [ROBIE CALL] Hartford about the renewal.",
            "noteLabels": [],
        },
    }
    assert discussion_is_robie_call(card) is True


def test_empty_note_labels_without_phrase_does_not_trigger():
    """Buster Brown live card: instruction-style note, empty labels, no Robie phrase."""
    assert discussion_is_robie_call(BUSTER_BROWN_LIVE_CARD) is False
    parsed = parse_call_note_instructions(
        f"{BUSTER_BROWN_LIVE_CARD['title']}\n{extract_discussion_note_text(BUSTER_BROWN_LIVE_CARD)}"
    )
    assert parsed["is_robie_call"] is False


def test_unrelated_org_labels_do_not_trigger():
    card = {
        "title": "Rest",
        "discussionNote": {
            "note": "uploaded loss runs",
            "noteLabels": [
                {"labelName": "CanopyConnect"},
                {"labelName": "Audit Result"},
                {"labelName": "Referral Request"},
                {"labelName": "Referral Spanish"},
            ],
        },
    }
    assert discussion_is_robie_call(card) is False


def test_dispatcher_triggers_on_note_labels_with_live_note_field(processed_store):
    """Label-only trigger using the live portal field names (note + noteLabels)."""
    mock_ezlynx = MagicMock()
    mock_voice = MagicMock()

    mock_ezlynx.get_applicant_discussions.return_value = [
        {
            "discussionId": 88002,
            "title": "Rest",
            "discussionNote": {
                "note": "call carlo at 7329953409 and ask him if the renewal is ready for progressive 123456789",
                "noteLabels": [{"labelName": "Robie Call", "organizationLabelId": 7, "applicantNoteId": 8}],
            },
        }
    ]
    mock_ezlynx.get_applicant.return_value = {
        "status": "success",
        "applicant": {"BusinessName": "Buster Brown"},
    }
    mock_voice.from_phone = "+17322986745"
    mock_voice.dispatch_call.return_value = {"call_id": "call_label_01", "status": "DISPATCHED"}

    dispatcher = _dispatcher(mock_ezlynx, mock_voice, processed_store)
    results = dispatcher.process_applicant_notes_for_calls("26356199", dry_run=True)

    assert len(results) == 1
    assert results[0]["call_id"] == "call_label_01"
    assert results[0]["phone"] == "+17329953409"
    mock_voice.dispatch_call.assert_called_once()
    mock_ezlynx.get_applicant_discussions.assert_called_once_with(
        "26356199", page_size=PORTAL_DISCUSSIONS_PAGE_SIZE
    )


def test_dispatcher_triggers_on_note_body_without_labels(processed_store):
    mock_ezlynx = MagicMock()
    mock_voice = MagicMock()

    mock_ezlynx.get_applicant_discussions.return_value = [
        {
            "discussionId": 88003,
            "title": "Rest",
            "discussionNote": {
                "note": "robie call\nWho to call: Carlo (732-995-3409)\nWhat to say: Ask if Progressive renewal is ready.",
                "noteLabels": [],
            },
        }
    ]
    mock_ezlynx.get_applicant.return_value = {
        "status": "success",
        "applicant": {"BusinessName": "Buster Brown"},
    }
    mock_voice.dispatch_call.return_value = {"call_id": "call_note_01", "status": "DISPATCHED"}

    dispatcher = _dispatcher(mock_ezlynx, mock_voice, processed_store)
    results = dispatcher.process_applicant_notes_for_calls("26356199", dry_run=True)

    assert len(results) == 1
    assert results[0]["phone"] == "+17329953409"
    mock_voice.dispatch_call.assert_called_once()


def test_dispatcher_skips_live_card_with_empty_note_labels(processed_store):
    """Without the Robie Call label or phrase, the live Buster note must not dispatch."""
    mock_ezlynx = MagicMock()
    mock_voice = MagicMock()
    mock_ezlynx.get_applicant_discussions.return_value = [BUSTER_BROWN_LIVE_CARD]
    mock_ezlynx.get_applicant.return_value = {
        "status": "success",
        "applicant": {"BusinessName": "Buster Brown"},
    }

    dispatcher = _dispatcher(mock_ezlynx, mock_voice, processed_store)
    results = dispatcher.process_applicant_notes_for_calls("26356199", dry_run=True)

    assert results == []
    mock_voice.dispatch_call.assert_not_called()
    mock_ezlynx.add_note_to_discussion.assert_not_called()


def test_latest_activity_is_from_robie_author_and_markers():
    ack = {
        "title": "Robie Call - Follow up",
        "discussionNote": {
            "noteId": 1,
            "createdByName": "Robie AI",
            "note": "🤖 [ROBIE AUTONOMOUS CALL DISPATCHED]\nRobie has placed an outbound call...",
        },
    }
    assert latest_activity_is_from_robie(ack) is True

    clarification = {
        "title": "Rest",
        "discussionNote": {
            "note": "⚠️ [ROBIE CALL - PHONE NUMBER NEEDED]\nPlease reply with a phone number.\n\nRobie was here",
        },
    }
    assert latest_activity_is_from_robie(clarification) is True

    transcript = {
        "discussionNote": {
            "note": "Autonomous Carrier Phone Outreach Completed:\n- Audio Recording: https://x\n\nRobie was here",
        }
    }
    assert latest_activity_is_from_robie(transcript) is True

    csr_trigger = {
        "title": "Robie Call - Follow up with Travelers",
        "discussionNote": {
            "createdByName": "Carlo Ferrara",
            "note": "[ROBIE CALL]\nWho to call: Travelers (800-238-6225)\nWhat to say: Ask for renewal quote.",
        },
    }
    assert latest_activity_is_from_robie(csr_trigger) is False


def test_discussion_note_identity_uses_discussion_and_note_id():
    card = {"discussionId": 88002, "discussionNote": {"noteId": 99002, "note": "hi"}}
    assert discussion_note_identity(card) == "88002:99002"
    assert discussion_note_identity({"discussionNote": {"noteId": 5}}) == "5"
    assert discussion_note_identity({"discussionId": 1, "discussionNote": {"note": "no id"}}) is None


def test_same_note_id_scanned_twice_dispatches_once(processed_store):
    """Dry-run still records processed noteId so cron/reruns cannot loop."""
    mock_ezlynx = MagicMock()
    mock_voice = MagicMock()
    card = {
        "discussionId": 77001,
        "title": "Rest",
        "discussionNote": {
            "noteId": 55001,
            "note": "robie call\nWho to call: Carlo (732-995-3409)\nWhat to say: Ask if Progressive is ready.",
            "noteLabels": [],
        },
    }
    mock_ezlynx.get_applicant_discussions.return_value = [card]
    mock_ezlynx.get_applicant.return_value = {
        "status": "success",
        "applicant": {"BusinessName": "Buster Brown"},
    }
    mock_voice.dispatch_call.return_value = {"call_id": "call_once", "status": "DISPATCHED"}

    dispatcher = _dispatcher(mock_ezlynx, mock_voice, processed_store)
    first = dispatcher.process_applicant_notes_for_calls("26356199", dry_run=True)
    second = dispatcher.process_applicant_notes_for_calls("26356199", dry_run=True)

    assert len(first) == 1
    assert first[0]["note_id"] == "55001"
    assert first[0]["note_identity"] == "77001:55001"
    assert second == []
    mock_voice.dispatch_call.assert_called_once()
    assert mock_voice.dispatch_call.call_args.kwargs["dry_run"] is True
    assert processed_store.has("77001:55001") is True


def test_latest_robie_ack_is_skipped_even_with_trigger_title(processed_store):
    mock_ezlynx = MagicMock()
    mock_voice = MagicMock()
    mock_ezlynx.get_applicant_discussions.return_value = [
        {
            "discussionId": 77002,
            "title": "robie call",
            "discussionNote": {
                "noteId": 55002,
                "createdByName": "Robie",
                "note": "🤖 [ROBIE AUTONOMOUS CALL DISPATCHED]\nCall ID: call_abc\nRobie was here",
                "noteLabels": [{"labelName": "Robie Call"}],
            },
        }
    ]
    mock_ezlynx.get_applicant.return_value = {
        "status": "success",
        "applicant": {"BusinessName": "Acme"},
    }

    dispatcher = _dispatcher(mock_ezlynx, mock_voice, processed_store)
    results = dispatcher.process_applicant_notes_for_calls("123456", dry_run=True)

    assert results == []
    mock_voice.dispatch_call.assert_not_called()
    mock_ezlynx.add_note_to_discussion.assert_not_called()


def test_new_csr_note_id_after_robie_ack_dispatches_once(processed_store):
    """A newer CSR noteId with Robie Call label may fire once after a prior Robie ack."""
    mock_ezlynx = MagicMock()
    mock_voice = MagicMock()
    mock_voice.dispatch_call.return_value = {"call_id": "call_new", "status": "DISPATCHED"}
    mock_ezlynx.get_applicant.return_value = {
        "status": "success",
        "applicant": {"BusinessName": "Buster Brown"},
    }

    robie_ack = {
        "discussionId": 77003,
        "title": "Rest",
        "discussionNote": {
            "noteId": 55010,
            "createdByName": "Robie AI",
            "note": "🤖 [ROBIE AUTONOMOUS CALL DISPATCHED]\nRobie was here",
            "noteLabels": [],
        },
    }
    csr_followup = {
        "discussionId": 77003,
        "title": "Rest",
        "discussionNote": {
            "noteId": 55011,
            "createdByName": "Carlo Ferrara",
            "note": "call carlo at 7329953409 and ask again about Progressive",
            "noteLabels": [{"labelName": "Robie Call", "organizationLabelId": 7, "applicantNoteId": 55011}],
        },
    }

    dispatcher = _dispatcher(mock_ezlynx, mock_voice, processed_store)
    mock_ezlynx.get_applicant_discussions.return_value = [robie_ack]
    assert dispatcher.process_applicant_notes_for_calls("26356199", dry_run=True) == []
    mock_voice.dispatch_call.assert_not_called()

    mock_ezlynx.get_applicant_discussions.return_value = [csr_followup]
    first = dispatcher.process_applicant_notes_for_calls("26356199", dry_run=True)
    second = dispatcher.process_applicant_notes_for_calls("26356199", dry_run=True)

    assert len(first) == 1
    assert first[0]["note_id"] == "55011"
    assert first[0]["call_id"] == "call_new"
    assert second == []
    mock_voice.dispatch_call.assert_called_once()
    assert processed_store.has("77003:55011") is True
    assert processed_store.has("77003:55010") is False


def test_note_text_trigger_without_label_respects_processed_note_id(processed_store):
    mock_ezlynx = MagicMock()
    mock_voice = MagicMock()
    card = {
        "discussionId": 77004,
        "title": "Activity",
        "discussionNote": {
            "noteId": 55020,
            "note": "Robie Call — Who to call: Hartford (800-555-1234)\nWhat to say: Check quote status.",
            "noteLabels": [],
        },
    }
    mock_ezlynx.get_applicant_discussions.return_value = [card]
    mock_ezlynx.get_applicant.return_value = {
        "status": "success",
        "applicant": {"BusinessName": "Main Street Cafe"},
    }
    mock_voice.dispatch_call.return_value = {"call_id": "call_text", "status": "DISPATCHED"}

    dispatcher = _dispatcher(mock_ezlynx, mock_voice, processed_store)
    first = dispatcher.process_applicant_notes_for_calls("123456", dry_run=True)
    second = dispatcher.process_applicant_notes_for_calls("123456", dry_run=True)

    assert len(first) == 1
    assert first[0]["note_identity"] == "77004:55020"
    assert second == []
    mock_voice.dispatch_call.assert_called_once()
    posted = mock_ezlynx.add_note_to_discussion.call_args.kwargs
    assert posted.get("label_to_apply") is None
    assert "ROBIE AUTONOMOUS CALL DISPATCHED" in posted["note_text"]


def test_extract_discussion_requestor_from_created_by_name():
    card = {
        "discussionNote": {
            "createdByName": "Mike Sosa",
            "note": "robie call",
        }
    }
    assert extract_discussion_requestor(card) == {"name": "Mike Sosa", "email": None}


def test_extract_discussion_requestor_from_created_by_object_and_email():
    card = {
        "discussionNote": {
            "createdBy": {
                "name": "Mike Sosa",
                "email": "mike@streetsmart.insurance",
            },
            "note": "robie call",
        }
    }
    assert extract_discussion_requestor(card) == {
        "name": "Mike Sosa",
        "email": "mike@streetsmart.insurance",
    }


def test_extract_discussion_requestor_from_user_name_fields():
    card = {
        "discussionNote": {
            "userName": "Mike Sosa",
            "userEmail": "mike@streetsmart.insurance",
        }
    }
    assert extract_discussion_requestor(card)["name"] == "Mike Sosa"
    assert extract_discussion_requestor(card)["email"] == "mike@streetsmart.insurance"


def test_extract_discussion_requestor_falls_back_to_last_modified():
    card = {"lastModifiedByName": "Mike Sosa", "discussionNote": {"note": "robie call"}}
    assert extract_discussion_requestor(card)["name"] == "Mike Sosa"


def test_extract_discussion_requestor_skips_robie():
    card = {"discussionNote": {"createdByName": "Robie AI", "note": "ack"}}
    assert extract_discussion_requestor(card) == {"name": None, "email": None}


@pytest.mark.parametrize(
    "phrase",
    [
        "Robie client outreach",
        "robie client outreach",
        "robie_client_outreach",
        "[robie client outreach]",
        "ROBIE CLIENT OUTREACH",
        "robie cancellation",
        "Robie Cancellation",
        "[robie cancellation]",
        "Robie audit",
        "robie_audit",
        "[robie audit]",
        "Robie returned mail",
        "robie-returned-mail",
        "Robie e-sign",
        "Robie esign",
        "robie_esign",
        "Robie additional info",
        "Robie recommendations",
        "Robie unresponsive",
        "Robie renewal reach-out",
        "Robie renewal reachout",
    ],
)
def test_client_outreach_trigger_variants(phrase):
    assert _text_matches_client_outreach_trigger(phrase) is True
    parsed = parse_call_note_instructions(
        f"{phrase}\nWhat to say: Please call the client about this account."
    )
    assert parsed["is_robie_call"] is True
    assert parsed["call_type"] == "client_outreach"
    assert infer_call_type(parsed, insured_name="Buster Brown") == "client_outreach"


def test_client_outreach_does_not_match_unrelated_robie_text():
    assert _text_matches_client_outreach_trigger("Robie Call") is False
    assert _text_matches_client_outreach_trigger("Robie lead follow-up") is False
    assert _text_matches_client_outreach_trigger("Please do client outreach") is False
    assert _text_matches_client_outreach_trigger("cancellation notice sent") is False
    assert _text_matches_client_outreach_trigger("Please finish the audit") is False
    assert _text_matches_client_outreach_trigger("Birthday") is False
    assert _text_matches_client_outreach_trigger("Winback") is False
    assert _text_matches_client_outreach_trigger("Sales Center New") is False
    assert _text_matches_client_outreach_trigger("new customer welcome") is False
    assert _text_matches_client_outreach_trigger("Robie audited the file") is False


def test_discussion_is_client_outreach_from_note_labels():
    card = {
        "title": "Rest",
        "discussionNote": {
            "note": "What to say: Need signed documents.",
            "noteLabels": [{"labelName": "Robie client outreach", "organizationLabelId": 31}],
        },
    }
    assert discussion_is_client_outreach(card) is True
    assert discussion_is_robie_call(card) is True
    assert discussion_is_lead_followup(card) is False


# Generic CSR copy with no Manual WF keywords — pathway must come from the label.
_GENERIC_OUTREACH_NOTE = "What to say: Please call the client about this account."


@pytest.mark.parametrize("label_name,pathway", EZLYNX_ADMIN_OUTREACH_LABEL_PATHWAYS)
def test_admin_outreach_label_dispatches_client_outreach_and_pathway(label_name, pathway):
    """Each Admin org label name dispatches client_outreach and infers its pathway."""
    card = {
        "title": "Rest",
        "discussionNote": {
            "note": _GENERIC_OUTREACH_NOTE,
            "noteLabels": [{"labelName": label_name}],
        },
    }
    assert discussion_is_client_outreach(card) is True
    assert discussion_is_robie_call(card) is True
    assert discussion_is_lead_followup(card) is False
    assert infer_outreach_pathway(_GENERIC_OUTREACH_NOTE, labels=[label_name]) == pathway
    parsed = parse_call_note_instructions(_GENERIC_OUTREACH_NOTE)
    assert infer_call_type(parsed, client_outreach=True) == "client_outreach"


@pytest.mark.parametrize(
    "label_name,pathway",
    [
        ("audit", "audit"),
        ("Robie_audit", "audit"),
        ("[Robie audit]", "audit"),
        ("returned mail", "returned_mail"),
        ("robie-returned-mail", "returned_mail"),
        ("e-sign", "esign"),
        ("esign", "esign"),
        ("[robie esign]", "esign"),
        ("additional info", "additional_info"),
        ("recommendations", "recommendations"),
        ("unresponsive", "unresponsive"),
        ("renewal reach-out", "renewal_reachout"),
        ("renewal reachout", "renewal_reachout"),
        ("cancellation", "cancellation"),
        ("[robie cancellation]", "cancellation"),
        ("client outreach", "generic"),
    ],
)
def test_outreach_label_close_variants_optional_prefix_and_separators(label_name, pathway):
    """Spaces/hyphens/underscores, optional robie prefix, and brackets match."""
    card = {
        "title": "Rest",
        "discussionNote": {
            "note": _GENERIC_OUTREACH_NOTE,
            "noteLabels": [{"labelName": label_name}],
        },
    }
    assert discussion_is_client_outreach(card) is True
    assert infer_outreach_pathway(_GENERIC_OUTREACH_NOTE, labels=[label_name]) == pathway


def test_standalone_robie_audit_label_dispatches_without_phrase_in_note():
    """Regression: Robie audit on noteLabels must dispatch even with no body keyword."""
    card = {
        "title": "Rest",
        "discussionNote": {
            "note": _GENERIC_OUTREACH_NOTE,
            "noteLabels": [{"labelName": "Robie audit"}],
        },
    }
    assert discussion_is_client_outreach(card) is True
    assert infer_outreach_pathway(_GENERIC_OUTREACH_NOTE, labels=["Robie audit"]) == "audit"


def test_bare_pathway_words_in_note_do_not_dispatch_without_label():
    card = {
        "title": "Rest",
        "discussionNote": {
            "note": "Please finish the audit and send returned mail notes.",
            "noteLabels": [],
        },
    }
    assert discussion_is_client_outreach(card) is False
    assert discussion_is_robie_call(card) is False


def test_not_ported_labels_do_not_dispatch_client_outreach():
    for label_name in (
        "Birthday",
        "Winback",
        "Sales Center New",
        "New Customer",
        "Robie Call",
        "Robie lead follow-up",
    ):
        card = {
            "title": "Rest",
            "discussionNote": {
                "note": _GENERIC_OUTREACH_NOTE,
                "noteLabels": [{"labelName": label_name}],
            },
        }
        assert discussion_is_client_outreach(card) is False, label_name


def test_client_outreach_label_defers_pathway_to_note_body():
    assert (
        infer_outreach_pathway("Please finish the audit", labels=["Robie client outreach"])
        == "audit"
    )
    assert infer_outreach_pathway(_GENERIC_OUTREACH_NOTE, labels=["Robie client outreach"]) == "generic"


def test_lead_followup_and_outreach_pathway_label_keeps_outreach_winner():
    """Existing winner: any client_outreach trigger beats lead follow-up.

    Do not invert this. Robie lead follow-up stays client_followup + requestor
    transfer when no outreach pathway label is present.
    """
    parsed = parse_call_note_instructions(
        "Robie lead follow-up\nRobie audit\nWhat to say: Review the quote."
    )
    assert parsed["call_type"] == "client_outreach"
    assert infer_call_type(parsed, lead_followup=True, client_outreach=True) == "client_outreach"

    card = {
        "title": "Rest",
        "discussionNote": {
            "note": "What to say: Review the quote.",
            "noteLabels": [
                {"labelName": "Robie lead follow-up"},
                {"labelName": "Robie audit"},
            ],
        },
    }
    assert discussion_is_lead_followup(card) is True
    assert discussion_is_client_outreach(card) is True
    assert (
        infer_call_type(
            parse_call_note_instructions("What to say: Review the quote."),
            lead_followup=True,
            client_outreach=True,
        )
        == "client_outreach"
    )


def test_infer_call_type_client_outreach_label_forces_outreach():
    parsed = parse_call_note_instructions(
        "Who to call: Travelers\nWhat to say: Policy is pending cancellation."
    )
    assert parsed["call_type"] is None
    assert infer_call_type(parsed, insured_name="Acme LLC") == "carrier"
    assert infer_call_type(parsed, insured_name="Acme LLC", client_outreach=True) == "client_outreach"
    assert infer_call_type(
        parsed, insured_name="Acme LLC", lead_followup=True, client_outreach=True
    ) == "client_outreach"


def test_robie_call_and_lead_followup_unchanged_when_outreach_absent():
    parsed = parse_call_note_instructions(
        "robie call\nWho to call: The Hartford\nWhat to say: Ask for terms."
    )
    assert infer_call_type(parsed, insured_name="Acme LLC") == "carrier"
    lead = parse_call_note_instructions(
        "Robie lead follow-up\nWhat to say: Review the quote."
    )
    assert infer_call_type(lead) == "client_followup"


def _outreach_card(
    note_id=555300,
    phone_note="What to say: Pending cancellation — please call us.",
    label="Robie client outreach",
):
    labels = [{"labelName": label}] if label else []
    return {
        "discussionId": 88200,
        "title": "Rest",
        "discussionNote": {
            "noteId": note_id,
            "createdByName": "Mike Sosa",
            "createdByEmail": "mike@streetsmart.insurance",
            "note": phone_note,
            "noteLabels": labels,
        },
    }


def test_dispatcher_client_outreach_label_forces_client_outreach(processed_store):
    mock_ezlynx = MagicMock()
    mock_voice = MagicMock()
    mock_voice.from_phone = "+17322986745"
    mock_voice.build_call_prompt.return_value = "outreach prompt"
    mock_voice.dispatch_call.return_value = {"call_id": "call_out_01", "status": "DISPATCHED"}
    mock_ezlynx.get_applicant_discussions.return_value = [_outreach_card()]
    mock_ezlynx.get_applicant.return_value = {
        "status": "success",
        "applicant": {
            "FirstName": "Buster",
            "LastName": "Brown",
            "BusinessName": "Buster Brown",
            "CellPhone": "7329953409",
            "CoApplicant": {"FirstName": "Jane"},
        },
    }
    mock_ezlynx.get_applicant_policies.return_value = []
    mock_ezlynx.get_sales_center_opportunities.return_value = [
        {"producerName": "Jake Ferrara", "status": "Open"}
    ]
    mock_ezlynx.get_applicant_sidebar.return_value = {
        "Applicant": {"Assignment": {"AssignedTo": "Carlo Ferrara"}}
    }

    dispatcher = _dispatcher(mock_ezlynx, mock_voice, processed_store)
    results = dispatcher.process_applicant_notes_for_calls("26356199", dry_run=True)

    assert len(results) == 1
    assert results[0]["call_type"] == "client_outreach"
    assert results[0]["phones"] == ["+17329953409"]
    assert results[0]["call_ids"] == ["call_out_01"]
    assert results[0]["producer_name"] is None
    assert results[0]["assigned_producer_name"] == "Carlo Ferrara"
    assert results[0]["assigned_producer_phone"] == "+17324622360"
    assert results[0]["outreach_pathway"] == "cancellation"
    dossier = mock_voice.dispatch_call.call_args.kwargs["dossier"]
    assert dossier.call_type == "client_outreach"
    assert dossier.outreach_pathway == "cancellation"
    assert dossier.client_first_name == "Buster"
    assert dossier.producer_name is None
    assert dossier.assigned_producer_phone == "+17324622360"
    assert dossier.assigned_csr_email == "carlo@streetsmart.insurance"
    ack = mock_ezlynx.add_note_to_discussion.call_args[1]["note_text"]
    assert "Call type: client_outreach" in ack
    assert "call_out_01" in ack
    assert "+17329953409" in ack
    assert "Carlo Ferrara" in ack
    assert "Jake Ferrara" not in ack


def test_dispatcher_client_outreach_primary_and_secondary_two_dials(processed_store):
    mock_ezlynx = MagicMock()
    mock_voice = MagicMock()
    mock_voice.from_phone = "+17322986745"
    mock_voice.dispatch_call.side_effect = [
        {"call_id": "call_primary", "status": "DISPATCHED"},
        {"call_id": "call_secondary", "status": "DISPATCHED"},
    ]
    mock_ezlynx.get_applicant_discussions.return_value = [_outreach_card()]
    mock_ezlynx.get_applicant.return_value = {
        "status": "success",
        "applicant": {
            "FirstName": "Buster",
            "BusinessName": "Buster Brown",
            "CellPhone": "7329953409",
            "CoApplicant": {"FirstName": "Jane", "CellPhone": "732-555-0100"},
        },
    }
    mock_ezlynx.get_applicant_policies.return_value = []
    mock_ezlynx.get_applicant_sidebar.return_value = {
        "Applicant": {"Assignment": {"AssignedTo": "Carlo Ferrara"}}
    }

    dispatcher = _dispatcher(mock_ezlynx, mock_voice, processed_store)
    results = dispatcher.process_applicant_notes_for_calls("26356199", dry_run=True)

    assert len(results) == 1
    assert results[0]["call_type"] == "client_outreach"
    assert results[0]["phones"] == ["+17329953409", "+17325550100"]
    assert results[0]["call_ids"] == ["call_primary", "call_secondary"]
    assert mock_voice.dispatch_call.call_count == 2
    first = mock_voice.dispatch_call.call_args_list[0].kwargs["dossier"]
    second = mock_voice.dispatch_call.call_args_list[1].kwargs["dossier"]
    assert first.carrier_phone == "+17329953409"
    assert first.client_first_name == "Buster"
    assert first.call_type == "client_outreach"
    assert first.producer_name is None
    assert second.carrier_phone == "+17325550100"
    assert second.client_first_name == "Jane"
    assert second.call_type == "client_outreach"
    ack = mock_ezlynx.add_note_to_discussion.call_args[1]["note_text"]
    assert "call_primary" in ack
    assert "call_secondary" in ack
    assert "+17329953409" in ack
    assert "+17325550100" in ack
    assert "Primary" in ack
    assert "Secondary" in ack
    assert processed_store.has("88200:555300") is True


def test_dispatcher_client_outreach_missing_secondary_phone_one_dial(processed_store):
    """Buster Brown: co-applicant has no cell — secondary dial is skipped."""
    mock_ezlynx = MagicMock()
    mock_voice = MagicMock()
    mock_voice.from_phone = "+17322986745"
    mock_voice.dispatch_call.return_value = {"call_id": "call_buster_only", "status": "DISPATCHED"}
    mock_ezlynx.get_applicant_discussions.return_value = [_outreach_card()]
    mock_ezlynx.get_applicant.return_value = {
        "status": "success",
        "applicant": {
            "FirstName": "Buster",
            "BusinessName": "Buster Brown",
            "CellPhone": "7329953409",
            "CoApplicant": {"FirstName": "Jane"},
        },
    }
    mock_ezlynx.get_applicant_policies.return_value = []

    dispatcher = _dispatcher(mock_ezlynx, mock_voice, processed_store)
    results = dispatcher.process_applicant_notes_for_calls("26356199", dry_run=True)

    assert len(results) == 1
    assert results[0]["phones"] == ["+17329953409"]
    assert results[0]["call_ids"] == ["call_buster_only"]
    mock_voice.dispatch_call.assert_called_once()
    dossier = mock_voice.dispatch_call.call_args.kwargs["dossier"]
    assert dossier.carrier_phone == "+17329953409"
    assert dossier.client_first_name == "Buster"


def test_dispatcher_client_outreach_same_number_dedupe(processed_store):
    mock_ezlynx = MagicMock()
    mock_voice = MagicMock()
    mock_voice.from_phone = "+17322986745"
    mock_voice.dispatch_call.return_value = {"call_id": "call_once", "status": "DISPATCHED"}
    mock_ezlynx.get_applicant_discussions.return_value = [_outreach_card()]
    mock_ezlynx.get_applicant.return_value = {
        "status": "success",
        "applicant": {
            "FirstName": "Buster",
            "CellPhone": "7329953409",
            "CoApplicant": {"FirstName": "Jane", "HomePhone": "732-995-3409"},
        },
    }
    mock_ezlynx.get_applicant_policies.return_value = []

    dispatcher = _dispatcher(mock_ezlynx, mock_voice, processed_store)
    results = dispatcher.process_applicant_notes_for_calls("26356199", dry_run=True)

    assert results[0]["phones"] == ["+17329953409"]
    assert results[0]["call_ids"] == ["call_once"]
    mock_voice.dispatch_call.assert_called_once()


def test_dispatcher_client_outreach_marks_note_processed_once_after_both_dials(processed_store):
    mock_ezlynx = MagicMock()
    mock_voice = MagicMock()
    mock_voice.from_phone = "+17322986745"
    mock_voice.dispatch_call.side_effect = [
        {"call_id": "call_a", "status": "DISPATCHED"},
        {"call_id": "call_b", "status": "DISPATCHED"},
    ]
    card = _outreach_card()
    mock_ezlynx.get_applicant_discussions.return_value = [card]
    mock_ezlynx.get_applicant.return_value = {
        "status": "success",
        "applicant": {
            "FirstName": "Buster",
            "CellPhone": "7329953409",
            "CoApplicant": {"FirstName": "Jane", "CellPhone": "7325550100"},
        },
    }
    mock_ezlynx.get_applicant_policies.return_value = []

    dispatcher = _dispatcher(mock_ezlynx, mock_voice, processed_store)
    first = dispatcher.process_applicant_notes_for_calls("26356199", dry_run=True)
    second = dispatcher.process_applicant_notes_for_calls("26356199", dry_run=True)

    assert len(first) == 1
    assert first[0]["call_ids"] == ["call_a", "call_b"]
    assert second == []
    assert mock_voice.dispatch_call.call_count == 2
    assert processed_store.has("88200:555300") is True
    mock_ezlynx.add_note_to_discussion.assert_called_once()


GREEN_LION_COMMERCIAL_APPLICANT = {
    "ApplicantType": "Commercial",
    "BusinessName": "Green Lion Lawn Care LLC",
    "FirstName": "",
    "LastName": "",
    "CellPhone": "7325550199",
    "CommercialDetail": {"ContactFirstName": "Luis", "ContactLastName": "Perez"},
}


def test_dispatcher_commercial_lead_followup_uses_contact_first_name_not_llc(processed_store):
    mock_ezlynx = MagicMock()
    mock_voice = MagicMock()
    mock_voice.from_phone = "+17322986745"
    mock_voice.build_call_prompt.return_value = "client prompt"
    mock_voice.dispatch_call.return_value = {"call_id": "call_gl_fu", "status": "DISPATCHED"}
    mock_ezlynx.get_applicant_discussions.return_value = [
        {
            "discussionId": 89001,
            "title": "Rest",
            "discussionNote": {
                "noteId": 99001,
                "createdByName": "Mike Sosa",
                "createdByEmail": "mike@streetsmart.insurance",
                "note": "What to say: Review the quote.",
                "noteLabels": [{"labelName": "Robie lead follow-up"}],
            },
        }
    ]
    mock_ezlynx.get_applicant.return_value = {
        "status": "success",
        "applicant": GREEN_LION_COMMERCIAL_APPLICANT,
    }
    mock_ezlynx.get_applicant_policies.return_value = [
        {"policyNumber": "GL-001", "carrierName": "Coterie"}
    ]
    mock_ezlynx.get_sales_center_opportunities.return_value = [
        {"producerName": "Carlo Ferrara", "status": "Open"}
    ]

    dispatcher = _dispatcher(mock_ezlynx, mock_voice, processed_store)
    results = dispatcher.process_applicant_notes_for_calls("21587333", dry_run=True)

    assert results[0]["call_type"] == "client_followup"
    dossier = mock_voice.dispatch_call.call_args.kwargs["dossier"]
    assert dossier.client_first_name == "Luis"
    assert dossier.insured_name == "Green Lion Lawn Care LLC"
    assert dossier.client_first_name != "Green"


def test_dispatcher_commercial_outreach_uses_contact_first_name_not_llc(processed_store):
    mock_ezlynx = MagicMock()
    mock_voice = MagicMock()
    mock_voice.from_phone = "+17322986745"
    mock_voice.build_call_prompt.return_value = "outreach prompt"
    mock_voice.dispatch_call.return_value = {"call_id": "call_gl_out", "status": "DISPATCHED"}
    mock_ezlynx.get_applicant_discussions.return_value = [_outreach_card()]
    mock_ezlynx.get_applicant.return_value = {
        "status": "success",
        "applicant": GREEN_LION_COMMERCIAL_APPLICANT,
    }
    mock_ezlynx.get_applicant_policies.return_value = []
    mock_ezlynx.get_sales_center_opportunities.return_value = [
        {"producerName": "Carlo Ferrara", "status": "Open"}
    ]

    dispatcher = _dispatcher(mock_ezlynx, mock_voice, processed_store)
    results = dispatcher.process_applicant_notes_for_calls("21587333", dry_run=True)

    assert results[0]["call_type"] == "client_outreach"
    dossier = mock_voice.dispatch_call.call_args.kwargs["dossier"]
    assert dossier.client_first_name == "Luis"
    assert dossier.client_first_name != "Green"
    assert dossier.producer_name is None


def test_dispatcher_client_outreach_transfers_to_assigned_producer_not_requestor_or_sales(
    processed_store,
):
    mock_ezlynx = MagicMock()
    mock_voice = MagicMock()
    mock_voice.from_phone = "+17322986745"
    mock_voice.dispatch_call.return_value = {"call_id": "call_xfer", "status": "DISPATCHED"}
    mock_ezlynx.get_applicant_discussions.return_value = [_outreach_card()]
    mock_ezlynx.get_applicant.return_value = {
        "status": "success",
        "applicant": {
            "FirstName": "Buster",
            "LastName": "Brown",
            "CellPhone": "7329953409",
        },
    }
    mock_ezlynx.get_applicant_policies.return_value = []
    mock_ezlynx.get_sales_center_opportunities.return_value = [
        {"producerName": "Jake Ferrara", "status": "Open"}
    ]
    mock_ezlynx.get_applicant_sidebar.return_value = {
        "Applicant": {"Assignment": {"AssignedTo": "Carlo Ferrara"}}
    }

    dispatcher = _dispatcher(mock_ezlynx, mock_voice, processed_store)
    results = dispatcher.process_applicant_notes_for_calls("26356199", dry_run=True)

    dossier = mock_voice.dispatch_call.call_args.kwargs["dossier"]
    assert dossier.assigned_producer_name == "Carlo Ferrara"
    assert dossier.assigned_producer_phone == "+17324622360"
    assert dossier.producer_name is None
    assert results[0]["assigned_producer_phone"] == "+17324622360"
    assert results[0]["assigned_producer_phone"] != "+17326540947"
    ack = mock_ezlynx.add_note_to_discussion.call_args[1]["note_text"]
    assert "Assigned Producer Carlo Ferrara" in ack
    assert "Jake Ferrara" not in ack


def test_dispatcher_client_outreach_skips_transfer_when_assigned_producer_has_no_did(
    processed_store, monkeypatch
):
    def _no_did(**kwargs):
        return {
            "name": kwargs.get("name") or "Pat Producer",
            "email": None,
            "phone": None,
            "aliases": [],
        }

    monkeypatch.setattr("src.voice.context_hydrator.lookup_producer", _no_did)
    mock_ezlynx = MagicMock()
    mock_voice = MagicMock()
    mock_voice.from_phone = "+17322986745"
    mock_voice.dispatch_call.return_value = {"call_id": "call_nodid", "status": "DISPATCHED"}
    mock_ezlynx.get_applicant_discussions.return_value = [_outreach_card()]
    mock_ezlynx.get_applicant.return_value = {
        "status": "success",
        "applicant": {"FirstName": "Buster", "CellPhone": "7329953409"},
    }
    mock_ezlynx.get_applicant_policies.return_value = []
    mock_ezlynx.get_applicant_sidebar.return_value = {
        "Applicant": {"Assignment": {"AssignedTo": "Pat Producer"}}
    }

    dispatcher = _dispatcher(mock_ezlynx, mock_voice, processed_store)
    results = dispatcher.process_applicant_notes_for_calls("26356199", dry_run=True)

    dossier = mock_voice.dispatch_call.call_args.kwargs["dossier"]
    assert dossier.assigned_producer_phone is None
    assert dossier.transfer_mode is None
    ack = mock_ezlynx.add_note_to_discussion.call_args[1]["note_text"]
    assert "Warm transfer: not available" in ack
    assert results[0]["phones"] == ["+17329953409"]


def _outreach_mocks(mock_voice, processed_store, note_text, note_id=555400, label="Robie client outreach"):
    mock_ezlynx = MagicMock()
    mock_voice.from_phone = "+17322986745"
    mock_voice.dispatch_call.return_value = {"call_id": "call_path", "status": "DISPATCHED"}
    mock_ezlynx.get_applicant_discussions.return_value = [
        _outreach_card(note_id=note_id, phone_note=note_text, label=label)
    ]
    mock_ezlynx.get_applicant.return_value = {
        "status": "success",
        "applicant": {"FirstName": "Buster", "CellPhone": "7329953409"},
    }
    mock_ezlynx.get_applicant_policies.return_value = []
    mock_ezlynx.get_applicant_sidebar.return_value = {
        "Applicant": {"Assignment": {"AssignedTo": "Carlo Ferrara"}}
    }
    return _dispatcher(mock_ezlynx, mock_voice, processed_store)


def test_dispatcher_outreach_pathway_from_manual_wf_note(processed_store):
    mock_voice = MagicMock()
    dispatcher = _outreach_mocks(
        mock_voice, processed_store, "What to say: Please finish the audit."
    )
    results = dispatcher.process_applicant_notes_for_calls("26356199", dry_run=True)
    assert results[0]["outreach_pathway"] == "audit"
    dossier = mock_voice.dispatch_call.call_args.kwargs["dossier"]
    assert dossier.outreach_pathway == "audit"


def test_dispatcher_renewal_reachout_only_when_csr_labels_it(processed_store):
    mock_voice = MagicMock()
    reach = _outreach_mocks(
        mock_voice,
        processed_store,
        "What to say: renewal reachout",
        note_id=555401,
    )
    reach_results = reach.process_applicant_notes_for_calls("26356199", dry_run=True)
    assert reach_results[0]["outreach_pathway"] == "renewal_reachout"

    mock_voice_pipeline = MagicMock()
    pipeline = _outreach_mocks(
        mock_voice_pipeline,
        processed_store,
        "What to say: Check if the renewal quote has been released",
        note_id=555402,
    )
    pipeline_results = pipeline.process_applicant_notes_for_calls("26356199", dry_run=True)
    assert pipeline_results[0]["outreach_pathway"] == "generic"
    assert pipeline_results[0]["outreach_pathway"] != "renewal_reachout"


def test_admin_outreach_label_names_match_pathway_table():
    assert [name for name, _ in EZLYNX_ADMIN_OUTREACH_LABEL_PATHWAYS] == list(
        EZLYNX_ADMIN_OUTREACH_LABELS
    )


@pytest.mark.parametrize("label_name,pathway", EZLYNX_ADMIN_OUTREACH_LABEL_PATHWAYS)
def test_dispatcher_admin_label_triggers_outreach_and_pathway(processed_store, label_name, pathway):
    """Each Admin label name alone (generic note body) dispatches + correct pathway."""
    mock_voice = MagicMock()
    dispatcher = _outreach_mocks(
        mock_voice,
        processed_store,
        _GENERIC_OUTREACH_NOTE,
        note_id=555500 + abs(hash(label_name)) % 10000,
        label=label_name,
    )
    results = dispatcher.process_applicant_notes_for_calls("26356199", dry_run=True)
    assert len(results) == 1
    assert results[0]["call_type"] == "client_outreach"
    assert results[0]["outreach_pathway"] == pathway
    dossier = mock_voice.dispatch_call.call_args.kwargs["dossier"]
    assert dossier.call_type == "client_outreach"
    assert dossier.outreach_pathway == pathway


def test_dispatcher_robie_call_stays_carrier_when_note_mentions_audit(processed_store):
    """Robie Call remains carrier; a payroll-audit sentence is not client_outreach."""
    mock_ezlynx = MagicMock()
    mock_voice = MagicMock()
    mock_voice.from_phone = "+17322986745"
    mock_voice.dispatch_call.return_value = {"call_id": "call_carrier_audit", "status": "DISPATCHED"}
    mock_ezlynx.get_applicant_discussions.return_value = [
        {
            "discussionId": 88300,
            "title": "Rest",
            "discussionNote": {
                "noteId": 555600,
                "createdByName": "Mike Sosa",
                "note": (
                    "Robie Call\nWho to call: The Hartford (800-555-1234)\n"
                    "What to say: Checking on the final payroll audit for the expiring term."
                ),
                "noteLabels": [{"labelName": "Robie Call"}],
            },
        }
    ]
    mock_ezlynx.get_applicant.return_value = {
        "status": "success",
        "applicant": {"BusinessName": "Acme LLC", "FirstName": "Pat"},
    }
    mock_ezlynx.get_applicant_policies.return_value = []

    dispatcher = _dispatcher(mock_ezlynx, mock_voice, processed_store)
    results = dispatcher.process_applicant_notes_for_calls("26356199", dry_run=True)
    assert len(results) == 1
    assert results[0]["call_type"] == "carrier"
    assert "outreach_pathway" not in results[0] or results[0].get("outreach_pathway") in (None, "")


def test_dispatcher_lead_and_audit_label_keeps_outreach_winner(processed_store):
    mock_voice = MagicMock()
    mock_ezlynx = MagicMock()
    mock_voice.from_phone = "+17322986745"
    mock_voice.dispatch_call.return_value = {"call_id": "call_both", "status": "DISPATCHED"}
    mock_ezlynx.get_applicant_discussions.return_value = [
        {
            "discussionId": 88400,
            "title": "Rest",
            "discussionNote": {
                "noteId": 555700,
                "createdByName": "Mike Sosa",
                "createdByEmail": "mike@streetsmart.insurance",
                "note": "What to say: Review the quote.",
                "noteLabels": [
                    {"labelName": "Robie lead follow-up"},
                    {"labelName": "Robie audit"},
                ],
            },
        }
    ]
    mock_ezlynx.get_applicant.return_value = {
        "status": "success",
        "applicant": {"FirstName": "Buster", "CellPhone": "7329953409"},
    }
    mock_ezlynx.get_applicant_policies.return_value = []
    mock_ezlynx.get_applicant_sidebar.return_value = {
        "Applicant": {"Assignment": {"AssignedTo": "Carlo Ferrara"}}
    }
    dispatcher = _dispatcher(mock_ezlynx, mock_voice, processed_store)
    results = dispatcher.process_applicant_notes_for_calls("26356199", dry_run=True)
    assert results[0]["call_type"] == "client_outreach"
    assert results[0]["outreach_pathway"] == "audit"
    dossier = mock_voice.dispatch_call.call_args.kwargs["dossier"]
    assert dossier.call_type == "client_outreach"
    assert dossier.outreach_pathway == "audit"
    assert dossier.producer_name is None

