"""Carrier -> retrieval-channel routing for the verification workers.

Ports ``data/carrier_directory.json`` (29 carriers, channels
``PORTAL`` / ``EMAIL`` / ``EMAIL_ASK_PORTAL``) from the renewal-automation-system
into ``robie_job_engine/data/carrier_directory.json``, with a loader and the
original fuzzy name match.

This is intentionally separate from :mod:`carrier_directory` (the DB-backed
carrier/endpoint/credential/LOB-rule store): that module never covered
carrier-to-channel routing, so this adds the missing piece without duplicating
it.

Carrier IDENTITY (name/abbreviation -> canonical carrier) resolves via the
EZLynx carrier directory first (Carlo's standing rule, 2026-09-15: the EZLynx
directory is the source of truth for carrier identity — never Google, never a
hardcoded guess). The packaged JSON remains the fallback for identity AND the
authority for channel routing (portal URLs, underwriter emails). If the EZLynx
directory is unreachable, routing falls back to the JSON fuzzy match — workers
never crash on a directory outage.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("robie.carrier_channel_routing")

CHANNELS = frozenset({"PORTAL", "EMAIL", "EMAIL_ASK_PORTAL"})

DIRECTORY_PATH = Path(__file__).resolve().parent / "data" / "carrier_directory.json"

DEFAULT_UNKNOWN_CARRIER = {
    "channel": "EMAIL",
    "notes": "Unregistered carrier; default to email outreach.",
}

# EZLynx carrier directory API.
#
# UNVERIFIED (2026-09-26): the exact CarrierApi path has not been confirmed
# against a live EZLynx tenant. The default follows the repo's proven API
# shapes (/PolicyApi/policy/v1/..., /DocumentApi/documents/v1/..., 
# /DiscussionApi/discussion/v1/...). Override with
# ROBIE_EZLYNX_CARRIER_API_PATH once the live endpoint is confirmed.
# Read-only GET; no credentials are logged.
DEFAULT_EZLYNX_CARRIER_API_PATH = "/CarrierApi/carriers/v1/search"
EZLYNX_CARRIER_API_PATH_ENV = "ROBIE_EZLYNX_CARRIER_API_PATH"
# Set to "1"/"true" to disable EZLynx directory lookup (JSON-only mode).
EZLYNX_CARRIER_LOOKUP_DISABLE_ENV = "ROBIE_EZLYNX_CARRIER_LOOKUP_DISABLED"


@dataclass
class EzlynxCarrierHit:
    """A carrier identity resolved via the EZLynx carrier directory."""

    canonical_name: str
    raw: dict[str, Any]


# Injected EZLynx lookup: (name) -> EzlynxCarrierHit | None.
# Wired at runtime by the worker entrypoint; tests inject a stub.
# None means "no EZLynx client configured" -> JSON fallback.
_ezlynx_lookup_fn: Callable[[str], "EzlynxCarrierHit | None"] | None = None


def set_ezlynx_carrier_lookup(
    fn: Callable[[str], "EzlynxCarrierHit | None"] | None,
) -> None:
    """Inject (or clear) the EZLynx carrier directory lookup function."""
    global _ezlynx_lookup_fn
    _ezlynx_lookup_fn = fn


def _ezlynx_lookup_enabled() -> bool:
    return os.environ.get(EZLYNX_CARRIER_LOOKUP_DISABLE_ENV, "").strip().lower() not in {
        "1",
        "true",
        "yes",
    }


def resolve_carrier_via_ezlynx(name: str) -> EzlynxCarrierHit | None:
    """Resolve a carrier name/abbreviation via the EZLynx carrier directory.

    Returns the canonical directory record on hit, None on miss, on
    unreachable directory, or when no lookup is configured. Never raises —
    callers fall back to the packaged JSON. Never logs credentials.
    """
    clean = str(name or "").strip()
    if not clean:
        return None
    if not _ezlynx_lookup_enabled():
        return None
    fn = _ezlynx_lookup_fn
    if fn is None:
        return None
    try:
        hit = fn(clean)
    except Exception as exc:  # noqa: BLE001 - directory outage must not crash workers
        logger.warning("EZLynx carrier directory lookup failed for %r: %s", clean, exc)
        return None
    if hit is None:
        return None
    canonical = str(getattr(hit, "canonical_name", "") or "").strip()
    if not canonical:
        return None
    logger.info("EZLynx directory resolved carrier %r -> %r", clean, canonical)
    return hit


def build_ezlynx_carrier_lookup(api_client: Any) -> Callable[[str], EzlynxCarrierHit | None]:
    """Build an EZLynx-directory lookup function from an EzlynxApiClient.

    The client must expose ``api_get(path, query=...)`` (see
    :mod:`robie_job_engine.ezlynx_api`). Read-only GET. The API path comes
    from ROBIE_EZLYNX_CARRIER_API_PATH or the documented default.
    """
    path = (
        os.environ.get(EZLYNX_CARRIER_API_PATH_ENV, "").strip()
        or DEFAULT_EZLYNX_CARRIER_API_PATH
    )

    def lookup(name: str) -> EzlynxCarrierHit | None:
        # api_get raises EzlynxApiError on transport/HTTP failure — the
        # caller (resolve_carrier_via_ezlynx) converts that to a safe None.
        payload = api_client.api_get(path, query={"name": name})
        records = _extract_carrier_records(payload)
        if not records:
            return None
        best = _pick_best_record(name, records)
        if best is None:
            return None
        return EzlynxCarrierHit(canonical_name=best, raw={})

    return lookup


def _extract_carrier_records(payload: Any) -> list[dict[str, Any]]:
    """Pull carrier dicts out of an EZLynx directory response (shape-tolerant)."""
    if isinstance(payload, dict):
        data = payload.get("data", payload)
    else:
        data = payload
    if isinstance(data, dict):
        for key in ("carriers", "results", "items"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list):
        return []
    return [r for r in data if isinstance(r, dict)]


def _pick_best_record(name: str, records: list[dict[str, Any]]) -> str | None:
    """Choose the canonical carrier name from directory records.

    Prefers an exact (case-insensitive) name hit, then a record whose name
    contains the query or vice versa. Returns None when nothing matches —
    callers fall back to the packaged JSON fuzzy match.
    """
    lowered = name.casefold()
    candidates: list[str] = []
    for rec in records:
        for key in ("name", "carrierName", "carrier_name", "displayName"):
            val = rec.get(key)
            if isinstance(val, str) and val.strip():
                candidates.append(val.strip())
                break
    for cand in candidates:
        if cand.casefold() == lowered:
            return cand
    for cand in candidates:
        c = cand.casefold()
        if c in lowered or lowered in c:
            return cand
    return None


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


def _route_by_name(name: str, directory: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """JSON fuzzy match (original behavior). Returns None on no match."""
    if name in directory:
        return dict(directory[name])
    lowered = name.casefold()
    for key, config in directory.items():
        key_lowered = key.casefold()
        if key_lowered in lowered or lowered in key_lowered:
            logger.info("carrier %r fuzzy-matched to directory entry %r", name, key)
            return dict(config)
    return None


def route_carrier(carrier_name: str | None) -> dict[str, Any]:
    """Return the channel config for a carrier name.

    Identity resolution order (Carlo's standing rule):
      1. EZLynx carrier directory (source of truth) — when a lookup is
         configured and reachable, its canonical name wins.
      2. Packaged JSON fuzzy match (fallback).
    Unknown or blank names fall back to ``EMAIL`` — never raise for an
    unregistered carrier, so workers can always proceed to email outreach.
    """
    directory = load_channel_directory()
    if not carrier_name or not str(carrier_name).strip():
        return dict(DEFAULT_UNKNOWN_CARRIER)
    name = str(carrier_name).strip()

    # Step 1: EZLynx directory is the source of truth for identity.
    hit = resolve_carrier_via_ezlynx(name)
    if hit is not None:
        routed = _route_by_name(hit.canonical_name, directory)
        if routed is not None:
            return routed
        logger.info(
            "EZLynx-resolved carrier %r has no channel entry; defaulting to EMAIL",
            hit.canonical_name,
        )
        return dict(DEFAULT_UNKNOWN_CARRIER)

    # Step 2: packaged JSON fuzzy match (original behavior).
    routed = _route_by_name(name, directory)
    if routed is not None:
        return routed
    logger.info("carrier %r not in directory; defaulting to EMAIL", name)
    return dict(DEFAULT_UNKNOWN_CARRIER)


def portal_carriers() -> list[str]:
    """Carrier names routed to portal retrieval."""
    return sorted(
        name
        for name, config in load_channel_directory().items()
        if config.get("channel") == "PORTAL"
    )
