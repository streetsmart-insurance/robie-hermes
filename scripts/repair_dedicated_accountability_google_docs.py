#!/usr/bin/env python3
"""Idempotently repair UTF-16 index accounting in the dedicated Docs publisher.

Google Docs API indexes text in UTF-16 code units. Python len() counts Unicode
code points, so any non-BMP character (for example an emoji) makes subsequent
insert locations drift into a table boundary and fail with HTTP 400.
"""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path


OLD_HELPER = '''def _final_insert_index(original: int, inserted_lengths: Sequence[Tuple[int, int]]) -> int:
    """Translate a pre-insert index after reverse-ordered cell text inserts."""
    return original + sum(length for index, length in inserted_lengths if index < original)
'''

NEW_HELPER = '''def _utf16_len(value: str) -> int:
    """Return the Google Docs API index width for a Python string."""
    return len(value.encode("utf-16-le")) // 2


def _final_insert_index(original: int, inserted_lengths: Sequence[Tuple[int, int]]) -> int:
    """Translate a pre-insert index after reverse-ordered cell text inserts."""
    return original + sum(length for index, length in inserted_lengths if index < original)
'''

REPLACEMENTS = (
    ("at + len(heading_text)", "at + _utf16_len(heading_text)"),
    ("(idx, len(value)) for idx, value in inserts", "(idx, _utf16_len(value)) for idx, value in inserts"),
    ("first + len(_clean(headers[column]))", "first + _utf16_len(_clean(headers[column]))"),
    ("1 + len(title) + len(scope_note)", "1 + _utf16_len(title) + _utf16_len(scope_note)"),
)


def _utf16_len(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def repair(path: Path) -> str:
    path = path.expanduser().resolve()
    original = path.read_text(encoding="utf-8")

    old_helper_count = original.count(OLD_HELPER)
    new_helper_count = original.count(NEW_HELPER)
    states = [(original.count(old), original.count(new)) for old, new in REPLACEMENTS]

    already_ready = new_helper_count == 1 and all(
        old_count == 0 and new_count >= 1 for old_count, new_count in states
    )
    if already_ready:
        return "already_repaired"

    if old_helper_count != 1 or new_helper_count != 0:
        raise RuntimeError("refusing unexpected Google Docs index helper source shape")
    if any(old_count < 1 or new_count != 0 for old_count, new_count in states):
        raise RuntimeError(f"refusing unexpected Google Docs index expression shape: {states}")

    backup = path.with_suffix(path.suffix + ".pre-utf16-index-repair")
    if not backup.exists():
        shutil.copy2(path, backup)
        os.chmod(backup, 0o600)

    updated = original.replace(OLD_HELPER, NEW_HELPER, 1)
    for old, new in REPLACEMENTS:
        updated = updated.replace(old, new)

    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(updated, encoding="utf-8")
    os.chmod(temporary, path.stat().st_mode & 0o777)
    temporary.replace(path)

    verified = path.read_text(encoding="utf-8")
    if (
        verified.count(NEW_HELPER) != 1
        or any(old in verified or new not in verified for old, new in REPLACEMENTS)
    ):
        raise RuntimeError("Google Docs UTF-16 index repair verification failed")
    return "repaired"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", required=True)
    args = parser.parse_args()
    print(repair(Path(args.path)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
