"""Unit tests for EZLynx Label and Note Call Dispatcher."""

from unittest.mock import MagicMock

import pytest

from src.voice.ezlynx_label_dispatcher import (
    PORTAL_DISCUSSIONS_PAGE_SIZE,
    EZLynxLabelCallDispatcher,
    discussion_is_robie_call,
    discussion_note_identity,
    extract_discussion_note_labels,
    extract_discussion_note_text,
    extract_discussion_requestor,
    infer_call_type,
    latest_activity_is_from_robie,
    parse_call_note_instructions,
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

