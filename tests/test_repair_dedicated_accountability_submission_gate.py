import ast
from pathlib import Path

import pytest

from scripts.repair_dedicated_accountability_submission_gate import (
    NEW_FETCH_BLOCK,
    OLD_FALLBACK_BLOCK,
    OLD_FETCH_BLOCK,
    repair,
)

FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "partial_accountability_submission_gate_production_main.py"
)


def _direct_fetch_assignments(tree: ast.AST) -> int:
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        value = node.value
        if (
            isinstance(target, ast.Name)
            and target.id == "submissions"
            and isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "fetch_live_submission_center"
            and not value.args
            and not value.keywords
        ):
            count += 1
    return count


def _assert_fail_closed_source(source: str) -> None:
    compile(source, "production_main.py", "exec")
    tree = ast.parse(source)
    assert _direct_fetch_assignments(tree) == 1
    assert "submission_error" not in source
    assert not any(isinstance(node, ast.Name) and node.id == "submission_error" for node in ast.walk(tree))
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "unverified_submission_row"
        for node in ast.walk(tree)
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



def test_repairs_partial_shape_after_fallback_was_already_removed(tmp_path: Path):
    target = tmp_path / "production_main.py"
    partial = _legacy_source().replace(OLD_FALLBACK_BLOCK, "")
    target.write_text(partial, encoding="utf-8")

    assert repair(target) == "repaired"
    repaired = target.read_text(encoding="utf-8")
    assert repaired.count(NEW_FETCH_BLOCK) == 1
    assert OLD_FETCH_BLOCK not in repaired
    assert "except SubmissionCenterSourceError" not in repaired
    assert "submission_error =" not in repaired
    assert repair(target) == "already_repaired"


def test_repaired_source_does_not_catch_submission_collection_failure(tmp_path: Path):
    target = tmp_path / "production_main.py"
    target.write_text(_legacy_source(), encoding="utf-8")
    repair(target)

    repaired = target.read_text(encoding="utf-8")
    assert "except SubmissionCenterSourceError" not in repaired
    assert "unverified_submission_row(submission_error)" not in repaired
    assert NEW_FETCH_BLOCK in repaired


def test_commented_partial_production_shape_is_not_false_green(tmp_path: Path):
    """Live Production kept ``if submission_error:`` behind comments.

    The stock checker treated that file as already_repaired because it only
    looked for ``submission_error =`` and the exact uncommented fallback block.
    """
    target = tmp_path / "production_main.py"
    original = FIXTURE.read_text(encoding="utf-8")
    assert "if submission_error:" in original
    assert "submission_error =" not in original
    assert OLD_FALLBACK_BLOCK not in original
    target.write_text(original, encoding="utf-8")

    assert repair(target) == "repaired"
    repaired = target.read_text(encoding="utf-8")
    _assert_fail_closed_source(repaired)
    assert "unverified_submission_row(" not in repaired
    assert repair(target) == "already_repaired"

    backup = target.with_suffix(".py.pre-submission-fail-closed")
    assert backup.read_text(encoding="utf-8") == original
    assert backup.stat().st_mode & 0o777 == 0o600


def test_refuses_unknown_source_shape(tmp_path: Path):
    target = tmp_path / "production_main.py"
    target.write_text("def unrelated():\n    return 1\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unexpected Submission Center source shape"):
        repair(target)
