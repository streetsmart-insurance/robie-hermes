#!/usr/bin/env python3
"""Idempotently enforce the approved dedicated-accountability recipient set."""

from __future__ import annotations

import argparse
import os
import re
import shutil
from pathlib import Path


APPROVED_RECIPIENTS = (
    "carlo@streetsmart.insurance",
    "jake@streetsmart.insurance",
    "ashley@streetsmart.insurance",
    "gabrielac@streetsmart.insurance",
)


def _block() -> str:
    values = ",\n".join(f'    "{address}"' for address in APPROVED_RECIPIENTS)
    return f"RECIPIENTS = [\n{values},\n]"


def repair(path: Path) -> str:
    path = path.expanduser().resolve()
    original = path.read_text(encoding="utf-8")
    expected = _block()
    if expected in original:
        return "already_repaired"
    pattern = re.compile(r"RECIPIENTS\s*=\s*\[(?:.|\n)*?\]", re.MULTILINE)
    matches = list(pattern.finditer(original))
    if len(matches) != 1:
        raise RuntimeError(f"refusing unexpected recipient source shape: found {len(matches)} lists")
    existing = matches[0].group(0)
    if "@streetsmart.insurance" not in existing:
        raise RuntimeError("refusing recipient block without the approved agency domain")
    backup = path.with_suffix(path.suffix + ".pre-recipient-repair")
    if not backup.exists():
        shutil.copy2(path, backup)
        os.chmod(backup, 0o600)
    updated = original[: matches[0].start()] + expected + original[matches[0].end() :]
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(updated, encoding="utf-8")
    os.chmod(temporary, path.stat().st_mode & 0o777)
    temporary.replace(path)
    verified = path.read_text(encoding="utf-8")
    if verified.count(expected) != 1 or "sandy@streetsmart.insurance" in verified:
        raise RuntimeError("recipient repair verification failed")
    return "repaired"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", required=True)
    args = parser.parse_args()
    print(repair(Path(args.path)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
