"""Unit tests for Voice Context Hydrator."""

from datetime import date
from unittest.mock import MagicMock, patch

from src.voice.context_hydrator import (
    CALL_TYPE_CARRIER,
    CALL_TYPE_CLIENT_FOLLOWUP,
    TRANSFER_MODE_WARM,
    CallingDossier,
    ContextHydrator,
    extract_client_first_name,
    extract_producer_name,
    extract_sales_center_producer_name,
    extract_sidebar_assigned_producer_full_name,
    normalize_call_type,
)

# Live Buster Brown (applicant 26356199) shapes verified 2026-09-05.
BUSTER_SALES_CENTER_PAYLOAD = {
    "opportunities": [
        {
            "producerName": "Carlo Ferrara",
            "status": "Open",
            "createdDate": "2026-08-12T14:30:00",
        }
    ]
}
BUSTER_CLASSIC_APPLICANT = {
    "FirstName": "Buster",
    "LastName": "Brown",
    "BusinessName": "Buster Brown",
    "AssignedTo": "Carlo1",
    "CsrUserModel": {"FullName": "Carlo Ferrara"},
    "Producer": "Should Not Use This",
}
BUSTER_COMMISSION_POLICY = {
    "policyNumber": "HOP622388401",
    "CommissionProducers": [
        {"Producer": {"ProducerName": "Brittni Example"}}
    ],
    "Producer": {"ProducerName": "Brittni Example"},
    "AssignedProducer": "Brittni Example",
}
BUSTER_SIDEBAR = {
    "Applicant": {
        "Assignment": {"AssignedTo": "Carlo Ferrara"},
    }
}


def _yes_we_do_record(**overrides):
    rec = MagicMock()
    rec.policy_number = "PWC1239278"
    rec.insured_name = "Yes We Do LLC"
    rec.carrier_name = "Associated Specialty Insurance Agency MGA"
    rec.line_of_business = "Workers Comp"
    rec.expiration_date = date(2026, 10, 15)
    rec.expiring_premium = None
    rec.applicant_id = 21588091
    rec.assigned_agent = "Eimy Ramos"
    for key, value in overrides.items():
        setattr(rec, key, value)
    return rec


def _patch_session(mock_session_cls, record):
    db = MagicMock()
    query = db.query.return_value
    query.filter.return_value.first.return_value = record
    mock_session_cls.return_value = db
    return db


@patch("src.voice.context_hydrator.SessionLocal")
def test_hydrate_from_policy_number(mock_session_cls):
    _patch_session(mock_session_cls, _yes_we_do_record())
    hydrator = ContextHydrator()
    dossier = hydrator.hydrate(policy_number="PWC1239278")
    assert dossier is not None
    assert dossier.policy_number == "PWC1239278"
    assert dossier.insured_name == "Yes We Do LLC"
    assert "Associated" in dossier.carrier_name
    assert dossier.assigned_csr_email == "eimy@streetsmart.insurance"
    assert dossier.call_type == CALL_TYPE_CARRIER


@patch("src.voice.context_hydrator.SessionLocal")
def test_hydrate_from_applicant_name(mock_session_cls):
    _patch_session(mock_session_cls, _yes_we_do_record())
    hydrator = ContextHydrator()
    dossier = hydrator.hydrate(applicant_name="Yes We Do")
    assert dossier is not None
    assert dossier.policy_number == "PWC1239278"
    assert dossier.insured_name == "Yes We Do LLC"


@patch("src.voice.context_hydrator.SessionLocal")
def test_hydrate_with_phone_override_and_instructions(mock_session_cls):
    _patch_session(mock_session_cls, _yes_we_do_record())
    hydrator = ContextHydrator()
    dossier = hydrator.hydrate(
        policy_number="PWC1239278",
        phone_override="800-555-0199",
        instructions="Ask if payroll audit was accepted",
    )
    assert dossier is not None
    assert dossier.carrier_phone == "800-555-0199"
    assert dossier.custom_instructions == "Ask if payroll audit was accepted"


def test_phone_formatting_and_extraction():
    assert ContextHydrator._format_phone("8778782468") == "877-878-2468"
    assert ContextHydrator._format_phone("18778782468") == "877-878-2468"
    text = "Please call our agency desk at 800-252-2268 for help."
    extracted = ContextHydrator._extract_phone_from_text(text)
    assert extracted == "800-252-2268"


def test_csr_email_resolution():
    assert ContextHydrator._resolve_csr_email("Jake Ferrara") == "jake@streetsmart.insurance"
    assert ContextHydrator._resolve_csr_email("Eimy Ramos") == "eimy@streetsmart.insurance"
    assert ContextHydrator._resolve_csr_email("Sandy") == "sandy@streetsmart.insurance"
    assert ContextHydrator._resolve_csr_email("Nicole") == "nicole@streetsmart.insurance"
    assert ContextHydrator._resolve_csr_email("Carlo") == "carlo@streetsmart.insurance"


def test_extract_client_first_name_prefers_ezlynx_first_name():
    applicant = {
        "FirstName": "Maria",
        "LastName": "Garcia",
        "PreferredName": "Mia",
        "BusinessName": "Garcia Landscaping LLC",
    }
    assert extract_client_first_name(applicant, "Garcia Landscaping LLC") == "Maria"


def test_extract_client_first_name_falls_back_to_preferred_then_personal_display():
    assert extract_client_first_name({"PreferredName": "Tony"}, "Garcia LLC") == "Tony"
    assert extract_client_first_name({}, "Anthony Rivera") == "Anthony"
    assert extract_client_first_name({}, "Yes We Do LLC") is None


def test_extract_producer_name_uses_sales_center_producer_name():
    """Greeting = Sales Center producerName. Assigned / commission Producer ignored."""
    assert (
        extract_sales_center_producer_name(BUSTER_SALES_CENTER_PAYLOAD) == "Carlo Ferrara"
    )
    assert (
        extract_producer_name(
            BUSTER_COMMISSION_POLICY,
            BUSTER_CLASSIC_APPLICANT,
            sales_opportunities=BUSTER_SALES_CENTER_PAYLOAD,
        )
        == "Carlo Ferrara"
    )
    assert extract_producer_name(BUSTER_COMMISSION_POLICY, BUSTER_CLASSIC_APPLICANT) is None
    assert extract_producer_name({"AssignedTo": "Carlo1"}) is None
    assert extract_producer_name({"Producer": "Jake Ferrara"}) is None
    assert extract_producer_name({"AssignedProducer": "Carlo Ferrara"}) is None


def test_extract_sales_center_producer_prefers_open_then_recent():
    payload = {
        "opportunities": [
            {
                "producerName": "Closed Producer",
                "status": "Closed",
                "createdDate": "2026-09-01T00:00:00",
            },
            {
                "producerName": "  ",
                "status": "Open",
                "createdDate": "2026-09-04T00:00:00",
            },
            {
                "producerName": "Open Older",
                "status": "Open",
                "createdDate": "2026-07-01T00:00:00",
            },
            {
                "producerName": "Open Newer",
                "status": "Active",
                "createdDate": "2026-08-20T00:00:00",
            },
        ]
    }
    assert extract_sales_center_producer_name(payload) == "Open Newer"
    first_only = {
        "opportunities": [
            {"producerName": "First Named"},
            {"producerName": "Second Named"},
        ]
    }
    assert extract_sales_center_producer_name(first_only) == "First Named"
    assert extract_sales_center_producer_name({"opportunities": [{"producerName": "  "}]}) is None


def test_sidebar_assigned_to_full_name_fallback_only():
    assert extract_sidebar_assigned_producer_full_name(BUSTER_SIDEBAR) == "Carlo Ferrara"
    assert (
        extract_producer_name(
            BUSTER_COMMISSION_POLICY,
            BUSTER_CLASSIC_APPLICANT,
            sales_opportunities={"opportunities": []},
            sidebar=BUSTER_SIDEBAR,
        )
        == "Carlo Ferrara"
    )
    # Classic username without resolution is not a greeting name.
    assert extract_sidebar_assigned_producer_full_name({"AssignedTo": "Carlo1"}) is None
    assert extract_sidebar_assigned_producer_full_name(
        {"Applicant": {"Assignment": {"AssignedTo": "Carlo1"}}}
    ) is None


def test_normalize_call_type():
    assert normalize_call_type("client") == CALL_TYPE_CLIENT_FOLLOWUP
    assert normalize_call_type("client follow-up") == CALL_TYPE_CLIENT_FOLLOWUP
    assert normalize_call_type("carrier") == CALL_TYPE_CARRIER
    assert normalize_call_type("existing") == CALL_TYPE_CARRIER
    assert normalize_call_type(None) == CALL_TYPE_CARRIER


def test_enrich_identity_transfers_to_requestor_not_producer():
    """Mike invokes Robie Call; Sales Center producer Jake is greeting-only."""
    hydrator = ContextHydrator()
    dossier = CallingDossier(
        policy_number="PWC1239278",
        insured_name="Garcia Landscaping LLC",
        carrier_name="The Hartford",
        line_of_business="GL",
    )
    hydrator.enrich_identity(
        dossier,
        applicant={"FirstName": "Maria", "Producer": "Brittni Example", "AssignedTo": "Carlo1"},
        policy=BUSTER_COMMISSION_POLICY,
        sales_opportunities={"opportunities": [{"producerName": "Jake Ferrara", "status": "Open"}]},
        call_type="client",
        requestor_name="Mike Sosa",
        requestor_email="mike@streetsmart.insurance",
    )
    assert dossier.client_first_name == "Maria"
    assert dossier.producer_name == "Jake Ferrara"
    assert dossier.requestor_name == "Mike Sosa"
    assert dossier.requestor_phone == "+17326540947"
    assert dossier.producer_phone is None
    assert dossier.call_type == CALL_TYPE_CLIENT_FOLLOWUP
    assert dossier.transfer_mode == TRANSFER_MODE_WARM


def test_enrich_identity_buster_sales_producer_ignores_commission_and_classic_assigned_to():
    """Buster Brown: greeting Carlo from Sales Center; transfer stays the label invoker."""
    hydrator = ContextHydrator()
    dossier = CallingDossier(
        policy_number="HOP622388401",
        insured_name="Buster Brown",
        carrier_name="Progressive",
        line_of_business="HO",
        applicant_id=26356199,
    )
    hydrator.enrich_identity(
        dossier,
        applicant=BUSTER_CLASSIC_APPLICANT,
        policy=BUSTER_COMMISSION_POLICY,
        sales_opportunities=BUSTER_SALES_CENTER_PAYLOAD,
        sidebar=BUSTER_SIDEBAR,
        call_type="client_followup",
        requestor_name="Mike Sosa",
        requestor_email="mike@streetsmart.insurance",
    )
    assert dossier.client_first_name == "Buster"
    assert dossier.producer_name == "Carlo Ferrara"
    assert dossier.requestor_name == "Mike Sosa"
    assert dossier.requestor_phone == "+17326540947"
    assert dossier.producer_phone is None
    assert dossier.call_type == CALL_TYPE_CLIENT_FOLLOWUP
    assert dossier.transfer_mode == TRANSFER_MODE_WARM


def test_enrich_identity_missing_requestor_phone_does_not_fall_back_to_producer():
    hydrator = ContextHydrator()
    dossier = CallingDossier(
        policy_number="X1",
        insured_name="Acme LLC",
        carrier_name="Hartford",
        line_of_business="GL",
    )
    hydrator.enrich_identity(
        dossier,
        applicant={"FirstName": "Pat"},
        sales_opportunities={"opportunities": [{"producerName": "Jake Ferrara"}]},
        requestor_name="Pat Nobody",
        requestor_email="pat.nobody@streetsmart.insurance",
    )
    assert dossier.producer_name == "Jake Ferrara"
    assert dossier.requestor_phone is None
    assert dossier.transfer_mode is None
    assert dossier.producer_phone is None


def test_enrich_identity_without_requestor_skips_transfer():
    hydrator = ContextHydrator()
    dossier = CallingDossier(
        policy_number="X1",
        insured_name="Acme LLC",
        carrier_name="Hartford",
        line_of_business="GL",
    )
    hydrator.enrich_identity(
        dossier,
        applicant={"FirstName": "Pat"},
        sales_opportunities={"opportunities": [{"producerName": "Jake Ferrara"}]},
    )
    assert dossier.producer_name == "Jake Ferrara"
    assert dossier.requestor_phone is None
    assert dossier.transfer_mode is None


def test_enrich_identity_from_ezlynx_uses_sales_center_not_commission():
    hydrator = ContextHydrator()
    dossier = CallingDossier(
        policy_number="HOP622388401",
        insured_name="Buster Brown",
        carrier_name="Progressive",
        line_of_business="HO",
        applicant_id=26356199,
    )
    client = MagicMock()
    client.get_applicant.return_value = {
        "status": "success",
        "applicant": BUSTER_CLASSIC_APPLICANT,
    }
    client.get_applicant_policies.return_value = [BUSTER_COMMISSION_POLICY]
    client.get_sales_center_opportunities.return_value = BUSTER_SALES_CENTER_PAYLOAD["opportunities"]
    client.get_applicant_sidebar.return_value = BUSTER_SIDEBAR

    hydrator.enrich_identity_from_ezlynx(dossier, ezlynx_client=client)

    assert dossier.producer_name == "Carlo Ferrara"
    assert dossier.client_first_name == "Buster"
    assert dossier.producer_phone is None
    client.get_sales_center_opportunities.assert_called_once_with("26356199")
    client.get_applicant_sidebar.assert_not_called()


def test_enrich_identity_from_ezlynx_falls_back_to_sidebar_full_name():
    hydrator = ContextHydrator()
    dossier = CallingDossier(
        policy_number="HOP622388401",
        insured_name="Buster Brown",
        carrier_name="Progressive",
        line_of_business="HO",
        applicant_id=26356199,
    )
    client = MagicMock()
    client.get_applicant.return_value = {
        "status": "success",
        "applicant": BUSTER_CLASSIC_APPLICANT,
    }
    client.get_applicant_policies.return_value = [BUSTER_COMMISSION_POLICY]
    client.get_sales_center_opportunities.return_value = []
    client.get_applicant_sidebar.return_value = BUSTER_SIDEBAR

    hydrator.enrich_identity_from_ezlynx(dossier, ezlynx_client=client)

    assert dossier.producer_name == "Carlo Ferrara"
    client.get_applicant_sidebar.assert_called_once_with("26356199")
