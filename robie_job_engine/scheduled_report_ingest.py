"""Fail-closed ingest for the weekday department-tab accountability package.

The dedicated Production collector must not decide that the four EZLynx
scheduled-report subjects are missing just because a Gmail ``metadata`` read
has no MIME attachment parts. Metadata is headers only. Attachment filenames
and attachment IDs require a second, field-masked structure read.

Exact subjects are taken from the 2026-09-07 Production rehearsal. Do not
invent a second report or weaken these strings.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .scheduled_report_email_sync import (
    ScheduledReportEvidenceError,
    _allowed_sender,
    _decode_attachment,
    _header,
    _parts,
)


MISSING_SUBJECTS_EXIT = 2

# Exact Production mailbox subjects. A metadata-only read can see these
# headers and still miss the XLSX parts.
EXACT_EZLYNX_SUBJECTS = {
    "activities": "Robie - EZLynx Activities",
    "overdue_tasks": "Robie - EZLynx Overdue Tasks",
    "sales_center": "Robie - EZLynx Sales Center",
    "policy_changes": "Robie - EZLynx Policy Changes",
}
RINGCENTRAL_SUBJECT = "Scheduled Reports from RingCentral"

HEADER_METADATA_FIELDS = ("From", "Subject")
# Nested part filenames and attachment IDs only. Never request MIME body data.
STRUCTURE_FIELD_MASK = ",".join(
    (
        "id",
        "internalDate",
        "payload/filename",
        "payload/mimeType",
        "payload/body/attachmentId",
        "payload/parts/filename",
        "payload/parts/mimeType",
        "payload/parts/body/attachmentId",
        "payload/parts/parts/filename",
        "payload/parts/parts/mimeType",
        "payload/parts/parts/body/attachmentId",
    )
)


def default_daily_report_config() -> dict[str, Any]:
    reports = {}
    for key, subject in EXACT_EZLYNX_SUBJECTS.items():
        reports[key] = {
            "label": subject.replace(" ", "_").replace("-", "_").upper(),
            "exact_subject": subject,
            "required": True,
            "destination": "source",
            "required_columns": [],
        }
    return {
        "allowed_sender_domains": ["appliedsystems.com", "ezlynx.com"],
        "max_age_hours": 36,
        "reports": reports,
    }


def exact_subject(value: str) -> str:
    return " ".join(str(value or "").split())


def missing_required_subjects(
    observed_subjects: list[str],
    *,
    required: Mapping[str, str] | None = None,
) -> list[str]:
    needed = list((required or EXACT_EZLYNX_SUBJECTS).values())
    have = {exact_subject(item) for item in observed_subjects}
    return [subject for subject in needed if subject not in have]


class MetadataOnlyAttachmentError(ScheduledReportEvidenceError):
    """A metadata Gmail payload cannot prove an XLSX attachment exists."""


def read_message_headers(service: Any, message_id: str) -> dict[str, Any]:
    """Approved-header metadata read. This response has no attachment parts."""
    return service.users().messages().get(
        userId="me",
        id=message_id,
        format="metadata",
        metadataHeaders=list(HEADER_METADATA_FIELDS),
    ).execute()


def read_message_structure(service: Any, message_id: str) -> dict[str, Any]:
    """Field-masked structure read. Rejects any request for MIME body data."""
    if "data" in STRUCTURE_FIELD_MASK.split(",") or "payload/body/data" in STRUCTURE_FIELD_MASK:
        raise ScheduledReportEvidenceError("structure field mask must not request MIME body data")
    return service.users().messages().get(
        userId="me",
        id=message_id,
        format="full",
        fields=STRUCTURE_FIELD_MASK,
    ).execute()


def attachment_filenames(payload: Mapping[str, Any]) -> list[str]:
    names: list[str] = []
    root_name = str(payload.get("filename") or "").strip()
    if root_name:
        names.append(root_name)
    for part in _parts(payload):
        name = str(part.get("filename") or "").strip()
        if name:
            names.append(name)
    return names


def assert_structure_has_tabular_attachment(payload: Mapping[str, Any], *, subject: str) -> None:
    names = attachment_filenames(payload)
    if not any(Path(name).suffix.casefold() in {".csv", ".xlsx"} for name in names):
        raise MetadataOnlyAttachmentError(
            f"Gmail metadata/structure for {subject!r} has no CSV/XLSX attachment parts"
        )


def _tabular_parts(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    found: list[Mapping[str, Any]] = []
    root_name = str(payload.get("filename") or "")
    if Path(root_name).suffix.casefold() in {".csv", ".xlsx"}:
        found.append(payload)
    for part in _parts(payload):
        name = str(part.get("filename") or "")
        if Path(name).suffix.casefold() in {".csv", ".xlsx"}:
            found.append(part)
    return found


def collect_exact_daily_sources(
    service: Any,
    *,
    output_dir: Path,
    config: Mapping[str, Any] | None = None,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Collect the four EZLynx daily sources using header + structure reads."""
    settings = dict(config or default_daily_report_config())
    reports = {str(key): dict(value or {}) for key, value in dict(settings.get("reports") or {}).items()}
    now = as_of or datetime.now(timezone.utc)
    max_age_hours = max(1, int(settings.get("max_age_hours") or 36))
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    listed = service.users().messages().list(
        userId="me", q="has:attachment newer_than:2d", maxResults=100
    ).execute()
    observed_subjects: list[str] = []
    accepted: dict[str, dict[str, Any]] = {}

    for message_meta in listed.get("messages") or []:
        message_id = str(message_meta.get("id") or "")
        if not message_id:
            continue
        headers = read_message_headers(service, message_id)
        payload = headers.get("payload") or {}
        subject = exact_subject(_header(payload, "Subject"))
        if not subject:
            continue
        if not _allowed_sender(_header(payload, "From"), settings):
            continue
        received_ms = int(headers.get("internalDate") or 0)
        received = datetime.fromtimestamp(received_ms / 1000, tz=timezone.utc)
        age_hours = (now - received).total_seconds() / 3600
        if age_hours < 0 or age_hours > max_age_hours:
            continue
        observed_subjects.append(subject)
        report_key = next(
            (key for key, spec in reports.items() if exact_subject(spec.get("exact_subject") or "") == subject),
            None,
        )
        if report_key is None:
            continue
        structure = read_message_structure(service, message_id)
        assert_structure_has_tabular_attachment(structure.get("payload") or {}, subject=subject)
        accepted[report_key] = {
            "message_id": message_id,
            "subject": subject,
            "received_at": received.isoformat(),
            "filenames": attachment_filenames(structure.get("payload") or {}),
        }

    missing = missing_required_subjects(observed_subjects)
    if missing:
        raise ScheduledReportEvidenceError(
            "no fresh scheduled report found for " + ", ".join(missing)
        )

    sources: dict[str, str] = {}
    evidence: dict[str, Any] = {}
    for report_key, info in accepted.items():
        # Second read is a field-masked structure or a later bytes fetch by
        # attachment id. Do not treat a metadata payload as the attachment list.
        structure = read_message_structure(service, str(info["message_id"]))
        payload = structure.get("payload") or {}
        parts = _tabular_parts(payload)
        if not parts:
            # gmail.readonly may still return inline body.data on an unmasked get.
            full = service.users().messages().get(userId="me", id=info["message_id"]).execute()
            parts = _tabular_parts(full.get("payload") or {})
        if not parts:
            raise ScheduledReportEvidenceError(
                f"no fresh scheduled report found for {info['subject']}"
            )
        part = parts[0]
        filenames = list(info.get("filenames") or [])
        filename = str(part.get("filename") or (filenames[0] if filenames else "report.xlsx"))
        try:
            content = _decode_attachment(service, str(info["message_id"]), part)
        except ScheduledReportEvidenceError:
            full = service.users().messages().get(userId="me", id=info["message_id"]).execute()
            full_parts = _tabular_parts(full.get("payload") or {})
            if not full_parts:
                raise
            part = full_parts[0]
            filename = str(part.get("filename") or filename)
            content = _decode_attachment(service, str(info["message_id"]), part)
        dest = output_dir / f"{report_key}-{Path(filename).name}"
        dest.write_bytes(content)
        sources[report_key] = str(dest.resolve())
        evidence[report_key] = {
            "source_status": "available",
            "received_at": info["received_at"],
            "filename": filename,
            "message_id": info["message_id"],
            "headers_format": "metadata",
            "structure_field_mask": STRUCTURE_FIELD_MASK,
        }

    snapshot = {
        "ok": True,
        "collected_at": now.isoformat(),
        "report_kind": "department_tab_google_doc",
        "subjects": EXACT_EZLYNX_SUBJECTS,
        "accepted": accepted,
        "sources": sources,
        "evidence": evidence,
        "manifest": None,
    }
    path = output_dir / f"collect-{now:%Y%m%d}.json"
    path.write_text(json.dumps(snapshot, indent=2, default=str), encoding="utf-8")
    snapshot["snapshot_path"] = str(path.resolve())
    snapshot["snapshot_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snapshot
