"""Fail-closed Gmail attachment intake for accountability evidence.

This collector is intentionally vendor-neutral.  A source is accepted only when
the manifest supplies an exact sender allowlist, an explicit subject marker,
an explicit filename marker, an allowed format, and a minimal schema.  It does
not infer a vendor or report type from a friendly filename.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
MAX_EVIDENCE_BYTES = 20 * 1024 * 1024
ALLOWED_SOURCE_KEYS = {
    "tasks",
    "activities",
    "sales",
    "sales_json",
    "retention",
    "retention_summary_json",
    "submissions",
    "magellan_json",
    "monthly_kpis_json",
    "churn_json",
}


class EvidenceEmailError(ValueError):
    """The mailbox did not contain authoritative configured evidence."""


def build_keyless_evidence_mailbox_service(service_account_email: str, mailbox: str) -> Any:
    """Build a keyless delegated Gmail client for the evidence mailbox."""
    import google.auth
    from google.auth import iam
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    source, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    signer = iam.Signer(Request(), source, service_account_email)
    delegated = service_account.Credentials(
        signer=signer,
        service_account_email=service_account_email,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=[GMAIL_READONLY_SCOPE],
        subject=mailbox,
    )
    return build("gmail", "v1", credentials=delegated, cache_discovery=False)


def _headers(payload: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(item.get("name") or "").casefold(): str(item.get("value") or "")
        for item in payload.get("headers", []) or []
    }


def _address(value: str) -> str:
    match = re.search(r"<([^<>]+)>", str(value or ""))
    return (match.group(1) if match else str(value or "")).strip().casefold()


def _parts(part: Mapping[str, Any]):
    for child in part.get("parts", []) or []:
        yield child
        yield from _parts(child)


def _decode_attachment(service: Any, message_id: str, part: Mapping[str, Any]) -> bytes:
    body = dict(part.get("body") or {})
    attachment_id = body.get("attachmentId")
    if attachment_id:
        response = service.users().messages().attachments().get(
            userId="me", messageId=message_id, id=attachment_id
        ).execute()
        encoded = response.get("data", "")
    else:
        encoded = body.get("data", "")
    if not encoded:
        raise EvidenceEmailError("configured evidence attachment has no data")
    try:
        content = base64.urlsafe_b64decode(str(encoded) + "===")
    except Exception as exc:
        raise EvidenceEmailError("configured evidence attachment is not valid base64") from exc
    if not content or len(content) > MAX_EVIDENCE_BYTES:
        raise EvidenceEmailError("configured evidence attachment size is outside the allowed range")
    return content


def _normalized_columns(values: Sequence[str]) -> set[str]:
    return {re.sub(r"[^a-z0-9]+", "", str(value).casefold()) for value in values if str(value).strip()}


def _validate_content(filename: str, content: bytes, spec: Mapping[str, Any]) -> str:
    suffix = Path(filename).suffix.casefold()
    extensions = {str(value).casefold() for value in spec.get("extensions", []) or []}
    if suffix not in extensions or suffix not in {".csv", ".json"}:
        raise EvidenceEmailError("configured evidence attachment has a refused file format")
    if suffix == ".csv":
        if b"\x00" in content:
            raise EvidenceEmailError("configured CSV evidence contains null bytes")
        try:
            text = content.decode("utf-8-sig")
            columns = next(csv.reader(io.StringIO(text)))
        except (UnicodeDecodeError, StopIteration, csv.Error) as exc:
            raise EvidenceEmailError("configured CSV evidence is unreadable") from exc
        required = _normalized_columns(spec.get("required_columns", []) or [])
        if not required:
            raise EvidenceEmailError("configured CSV evidence requires an explicit column schema")
        missing = required - _normalized_columns(columns)
        if missing:
            raise EvidenceEmailError("configured CSV evidence is missing required columns")
    else:
        try:
            data = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EvidenceEmailError("configured JSON evidence is unreadable") from exc
        if not isinstance(data, dict):
            raise EvidenceEmailError("configured JSON evidence must be an object")
        required_keys = {str(value) for value in spec.get("required_json_keys", []) or []}
        if not required_keys:
            raise EvidenceEmailError("configured JSON evidence requires explicit top-level keys")
        if not required_keys.issubset(data):
            raise EvidenceEmailError("configured JSON evidence is missing required top-level keys")
    return suffix


def _validated_spec(source: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    if source not in ALLOWED_SOURCE_KEYS:
        raise EvidenceEmailError(f"unsupported accountability evidence source: {source}")
    spec = dict(raw)
    senders = {_address(value) for value in spec.get("sender_allowlist", []) or [] if _address(value)}
    subjects = [str(value).strip().casefold() for value in spec.get("subject_markers", []) or [] if str(value).strip()]
    filenames = [str(value).strip().casefold() for value in spec.get("filename_markers", []) or [] if str(value).strip()]
    if not senders or not subjects or not filenames:
        raise EvidenceEmailError(
            f"{source} evidence requires sender_allowlist, subject_markers, and filename_markers"
        )
    spec["sender_allowlist"] = senders
    spec["subject_markers"] = subjects
    spec["filename_markers"] = filenames
    spec["extensions"] = [str(value).casefold() for value in spec.get("extensions", []) or []]
    return spec


def collect_evidence_attachments(
    service: Any,
    *,
    output_dir: Path,
    source_specs: Mapping[str, Mapping[str, Any]],
    as_of: datetime | None = None,
    max_messages: int = 100,
) -> tuple[dict[str, str], Path]:
    """Collect one newest unambiguous attachment for every configured source."""
    if not source_specs:
        raise EvidenceEmailError("no accountability evidence email sources are configured")
    specs = {source: _validated_spec(source, spec) for source, spec in source_specs.items()}
    now = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)
    response = service.users().messages().list(
        userId="me", q="has:attachment newer_than:14d -in:spam -in:trash", maxResults=max(1, min(max_messages, 500))
    ).execute()
    candidates: dict[str, list[dict[str, Any]]] = {source: [] for source in specs}
    for item in response.get("messages", []) or []:
        message_id = str(item.get("id") or "")
        if not message_id:
            continue
        message = service.users().messages().get(userId="me", id=message_id, format="full").execute()
        received = datetime.fromtimestamp(int(message.get("internalDate") or 0) / 1000, tz=timezone.utc)
        payload = dict(message.get("payload") or {})
        headers = _headers(payload)
        sender = _address(headers.get("from", ""))
        subject = headers.get("subject", "").casefold()
        authentication = headers.get("authentication-results", "").casefold()
        for source, spec in specs.items():
            max_age = max(1, int(spec.get("max_age_hours") or 36))
            age_hours = (now - received).total_seconds() / 3600
            if age_hours < 0 or age_hours > max_age:
                continue
            if sender not in spec["sender_allowlist"]:
                continue
            sender_domain = re.escape(sender.rsplit("@", 1)[-1])
            if "dmarc=pass" not in authentication or not re.search(
                rf"header\.from\s*=\s*{sender_domain}(?:\s|;|$)", authentication
            ):
                continue
            if not any(marker in subject for marker in spec["subject_markers"]):
                continue
            for part in _parts(payload):
                filename = str(part.get("filename") or "")
                folded = filename.casefold()
                if not any(marker in folded for marker in spec["filename_markers"]):
                    continue
                content = _decode_attachment(service, message_id, part)
                suffix = _validate_content(filename, content, spec)
                candidates[source].append({
                    "content": content,
                    "suffix": suffix,
                    "received_at": received,
                    "message_id": message_id,
                    "sender": sender,
                })

    output_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}
    receipts: list[dict[str, Any]] = []
    for source, found in candidates.items():
        if not found:
            raise EvidenceEmailError(f"no fresh explicitly configured {source} evidence attachment was found")
        found.sort(key=lambda value: value["received_at"], reverse=True)
        newest = found[0]["received_at"]
        newest_items = [value for value in found if value["received_at"] == newest]
        digests = {hashlib.sha256(value["content"]).hexdigest() for value in newest_items}
        if len(digests) != 1:
            raise EvidenceEmailError(f"newest {source} evidence is ambiguous")
        selected = newest_items[0]
        digest = hashlib.sha256(selected["content"]).hexdigest()
        target = output_dir / f"{source}-{newest:%Y%m%dT%H%M%SZ}-{digest[:12]}{selected['suffix']}"
        target.write_bytes(selected["content"])
        paths[source] = str(target.resolve())
        receipts.append({
            "source": source,
            "path": str(target.resolve()),
            "sha256": digest,
            "received_at": newest.isoformat(),
            "message_id_sha256": hashlib.sha256(selected["message_id"].encode()).hexdigest(),
            "sender_sha256": hashlib.sha256(selected["sender"].encode()).hexdigest(),
        })
    manifest = output_dir / f"accountability-evidence-{now:%Y%m%dT%H%M%SZ}.json"
    manifest.write_text(json.dumps({
        "schema_version": 1,
        "collected_at": now.isoformat(),
        "scope": GMAIL_READONLY_SCOPE,
        "attachments": receipts,
    }, indent=2, sort_keys=True), encoding="utf-8")
    return paths, manifest


def collect_scheduled_evidence(
    *,
    service_account_email: str,
    mailbox: str,
    output_dir: Path,
    source_specs: Mapping[str, Mapping[str, Any]],
    service_factory: Any = build_keyless_evidence_mailbox_service,
) -> tuple[dict[str, str], Path]:
    if not service_account_email or not mailbox:
        raise EvidenceEmailError("scheduled evidence collection requires a delegated service account and mailbox")
    service = service_factory(service_account_email, mailbox)
    return collect_evidence_attachments(service, output_dir=output_dir, source_specs=source_specs)
