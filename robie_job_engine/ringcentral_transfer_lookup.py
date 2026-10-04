"""Map a client's assigned producer to a RingCentral DID.

The directory is the static staff file, keyed by name. A live RingCentral
read is optional, off by default, and never supplies a number the static
file does not already list. No match means no transfer. There is no
fallback person and no fallback number.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from .ezlynx_applicant_phone import to_e164_us

logger = logging.getLogger(__name__)

LIVE_CHECK_ENV = "ROBIE_RINGCENTRAL_LIVE_CHECK"
SECRET_CLIENT_ID = "ringcentral-accountability-client-id"
SECRET_CLIENT_SECRET = "ringcentral-accountability-client-secret"
SECRET_JWT = "ringcentral-accountability-jwt"
SECRET_SERVER_URL = "ringcentral-accountability-server-url"
_DIRECTORY_PATH = Path(__file__).resolve().parent / "staff_direct_dials.json"


def _norm_name(name: str) -> str:
    return " ".join(str(name or "").casefold().split())


def load_staff_directory(path: Path | None = None) -> dict[str, dict[str, str]]:
    """Staff keyed by normalized name. DID values are validated E.164."""
    payload = json.loads((path or _DIRECTORY_PATH).read_text(encoding="utf-8"))
    staff = payload.get("staff") if isinstance(payload, dict) else None
    if not isinstance(staff, dict):
        raise ValueError("staff directory is missing a staff object")
    directory: dict[str, dict[str, str]] = {}
    for name, row in staff.items():
        if not isinstance(row, dict):
            logger.warning("skipping staff row %s; not an object", name)
            continue
        did = to_e164_us(row.get("did"))
        if not did:
            logger.warning("skipping staff row %s; DID is not E.164", name)
            continue
        directory[_norm_name(name)] = {
            "name": str(name),
            "did": did,
            "extension": str(row.get("extension") or ""),
            "department": str(row.get("department") or ""),
        }
    return directory


class RingCentralTransferLookup:
    """TransferLookupPort. Returns a DID only when the producer is in the directory."""

    def __init__(self, directory: Mapping[str, Mapping[str, str]] | None = None):
        self._directory = dict(directory if directory is not None else load_staff_directory())

    def get_transfer_number(self, assignee_name: str) -> Optional[str]:
        key = _norm_name(assignee_name)
        if not key:
            logger.info("transfer lookup missed: no producer name")
            return None
        row = self._directory.get(key)
        if not row:
            logger.info("transfer lookup missed for %s; no transfer", assignee_name.strip())
            return None
        did = to_e164_us(row.get("did"))
        if not did:
            logger.info("transfer lookup missed for %s; directory DID is not dialable", assignee_name.strip())
            return None
        logger.info(
            "transfer lookup matched %s extension %s",
            row.get("name") or assignee_name.strip(),
            row.get("extension") or "n/a",
        )
        return did


def live_check_enabled(env: Mapping[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    return source.get(LIVE_CHECK_ENV) == "1"


def live_directory_mismatches(
    directory: Mapping[str, Mapping[str, str]],
    records: list[Mapping[str, Any]],
) -> list[str]:
    """Names whose live DID differs from the static file. Numbers are not included."""
    mismatches: list[str] = []
    for record in records:
        name = str(record.get("name") or "").strip()
        row = directory.get(_norm_name(name))
        if row is None:
            continue
        live = to_e164_us(record.get("did"))
        if live and live != row.get("did"):
            mismatches.append(name)
    return mismatches


def run_live_check(
    directory: Mapping[str, Mapping[str, str]],
    *,
    env: Mapping[str, str] | None = None,
    secret_reader: Callable[[str], str] | None = None,
    fetch_records: Callable[[], list[Mapping[str, Any]]] | None = None,
) -> list[str]:
    """Read-only comparison. Does nothing unless the live-check flag is on."""
    if not live_check_enabled(env):
        return []
    if secret_reader is None or fetch_records is None:
        logger.warning("RingCentral live check skipped: reader is not configured")
        return []
    for name in (SECRET_CLIENT_ID, SECRET_CLIENT_SECRET, SECRET_JWT, SECRET_SERVER_URL):
        secret_reader(name)
    mismatches = live_directory_mismatches(directory, fetch_records())
    if mismatches:
        logger.warning(
            "RingCentral live check differs for %s",
            ", ".join(sorted(mismatches)),
        )
    else:
        logger.info("RingCentral live check matched the static directory")
    return mismatches
