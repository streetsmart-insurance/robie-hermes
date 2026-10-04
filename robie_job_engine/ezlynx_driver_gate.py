"""Cross-VM ownership gate for the shared SSRobie EZLynx login.

Production and Test currently use the same EZLynx user. A login on one host
invalidates the other host's session, so a local file lock is not enough. The
coordinator stores one short-lived lease in GCP project common metadata. Both
VMs can read that lease through the metadata server without credentials.

Known ROBIE hosts fail closed. Developer and CI hosts do not require the gate
unless ROBIE_EZLYNX_DRIVER_GATE_REQUIRED=1 is set.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable
from urllib.request import Request, urlopen


METADATA_URL = (
    "http://metadata.google.internal/computeMetadata/v1/project/attributes/"
    "robie-ezlynx-driver"
)
KNOWN_HOLDERS = {
    "hermes-poc-01": "PRODUCTION",
    "hermes-test-01": "TEST",
}
REFUSED = "EZLYNX_DRIVER_NOT_IN"
LEASE_NOT_WITH_PRODUCTION = "driver lease not with PRODUCTION"


class EzlynxDriverGateRefused(RuntimeError):
    pass


@dataclass(frozen=True)
class DriverDecision:
    allowed: bool
    holder: str | None
    reason: str


def _truthy(value: str) -> bool:
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def expected_holder(hostname: str | None = None) -> str | None:
    override = os.environ.get("ROBIE_EZLYNX_DRIVER_HOLDER", "").strip().upper()
    if override:
        return override
    short = (hostname or socket.gethostname()).split(".", 1)[0].casefold()
    return KNOWN_HOLDERS.get(short)


def gate_required(hostname: str | None = None) -> bool:
    override = os.environ.get("ROBIE_EZLYNX_DRIVER_GATE_REQUIRED")
    if override is not None:
        return _truthy(override)
    return expected_holder(hostname) is not None


def read_metadata(timeout_seconds: float = 2.0) -> str:
    request = Request(METADATA_URL, headers={"Metadata-Flavor": "Google"})
    with urlopen(request, timeout=timeout_seconds) as response:
        return response.read().decode("utf-8")


def _parse_expiry(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("missing expiry")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("expiry must include timezone")
    return parsed.astimezone(timezone.utc)


def check_driver_gate(
    *,
    hostname: str | None = None,
    now: datetime | None = None,
    reader: Callable[[], str] | None = None,
) -> DriverDecision:
    expected = expected_holder(hostname)
    if not gate_required(hostname):
        return DriverDecision(True, expected, "gate not required on this host")
    if expected not in set(KNOWN_HOLDERS.values()):
        return DriverDecision(False, expected, "unknown expected holder")
    try:
        payload = json.loads((reader or read_metadata)())
        if payload.get("version") != 1:
            raise ValueError("unsupported lease version")
        state = str(payload.get("state") or "").strip().upper()
        actual = str(payload.get("holder") or "").strip().upper()
        expires_at = _parse_expiry(payload.get("expires_at"))
    except Exception:  # noqa: BLE001 - malformed/unavailable state must fail closed
        return DriverDecision(False, expected, "shared driver lease unavailable")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if state != "IN":
        return DriverDecision(False, expected, "driver is checked OUT")
    if actual != expected:
        return DriverDecision(False, expected, f"driver belongs to {actual or 'nobody'}")
    if expires_at <= current:
        return DriverDecision(False, expected, "driver lease expired")
    return DriverDecision(True, expected, "driver is IN")


def production_driver_refused(
    *,
    hostname: str | None = None,
    now: datetime | None = None,
    reader: Callable[[], str] | None = None,
) -> bool:
    """True when this process must hold PRODUCTION and the lease does not.

    CI hosts leave the gate off, so this stays false there. A missing,
    expired, or TEST-held lease is a refusal: nothing should be dialed
    or written until PRODUCTION holds it again.
    """
    if not gate_required(hostname):
        return False
    if expected_holder(hostname) != "PRODUCTION":
        return False
    decision = check_driver_gate(hostname=hostname, now=now, reader=reader)
    return not decision.allowed


def require_driver_in(**kwargs: object) -> DriverDecision:
    decision = check_driver_gate(**kwargs)
    if not decision.allowed:
        raise EzlynxDriverGateRefused(f"{REFUSED}: {decision.reason}")
    return decision


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("check",), nargs="?", default="check")
    args = parser.parse_args(argv)
    del args
    decision = check_driver_gate()
    status = "ALLOWED" if decision.allowed else "REFUSED"
    print(f"{status} holder={decision.holder or 'UNMANAGED'} reason={decision.reason}")
    return 0 if decision.allowed else 1


if __name__ == "__main__":
    raise SystemExit(main())
