from pathlib import Path

import pytest

from scripts.repair_dedicated_accountability_magellan import (
    NEW_BLOCK,
    OLD_BLOCK,
    repair,
)


def test_repairs_exact_legacy_block(tmp_path: Path):
    target = tmp_path / "magellan_playwright.py"
    target.write_text("prefix\n" + OLD_BLOCK + "suffix\n", encoding="utf-8")

    assert repair(target) == "repaired"
    content = target.read_text(encoding="utf-8")
    assert OLD_BLOCK not in content
    assert content.count(NEW_BLOCK) == 1
    assert target.with_suffix(".py.pre-session-repair").is_file()


def test_repair_is_idempotent(tmp_path: Path):
    target = tmp_path / "magellan_playwright.py"
    target.write_text("prefix\n" + NEW_BLOCK + "suffix\n", encoding="utf-8")

    assert repair(target) == "already_repaired"


def test_refuses_unknown_source_shape(tmp_path: Path):
    target = tmp_path / "magellan_playwright.py"
    target.write_text("unexpected\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="unexpected Magellan source shape"):
        repair(target)
