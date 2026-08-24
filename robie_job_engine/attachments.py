from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Protocol

from .operations import OperationsStore, ingest_chat_attachments


class DriveFilePort(Protocol):
    def fetch(self, drive_file_id: str) -> tuple[bytes, str, str]:
        """Return ``(bytes, mime_type, filename)`` for a Drive file chip."""


@dataclass(frozen=True)
class AttachmentRef:
    kind: str
    source_external_id: str
    local_path: str | None = None
    mime_type: str | None = None
    drive_file_id: str | None = None
    content_name: str | None = None


def refs_from_chat_payload(attachments: Iterable[dict[str, Any]]) -> list[AttachmentRef]:
    """Normalize ordinary Chat uploads and Drive file chips from a Chat payload."""
    refs: list[AttachmentRef] = []
    for index, item in enumerate(attachments or []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or f"attachment:{index}")
        resource = ((item.get("attachmentDataRef") or {}).get("resourceName") or "").strip()
        drive_id = ((item.get("driveDataRef") or {}).get("driveFileId") or "").strip()
        mime = item.get("contentType") or None
        content_name = item.get("contentName") or None
        if resource:
            refs.append(
                AttachmentRef(
                    kind="chat_upload",
                    source_external_id=f"{name}:media",
                    mime_type=mime,
                    content_name=content_name,
                )
            )
        if drive_id:
            refs.append(
                AttachmentRef(
                    kind="drive_chip",
                    source_external_id=f"{name}:drive:{drive_id}",
                    mime_type=mime,
                    drive_file_id=drive_id,
                    content_name=content_name,
                )
            )
        if not resource and not drive_id:
            refs.append(
                AttachmentRef(
                    kind="unknown",
                    source_external_id=f"{name}:unknown",
                    mime_type=mime,
                    content_name=content_name,
                )
            )
    return refs


def ingest_attachment_refs(
    db_path: str,
    job_id: str,
    message_id: str,
    refs: Iterable[AttachmentRef | tuple[str, str]],
    *,
    drive_port: DriveFilePort | None = None,
    artifact_root: str | None = None,
) -> list[dict[str, Any]]:
    """Stage Chat uploads and Drive chips into the private artifact store."""
    store = OperationsStore(db_path, artifact_root)
    records: list[dict[str, Any]] = []
    cached: list[tuple[str, str]] = []
    for index, ref in enumerate(refs):
        if isinstance(ref, tuple):
            cached.append(ref)
            continue
        if ref.kind == "chat_upload" and ref.local_path:
            records.append(
                store.ingest_cached_file(
                    job_id=job_id,
                    source_path=ref.local_path,
                    source_external_id=ref.source_external_id or f"{message_id}:{index}",
                    mime_type=ref.mime_type,
                    source_platform="google_chat",
                )
            )
            continue
        if ref.kind == "drive_chip":
            if not ref.drive_file_id:
                raise ValueError("Drive chip is missing driveFileId")
            if drive_port is None:
                raise ValueError("Drive chip cannot be staged without a Drive fetcher")
            data, mime, filename = drive_port.fetch(ref.drive_file_id)
            records.append(
                store.ingest_bytes(
                    job_id=job_id,
                    data=data,
                    original_name=ref.content_name or filename or "drive-attachment",
                    source_external_id=ref.source_external_id or f"{message_id}:drive:{ref.drive_file_id}",
                    mime_type=ref.mime_type or mime,
                    source_platform="google_drive",
                )
            )
            continue
        if ref.kind == "chat_upload" and not ref.local_path:
            raise ValueError("Chat upload is missing staged bytes")
        raise ValueError(f"unsupported attachment kind: {ref.kind}")
    if cached:
        records.extend(
            ingest_chat_attachments(
                db_path, job_id, message_id, cached, artifact_root=artifact_root
            )
        )
    return records
