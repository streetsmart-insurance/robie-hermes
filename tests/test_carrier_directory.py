from __future__ import annotations

from pathlib import Path
import pytest

from robie_job_engine.carrier_directory import (
    Carrier,
    CarrierDirectoryStore,
    CarrierEndpoint,
    CarrierCredential,
    CarrierLobRule,
    CarrierAuthRequirement,
)


@pytest.fixture
def carrier_store(tmp_path: Path) -> CarrierDirectoryStore:
    db = tmp_path / "jobs.db"
    return CarrierDirectoryStore(db)


def test_carrier_crud(carrier_store: CarrierDirectoryStore):
    carrier = carrier_store.add_carrier(
        name="Travelers Insurance",
        slug="travelers",
        naic_code="25658",
        am_best_rating="A++",
        website_url="https://www.travelers.com",
    )
    assert carrier.id is not None
    assert carrier.name == "Travelers Insurance"
    assert carrier.slug == "travelers"
    assert carrier.naic_code == "25658"
    assert carrier.status == "ACTIVE"

    fetched_by_id = carrier_store.get_carrier(carrier.id)
    fetched_by_slug = carrier_store.get_carrier("travelers")
    assert fetched_by_id is not None
    assert fetched_by_slug is not None
    assert fetched_by_id.id == fetched_by_slug.id == carrier.id

    carriers = carrier_store.list_carriers()
    assert len(carriers) == 1
    assert carriers[0].name == "Travelers Insurance"

    updated = carrier_store.update_carrier(carrier.id, status="MAINTENANCE", am_best_rating="A+")
    assert updated.status == "MAINTENANCE"
    assert updated.am_best_rating == "A+"
    c = carrier_store.get_carrier("travelers")
    assert c is not None
    assert c.status == "MAINTENANCE"


def test_carrier_endpoints(carrier_store: CarrierDirectoryStore):
    carrier = carrier_store.add_carrier("Progressive Commercial", slug="progressive")
    
    login_ep = carrier_store.set_endpoint(
        carrier.id,
        endpoint_type="login",
        url="https://foragentsonly.com/login",
        environment="production",
        http_method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert login_ep.url == "https://foragentsonly.com/login"
    assert login_ep.endpoint_type == "login"

    quote_ep = carrier_store.set_endpoint(
        carrier.slug,
        endpoint_type="quote",
        url="https://foragentsonly.com/api/quote",
        environment="production",
        http_method="POST",
        timeout_seconds=90,
    )
    assert quote_ep.timeout_seconds == 90

    ep = carrier_store.get_endpoint("progressive", "login", environment="production")
    assert ep is not None
    assert ep.url == "https://foragentsonly.com/login"

    eps = carrier_store.list_endpoints("progressive")
    assert len(eps) == 2


def test_carrier_credentials_and_rotation(carrier_store: CarrierDirectoryStore):
    carrier = carrier_store.add_carrier("Hartford", slug="hartford")

    cred = carrier_store.set_credential(
        carrier.id,
        username="agent_streetsmart_01",
        secret_ref="vault://credentials/hartford/agent01",
        agency_code="AG-4892",
        producer_code="PR-991",
        auth_type="credentials",
    )
    assert cred.username == "agent_streetsmart_01"
    assert cred.status == "ACTIVE"
    assert cred.agency_code == "AG-4892"

    active_cred = carrier_store.get_active_credential("hartford")
    assert active_cred is not None
    assert active_cred.username == "agent_streetsmart_01"

    rotated = carrier_store.rotate_credential(
        "hartford",
        username="agent_streetsmart_01",
        new_secret_ref="vault://credentials/hartford/agent01_v2",
    )
    assert rotated.secret_ref == "vault://credentials/hartford/agent01_v2"
    assert rotated.last_rotated_at is not None


def test_carrier_lob_rules_and_appetite_validation(carrier_store: CarrierDirectoryStore):
    carrier = carrier_store.add_carrier("Liberty Mutual", slug="liberty")

    rule = carrier_store.set_lob_rule(
        carrier.id,
        line_of_business="commercial_auto",
        state_eligibility=["CA", "TX", "AZ", "NV"],
        appetite_rules={
            "min_years_in_business": 3,
            "max_limit": 2_000_000,
            "prohibited_class_codes": ["91580", "55120"],
        },
        required_fields=["vin", "radius_of_operation", "years_in_business", "requested_limit"],
        submission_mode="portal_automation",
        quote_auto_approval=True,
    )
    assert rule.line_of_business == "commercial_auto"
    assert "CA" in rule.state_eligibility
    assert rule.quote_auto_approval is True

    valid_payload = {
        "vin": "1HGCR2F83HA000000",
        "radius_of_operation": 50,
        "years_in_business": 5,
        "requested_limit": 1_000_000,
        "class_code": "01000",
    }
    res = carrier_store.validate_submission_appetite(
        "liberty", "commercial_auto", state="CA", payload=valid_payload
    )
    assert res["eligible"] is True
    assert len(res["reasons"]) == 0

    res_state = carrier_store.validate_submission_appetite(
        "liberty", "commercial_auto", state="NY", payload=valid_payload
    )
    assert res_state["eligible"] is False
    assert any("State 'NY' not eligible" in r for r in res_state["reasons"])

    bad_payload = dict(valid_payload)
    del bad_payload["vin"]
    res_missing = carrier_store.validate_submission_appetite(
        "liberty", "commercial_auto", state="TX", payload=bad_payload
    )
    assert res_missing["eligible"] is False
    assert any("Missing required field: 'vin'" in r for r in res_missing["reasons"])

    over_limit = dict(valid_payload, requested_limit=5_000_000)
    res_limit = carrier_store.validate_submission_appetite(
        "liberty", "commercial_auto", state="TX", payload=over_limit
    )
    assert res_limit["eligible"] is False
    assert any("exceeds carrier max" in r for r in res_limit["reasons"])

    prohibited = dict(valid_payload, class_code="91580")
    res_class = carrier_store.validate_submission_appetite(
        "liberty", "commercial_auto", state="TX", payload=prohibited
    )
    assert res_class["eligible"] is False
    assert any("prohibited appetite list" in r for r in res_class["reasons"])


def test_carrier_auth_requirements(carrier_store: CarrierDirectoryStore):
    carrier = carrier_store.add_carrier("Chubb", slug="chubb")

    auth_req = carrier_store.set_auth_requirement(
        carrier.id,
        mfa_type="totp",
        mfa_secret_ref="vault://mfa/chubb/seed",
        session_timeout_minutes=15,
        password_rotation_days=60,
        captcha_type="recaptcha_v3",
        ip_allowlist=["192.168.1.10", "10.0.0.1"],
    )
    assert auth_req.mfa_type == "totp"
    assert auth_req.session_timeout_minutes == 15
    assert len(auth_req.ip_allowlist) == 2

    fetched = carrier_store.get_auth_requirement("chubb")
    assert fetched is not None
    assert fetched.captcha_type == "recaptcha_v3"
    assert fetched.mfa_type == "totp"
