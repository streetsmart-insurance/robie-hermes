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

UTF16_HELPER = '''def _utf16_len(value: str) -> int:
    """Return the Google Docs API index width for a Python string."""
    return len(value.encode("utf-16-le")) // 2


'''

FINAL_HELPER_MARKER = "def _final_insert_index("
FINAL_HELPER_RETURN = "return original + sum(length for index, length in inserted_lengths if index < original)"
MAIN_MARKER = "def main("


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
    utf16_helper_count = original.count(UTF16_HELPER)
    final_helper_count = original.count(FINAL_HELPER_MARKER)
    final_return_count = original.count(FINAL_HELPER_RETURN)
    main_count = original.count(MAIN_MARKER)
    states = [(original.count(old), original.count(new)) for old, new in REPLACEMENTS]

    not_applicable = (
        utf16_helper_count == 0
        and final_helper_count == 0
        and final_return_count == 0
        and main_count == 1
        and all(old_count == 0 and new_count == 0 for old_count, new_count in states)
    )
    if not_applicable:
        return "not_applicable"

    # OLD_HELPER is a textual suffix of NEW_HELPER, so its count remains one
    # after the canonical UTF-16 helper is inserted.
    helper_ready = (
        utf16_helper_count == 1
        and (
            (final_helper_count == 1 and final_return_count == 1)
            or (final_helper_count == 0 and final_return_count == 0 and main_count == 1)
        )
    )
    helper_legacy = (
        utf16_helper_count == 0
        and (
            (final_helper_count == 1 and final_return_count == 1)
            or (final_helper_count == 0 and final_return_count == 0 and main_count == 1)
        )
    )
    expressions_ready = all(
        old_count == 0 and new_count >= 1 for old_count, new_count in states
    )
    if helper_ready and expressions_ready:
        return "already_repaired"

    if not (helper_ready or helper_legacy):
        raise RuntimeError(
            "refusing unexpected Google Docs index helper source shape: "
            f"old={old_helper_count}, utf16={utf16_helper_count}, "
            f"final={final_helper_count}, return={final_return_count}, main={main_count}"
        )
    if any(
        not (
            (old_count >= 1 and new_count == 0)
            or (old_count == 0 and new_count >= 1)
        )
        for old_count, new_count in states
    ):
        raise RuntimeError(f"refusing unexpected Google Docs index expression shape: {states}")

    backup = path.with_suffix(path.suffix + ".pre-utf16-index-repair")
    if not backup.exists():
        shutil.copy2(path, backup)
        os.chmod(backup, 0o600)

    updated = original
    if helper_legacy:
        if old_helper_count == 1:
            updated = updated.replace(OLD_HELPER, NEW_HELPER, 1)
        elif old_helper_count == 0 and final_helper_count == 1:
            updated = updated.replace(FINAL_HELPER_MARKER, UTF16_HELPER + FINAL_HELPER_MARKER, 1)
        elif old_helper_count == 0 and final_helper_count == 0 and main_count == 1:
            updated = updated.replace(MAIN_MARKER, UTF16_HELPER + MAIN_MARKER, 1)
        else:
            raise RuntimeError("refusing duplicate legacy Google Docs index helpers")
    for old, new in REPLACEMENTS:
        if old in updated:
            updated = updated.replace(old, new)

    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(updated, encoding="utf-8")
    os.chmod(temporary, path.stat().st_mode & 0o777)
    temporary.replace(path)

    verified = path.read_text(encoding="utf-8")
    if (
        verified.count(UTF16_HELPER) != 1
        or verified.count(FINAL_HELPER_MARKER) != final_helper_count
        or verified.count(FINAL_HELPER_RETURN) != final_return_count
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
