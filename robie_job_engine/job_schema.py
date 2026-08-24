"""Registered schemas for bounded Job types.

Intake must match COMPLETE: a missing or unregistered schema cannot start
an external action.
"""

from __future__ import annotations

from typing import Any

from .request_routing import BOUNDED_ENGINE_ACTIONS


BOUNDED_JOB_SCHEMAS: dict[str, dict[str, Any]] = {
    "carrier.proposal": {
        "schema_verified": True,
        "required": (),
        "identity": ("proposal_id",),
    },
    "browser.read": {
        "schema_verified": True,
        "required": (),
        "identity": ("locator", "url"),
    },
    "ezlynx.reassign": {
        "schema_verified": True,
        "required": (),
        "identity": ("resource_id",),
    },
    "ezlynx.move_document": {
        "schema_verified": True,
        "required": (),
        "identity": ("resource_id",),
    },
    "ezlynx.apply_label": {
        "schema_verified": True,
        "required": (),
        "identity": ("resource_id",),
    },
}


def get_bounded_job_schema(action_type: str) -> dict[str, Any] | None:
    return BOUNDED_JOB_SCHEMAS.get(action_type)


def bounded_schema_hold_reason(
    action_type: str,
    payload: dict[str, Any] | None = None,
) -> str | None:
    """Return a hold reason if a bounded Job must not start an action."""
    if action_type not in BOUNDED_ENGINE_ACTIONS:
        return None
    payload = dict(payload or {})
    if payload.get("missing_schema"):
        return "missing_schema"
    spec = get_bounded_job_schema(action_type)
    if spec is None or not spec.get("schema_verified"):
        return f"unregistered or unverified schema for {action_type}"
    for field in spec.get("required") or ():
        if not payload.get(field):
            return f"missing required schema field: {field}"
    return None
