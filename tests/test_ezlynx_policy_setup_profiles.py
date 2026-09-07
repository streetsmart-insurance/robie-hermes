from __future__ import annotations

from pathlib import Path

from robie_job_engine.ezlynx_policy_setup_profiles import (
    NEEDS_CLARIFICATION,
    NEEDS_SKILL,
    READY,
    REQUIRED_CHECKPOINTS,
    duplicate_key,
    expected_contract,
    load_profiles,
    preflight_policy_setup,
    redact_evidence,
    resolve_profile,
    verify_reopened_policy,
)


def _base(**extra):
    return {
        "applicant_id": "220250093",
        "carrier": "Test Carrier",
        "policy_number": "TEST-0001",
        "effective_date": "09/01/2026",
        "expiration_date": "09/01/2027",
        **extra,
    }


def test_manifest_has_exact_testing_profiles_and_write_lock():
    catalog = load_profiles()
    assert catalog["required_checkpoints"] == list(REQUIRED_CHECKPOINTS)
    assert len(catalog["profiles"]) == 12
    assert {item["state"] for item in catalog["profiles"]} == {"Testing"}
    enabled = [
        item["id"] for item in catalog["profiles"]
        if item["consequential_writes_enabled"]
    ]
    assert enabled == ["homeowners"]


def test_repository_and_deploy_skill_packages_are_exact_mirrors():
    repository = Path("skills/ezlynx-policy-setup")
    deploy = Path("deploy/hermes/skills/ezlynx-policy-setup")
    for relative in ("SKILL.md", "references/profiles.json", "references/selector-inventory.md"):
        assert (repository / relative).read_bytes() == (deploy / relative).read_bytes()


def test_duplicate_identity_is_stable_and_uses_all_business_keys():
    request = _base(lob="Homeowners")
    same = {**request, "carrier": "  TEST   CARRIER "}
    assert duplicate_key(request) == duplicate_key(same)
    assert duplicate_key(request) != duplicate_key({**request, "policy_number": "TEST-0002"})


def test_ready_preflight_authorizes_only_exact_synthetic_homeowners_test_save():
    result = preflight_policy_setup(
        _base(
            lob="Homeowners",
            property_location="SANITIZED LOCATION",
            coverages={"A": "sanitized"},
            deductibles={"all_peril": "sanitized"},
            replacement_cost_basis="Replacement Cost",
            environment="TEST",
            premium="$1.00",
            synthetic_fixture=True,
            save_authorized=True,
        )
    )
    assert result.status == READY
    assert result.duplicate_key
    assert result.consequential_writes_enabled is True


def test_homeowners_write_scope_fails_closed_outside_exact_test_contract():
    base = _base(
        lob="Homeowners",
        property_location="SANITIZED LOCATION",
        coverages={"A": "sanitized"},
        deductibles={"all_peril": "sanitized"},
        replacement_cost_basis="Replacement Cost",
        environment="TEST",
        premium="1.00",
        synthetic_fixture=True,
        save_authorized=True,
    )
    mutations = (
        {"environment": "PRODUCTION"},
        {"applicant_id": "220250094"},
        {"policy_number": "REAL-HO-0001"},
        {"premium": "1796.00"},
        {"synthetic_fixture": False},
        {"save_authorized": False},
    )
    for mutation in mutations:
        result = preflight_policy_setup({**base, **mutation})
        assert result.status == NEEDS_CLARIFICATION
        assert result.consequential_writes_enabled is False


def test_unresolved_rules_fail_closed():
    dwelling = preflight_policy_setup(
        _base(
            lob="Dwelling Fire",
            mailing_address="SANITIZED",
            property_location="SANITIZED",
            coverages={"A": "sanitized"},
            deductibles={"all_peril": "sanitized"},
        )
    )
    assert dwelling.status == NEEDS_CLARIFICATION
    assert "mailing-versus-location" in " ".join(dwelling.reasons)

    gl = preflight_policy_setup(
        _base(
            lob="GL",
            coverage_basis="Occurrence",
            limits={"occurrence": "sanitized"},
            classifications=["sanitized"],
            exposures=["subcontractor cost, if any"],
        )
    )
    assert gl.status == NEEDS_CLARIFICATION
    assert "aggregate basis" in " ".join(gl.reasons)
    assert "if any" in " ".join(gl.reasons)

    package = preflight_policy_setup(
        _base(
            lob="Commercial Package",
            package_parts=["General Liability", "Commercial Property"],
            locations=["SANITIZED"],
            coverages={"occurrence": "sanitized"},
            classifications=["sanitized"],
        )
    )
    assert package.status == NEEDS_CLARIFICATION
    assert "aggregate basis" in " ".join(package.reasons)


def test_trucking_and_unsupported_bonds_need_a_different_skill():
    trucking = preflight_policy_setup(
        _base(lob="Commercial Auto — Contractors", business_use="interstate trucking")
    )
    assert trucking.status == NEEDS_SKILL

    bid = preflight_policy_setup(_base(lob="Bonds", bond_type="Bid Bond"))
    assert bid.status == NEEDS_SKILL


def test_supported_bond_variant_can_reach_ready_without_enabling_save():
    result = preflight_policy_setup(
        _base(
            lob="Bonds",
            bond_type="Employee Dishonesty",
            bond_amount="SANITIZED",
            obligee="SANITIZED",
        )
    )
    assert result.status == READY
    assert result.consequential_writes_enabled is False


def test_non_allowlisted_applicant_never_reaches_ready():
    result = preflight_policy_setup(
        {
            **_base(
                lob="Bonds",
                bond_type="Home Improvement Bond",
                bond_amount="SANITIZED",
                obligee="SANITIZED",
            ),
            "applicant_id": "220250094",
        }
    )
    assert result.status == NEEDS_CLARIFICATION
    assert "business-write allowlist" in " ".join(result.reasons)


def test_expected_actual_requires_reopened_exact_match_and_never_completes():
    catalog = load_profiles()
    profile = resolve_profile("Crime", catalog)
    request = _base(
        lob="Crime",
        locations=["SANITIZED"],
        coverage_basis="Discovery",
        crime_coverages={"employee_dishonesty": "SANITIZED"},
        limits={"employee_dishonesty": "SANITIZED"},
    )
    expected = expected_contract(request, profile)
    passed = verify_reopened_policy(expected, dict(expected))
    failed = verify_reopened_policy(expected, {**expected, "coverage_basis": "Loss Sustained"})
    assert passed["verdict"] == "PASS"
    assert passed["independently_verified"] is True
    assert passed["authorizes_complete"] is False
    assert failed["verdict"] == "FAIL"
    assert failed["mismatches"][0]["field"] == "coverage_basis"


def test_reusable_evidence_redacts_customer_fields():
    redacted = redact_evidence(
        {
            "named_insured": "Example Person",
            "applicant_id": "SANITIZED-APPLICANT-001",
            "policy_number": "CLIENT-001",
            "property_location": "123 Example Street",
            "nested": {"vin": "1TEST", "coverage": "500000"},
        }
    )
    assert redacted["named_insured"] == "[REDACTED]"
    assert redacted["applicant_id"] == "[REDACTED]"
    assert redacted["policy_number"] == "[REDACTED]"
    assert redacted["property_location"] == "[REDACTED]"
    assert redacted["nested"]["vin"] == "[REDACTED]"
    assert redacted["nested"]["coverage"] == "500000"


def test_reusable_evidence_redacts_generic_name_license_and_common_variants():
    redacted = redact_evidence(
        {
            "name": "Synthetic Person",
            "LICENSE": "QA-LICENSE",
            "Nested": {
                "Full Name": "Synthetic Nested Person",
                "driverLicenseNumber": "QA-DRIVER-LICENSE",
                "Email-Address": "synthetic@example.invalid",
                "coverage_name": "General Liability",
                "license_status": "Active",
                "coverage": {"name_of_coverage": "Occurrence", "limit": "500000"},
            },
            "Rows": [{"NamedInsured": "Synthetic Insured", "PhoneNumber": "5550100"}],
        }
    )

    assert redacted["name"] == "[REDACTED]"
    assert redacted["LICENSE"] == "[REDACTED]"
    assert redacted["Nested"]["Full Name"] == "[REDACTED]"
    assert redacted["Nested"]["driverLicenseNumber"] == "[REDACTED]"
    assert redacted["Nested"]["Email-Address"] == "[REDACTED]"
    assert redacted["Rows"][0]["NamedInsured"] == "[REDACTED]"
    assert redacted["Rows"][0]["PhoneNumber"] == "[REDACTED]"
    assert redacted["Nested"]["coverage_name"] == "General Liability"
    assert redacted["Nested"]["license_status"] == "Active"
    assert redacted["Nested"]["coverage"] == {
        "name_of_coverage": "Occurrence",
        "limit": "500000",
    }
