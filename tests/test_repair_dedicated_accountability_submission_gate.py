from pathlib import Path

import pytest

from scripts.repair_dedicated_accountability_submission_gate import (
    NEW_FETCH_BLOCK,
    OLD_FALLBACK_BLOCK,
    OLD_FETCH_BLOCK,
    repair,
)


def _legacy_source() -> str:
    return (
        "def build_report():\n"
        + OLD_FETCH_BLOCK
        + "    departments = {}\n"
        + OLD_FALLBACK_BLOCK
        + "    return submissions\n"
    )


def test_repair_removes_unverified_fallback_and_is_idempotent(tmp_path: Path):
    target = tmp_path / "production_main.py"
    target.write_text(_legacy_source(), encoding="utf-8")

    assert repair(target) == "repaired"
    repaired = target.read_text(encoding="utf-8")
    assert repaired.count(NEW_FETCH_BLOCK) == 1
    assert OLD_FETCH_BLOCK not in repaired
    assert OLD_FALLBACK_BLOCK not in repaired
    assert "submission_error =" not in repaired
    assert "submissions = []" not in repaired

    assert repair(target) == "already_repaired"

    backup = target.with_suffix(".py.pre-submission-fail-closed")
    assert backup.read_text(encoding="utf-8") == _legacy_source()
    assert backup.stat().st_mode & 0o777 == 0o600


def test_repaired_source_does_not_catch_submission_collection_failure(tmp_path: Path):
    target = tmp_path / "production_main.py"
    target.write_text(_legacy_source(), encoding="utf-8")
    repair(target)

    repaired = target.read_text(encoding="utf-8")
    assert "except SubmissionCenterSourceError" not in repaired
    assert "unverified_submission_row(submission_error)" not in repaired
    assert NEW_FETCH_BLOCK in repaired


def test_refuses_unknown_source_shape(tmp_path: Path):
    target = tmp_path / "production_main.py"
    target.write_text("def unrelated():\n    return 1\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unexpected Submission Center source shape"):
        repair(target)
