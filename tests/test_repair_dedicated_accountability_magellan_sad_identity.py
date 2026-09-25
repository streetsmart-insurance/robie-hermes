"""Idempotent repair tests for Magellan SAD client phone/name on the dedicated app."""

from pathlib import Path

from scripts.repair_dedicated_accountability_magellan_sad_identity import (
    HELPERS_MARKER,
    NEW_EXTRACT_PUSH,
    NEW_SAD_LOOP,
    OLD_EXTRACT_PUSH,
    OLD_SAD_LOOP,
    repair,
    repair_magellan_extractor,
    repair_production_main,
)


def _legacy_production_main() -> str:
    return (
        "from typing import Any, Dict, List\n"
        "import re\n"
        "from .production_config import MAGELLAN_ACCOUNT_OVERRIDES\n\n"
        "def _phone(value: Any) -> str:\n"
        "    digits = re.sub(r'\\D', '', str(value or ''))\n"
        "    return digits[-10:] if len(digits) >= 10 else digits\n\n"
        "def build_report(magellan, sales_by_phone, roster, active_sales_statuses):\n"
        + OLD_SAD_LOOP
        + "    return sad\n"
    )


def _legacy_extractor() -> str:
    return (
        "def fetch():\n"
        "    extract_calls_js = \"\"\"() => {\n"
        "                const res = [];\n"
        + OLD_EXTRACT_PUSH
        + "\n"
        "                return res;\n"
        "            }\"\"\"\n"
        "    return extract_calls_js\n"
    )


def test_production_main_repair_selects_client_phone_and_is_idempotent(tmp_path: Path):
    target = tmp_path / "production_main.py"
    target.write_text(_legacy_production_main(), encoding="utf-8")

    assert repair_production_main(target) == "repaired"
    repaired = target.read_text(encoding="utf-8")
    assert OLD_SAD_LOOP not in repaired
    assert repaired.count(NEW_SAD_LOOP) == 1
    assert HELPERS_MARKER in repaired
    assert "_magellan_client_phone" in repaired
    assert "_magellan_client_name" in repaired
    assert 'MAGELLAN_AGENCY_DIDS = {"7324628343"}' in repaired
    assert "return \"Unknown\"" in repaired
    assert '"Phone": client_phone' in repaired
    compile(repaired, str(target), "exec")

    assert repair_production_main(target) == "already_repaired"
    backup = target.with_suffix(".py.pre-magellan-sad-identity")
    assert backup.read_text(encoding="utf-8") == _legacy_production_main()
    assert backup.stat().st_mode & 0o777 == 0o600


def test_extractor_repair_adds_caller_name_and_tel_hrefs(tmp_path: Path):
    target = tmp_path / "magellan_playwright.py"
    target.write_text(_legacy_extractor(), encoding="utf-8")

    assert repair_magellan_extractor(target) == "repaired"
    repaired = target.read_text(encoding="utf-8")
    assert OLD_EXTRACT_PUSH not in repaired
    assert repaired.count(NEW_EXTRACT_PUSH) == 1
    assert "caller_name" in repaired
    assert "a[href^=\"tel:\"]" in repaired
    assert repair_magellan_extractor(target) == "already_repaired"


def test_combined_repair_updates_both_targets(tmp_path: Path):
    main = tmp_path / "production_main.py"
    extractor = tmp_path / "magellan_playwright.py"
    main.write_text(_legacy_production_main(), encoding="utf-8")
    extractor.write_text(_legacy_extractor(), encoding="utf-8")

    result = repair(production_main=main, magellan_extractor=extractor)
    assert result == {"production_main": "repaired", "magellan_extractor": "repaired"}
    assert repair(production_main=main, magellan_extractor=extractor) == {
        "production_main": "already_repaired",
        "magellan_extractor": "already_repaired",
    }


def test_repaired_helpers_prefer_client_phone_and_names(tmp_path: Path):
    """Execute the injected helpers against agency-DID vs client fixtures."""
    target = tmp_path / "production_main.py"
    target.write_text(_legacy_production_main(), encoding="utf-8")
    repair_production_main(target)
    source = target.read_text(encoding="utf-8")
    start = source.index("MAGELLAN_AGENCY_DIDS = ")
    end = source.index("\ndef _phone(")
    helpers = source[start:end]
    namespace: dict = {
        "Any": object,
        "Dict": dict,
        "re": __import__("re"),
        "MAGELLAN_ACCOUNT_OVERRIDES": {"9084161464": "George Fahmy"},
    }
    exec(compile(helpers, str(target), "exec"), namespace)

    client_phone = namespace["_magellan_client_phone"]
    client_name = namespace["_magellan_client_name"]

    assert client_phone({"from_phone": "(732) 462-8343", "to_phone": "(908) 416-1464"}) == "(908) 416-1464"
    assert client_phone({"from_phone": "(908) 416-1464", "to_phone": "(732) 462-8343"}) == "(908) 416-1464"

    assert (
        client_name(
            {"from_phone": "(908) 416-1464", "caller_name": "Magellan Name"},
            {"Account Name": "EZLynx Name"},
            "9084161464",
        )
        == "Magellan Name"
    )
    assert (
        client_name(
            {"from_phone": "(732) 462-8343", "to_phone": "(908) 416-1464"},
            {"Account Name": "EZLynx Name"},
            "9084161464",
        )
        == "George Fahmy"
    )
    assert (
        client_name(
            {"from_phone": "(201) 555-0199", "to_phone": "(732) 462-8343"},
            {"Account Name": "EZLynx Name"},
            "2015550199",
        )
        == "EZLynx Name"
    )
    assert (
        client_name(
            {"from_phone": "(908) 416-1464", "caller_name": "Street Smart"},
            {"Account Name": "Street Smart Insurance"},
            "9084161464",
        )
        == "George Fahmy"
    )
    assert (
        client_name(
            {"from_phone": "(201) 555-0100", "caller_name": "StreetSmart"},
            {"Account Name": "Street Smart Insurance"},
            "2015550100",
        )
        == "Unknown"
    )
