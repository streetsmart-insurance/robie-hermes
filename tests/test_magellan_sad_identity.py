"""Unit tests for Magellan SAD client phone and name resolution."""

from robie_job_engine.magellan_sad_identity import (
    build_sad_identity,
    is_usable_client_name,
    parse_magellan_party_cell,
    resolve_sad_client_name,
    select_client_phone,
)

AGENCY = "7324628343"
CLIENT = "9084161464"


def test_select_client_phone_prefers_from_when_not_agency_did():
    phone = select_client_phone(
        from_phone=f"({CLIENT[:3]}) {CLIENT[3:6]}-{CLIENT[6:]}",
        to_phone=f"({AGENCY[:3]}) {AGENCY[3:6]}-{AGENCY[6:]}",
    )
    assert CLIENT in "".join(ch for ch in phone if ch.isdigit())
    assert select_client_phone(
        from_phone="(908) 416-1464",
        to_phone="(732) 462-8343",
    ) == "(908) 416-1464"


def test_select_client_phone_uses_to_when_from_is_agency_did():
    phone = select_client_phone(
        from_phone="(732) 462-8343",
        to_phone="(908) 416-1464",
    )
    assert phone == "(908) 416-1464"


def test_select_client_phone_empty_when_only_agency_numbers():
    assert select_client_phone(from_phone="(732) 462-8343", to_phone="7324628343") == ""


def test_parse_magellan_party_cell_splits_name_and_phone():
    name, phone = parse_magellan_party_cell("Jane Client\n(908) 416-1464")
    assert name == "Jane Client"
    assert phone == "(908) 416-1464"


def test_parse_magellan_party_cell_rejects_agency_brand_as_name():
    name, phone = parse_magellan_party_cell("Street Smart Insurance\n(732) 462-8343")
    assert name == ""
    assert phone == "(732) 462-8343"


def test_magellan_name_wins_over_ezlynx():
    identity = build_sad_identity(
        {
            "from_phone": "(908) 416-1464",
            "to_phone": "(732) 462-8343",
            "caller_name": "Magellan Caller",
        },
        ezlynx_match={"Account Name": "EZLynx Applicant"},
    )
    assert identity["client_name"] == "Magellan Caller"
    assert identity["client_phone"] == "(908) 416-1464"


def test_ezlynx_enrichment_when_magellan_name_missing():
    identity = build_sad_identity(
        {
            "from_phone": "(732) 462-8343",
            "to_phone": "(908) 416-1464",
        },
        ezlynx_match={"Account Name": "EZLynx Applicant"},
    )
    assert identity["client_phone"] == "(908) 416-1464"
    assert identity["client_name"] == "EZLynx Applicant"


def test_override_used_as_ezlynx_tier_when_magellan_name_missing():
    identity = build_sad_identity(
        {"from_phone": "(908) 416-1464", "to_phone": "(732) 462-8343"},
        phone_overrides={"9084161464": "George Fahmy"},
    )
    assert identity["client_name"] == "George Fahmy"


def test_no_agency_name_fallback_when_unmatched():
    identity = build_sad_identity(
        {
            "from_phone": "Street Smart Insurance (732) 462-8343",
            "to_phone": "(908) 416-1464",
            "caller_name": "StreetSmart",
        },
        ezlynx_match={"Account Name": "Street Smart Insurance"},
    )
    assert identity["client_phone"] == "(908) 416-1464"
    assert identity["client_name"] == "Unknown"
    assert "Street" not in identity["client_name"]


def test_resolve_sad_client_name_unknown_marker():
    assert resolve_sad_client_name(magellan_name="", ezlynx_name="") == "Unknown"
    assert resolve_sad_client_name(magellan_name="(908) 416-1464") == "Unknown"
    assert not is_usable_client_name("Street Smart Insurance")
    assert is_usable_client_name("Jane Client")
