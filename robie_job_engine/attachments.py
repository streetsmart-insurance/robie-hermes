from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Protocol

from .operations import OperationsStore, ingest_chat_attachments

DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
_SA_EMAIL_SUFFIX = ".iam.gserviceaccount.com"


class DriveFilePort(Protocol):
    def fetch(self, drive_file_id: str) -> tuple[bytes, str, str]:
        """Return ``(bytes, mime_type, filename)`` for a Drive file chip."""


class CallableDrivePort:
    """Wrap a ``fetch(drive_file_id) -> (bytes, mime, name)`` callable."""

    def __init__(self, fetch: Callable[[str], tuple[bytes, str, str]]) -> None:
        self._fetch = fetch

    def fetch(self, drive_file_id: str) -> tuple[bytes, str, str]:
        result = self._fetch(drive_file_id)
        if result is None:
            raise RuntimeError("Drive file did not download")
        return result


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


def unmatched_drive_chip_refs(
    attachments: Iterable[dict[str, Any]],
    staged_count: int,
) -> list[AttachmentRef]:
    """Return Drive chips that still need a fetch after local downloads.

    Chat paperclip / ``media.download`` successes are already counted in
    ``staged_count``. A Drive picker chip that produced no local path is
    returned so ``open_chat_job`` can fetch it through ``drive_port``.
    """
    raw = [item for item in (attachments or []) if isinstance(item, dict)]
    refs = [ref for ref in refs_from_chat_payload(raw) if ref.kind == "drive_chip"]
    if not refs:
        return []
    if staged_count >= len(raw):
        return []
    return refs


def proven_drive_share_identity(credentials: Any) -> str | None:
    """Return a shareable bot Drive email only when the credential proves it.

    Never invents an address. Service-account JSON / ADC objects expose
    ``service_account_email``. User OAuth tokens do not, and must not be
    guessed from repo docs or mailbox constants.
    """
    email = getattr(credentials, "service_account_email", None)
    if not isinstance(email, str):
        return None
    cleaned = email.strip()
    if cleaned.endswith(_SA_EMAIL_SUFFIX) and "@" in cleaned:
        return cleaned
    return None


def drive_chip_download_failed_message(share_identity: str | None = None) -> str:
    """Honest Chat text when a Drive picker chip could not be downloaded.

    No @robie mention. No invented email. Paperclip is always the fallback.
    """
    lines = [
        "The Drive file did not download, so this job did not start.",
        "Paperclip the PDF in Chat instead of using the Drive picker.",
    ]
    if share_identity:
        lines.append(
            f"If you want Drive picker files to work, share the file with {share_identity}."
        )
    return "\n".join(lines)


def is_attachment_ingestion_error(last_error: str | None) -> bool:
    """True when ``open_chat_job`` fail-closed on missing or unreadable files."""
    error = str(last_error or "")
    return (
        "attachment ingestion incomplete" in error
        or "attachment ingestion failed" in error
    )


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
            if ref.local_path:
                records.append(
                    store.ingest_cached_file(
                        job_id=job_id,
                        source_path=ref.local_path,
                        source_external_id=ref.source_external_id or f"{message_id}:{index}",
                        mime_type=ref.mime_type,
                        source_platform="google_drive",
                    )
                )
                continue
            if not ref.drive_file_id:
                raise ValueError("Drive chip is missing driveFileId")
            if drive_port is None:
                raise ValueError("Drive chip cannot be staged without a Drive fetcher")
            data, mime, filename = drive_port.fetch(ref.drive_file_id)
            if not data:
                raise RuntimeError("Drive file did not download")
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
