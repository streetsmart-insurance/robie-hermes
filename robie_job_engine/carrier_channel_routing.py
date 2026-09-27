"""Carrier channel routing: portal-first contact ladder per carrier.

Reads the curated directory at robie_job_engine/data/carrier_directory.json
(31 carriers, added PR #607). route_carrier() resolves a Master Company
name to a routing dict the verification workers consume:

    {"channel": "PORTAL"|"EMAIL"|"EMAIL_ASK_PORTAL",
     "portal_url": ..., "underwriter_email": ...,
     "phone": ..., "phone_label": ..., "notes": ...}

Unknown carriers fail CLOSED to a HOLD route ({"channel": "HOLD"}) — the
worker then holds the item for carrier-desk contact confirmation instead
of planning outreach against a guessed channel. Matching is
case-insensitive on the full name, then on distinctive tokens.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

DIRECTORY_PATH = Path(__file__).resolve().parent / "data" / "carrier_directory.json"

DEFAULT_ROUTE: dict[str, Any] = {
    "channel": "HOLD",
    "reason": "carrier not in the carrier directory — confirm the carrier "
              "desk contact in EZLynx before any outreach",
}


@lru_cache(maxsize=1)
def _directory() -> dict[str, dict[str, Any]]:
    try:
        with open(DIRECTORY_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _normalize(name: str) -> str:
    return " ".join(str(name or "").casefold().split())


def route_carrier(carrier: str) -> dict[str, Any]:
    """Return the contact route for a carrier name.

    Exact (case-insensitive) match first; then a token-subset match so
    "NJCRIB" finds "NJCRIB - Hartford Assigned Risk". Unknown carriers
    get the fail-closed HOLD default — never a guessed channel or portal.
    """
    directory = _directory()
    want = _normalize(carrier)
    if not want:
        return dict(DEFAULT_ROUTE)
    for name, route in directory.items():
        if _normalize(name) == want:
            return dict(route)
    want_tokens = set(want.split())
    for name, route in directory.items():
        name_tokens = set(_normalize(name).split())
        if want_tokens and want_tokens <= name_tokens:
            return dict(route)
    # Distinctive single-token fallback (e.g. "hartford" in a longer name).
    for name, route in directory.items():
        if want in _normalize(name) or _normalize(name) in want:
            return dict(route)
    return dict(DEFAULT_ROUTE)


def clear_cache() -> None:
    """Test hook: drop the cached directory."""
    _directory.cache_clear()
