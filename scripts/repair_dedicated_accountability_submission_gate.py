#!/usr/bin/env python3
"""Make the dedicated accountability Submission Center source fail closed.

The standalone Production application previously converted an EZLynx
Submission Center collection error into synthetic UNVERIFIED rows and then
continued to publish and deliver the report. That is not acceptable evidence:
an unavailable source must stop the run before publication or email delivery.
"""

from __future__ import annotations

import argparse
import os
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


def repair(path: Path) -> str:
    path = path.expanduser().resolve()
    original = path.read_text(encoding="utf-8")

    old_fetch_count = original.count(OLD_FETCH_BLOCK)
    # The direct block is a textual suffix of the legacy try block, so count
    # only occurrences that are not contained inside the legacy block.
    new_fetch_count = original.count(NEW_FETCH_BLOCK) - old_fetch_count
    old_fallback_count = original.count(OLD_FALLBACK_BLOCK)

    already_ready = (
        old_fetch_count == 0
        and new_fetch_count == 1
        and old_fallback_count == 0
        and "submission_error =" not in original
    )
    if already_ready:
        return "already_repaired"

    if (
        old_fetch_count != 1
        or new_fetch_count != 0
        or old_fallback_count != 1
    ):
        raise RuntimeError(
            "refusing unexpected Submission Center source shape: "
            f"fetch={old_fetch_count}, direct={new_fetch_count}, "
            f"fallback={old_fallback_count}"
        )

    backup = path.with_suffix(path.suffix + ".pre-submission-fail-closed")
    if not backup.exists():
        shutil.copy2(path, backup)
        os.chmod(backup, 0o600)

    updated = original.replace(OLD_FETCH_BLOCK, NEW_FETCH_BLOCK, 1)
    updated = updated.replace(OLD_FALLBACK_BLOCK, "", 1)

    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(updated, encoding="utf-8")
    os.chmod(temporary, path.stat().st_mode & 0o777)
    temporary.replace(path)

    verified = path.read_text(encoding="utf-8")
    if (
        OLD_FETCH_BLOCK in verified
        or OLD_FALLBACK_BLOCK in verified
        or verified.count(NEW_FETCH_BLOCK) != 1
        or "submission_error =" in verified
    ):
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
