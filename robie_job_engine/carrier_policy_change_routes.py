"""Carrier policy-change routing table (phase 2 of the 4359 program).

The table maps carriers to where their policy changes go (email / portal /
phone), curated from Carlo's EZLynx company-directory deep read of
2026-09-27 (all 206 records). Shipped data file:

    robie_job_engine/data/carrier_policy_change_routes.json

Refresh path (no code changes): when Nicole's directory fill-in adds new
policy-change contacts, re-run tools/extract_carrier_policy_change_routes.py
-> tools/draft_carrier_routes.py -> tools/curate_carrier_routes.py, review
the diff, and ship the regenerated JSON.

Carrier-name matching: the 4359 report's "Master Company" (e.g. "Merchants
Insurance Group") is normalized and matched against directory record names.
Matching is conservative: exact normalized match first, then a token-overlap
score with a high bar. Below the bar the carrier is UNRESOLVED — the change
goes to the awaiting-directory queue and is never contacted. A wrong-carrier
email would send client policy data to the wrong company; the battery tests
pin this.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROUTING_TABLE_FILENAME = "carrier_policy_change_routes.json"
DEFAULT_ROUTING_TABLE = str(
    Path(__file__).with_name("data") / ROUTING_TABLE_FILENAME
)

# Suffixes that carry no signal when matching "Master Company" to a
# directory record name.
_NAME_SUFFIXES = (
    "insurance", "ins", "company", "co", "group", "corporation", "corp",
    "incorporated", "inc", "limited", "ltd", "llc", "rrg", "mga",
    "agency", "services", "service", "general", "mutual",
    "center", "centre",
)

# Manual aliases: 4359 "Master Company" value -> normalized directory name
# fragment. Used only when normalization alone cannot match. Empty unless a
# real mismatch is proven by a battery test.
CARRIER_ALIASES: dict[str, str] = {
}

# Token-overlap bar for fuzzy matches. Deliberately high: a wrong-carrier
# email is worse than a missed one (misses surface in the queue report).
_FUZZY_MIN_SCORE = 0.72


def default_routing_table_path() -> str:
    override = os.environ.get("ROBIE_4359_CARRIER_ROUTES", "").strip()
    if override:
        return override
    return DEFAULT_ROUTING_TABLE


def normalize_carrier_name(name: Any) -> str:
    """Lowercase, strip punctuation/suffixes, collapse whitespace."""
    text = str(name or "").casefold()
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    tokens = [t for t in text.split() if t not in _NAME_SUFFIXES]
    return " ".join(tokens)


def load_routing_table(path: str | Path | None = None) -> dict[str, Any]:
    """Load and validate the routing table. Raises ValueError on bad data."""
    table_path = Path(str(path or default_routing_table_path())).expanduser()
    try:
        data = json.loads(table_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot load carrier routing table at {table_path}: {exc}") from exc
    carriers = data.get("carriers")
    if not isinstance(carriers, list) or not carriers:
        raise ValueError(f"carrier routing table at {table_path} has no carriers")
    email_re = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
    statuses = {"ok", "manual", "missing"}
    route_types = {"email", "portal", "phone", "fax"}
    for carrier in carriers:
        status = carrier.get("route_status")
        if status not in statuses:
            raise ValueError(
                f"carrier {carrier.get('record_id')}: bad route_status {status!r}")
        routes = carrier.get("routes") or []
        for route in routes:
            if route.get("type") not in route_types:
                raise ValueError(
                    f"carrier {carrier.get('record_id')}: bad route type "
                    f"{route.get('type')!r}")
            if route.get("type") == "email":
                addr = str(route.get("email") or "")
                if not email_re.match(addr):
                    raise ValueError(
                        f"carrier {carrier.get('record_id')}: bad policy-change email {addr!r}"
                    )
    by_record = {str(c.get("record_id")): c for c in carriers}
    if len(by_record) != len(carriers):
        raise ValueError("carrier routing table has duplicate record_ids")
    return {"path": str(table_path), "carriers": carriers, "by_record": by_record}


def _token_score(a: str, b: str) -> float:
    """Overlap of the smaller token set over the larger (0..1)."""
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(len(ta), len(tb))


def resolve_carrier(master_company: Any, table: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve a 4359 "Master Company" to a directory carrier record.

    Returns the carrier dict, or None when there is no confident match.
    Never guesses: ambiguous or weak matches return None.
    """
    raw = str(master_company or "").strip()
    if not raw:
        return None
    carriers: list[dict[str, Any]] = table.get("carriers") or []
    norm = normalize_carrier_name(raw)
    alias = CARRIER_ALIASES.get(norm)
    if alias:
        norm = alias
    if not norm:
        return None
    # 1) exact normalized match
    for carrier in carriers:
        if normalize_carrier_name(carrier.get("name")) == norm:
            return carrier
    # 2) one token set contains the other ("merchants" vs "merchants group")
    contained = [
        c for c in carriers
        if (norm and normalize_carrier_name(c.get("name")).startswith(norm))
        or (normalize_carrier_name(c.get("name")) and norm.startswith(normalize_carrier_name(c.get("name"))))
    ]
    if len(contained) == 1:
        return contained[0]
    if len(contained) > 1:
        return None  # ambiguous — never guess
    # 3) fuzzy token overlap, high bar, unique winner only
    scored = sorted(
        ((_token_score(norm, normalize_carrier_name(c.get("name"))), c)
         for c in carriers),
        key=lambda pair: pair[0], reverse=True,
    )
    if scored and scored[0][0] >= _FUZZY_MIN_SCORE:
        if len(scored) == 1 or scored[1][0] < scored[0][0] - 0.05:
            return scored[0][1]
    return None


def emailable_routes(carrier: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Email-type routes for a resolved carrier (worker can send these)."""
    if not carrier:
        return []
    return [r for r in (carrier.get("routes") or []) if r.get("type") == "email" and r.get("email")]


def manual_routes(carrier: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Portal/phone/fax routes: the worker cannot email these; they surface in
    the manual-action queue for an agent instead."""
    if not carrier:
        return []
    return [r for r in (carrier.get("routes") or [])
            if r.get("type") in ("portal", "phone", "fax")]
