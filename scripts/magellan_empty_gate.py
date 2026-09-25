#!/usr/bin/env python3
"""Fail closed when a Magellan accountability extract is an unverified zero.

The 2026-09-25 leadership digest shipped with ``magellan_sad: 0`` in every
department while 17 Sad calls existed for 2026-09-24. The extractor returned
no calls and no error, and ``production_main --publish --deliver`` exited 0.

An empty rendered table disables the next-page control, so the pagination
repair records ``pagination_exhausted`` and ``pages_complete`` without ever
seeing a row. That is an empty-table race, not a quiet day.

A verified zero-call day requires all of:

- ``calls`` is present and empty
- ``source_status`` is ``available``
- ``pages_complete`` is true
- ``older_boundary_reached`` is true (a rendered row is dated before the target)
- ``rows_inspected`` is a positive integer (the table actually painted rows)

Calls with no Sad sentiment are a normal day and are not blocked. Collect-only
runs (neither publish nor deliver) stay soft. ``MAGELLAN_ALLOW_EMPTY=1`` is the
explicit override for publish/deliver, default off.
"""

from __future__ import annotations

EMPTY_MAGELLAN_REASON = (
    "Magellan live extract returned no calls for the target date without a verified "
    "older-date table boundary. Refusing to publish or deliver an empty Magellan risk "
    "section. Set MAGELLAN_ALLOW_EMPTY=1 only for a confirmed zero-call day."
)


class MagellanEmptyExtractError(RuntimeError):
    """Publish/deliver saw an empty Magellan extract that was not a verified zero-call day."""


def magellan_allow_empty(environ=None) -> bool:
    import os

    source = os.environ if environ is None else environ
    return str(source.get("MAGELLAN_ALLOW_EMPTY", "")).strip() == "1"


def magellan_call_count(magellan) -> int:
    if not isinstance(magellan, dict):
        return 0
    calls = magellan.get("calls")
    if isinstance(calls, list):
        return len(calls)
    recorded = magellan.get("records_on_target_date")
    if isinstance(recorded, int) and not isinstance(recorded, bool) and recorded > 0:
        return recorded
    return 0


def magellan_verified_genuine_zero(magellan) -> bool:
    """True only when the table was walked through the target date and had no calls."""
    if not isinstance(magellan, dict):
        return False
    if magellan_call_count(magellan) != 0:
        return False
    if "calls" not in magellan or not isinstance(magellan.get("calls"), list):
        return False
    if str(magellan.get("source_status") or "").casefold() != "available":
        return False
    if magellan.get("pages_complete") is not True:
        return False
    if magellan.get("older_boundary_reached") is not True:
        return False
    rows = magellan.get("rows_inspected")
    if isinstance(rows, bool) or not isinstance(rows, int) or rows <= 0:
        return False
    return True


def empty_magellan_reason(magellan=None) -> str:
    if not isinstance(magellan, dict):
        return EMPTY_MAGELLAN_REASON
    detail = (
        f" source_status={magellan.get('source_status')!s}"
        f" pages_complete={magellan.get('pages_complete')!s}"
        f" rows_inspected={magellan.get('rows_inspected')!s}"
        f" older_boundary_reached={magellan.get('older_boundary_reached')!s}"
        f" pagination_exhausted={magellan.get('pagination_exhausted')!s}"
        f" records_on_target_date={magellan_call_count(magellan)}"
    )
    return EMPTY_MAGELLAN_REASON + detail


def refuse_unverified_empty_magellan(magellan, *, publish, deliver, environ=None) -> None:
    """Raise before publish or deliver when the live extract is an unverified zero."""
    if not publish and not deliver:
        return
    if magellan_allow_empty(environ):
        return
    if magellan_call_count(magellan) > 0:
        return
    if magellan_verified_genuine_zero(magellan):
        return
    raise MagellanEmptyExtractError(empty_magellan_reason(magellan))


def snapshot_magellan_blocks_delivery(departments, environ=None) -> bool:
    """True when a prepared snapshot must not be published or delivered.

    Non-empty Sad rows, or a stamped positive call count, are enough to allow
    delivery. A stamped verified zero (table walked past the target date) is
    also allowed. A snapshot with no Sad rows and no such stamp is the
    fail-open shape from before this gate.
    """
    if magellan_allow_empty(environ):
        return False
    if not isinstance(departments, dict) or not departments:
        return True
    sad_rows = 0
    saw_call_count = False
    any_calls = False
    all_verified_empty = True
    saw_verified_flag = False
    for data in departments.values():
        if not isinstance(data, dict):
            return True
        sad = data.get("magellan_sad")
        if not isinstance(sad, list):
            return True
        sad_rows += len(sad)
        if "magellan_calls_on_target_date" in data:
            saw_call_count = True
            count = data.get("magellan_calls_on_target_date")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                return True
            if count > 0:
                any_calls = True
        else:
            all_verified_empty = False
        if "magellan_extract_verified_empty" in data:
            saw_verified_flag = True
            if data.get("magellan_extract_verified_empty") is not True:
                all_verified_empty = False
        else:
            all_verified_empty = False
    if sad_rows > 0 or any_calls:
        return False
    if saw_call_count and saw_verified_flag and all_verified_empty:
        return False
    return True
