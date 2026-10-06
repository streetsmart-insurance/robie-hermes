"""Phone normalization.

The normalization contract is IDENTICAL to the watchdog's build_phone_index.py
(/opt/streetsmart-phone-watchdog/scripts/build_phone_index.py), because the
offline phone index keys were built with that exact rule. Do not "improve"
this function without rebuilding the index contract too, or lookups will
silently miss.
"""

from __future__ import annotations

import re
from typing import Optional


def normalize_phone(raw: object) -> Optional[str]:
    """Normalize a phone value to digits.

    Returns None for empty/invalid values. Strips a leading US country code.
    Accepts 7+ digit fragments (same as the index builder) to avoid junk.
    """
    if raw is None:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if not digits:
        return None
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) < 7:
        return None
    return digits


def display_phone(digits: str) -> str:
    """Human-friendly display: (732) 462-8343. Falls back to raw digits."""
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return digits
