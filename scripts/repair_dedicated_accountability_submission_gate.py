#!/usr/bin/env python3
"""Make the dedicated accountability Submission Center source fail closed.

The standalone Production application previously converted an EZLynx
Submission Center collection error into synthetic UNVERIFIED rows and then
continued to publish and deliver the report. That is not acceptable evidence:
an unavailable source must stop the run before publication or email delivery.

A partial repair left the direct ``submissions = fetch_live_submission_center()``
assignment in place and kept the synthetic fallback, with comments between
``if submission_error:`` and the loop. ``already_repaired`` is true only when
the identifier ``submission_error`` is absent everywhere.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import shutil
from pathlib import Path


OLD_FETCH_BLOCK = """    try:
        submissions = fetch_live_submission_center()
        submission_error = None
    except SubmissionCenterSourceError as exc:
        submissions = []
        submission_error = str(exc)
"""

NEW_FETCH_BLOCK = """    submissions = fetch_live_submission_center()
"""

OLD_FALLBACK_BLOCK = """    if submission_error:
        for department in departments.values():
            department["submissions"].append(unverified_submission_row(submission_error))
"""

# Comments may sit between the condition and the loop. That is the live
# streetsmart-accountability-prod shape: the exact OLD_FALLBACK_BLOCK does
# not match, but ``if submission_error:`` still executes.
_COMMENTED_FALLBACK = re.compile(
    r"^[ \t]*if submission_error:\n"
    r"(?:[ \t]*#[^\n]*\n)*"
    r"[ \t]*for department in departments\.values\(\):\n"
    r"[ \t]*department\[\"submissions\"\]\.append\(unverified_submission_row\(submission_error\)\)\n",
    re.MULTILINE,
)
_SUBMISSION_ERROR_IDENTIFIER = re.compile(r"\bsubmission_error\b")


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


def _is_fail_closed(source: str) -> bool:
    """True only when the source cannot read ``submission_error`` at all."""
    if _SUBMISSION_ERROR_IDENTIFIER.search(source):
        return False
    if re.search(r"\bunverified_submission_row\s*\(", source):
        return False
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    if _direct_fetch_assignments(tree) != 1:
        return False
    if any(isinstance(node, ast.Name) and node.id == "submission_error" for node in ast.walk(tree)):
        return False
    if any(
        isinstance(node, ast.Call)
        and isinstance(getattr(node.func, "id", None), str)
        and node.func.id == "unverified_submission_row"
        for node in ast.walk(tree)
    ):
        return False
    return True


def repair(path: Path) -> str:
    path = path.expanduser().resolve()
    original = path.read_text(encoding="utf-8")

    if _is_fail_closed(original):
        return "already_repaired"

    old_fetch_count = original.count(OLD_FETCH_BLOCK)
    # The direct block is a textual suffix of the legacy try block, so count
    # only occurrences that are not contained inside the legacy block.
    new_fetch_count = original.count(NEW_FETCH_BLOCK) - old_fetch_count
    fallback_count = len(_COMMENTED_FALLBACK.findall(original))

    legacy_fetch = old_fetch_count == 1 and new_fetch_count == 0
    direct_fetch = old_fetch_count == 0 and new_fetch_count == 1
    if not (
        (legacy_fetch and fallback_count in (0, 1))
        or (direct_fetch and fallback_count == 1)
    ):
        raise RuntimeError(
            "refusing unexpected Submission Center source shape: "
            f"fetch={old_fetch_count}, direct={new_fetch_count}, "
            f"fallback={fallback_count}"
        )

    backup = path.with_suffix(path.suffix + ".pre-submission-fail-closed")
    if not backup.exists():
        shutil.copy2(path, backup)
        os.chmod(backup, 0o600)

    updated = original
    if legacy_fetch:
        updated = updated.replace(OLD_FETCH_BLOCK, NEW_FETCH_BLOCK, 1)
    updated, replaced = _COMMENTED_FALLBACK.subn("", updated, count=1)
    if replaced != fallback_count:
        raise RuntimeError("Submission Center fail-closed repair did not remove the fallback")

    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(updated, encoding="utf-8")
    os.chmod(temporary, path.stat().st_mode & 0o777)
    temporary.replace(path)

    verified = path.read_text(encoding="utf-8")
    if not _is_fail_closed(verified):
        raise RuntimeError("Submission Center fail-closed repair verification failed")
    return "repaired"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", required=True)
    args = parser.parse_args()
    print(repair(Path(args.path)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
