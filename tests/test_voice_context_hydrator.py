"""Unit tests for Voice Context Hydrator."""

from datetime import date
from unittest.mock import MagicMock, patch

from src.voice.context_hydrator import (
    CALL_TYPE_CARRIER,
    CALL_TYPE_CLIENT_FOLLOWUP,
    CALL_TYPE_CLIENT_OUTREACH,
    TRANSFER_MODE_WARM,
    CallingDossier,
    ContextHydrator,
    client_account_context_name,
    client_spoken_greeting,
    extract_client_first_name,
    extract_co_applicant,
    extract_contact_phone,
    extract_producer_name,
    extract_sales_center_producer_name,
    extract_sidebar_assigned_producer_full_name,
    normalize_call_type,
    resolve_client_outreach_targets,
    spoken_client_first_name,
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
    assert spoken_client_first_name("Buster Brown") == "Buster"
    assert spoken_client_first_name("Green Lion Lawn Care LLC") is None
    assert client_spoken_greeting("Buster Brown") == "Hi Buster"
    assert client_spoken_greeting(None) == "Hi"
    assert client_spoken_greeting(None, voicemail=True) == "Hello"
    assert client_account_context_name("Buster Brown", "Buster") is None
    assert client_account_context_name("Green Lion Lawn Care LLC", "Buster") == (
        "Green Lion Lawn Care LLC"
    )


# Commercial shapes investigated for Green Lion Lawn Care LLC (21587333) and
# Marek PKS (84705043): Classic Applicant/v2 + GetApplicantSidebar. Prefer a
# nested contact FirstName over BusinessName / first LLC token.
GREEN_LION_INSURED = "Green Lion Lawn Care LLC"
GREEN_LION_CLASSIC_CONTACT = {
    "ApplicantType": "Commercial",
    "BusinessName": GREEN_LION_INSURED,
    "FirstName": "",
    "LastName": "",
    "CommercialDetail": {
        "ContactFirstName": "Luis",
        "ContactLastName": "Perez",
    },
}
GREEN_LION_SIDEBAR_CONTACT = {
    "Applicant": {
        "ApplicantType": "Commercial",
        "BusinessName": GREEN_LION_INSURED,
        "CommercialDetail": {
            "PrimaryContact": {"FirstName": "Luis", "LastName": "Perez"},
        },
    }
}
GREEN_LION_CONTACTS_LIST = {
    "ApplicantType": "Commercial",
    "BusinessName": GREEN_LION_INSURED,
    "Contacts": [
        {"FirstName": "Luis", "LastName": "Perez", "IsPrimary": True},
        {"FirstName": "Other", "LastName": "Contact"},
    ],
}
GREEN_LION_NO_CONTACT = {
    "ApplicantType": "Commercial",
    "BusinessName": GREEN_LION_INSURED,
    "FirstName": "",
    "LastName": "",
}
MAREK_PKS_NO_CONTACT = {
    "ApplicantType": "Commercial",
    "BusinessName": "Marek PKS",
    "FirstName": None,
}


def test_extract_client_first_name_commercial_uses_contact_not_llc_token():
    assert extract_client_first_name(GREEN_LION_CLASSIC_CONTACT, GREEN_LION_INSURED) == "Luis"
    assert extract_client_first_name(
        GREEN_LION_NO_CONTACT, GREEN_LION_INSURED, sidebar=GREEN_LION_SIDEBAR_CONTACT
    ) == "Luis"
    assert extract_client_first_name(GREEN_LION_CONTACTS_LIST, GREEN_LION_INSURED) == "Luis"
    assert extract_client_first_name(
        {"ApplicantType": "Commercial", "PrimaryContactFirstName": "Luis"},
        GREEN_LION_INSURED,
    ) == "Luis"


def test_extract_client_first_name_trusts_classic_firstname_even_if_in_business_name():
    """Live hermes-poc-01 2026-09-05: Classic FirstName is the spoken name.

    Marek PKS (84705043): FirstName=Marek, BusinessName starts with Marek.
    Green Lion (21587333): FirstName=Anthony. Buster (26356199): FirstName=Buster.
    """
    assert (
        extract_client_first_name(
            {
                "ApplicantType": "Commercial",
                "FirstName": "Marek",
                "LastName": "PKS",
                "BusinessName": "Marek PKS Transportation Inc",
            },
            "Marek PKS Transportation Inc",
        )
        == "Marek"
    )
    assert (
        extract_client_first_name(
            {
                "ApplicantType": "Commercial",
                "FirstName": "Anthony",
                "BusinessName": GREEN_LION_INSURED,
            },
            GREEN_LION_INSURED,
        )
        == "Anthony"
    )
    assert extract_client_first_name(BUSTER_CLASSIC_APPLICANT, "Buster Brown") == "Buster"
    assert extract_client_first_name({"FirstName": "Buster"}, "Buster Brown") == "Buster"


def test_extract_client_first_name_commercial_llc_without_contact_is_generic():
    """Do not greet 'Hi Green' from Green Lion Lawn Care LLC when FirstName is missing."""
    assert extract_client_first_name(GREEN_LION_NO_CONTACT, GREEN_LION_INSURED) is None
    assert extract_client_first_name({}, GREEN_LION_INSURED) is None
    assert extract_client_first_name(MAREK_PKS_NO_CONTACT, "Marek PKS Transportation Inc") is None
    assert extract_client_first_name(
        {
            "ApplicantType": "Commercial",
            "BusinessName": GREEN_LION_INSURED,
            "FirstName": "",
        },
        GREEN_LION_INSURED,
    ) is None
    assert extract_client_first_name(
        {
            "ApplicantType": "Commercial",
            "BusinessName": GREEN_LION_INSURED,
            "FirstName": "n/a",
        },
        GREEN_LION_INSURED,
    ) is None


def test_enrich_identity_commercial_sidebar_contact_first_name():
    hydrator = ContextHydrator()
    dossier = CallingDossier(
        policy_number="GL-001",
        insured_name=GREEN_LION_INSURED,
        carrier_name="Coterie",
        line_of_business="GL",
        applicant_id=21587333,
    )
    hydrator.enrich_identity(
        dossier,
        applicant=GREEN_LION_NO_CONTACT,
        sidebar=GREEN_LION_SIDEBAR_CONTACT,
        call_type="client_followup",
    )
    assert dossier.client_first_name == "Luis"
    assert dossier.call_type == CALL_TYPE_CLIENT_FOLLOWUP

    outreach = CallingDossier(
        policy_number="GL-001",
        insured_name=GREEN_LION_INSURED,
        carrier_name="Coterie",
        line_of_business="GL",
    )
    hydrator.enrich_identity(
        outreach,
        applicant=GREEN_LION_CLASSIC_CONTACT,
        call_type="client_outreach",
    )
    assert outreach.client_first_name == "Luis"
    assert outreach.call_type == CALL_TYPE_CLIENT_OUTREACH


def test_enrich_identity_from_ezlynx_fetches_sidebar_when_commercial_has_no_first_name():
    hydrator = ContextHydrator()
    dossier = CallingDossier(
        policy_number="GL-001",
        insured_name=GREEN_LION_INSURED,
        carrier_name="Coterie",
        line_of_business="GL",
        applicant_id=21587333,
    )
    client = MagicMock()
    client.get_applicant.return_value = {
        "status": "success",
        "applicant": GREEN_LION_NO_CONTACT,
    }
    client.get_applicant_policies.return_value = []
    client.get_sales_center_opportunities.return_value = [
        {"producerName": "Carlo Ferrara", "status": "Open"}
    ]
    client.get_applicant_sidebar.return_value = GREEN_LION_SIDEBAR_CONTACT

    hydrator.enrich_identity_from_ezlynx(dossier, ezlynx_client=client)

    assert dossier.client_first_name == "Luis"
    assert dossier.producer_name == "Carlo Ferrara"
    client.get_applicant_sidebar.assert_called_once_with("21587333")


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
    assert normalize_call_type("client_outreach") == CALL_TYPE_CLIENT_OUTREACH
    assert normalize_call_type("client outreach") == CALL_TYPE_CLIENT_OUTREACH
    assert normalize_call_type("outreach") == CALL_TYPE_CLIENT_OUTREACH
    assert normalize_call_type("cancellation") == CALL_TYPE_CLIENT_OUTREACH
    assert normalize_call_type("carrier") == CALL_TYPE_CARRIER
    assert normalize_call_type("existing") == CALL_TYPE_CARRIER
    assert normalize_call_type(None) == CALL_TYPE_CARRIER


def test_extract_contact_phone_prefers_cell_then_home_then_work():
    assert extract_contact_phone({"CellPhone": "7329953409", "HomePhone": "7325550100"}) == "+17329953409"
    assert extract_contact_phone({"HomePhone": "732-555-0100", "WorkPhone": "7325550199"}) == "+17325550100"
    assert extract_contact_phone({"WorkPhone": "(732) 555-0199"}) == "+17325550199"
    assert extract_contact_phone({"BusinessPhone": "7325550199"}) == "+17325550199"
    assert extract_contact_phone({"ContactInfo": {"CellPhone": "7329953409"}}) == "+17329953409"
    assert extract_contact_phone({"CellPhone": "12"}) is None
    assert extract_contact_phone({}) is None


def test_resolve_client_outreach_targets_primary_then_secondary():
    applicant = {
        "FirstName": "Buster",
        "LastName": "Brown",
        "CellPhone": "7329953409",
        "CoApplicant": {
            "FirstName": "Jane",
            "LastName": "Brown",
            "CellPhone": "732-555-0100",
        },
    }
    targets = resolve_client_outreach_targets(applicant, insured_name="Buster Brown")
    assert [t["role"] for t in targets] == ["primary", "secondary"]
    assert [t["phone"] for t in targets] == ["+17329953409", "+17325550100"]
    assert targets[0]["first_name"] == "Buster"
    assert targets[1]["first_name"] == "Jane"


def test_resolve_client_outreach_targets_skips_secondary_without_phone():
    """Buster Brown 26356199: primary 7329953409; co-applicant has no cell."""
    applicant = {
        "FirstName": "Buster",
        "CellPhone": "7329953409",
        "CoApplicant": {"FirstName": "Jane", "LastName": "Brown"},
    }
    targets = resolve_client_outreach_targets(applicant, insured_name="Buster Brown")
    assert len(targets) == 1
    assert targets[0]["role"] == "primary"
    assert targets[0]["phone"] == "+17329953409"
    assert extract_co_applicant(applicant)["FirstName"] == "Jane"


def test_resolve_client_outreach_targets_dedupes_shared_number():
    applicant = {
        "FirstName": "Buster",
        "CellPhone": "7329953409",
        "CoApplicant": {"FirstName": "Jane", "HomePhone": "732-995-3409"},
    }
    targets = resolve_client_outreach_targets(applicant)
    assert len(targets) == 1
    assert targets[0]["phone"] == "+17329953409"
    assert targets[0]["role"] == "primary"


def test_resolve_client_outreach_targets_reads_sidebar_contactinfo():
    sidebar = {
        "Applicant": {
            "FirstName": "Buster",
            "ContactInfo": {"CellPhone": "7329953409"},
            "CoApplicant": {
                "FirstName": "Pat",
                "ContactInfo": {"WorkPhone": "7325550111"},
            },
        }
    }
    targets = resolve_client_outreach_targets(sidebar=sidebar)
    assert [t["phone"] for t in targets] == ["+17329953409", "+17325550111"]
    assert targets[1]["first_name"] == "Pat"


def test_resolve_client_outreach_targets_commercial_contact_first_name():
    applicant = {
        "ApplicantType": "Commercial",
        "BusinessName": GREEN_LION_INSURED,
        "CellPhone": "7325550199",
        "CommercialDetail": {"ContactFirstName": "Luis"},
    }
    targets = resolve_client_outreach_targets(applicant, insured_name=GREEN_LION_INSURED)
    assert targets[0]["first_name"] == "Luis"
    assert targets[0]["phone"] == "+17325550199"


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
