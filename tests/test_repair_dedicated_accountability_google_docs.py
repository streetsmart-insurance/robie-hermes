from pathlib import Path

import pytest

from scripts.repair_dedicated_accountability_google_docs import (
    NEW_HELPER,
    OLD_HELPER,
    REPLACEMENTS,
    _utf16_len,
    repair,
)


def _legacy_source() -> str:
    expressions = "\n".join(f"value_{index} = {old}" for index, (old, _) in enumerate(REPLACEMENTS))
    return (
        "from typing import Sequence, Tuple\n\n"
        + OLD_HELPER
        + "\n"
        + expressions
        + "\n"
    )


def test_utf16_width_matches_google_docs_index_units():
    assert _utf16_len("plain") == 5
    assert _utf16_len("risk ⚠") == 6
    assert _utf16_len("call 🚨") == 7


def test_repairs_legacy_index_math_and_is_idempotent(tmp_path: Path):
    target = tmp_path / "production_main.py"
    target.write_text(_legacy_source(), encoding="utf-8")

    assert repair(target) == "repaired"
    repaired = target.read_text(encoding="utf-8")
    assert repaired.count(NEW_HELPER) == 1
    for old, new in REPLACEMENTS:
        assert old not in repaired
        assert new in repaired

    namespace = {}
    exec(repaired, namespace)
    assert namespace["_utf16_len"]("🚨") == 2
    assert repair(target) == "already_repaired"

    backup = target.with_suffix(".py.pre-utf16-index-repair")
    assert backup.read_text(encoding="utf-8") == _legacy_source()
    assert backup.stat().st_mode & 0o777 == 0o600


def test_refuses_unknown_source_shape(tmp_path: Path):
    target = tmp_path / "production_main.py"
    target.write_text("def unrelated():\n    return 1\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unexpected Google Docs index helper"):
        repair(target)
