"""Splice-replacement client outreach pathway copy and transfer rules."""

from src.voice.context_hydrator import CallingDossier, ContextHydrator
from src.voice.outreach_pathways import (
    MANUAL_WF_BODIES,
    PATHWAY_ADDITIONAL_INFO,
    PATHWAY_AUDIT,
    PATHWAY_CANCELLATION,
    PATHWAY_ESIGN,
    PATHWAY_GENERIC,
    PATHWAY_RECOMMENDATIONS,
    PATHWAY_RENEWAL_REACHOUT,
    PATHWAY_RETURNED_MAIL,
    PATHWAY_UNRESPONSIVE,
    assigned_producer_first_name,
    build_outreach_live_script,
    build_outreach_voicemail_script,
    infer_outreach_pathway,
)
from src.voice.voice_client import (
    AGENCY_MAIN_CALLBACK_DISPLAY,
    AGENCY_MAIN_CALLBACK_SPOKEN,
    CarrierVoiceClient,
)


BUSTER_SIDEBAR = {"Applicant": {"Assignment": {"AssignedTo": "Carlo Ferrara"}}}
JAKE_SALES = {"opportunities": [{"producerName": "Jake Ferrara", "status": "Open"}]}

# Carlo's Google Doc Manual WF bodies — must appear verbatim in spoken copy.
_DOC_BODIES = {
    PATHWAY_AUDIT: (
        "It appears that an audit for your account is currently incomplete. "
        "Please take the necessary steps to finalize this audit as soon as possible."
    ),
    PATHWAY_RECOMMENDATIONS: (
        "We are following up on some recommendations that were made for your account. "
        "Please take the necessary steps to address these recommendations as soon as possible."
    ),
    PATHWAY_RETURNED_MAIL: (
        "We have received some returned mail for your account. "
        "Please contact our office to update your information as soon as possible."
    ),
    PATHWAY_ESIGN: (
        "We are following up on an e-signature request for your account. "
        "Please complete the e-signature process as soon as possible."
    ),
    PATHWAY_ADDITIONAL_INFO: (
        "We are following up on a request for additional information for your account. "
        "Please provide the requested information as soon as possible."
    ),
    PATHWAY_UNRESPONSIVE: "We are reaching out regarding your policies.",
    PATHWAY_RENEWAL_REACHOUT: (
        "Your insurance policy will be up for renewal soon. "
        "We want to ensure you have the proper coverage and would like to discuss your options."
    ),
}

_IVR_FORBIDDEN = (
    "press 1",
    "press 2",
    "press 4",
    "press 6",
    "press one",
    "press two",
    "press four",
    "press six",
    "toll-free",
    "toll free",
    "tollfree",
    "1-800",
    "1 800",
)


def _assert_conversational_wrap(live: str, vm: str, body: str) -> None:
    assert live.startswith("Hi Buster, this is Robie from StreetSmart Insurance.")
    assert body in live
    assert body in vm
    assert "Buster Brown" not in live
    assert "Buster Brown" not in vm
    assert "connect you to Carlo now" in live
    assert AGENCY_MAIN_CALLBACK_DISPLAY in vm
    assert AGENCY_MAIN_CALLBACK_SPOKEN in vm
    assert "732-462-8343" in vm
    for token in _IVR_FORBIDDEN:
        assert token not in live.lower()
        assert token not in vm.lower()


def test_infer_outreach_pathway_from_alias_and_copy():
    assert infer_outreach_pathway(alias="robie cancellation") == PATHWAY_CANCELLATION
    assert (
        infer_outreach_pathway("Policy is pending cancellation — overdue payment")
        == PATHWAY_CANCELLATION
    )
    assert infer_outreach_pathway("Please finish the audit") == PATHWAY_AUDIT
    assert infer_outreach_pathway("We got returned mail — update address") == PATHWAY_RETURNED_MAIL
    assert infer_outreach_pathway("e-signature needed to avoid interruption") == PATHWAY_ESIGN
    assert infer_outreach_pathway("We need additional information") == PATHWAY_ADDITIONAL_INFO
    assert infer_outreach_pathway("Follow up on recommendations") == PATHWAY_RECOMMENDATIONS
    assert infer_outreach_pathway("Reaching out — client unresponsive") == PATHWAY_UNRESPONSIVE
    assert infer_outreach_pathway("Touch base about the account.") == PATHWAY_GENERIC


def test_manual_wf_bodies_match_carlo_google_doc():
    assert MANUAL_WF_BODIES == _DOC_BODIES


def test_manual_wf_bodies_verbatim_on_live_and_voicemail():
    for pathway, body in _DOC_BODIES.items():
        live = build_outreach_live_script(
            pathway=pathway,
            client_first="Buster",
            line_of_business="Homeowners",
            carrier_name="Progressive",
            producer_first="Carlo",
            action_date="09/15/2026",
        )
        vm = build_outreach_voicemail_script(
            pathway=pathway,
            client_first="Buster",
            line_of_business="Homeowners",
            carrier_name="Progressive",
            action_date="09/15/2026",
        )
        _assert_conversational_wrap(live, vm, body)


def test_renewal_reachout_only_on_explicit_csr_phrase():
    assert infer_outreach_pathway("renewal reachout") == PATHWAY_RENEWAL_REACHOUT
    assert infer_outreach_pathway("renewal reach-out") == PATHWAY_RENEWAL_REACHOUT
    assert infer_outreach_pathway("robie renewal reachout") == PATHWAY_RENEWAL_REACHOUT
    assert infer_outreach_pathway(alias="Renewal Reach-Out") == PATHWAY_RENEWAL_REACHOUT
    assert infer_outreach_pathway(labels=["Renewal Reachout"]) == PATHWAY_RENEWAL_REACHOUT
    # Pipeline / automation language must never infer this pathway.
    assert (
        infer_outreach_pathway("Check if the renewal quote has been released")
        == PATHWAY_GENERIC
    )
    assert infer_outreach_pathway("upcoming renewal") == PATHWAY_GENERIC
    assert infer_outreach_pathway("Upcoming Renewal / Expiration") == PATHWAY_GENERIC
    assert infer_outreach_pathway("Your insurance policy will be up for renewal soon") == PATHWAY_GENERIC
    assert infer_outreach_pathway("Policy Renewed") == PATHWAY_GENERIC


def test_not_ported_splice_automations_stay_generic():
    for phrase in (
        "Birthday",
        "Additional Policy",
        "Applicant Created",
        "New Customer Welcome",
        "Policy Reinstatement",
        "Policy Renewed",
        "Upcoming Renewal",
        "Upcoming Expiration",
        "Winback",
        "Sales Center New",
        "Sales Center Contacted",
        "Sales Center Quoted",
        "Sales Center Won",
        "quote we released a few days ago",
    ):
        assert infer_outreach_pathway(phrase) == PATHWAY_GENERIC, phrase


def test_generic_renewal_pipeline_copy_uses_csr_what_to_say_not_reachout_script():
    note = "Check if the renewal quote has been released"
    pathway = infer_outreach_pathway(note)
    live = build_outreach_live_script(
        pathway=pathway,
        client_first="Buster",
        line_of_business="GL",
        carrier_name="Hartford",
        producer_first="Carlo",
        action_date=None,
        csr_instructions=note,
    )
    assert pathway == PATHWAY_GENERIC
    assert note in live
    assert _DOC_BODIES[PATHWAY_RENEWAL_REACHOUT] not in live


def test_cancellation_pathway_copy_uses_first_name_only_and_agency_main():
    live = build_outreach_live_script(
        pathway=PATHWAY_CANCELLATION,
        client_first="Buster",
        line_of_business="Homeowners",
        carrier_name="Progressive",
        producer_first="Carlo",
        action_date="09/15/2026",
    )
    vm = build_outreach_voicemail_script(
        pathway=PATHWAY_CANCELLATION,
        client_first="Buster",
        line_of_business="Homeowners",
        carrier_name="Progressive",
        action_date="09/15/2026",
    )
    assert live.startswith("Hi Buster, this is Robie from StreetSmart Insurance.")
    assert "Buster Brown" not in live
    assert "overdue payment" in live
    assert "09/15/2026" in live
    assert "connect you to Carlo now" in live
    assert AGENCY_MAIN_CALLBACK_DISPLAY in vm
    assert AGENCY_MAIN_CALLBACK_SPOKEN in vm
    assert "Hi Buster" in vm
    assert "Buster Brown" not in vm
    assert "press 1" not in live.lower()
    assert "press 2" not in vm.lower()


def test_voice_client_cancellation_pathway_and_assigned_producer_transfer():
    client = CarrierVoiceClient(api_key="test-key")
    dossier = CallingDossier(
        policy_number="HOP622388401",
        insured_name="Buster Brown",
        carrier_name="Progressive",
        line_of_business="Homeowners",
        expiration_date="2026-09-15",
        client_first_name="Buster",
        producer_name="Jake Ferrara",
        requestor_name="Mike Sosa",
        requestor_phone="+17326540947",
        assigned_producer_name="Carlo Ferrara",
        assigned_producer_phone="+17324622360",
        call_type="client_outreach",
        outreach_pathway=PATHWAY_CANCELLATION,
        custom_instructions="Policy is pending cancellation — payment due by 09/15/2026.",
        transfer_mode="warm",
        carrier_phone="+17329953409",
    )
    first = client.build_client_first_sentence(dossier)
    prompt = client.build_call_prompt(dossier)
    voicemail = client._voicemail_message(dossier)
    fields = client.build_bland_transfer_fields(dossier)

    assert first.startswith("Hi Buster, this is Robie from StreetSmart Insurance.")
    assert "Buster Brown" not in first
    assert "Jake Ferrara" not in first
    assert "put together" not in first.lower()
    assert "connect you to Carlo now" in first
    assert AGENCY_MAIN_CALLBACK_DISPLAY in voicemail
    assert "732-462-8343" in voicemail
    assert fields["transfer_phone_number"] == "+17324622360"
    assert fields["transfer_list"]["assigned_producer"] == "+17324622360"
    assert "requestor" not in fields["transfer_list"]
    assert "+17326540947" not in str(fields)
    assert "Assigned Producer" in prompt
    assert "Mike Sosa" not in prompt or "NOT the transfer target" in prompt
    assert "label invoker is not the transfer target" in prompt.lower()


def test_voice_client_audit_pathway_uses_exact_manual_wf_body():
    client = CarrierVoiceClient(api_key="test-key")
    dossier = CallingDossier(
        policy_number="HOP622388401",
        insured_name="Buster Brown",
        carrier_name="Progressive",
        line_of_business="Homeowners",
        client_first_name="Buster",
        assigned_producer_name="Carlo Ferrara",
        assigned_producer_phone="+17324622360",
        call_type="client_outreach",
        outreach_pathway=PATHWAY_AUDIT,
        custom_instructions="Please finish the audit",
        transfer_mode="warm",
        carrier_phone="+17329953409",
    )
    first = client.build_client_first_sentence(dossier)
    voicemail = client._voicemail_message(dossier)
    assert _DOC_BODIES[PATHWAY_AUDIT] in first
    assert _DOC_BODIES[PATHWAY_AUDIT] in voicemail
    assert first.startswith("Hi Buster, this is Robie from StreetSmart Insurance.")
    assert "press 1" not in first.lower()
    assert "732-462-8343" in voicemail


def test_enrich_identity_outreach_transfers_to_assigned_producer_not_requestor_or_sales():
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
        applicant={"FirstName": "Buster", "LastName": "Brown"},
        sales_opportunities=JAKE_SALES,
        sidebar=BUSTER_SIDEBAR,
        call_type="client_outreach",
        requestor_name="Mike Sosa",
        requestor_email="mike@streetsmart.insurance",
    )
    assert dossier.call_type == "client_outreach"
    assert dossier.assigned_producer_name == "Carlo Ferrara"
    assert dossier.assigned_producer_phone == "+17324622360"
    assert dossier.transfer_mode == "warm"
    # Sales Center Jake is greeting-only for follow-up; outreach must not use him.
    assert dossier.producer_name is None
    assert dossier.producer_phone is None
    # Requestor Mike must not become the transfer DID.
    assert dossier.assigned_producer_phone != "+17326540947"


def test_enrich_identity_outreach_skips_transfer_when_producer_has_no_did(monkeypatch):
    hydrator = ContextHydrator()

    def _no_did(**kwargs):
        return {
            "name": kwargs.get("name") or "Eimy Ramos",
            "email": "eimy@streetsmart.insurance",
            "phone": None,
            "aliases": [],
        }

    monkeypatch.setattr("src.voice.context_hydrator.lookup_producer", _no_did)
    dossier = CallingDossier(
        policy_number="X1",
        insured_name="Acme LLC",
        carrier_name="Hartford",
        line_of_business="GL",
    )
    hydrator.enrich_identity(
        dossier,
        applicant={"FirstName": "Pat"},
        sales_opportunities=JAKE_SALES,
        sidebar={"Applicant": {"Assignment": {"AssignedTo": "Eimy Ramos"}}},
        call_type="client_outreach",
        requestor_name="Mike Sosa",
        requestor_email="mike@streetsmart.insurance",
    )
    assert dossier.assigned_producer_name == "Eimy Ramos"
    assert dossier.assigned_producer_phone is None
    assert dossier.transfer_mode is None
    fields = CarrierVoiceClient(api_key="test-key").build_bland_transfer_fields(dossier)
    assert fields == {}
    prompt = CarrierVoiceClient(api_key="test-key").build_call_prompt(dossier)
    assert "do not transfer" in prompt.lower()
    assert AGENCY_MAIN_CALLBACK_DISPLAY in prompt


def test_assigned_producer_first_name_only():
    assert assigned_producer_first_name("Carlo Ferrara") == "Carlo"
    assert assigned_producer_first_name(None) is None


def test_lead_followup_still_transfers_to_requestor():
    """Robie lead follow-up is unchanged: Sales Center greeting + invoker transfer."""
    hydrator = ContextHydrator()
    dossier = CallingDossier(
        policy_number="HOP622388401",
        insured_name="Buster Brown",
        carrier_name="Progressive",
        line_of_business="HO",
    )
    hydrator.enrich_identity(
        dossier,
        applicant={"FirstName": "Buster"},
        sales_opportunities={"opportunities": [{"producerName": "Carlo Ferrara", "status": "Open"}]},
        sidebar=BUSTER_SIDEBAR,
        call_type="client_followup",
        requestor_name="Mike Sosa",
        requestor_email="mike@streetsmart.insurance",
    )
    assert dossier.producer_name == "Carlo Ferrara"
    assert dossier.requestor_phone == "+17326540947"
    assert dossier.assigned_producer_phone is None
    client = CarrierVoiceClient(api_key="test-key")
    fields = client.build_bland_transfer_fields(dossier)
    first = client.build_client_first_sentence(dossier)
    assert fields["transfer_phone_number"] == "+17326540947"
    assert fields["transfer_list"]["requestor"] == "+17326540947"
    assert "quote Carlo put together" in first
    assert _DOC_BODIES[PATHWAY_RENEWAL_REACHOUT] not in first
    assert "press 1" not in first.lower()
