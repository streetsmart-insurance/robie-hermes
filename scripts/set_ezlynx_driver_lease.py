#!/usr/bin/env python3
"""Prepare the shared EZLynx driver lease from a project-info describe.

The describe document is the stdout of
``gcloud compute project-info describe --format=json``. A read that is not
that document fails closed. A missing ``robie-ezlynx-driver`` key is an empty
lease, so the first check-in can proceed. Check-in refuses when another
holder is already IN. Check-out is how a malformed lease is replaced.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import sys

METADATA_KEY = "robie-ezlynx-driver"
READ_FAILED = "could not read robie-ezlynx-driver metadata"
MALFORMED = "existing driver metadata is malformed; check OUT before replacing it"
OTHER_IN = "another environment is IN; check it OUT through Moe before switching"


def lease_value_from_describe(payload: object) -> str:
    """Return the lease string, or empty when the key is absent.

    Anything that is not a project-info describe fails closed.
    """
    if not isinstance(payload, dict):
        raise SystemExit(READ_FAILED)
    meta = payload.get("commonInstanceMetadata")
    if not isinstance(meta, dict):
        raise SystemExit(READ_FAILED)
    items = meta.get("items") or []
    if not isinstance(items, list):
        raise SystemExit(READ_FAILED)
    for item in items:
        if not isinstance(item, dict) or item.get("key") != METADATA_KEY:
            continue
        value = item.get("value")
        if not isinstance(value, str):
            raise SystemExit(MALFORMED)
        return value
    return ""


def refuse_other_holder(raw: str, holder: str) -> None:
    """Check-in only. An empty lease is the first check-in."""
    if not raw.strip():
        return
    try:
        current = json.loads(raw)
    except ValueError:
        raise SystemExit(MALFORMED) from None
    if not isinstance(current, dict):
        raise SystemExit(MALFORMED)
    if current.get("state") == "IN" and current.get("holder") != holder:
        raise SystemExit(OTHER_IN)


def build_lease(
    *,
    action: str,
    holder: str,
    ttl_minutes: int,
    actor: str,
    now: datetime.datetime | None = None,
) -> dict[str, object]:
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    checking_in = action == "check-in"
    expires = moment if not checking_in else moment + datetime.timedelta(minutes=ttl_minutes)
    return {
        "version": 1,
        "state": "IN" if checking_in else "OUT",
        "holder": holder if checking_in else "NONE",
        "expires_at": expires.isoformat().replace("+00:00", "Z"),
        "checked_in_by": actor,
        "updated_at": moment.isoformat().replace("+00:00", "Z"),
    }


def prepare_lease(describe_text: str, *, action: str, holder: str, ttl_minutes: int, actor: str) -> dict[str, object]:
    try:
        payload = json.loads(describe_text)
    except ValueError:
        raise SystemExit(READ_FAILED) from None
    raw = lease_value_from_describe(payload)
    if action == "check-in":
        refuse_other_holder(raw, holder)
    return build_lease(action=action, holder=holder, ttl_minutes=ttl_minutes, actor=actor)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--describe-file", required=True, type=pathlib.Path)
    parser.add_argument("--lease-file", required=True, type=pathlib.Path)
    args = parser.parse_args(argv)
    action = os.environ["ACTION"]
    if action not in {"check-in", "check-out"}:
        raise SystemExit(f"unknown driver action {action}")
    lease = prepare_lease(
        args.describe_file.read_text(encoding="utf-8"),
        action=action,
        holder=os.environ["HOLDER"],
        ttl_minutes=int(os.environ["TTL_MINUTES"]),
        actor=os.environ["ACTOR"],
    )
    args.lease_file.write_text(json.dumps(lease, separators=(",", ":")), encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit as exc:
        if exc.code not in (0, None):
            print(exc.code, file=sys.stderr)
        raise
