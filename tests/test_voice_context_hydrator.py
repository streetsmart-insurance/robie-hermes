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
    normalize_call_type,
)


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


def test_extract_producer_name_uses_ezlynx_producer_field_only():
    applicant = {"AssignedTo": "Robie AI", "Producer": "Jake Ferrara"}
    policy = {"AssignedProducer": "Carlo Ferrara"}
    assert extract_producer_name(policy, applicant) == "Carlo Ferrara"
    assert extract_producer_name(applicant) == "Jake Ferrara"
    assert extract_producer_name({"AssignedTo": "Someone"}) is None


def test_normalize_call_type():
    assert normalize_call_type("client") == CALL_TYPE_CLIENT_FOLLOWUP
    assert normalize_call_type("client follow-up") == CALL_TYPE_CLIENT_FOLLOWUP
    assert normalize_call_type("carrier") == CALL_TYPE_CARRIER
    assert normalize_call_type("existing") == CALL_TYPE_CARRIER
    assert normalize_call_type(None) == CALL_TYPE_CARRIER


def test_enrich_identity_sets_producer_phone_and_warm_transfer():
    hydrator = ContextHydrator()
    dossier = CallingDossier(
        policy_number="PWC1239278",
        insured_name="Garcia Landscaping LLC",
        carrier_name="The Hartford",
        line_of_business="GL",
    )
    hydrator.enrich_identity(
        dossier,
        applicant={"FirstName": "Maria", "Producer": "Jake Ferrara"},
        call_type="client",
    )
    assert dossier.client_first_name == "Maria"
    assert dossier.producer_name == "Jake Ferrara"
    assert dossier.producer_phone == "+17326688161"
    assert dossier.call_type == CALL_TYPE_CLIENT_FOLLOWUP
    assert dossier.transfer_mode == TRANSFER_MODE_WARM


def test_enrich_identity_without_directory_phone_skips_transfer():
    hydrator = ContextHydrator()
    dossier = CallingDossier(
        policy_number="X1",
        insured_name="Acme LLC",
        carrier_name="Hartford",
        line_of_business="GL",
    )
    hydrator.enrich_identity(
        dossier,
        applicant={"FirstName": "Pat", "Producer": "Eimy Ramos"},
    )
    assert dossier.producer_name == "Eimy Ramos"
    assert dossier.producer_phone is None
    assert dossier.transfer_mode is None
