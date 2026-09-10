"""Carrier -> retrieval-channel routing for the verification workers.

Ports ``data/carrier_directory.json`` (29 carriers, channels
``PORTAL`` / ``EMAIL`` / ``EMAIL_ASK_PORTAL``) from the renewal-automation-system
into ``robie_job_engine/data/carrier_directory.json``, with a loader and the
original fuzzy name match.

This is intentionally separate from :mod:`carrier_directory` (the DB-backed
carrier/endpoint/credential/LOB-rule store): that module never covered
carrier-to-channel routing, so this adds the missing piece without duplicating
it.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger("robie.carrier_channel_routing")

CHANNELS = frozenset({"PORTAL", "EMAIL", "EMAIL_ASK_PORTAL"})

DIRECTORY_PATH = Path(__file__).resolve().parent / "data" / "carrier_directory.json"

DEFAULT_UNKNOWN_CARRIER = {
    "channel": "EMAIL",
    "notes": "Unregistered carrier; default to email outreach.",
}


@lru_cache(maxsize=1)
def load_channel_directory() -> dict[str, dict[str, Any]]:
    """Load the packaged 29-carrier channel directory. Fail closed on corruption."""
    try:
        data = json.loads(DIRECTORY_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"carrier channel directory is missing: {DIRECTORY_PATH}"
        ) from exc
    except ValueError as exc:
        raise RuntimeError(
            f"carrier channel directory is corrupt: {DIRECTORY_PATH}: {exc}"
        ) from exc
    if not isinstance(data, dict) or not data:
        raise RuntimeError(
            f"carrier channel directory is empty or malformed: {DIRECTORY_PATH}"
        )
    cleaned: dict[str, dict[str, Any]] = {}
    for name, entry in data.items():
        if not isinstance(entry, dict):
            raise RuntimeError(
                f"carrier channel directory entry {name!r} is malformed"
            )
        channel = str(entry.get("channel") or "").strip().upper()
        if channel not in CHANNELS:
            raise RuntimeError(
                f"carrier channel directory entry {name!r} has unknown "
                f"channel {channel!r}"
            )
        item = dict(entry)
        item["channel"] = channel
        cleaned[str(name)] = item
    return cleaned


def list_carriers() -> list[str]:
    """Sorted carrier names in the directory."""
    return sorted(load_channel_directory())


def route_carrier(carrier_name: str | None) -> dict[str, Any]:
    """Return the channel config for a carrier name (fuzzy match).

    Exact match first, then case-insensitive/partial match (ported from the
    original ``CarrierRoutingMatrix.get_carrier_config``). Unknown or blank
    names fall back to ``EMAIL`` — never raise for an unregistered carrier, so
    workers can always proceed to email outreach.
    """
    directory = load_channel_directory()
    if not carrier_name or not str(carrier_name).strip():
        return dict(DEFAULT_UNKNOWN_CARRIER)
    name = str(carrier_name).strip()
    if name in directory:
        return dict(directory[name])
    lowered = name.casefold()
    for key, config in directory.items():
        key_lowered = key.casefold()
        if key_lowered in lowered or lowered in key_lowered:
            logger.info("carrier %r fuzzy-matched to directory entry %r", name, key)
            return dict(config)
    logger.info("carrier %r not in directory; defaulting to EMAIL", name)
    return dict(DEFAULT_UNKNOWN_CARRIER)


def portal_carriers() -> list[str]:
    """Carrier names routed to portal retrieval."""
    return sorted(
        name
        for name, config in load_channel_directory().items()
        if config.get("channel") == "PORTAL"
    )
