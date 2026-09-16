#!/usr/bin/env python3
"""Idempotently repair the dedicated accountability Magellan collector.

The installed standalone collector already authenticates through Secret Manager.
Its failure is caused by an immediate, redundant second dashboard navigation after
successful authentication. That second navigation can discard the just-created
session and redirect back to login. This repair removes only that exact block.
"""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path


OLD_BLOCK = """            _ensure_authenticated(page, context)
            page.goto("https://app.magellan.insure/dashboard", timeout=35000)
            page.wait_for_load_state("networkidle")
"""

NEW_BLOCK = """            _ensure_authenticated(page, context)
            page.wait_for_load_state("domcontentloaded")
"""


def repair(path: Path) -> str:
    path = path.expanduser().resolve()
    original = path.read_text(encoding="utf-8")
    old_count = original.count(OLD_BLOCK)
    new_count = original.count(NEW_BLOCK)

    if old_count == 0 and new_count == 1:
        return "already_repaired"
    if old_count != 1:
        raise RuntimeError(
            f"refusing unexpected Magellan source shape: expected one legacy block, found {old_count}"
        )

    backup = path.with_suffix(path.suffix + ".pre-session-repair")
    if not backup.exists():
        shutil.copy2(path, backup)
        os.chmod(backup, 0o600)

    updated = original.replace(OLD_BLOCK, NEW_BLOCK, 1)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(updated, encoding="utf-8")
    os.chmod(temporary, path.stat().st_mode & 0o777)
    temporary.replace(path)

    verified = path.read_text(encoding="utf-8")
    if OLD_BLOCK in verified or verified.count(NEW_BLOCK) != 1:
        raise RuntimeError("Magellan source repair verification failed")
    return "repaired"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", required=True)
    args = parser.parse_args()
    print(repair(Path(args.path)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
