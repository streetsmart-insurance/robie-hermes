from pathlib import Path

import pytest

from scripts.repair_dedicated_accountability_magellan_sentiment_heading import (
    NEW_HEADING,
    OLD_HEADING,
    repair,
)


EXACT_HEADING = "Customer sentiment (SAD) — Magellan"


def _section_source(heading: str) -> str:
    return (
        "def _section_specs(data):\n"
        "    return [\n"
        f'        ("{heading}", [("Sentiment", "Sentiment")], data.get("magellan_sad", [])),\n'
        "    ]\n"
    )


def test_requested_heading_is_exact():
    assert NEW_HEADING == EXACT_HEADING
    assert OLD_HEADING == "Customer sentiment — Magellan"
    assert OLD_HEADING not in NEW_HEADING


def test_repairs_department_heading_and_is_idempotent(tmp_path: Path):
    target = tmp_path / "stable_google_doc.py"
    target.write_text(_section_source(OLD_HEADING), encoding="utf-8")

    assert repair(target) == "repaired"
    repaired = target.read_text(encoding="utf-8")
    assert repaired.count(EXACT_HEADING) == 1
    assert OLD_HEADING not in repaired
    assert f'("{EXACT_HEADING}"' in repaired

    assert repair(target) == "already_repaired"
    backup = target.with_suffix(".py.pre-magellan-sentiment-heading")
    assert backup.read_text(encoding="utf-8") == _section_source(OLD_HEADING)
    assert backup.stat().st_mode & 0o777 == 0o600


def test_refuses_missing_or_duplicate_heading(tmp_path: Path):
    missing = tmp_path / "missing.py"
    missing.write_text("def unrelated():\n    return 1\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unexpected Magellan customer-sentiment heading"):
        repair(missing)

    duplicate = tmp_path / "duplicate.py"
    duplicate.write_text(
        _section_source(OLD_HEADING) + _section_source(OLD_HEADING),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="unexpected Magellan customer-sentiment heading"):
        repair(duplicate)
