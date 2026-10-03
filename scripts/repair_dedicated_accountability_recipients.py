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
    "sandy@streetsmart.insurance",
)
CARLO_ONLY_RECIPIENTS = ("carlo@streetsmart.insurance",)
BEGIN_MARKER = "# BEGIN ROBIE ACCOUNTABILITY RECIPIENT GUARD"
END_MARKER = "# END ROBIE ACCOUNTABILITY RECIPIENT GUARD"


def _block() -> str:
    values = ",\n".join(f'    "{address}"' for address in APPROVED_RECIPIENTS)
    carlo = CARLO_ONLY_RECIPIENTS[0]
    return f'''{BEGIN_MARKER}
import os as _accountability_os

DEFAULT_RECIPIENTS = [
{values},
]
_recipient_override = _accountability_os.environ.get(
    "ACCOUNTABILITY_DELIVERY_RECIPIENTS", ""
).strip()
if _recipient_override:
    RECIPIENTS = [
        address.strip()
        for address in _recipient_override.split(",")
        if address.strip()
    ]
    if RECIPIENTS not in (["{carlo}"], list(DEFAULT_RECIPIENTS)):
        raise RuntimeError("refusing unapproved accountability recipient override")
else:
    RECIPIENTS = list(DEFAULT_RECIPIENTS)
{END_MARKER}'''


def repair(path: Path) -> str:
    path = path.expanduser().resolve()
    original = path.read_text(encoding="utf-8")
    expected = _block()
    if expected in original:
        return "already_repaired"

    backup = path.with_suffix(path.suffix + ".pre-recipient-repair")
    if not backup.exists():
        shutil.copy2(path, backup)
        os.chmod(backup, 0o600)

    if BEGIN_MARKER in original and END_MARKER in original:
        pattern = re.compile(
            re.escape(BEGIN_MARKER) + r"(?:.|\n)*?" + re.escape(END_MARKER),
            re.MULTILINE,
        )
        matches = list(pattern.finditer(original))
        if len(matches) != 1:
            raise RuntimeError(
                f"refusing unexpected recipient guard shape: found {len(matches)} blocks"
            )
        updated = original[: matches[0].start()] + expected + original[matches[0].end() :]
        status = "upgraded"
    elif BEGIN_MARKER in original or END_MARKER in original:
        raise RuntimeError("refusing partial or unexpected recipient guard")
    else:
        pattern = re.compile(r"RECIPIENTS\s*=\s*\[(?:.|\n)*?\]", re.MULTILINE)
        matches = list(pattern.finditer(original))
        if len(matches) != 1:
            raise RuntimeError(
                f"refusing unexpected recipient source shape: found {len(matches)} lists"
            )
        existing = matches[0].group(0)
        if "@streetsmart.insurance" not in existing:
            raise RuntimeError("refusing recipient block without the approved agency domain")
        updated = original[: matches[0].start()] + expected + original[matches[0].end() :]
        status = "repaired"

    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(updated, encoding="utf-8")
    os.chmod(temporary, path.stat().st_mode & 0o777)
    temporary.replace(path)
    verified = path.read_text(encoding="utf-8")
    if verified.count(expected) != 1:
        raise RuntimeError("recipient repair verification failed")
    for address in APPROVED_RECIPIENTS:
        if verified.count(address) < 1:
            raise RuntimeError(f"recipient repair missing approved address: {address}")
    return status


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", required=True)
    args = parser.parse_args()
    print(repair(Path(args.path)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
