#!/usr/bin/env python3
"""Idempotently retitle the Magellan customer-sentiment section.

The department accountability Google Doc heading is the first element of the
Magellan section spec in stable_google_doc.py. The installed label is
"Customer sentiment — Magellan". Carlo asked for the exact label
"Customer sentiment (SAD) — Magellan".
"""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path


OLD_HEADING = "Customer sentiment \u2014 Magellan"
NEW_HEADING = "Customer sentiment (SAD) \u2014 Magellan"


def repair(path: Path) -> str:
    path = path.expanduser().resolve()
    original = path.read_text(encoding="utf-8")
    old_count = original.count(OLD_HEADING)
    new_count = original.count(NEW_HEADING)
    if old_count == 0 and new_count == 1:
        return "already_repaired"
    if old_count != 1 or new_count != 0:
        raise RuntimeError(
            "refusing unexpected Magellan customer-sentiment heading shape: "
            f"old={old_count}, new={new_count}"
        )

    backup = path.with_suffix(path.suffix + ".pre-magellan-sentiment-heading")
    if not backup.exists():
        shutil.copy2(path, backup)
        os.chmod(backup, 0o600)

    updated = original.replace(OLD_HEADING, NEW_HEADING, 1)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(updated, encoding="utf-8")
    os.chmod(temporary, path.stat().st_mode & 0o777)
    temporary.replace(path)

    verified = path.read_text(encoding="utf-8")
    if verified.count(OLD_HEADING) != 0 or verified.count(NEW_HEADING) != 1:
        raise RuntimeError("Magellan customer-sentiment heading repair verification failed")
    return "repaired"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", required=True)
    args = parser.parse_args()
    print(repair(Path(args.path)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
