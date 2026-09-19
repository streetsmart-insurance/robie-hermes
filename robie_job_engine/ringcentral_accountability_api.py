"""Fail-closed RingCentral API evidence collection for accountability reports."""

from __future__ import annotations

import hashlib
import json
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from .ringcentral_client import RingCentralClient


class RingCentralApiEvidenceError(RuntimeError):
    """The API response was unavailable, incomplete, or failed reconciliation."""


def _fold(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _request_json(client: RingCentralClient, url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=client._get_headers(), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise RingCentralApiEvidenceError(
            f"RingCentral API request failed: {type(exc).__name__}"
        ) from exc
    if not isinstance(payload, dict):
        raise RingCentralApiEvidenceError("RingCentral API returned a non-object response")
    return payload


def _all_pages(client: RingCentralClient, url: str) -> tuple[list[dict[str, Any]], int]:
    records: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    pages = 0
    next_url: str | None = url
    while next_url:
        if next_url in seen_urls:
            raise RingCentralApiEvidenceError("RingCentral pagination loop detected")
        if pages >= 1000:
            raise RingCentralApiEvidenceError("RingCentral pagination exceeded the safety limit")
        seen_urls.add(next_url)
        payload = _request_json(client, next_url)
        page_records = payload.get("records")
        if not isinstance(page_records, list):
            raise RingCentralApiEvidenceError("RingCentral page is missing its records array")
        records.extend(item for item in page_records if isinstance(item, dict))
        pages += 1
        navigation = payload.get("navigation") or {}
        candidate = (navigation.get("nextPage") or {}).get("uri")
        next_url = str(candidate) if candidate else None
    return records, pages


def _party(record: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = record.get(key) or {}
    return value if isinstance(value, Mapping) else {}


def _name(party: Mapping[str, Any]) -> str:
    return str(party.get("name") or party.get("extensionNumber") or "").strip()


def _number(party: Mapping[str, Any]) -> str:
    return str(party.get("phoneNumber") or party.get("extensionNumber") or "").strip()


def _call_rows(
    records: Sequence[Mapping[str, Any]],
    *,
    extension_names: Mapping[str, str],
    required_queues: Sequence[str],
) -> list[dict[str, str]]:
    queues = {_fold(value): str(value).strip() for value in required_queues}
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, ...]] = set()
    for record in records:
        session_id = str(record.get("sessionId") or record.get("id") or "").strip()
        if not session_id:
            raise RingCentralApiEvidenceError("RingCentral call record lacks a session identifier")
        legs = record.get("legs")
        items = legs if isinstance(legs, list) and legs else [record]
        for index, raw_leg in enumerate(items):
            if not isinstance(raw_leg, Mapping):
                raise RingCentralApiEvidenceError("RingCentral detailed call contains an invalid leg")
            source = _party(raw_leg, "from")
            destination = _party(raw_leg, "to")
            extension = raw_leg.get("extension") or {}
            extension_id = str(extension.get("id") or extension.get("extensionNumber") or "").strip() if isinstance(extension, Mapping) else ""
            extension_name = extension_names.get(extension_id, "")
            candidates = (_name(source), _name(destination), extension_name)
            queue = next(
                (queues[_fold(candidate)] for candidate in candidates if _fold(candidate) in queues),
                "",
            )
            direction = str(raw_leg.get("direction") or record.get("direction") or "").strip()
            employee = extension_name
            if not employee:
                employee = _name(destination) if direction.casefold().startswith("in") else _name(source)
            result = str(raw_leg.get("result") or raw_leg.get("action") or "").strip()
            if result.casefold() == "accepted":
                result = "Call connected"
            duration = int(raw_leg.get("duration") or 0)
            row = {
                "Session Id": session_id,
                "Call ID": f"{session_id}:{index}",
                "From Name": _name(source),
                "From Number": _number(source),
                "To Name": _name(destination) or employee,
                "To Number": _number(destination),
                "Result": result,
                "Call Length": str(duration),
                "Handle Time": str(duration),
                "Call Start Time": str(raw_leg.get("startTime") or record.get("startTime") or ""),
                "Call Direction": direction,
                "Queue": queue,
                "Extension ID": extension_id,
            }
            required = ("Session Id", "From Number", "To Number", "Result", "Call Start Time", "Call Direction")
            if any(not row[key] for key in required):
                raise RingCentralApiEvidenceError(
                    "RingCentral detailed call leg is missing a required field"
                )
            fingerprint = tuple(row[key] for key in sorted(row))
            if fingerprint not in seen:
                seen.add(fingerprint)
                rows.append(row)
    return rows


def collect_ringcentral_api_evidence(
    client: RingCentralClient,
    *,
    output_path: Path,
    report_kind: str,
    target_date: date,
    required_users: Sequence[str],
    required_queues: Sequence[str],
    required_queue_members: Mapping[str, Sequence[str]],
    timezone_name: str = "America/New_York",
) -> Path:
    """Collect a complete, bounded, read-only daily API snapshot."""
    if report_kind != "daily":
        raise RingCentralApiEvidenceError(
            "RingCentral API evidence is enabled for daily reports only until weekly analytics reconciliation is certified"
        )
    if not client.is_configured():
        raise RingCentralApiEvidenceError("RingCentral API credentials are not configured")
    if not required_users or not required_queues:
        raise RingCentralApiEvidenceError("current RingCentral users and queues must be explicitly configured")

    zone = ZoneInfo(timezone_name)
    start = datetime.combine(target_date, time.min, tzinfo=zone).astimezone(timezone.utc)
    end = datetime.combine(target_date + timedelta(days=1), time.min, tzinfo=zone).astimezone(timezone.utc)

    extension_url = (
        f"{client.server_url}/restapi/v1.0/account/~/extension?"
        + urllib.parse.urlencode({"status": "Enabled", "perPage": "250"})
    )
    extension_records, extension_pages = _all_pages(client, extension_url)
    extension_names: dict[str, str] = {}
    extension_numbers: dict[str, str] = {}
    for record in extension_records:
        name = str(record.get("name") or "").strip()
        identifier = str(record.get("id") or "").strip()
        number = str(record.get("extensionNumber") or "").strip()
        if name and identifier:
            extension_names[identifier] = name
        if name and number:
            extension_names[number] = name
            extension_numbers[name] = number

    actual_users = {_fold(value) for value in extension_names.values()}
    missing_users = [value for value in required_users if _fold(value) not in actual_users]
    missing_queues = [value for value in required_queues if _fold(value) not in actual_users]
    if missing_users or missing_queues:
        details = []
        if missing_users:
            details.append("missing current users: " + ", ".join(missing_users))
        if missing_queues:
            details.append("missing current queues: " + ", ".join(missing_queues))
        raise RingCentralApiEvidenceError("; ".join(details))

    known_users = {_fold(value) for value in required_users}
    missing_rosters = [queue for queue in required_queues if _fold(queue) not in {_fold(key) for key in required_queue_members}]
    unknown_members = [
        f"{queue}: {member}"
        for queue, members in required_queue_members.items()
        for member in members
        if _fold(member) not in known_users
    ]
    if missing_rosters or unknown_members:
        raise RingCentralApiEvidenceError("RingCentral queue membership configuration is incomplete")

    call_url = (
        f"{client.server_url}/restapi/v1.0/account/~/call-log?"
        + urllib.parse.urlencode({
            "dateFrom": start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "dateTo": end.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "view": "Detailed",
            "type": "Voice",
            "perPage": "250",
        })
    )
    call_records, call_pages = _all_pages(client, call_url)
    rows = _call_rows(
        call_records,
        extension_names=extension_names,
        required_queues=required_queues,
    )
    if not rows:
        raise RingCentralApiEvidenceError("RingCentral API returned no detailed call legs for the target date")

    payload: dict[str, Any] = {
        "schema_version": 1,
        "source_type": "ringcentral_api",
        "report_kind": report_kind,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "target_date": target_date.isoformat(),
        "window": {"date_from": start.isoformat(), "date_to": end.isoformat()},
        "complete": True,
        "pages": {"extensions": extension_pages, "calls": call_pages},
        "record_counts": {"extensions": len(extension_records), "sessions": len(call_records), "legs": len(rows)},
        "required_users": list(required_users),
        "required_queues": list(required_queues),
        "required_queue_members": {str(key): list(value) for key, value in required_queue_members.items()},
        "users": [{"Name": name, "Ext": extension_numbers.get(name, "")} for name in required_users],
        "queues": [{"Name": name, "Ext": extension_numbers.get(name, "")} for name in required_queues],
        "calls": rows,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload["evidence_sha256"] = hashlib.sha256(canonical).hexdigest()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return output_path
