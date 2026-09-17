from pathlib import Path

import pytest

from scripts.repair_dedicated_accountability_magellan import (
    NEW_BLOCK,
    NEW_EXTRACT_END,
    NEW_EXTRACT_START,
    NEW_PAGE_BLOCK,
    OLD_BLOCK,
    OLD_EXTRACT_END,
    OLD_EXTRACT_START,
    OLD_PAGE_BLOCK,
    V1_PAGE_BLOCK,
    repair,
)


def _legacy_source() -> str:
    return (
        "prefix\n"
        + OLD_BLOCK
        + OLD_EXTRACT_START
        + "                return res;\n"
        + OLD_EXTRACT_END
        + 'datetime.strptime(target_date, "%Y-%m-%d").strftime("%m/%d/%Y")\n'
        + OLD_PAGE_BLOCK
        + "suffix\n"
    )


def _v1_source() -> str:
    return (
        "prefix\n"
        + NEW_BLOCK
        + NEW_EXTRACT_START
        + "                return res;\n"
        + NEW_EXTRACT_END
        + 'datetime.strptime(target_date, "%Y-%m-%d").strftime("%m/%d/%Y")\n'
        + V1_PAGE_BLOCK
        + "suffix\n"
    )


def _repaired_source() -> str:
    return (
        "prefix\n"
        + NEW_BLOCK
        + NEW_EXTRACT_START
        + "                return res;\n"
        + NEW_EXTRACT_END
        + 'datetime.strptime(target_date, "%Y-%m-%d").strftime("%m/%d/%Y")\n'
        + NEW_PAGE_BLOCK
        + "suffix\n"
    )


def test_repairs_exact_legacy_blocks(tmp_path: Path):
    target = tmp_path / "magellan_playwright.py"
    target.write_text(_legacy_source(), encoding="utf-8")

    assert repair(target) == "repaired"
    content = target.read_text(encoding="utf-8")
    for value in (OLD_BLOCK, OLD_EXTRACT_START, OLD_EXTRACT_END, OLD_PAGE_BLOCK):
        assert value not in content
    for value in (NEW_BLOCK, NEW_EXTRACT_START, NEW_EXTRACT_END, NEW_PAGE_BLOCK):
        assert content.count(value) == 1
    assert target.with_suffix(".py.pre-session-repair").is_file()


def test_upgrades_installed_v1_pagination_to_evidence_contract(tmp_path: Path):
    target = tmp_path / "magellan_playwright.py"
    target.write_text(_v1_source(), encoding="utf-8")

    assert repair(target) == "repaired"
    content = target.read_text(encoding="utf-8")
    assert V1_PAGE_BLOCK not in content
    assert content.count(NEW_PAGE_BLOCK) == 1
    for field in (
        '"source_status"] = "available"',
        '"pages_reviewed"] = pages_reviewed',
        '"rows_inspected"] = rows_inspected',
        '"older_boundary_reached"] = older_boundary',
        '"pagination_exhausted"] = exhausted',
        '"records_on_target_date"] = len(collected)',
    ):
        assert field in content


def test_repair_is_idempotent(tmp_path: Path):
    target = tmp_path / "magellan_playwright.py"
    target.write_text(_repaired_source(), encoding="utf-8")

    assert repair(target) == "already_repaired"


def test_refuses_unknown_source_shape(tmp_path: Path):
    target = tmp_path / "magellan_playwright.py"
    target.write_text("unexpected\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="unexpected Magellan"):
        repair(target)
