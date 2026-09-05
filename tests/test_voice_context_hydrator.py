"""Unit tests for Voice Context Hydrator."""

import pytest
from src.voice.context_hydrator import ContextHydrator, CallingDossier


def test_hydrate_from_policy_number():
    hydrator = ContextHydrator()
    dossier = hydrator.hydrate(policy_number="PWC1239278")
    assert dossier is not None
    assert dossier.policy_number == "PWC1239278"
    assert dossier.insured_name == "Yes We Do LLC"
    assert "Associated" in dossier.carrier_name
    assert dossier.carrier_phone is not None
    assert dossier.assigned_csr_email == "eimy@streetsmart.insurance"


def test_hydrate_from_applicant_name():
    hydrator = ContextHydrator()
    dossier = hydrator.hydrate(applicant_name="Yes We Do")
    assert dossier is not None
    assert dossier.policy_number == "PWC1239278"
    assert dossier.insured_name == "Yes We Do LLC"


def test_hydrate_with_phone_override_and_instructions():
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
