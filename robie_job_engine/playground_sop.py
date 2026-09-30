"""StreetSmart SOP retrieval for the Playground.

Folder ids are configurable. The exclude list is not. Copies are dropped.
Nothing here reads or stores a secret.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

from .playground_config import EXCLUDED_SHARED_DRIVES, sop_folders, sop_index_path
from .secrets import redact_text

_COPY_SUFFIX = re.compile(r"\s*(?:\(\d+\)|copy(?:\s+of)?)\s*", re.IGNORECASE)
_WORD = re.compile(r"[a-z0-9']+")
_STOPWORDS = frozenset(
    "a an the of to for and or in on our we how what is do does with a".split()
)


class DrivePort(Protocol):
    def list_folder(self, folder_id: str) -> list[dict[str, Any]]: ...

    def export_text(self, file_id: str, mime: str) -> str: ...


@dataclass(frozen=True)
class SopHit:
    title: str
    folder: str
    excerpt: str
    doc_id: str

    @property
    def citation(self) -> str:
        return f"{self.title} ({self.folder})"


def is_excluded_drive_file(meta: dict[str, Any], *, folder_label: str = "") -> bool:
    """Hard exclude. Payroll, secrets, HR, finance, scans, book, and legal."""
    name = str(meta.get("name") or "")
    folded = name.casefold()
    drive_id = str(meta.get("driveId") or meta.get("drive_id") or "")
    if drive_id in EXCLUDED_SHARED_DRIVES:
        return True
    if "payroll" in folded:
        return True
    if "ezlynx" in folded and "api" in folded and "key" in folded:
        return True
    if "carrier" in folded and any(word in folded for word in ("login", "password", "website")):
        return True
    if "adobe" in folded or "mail client scan" in folded or "mail-client" in folded:
        return True
    if "book of business" in folded or "book-of-business" in folded:
        return True
    if "lawsuit" in folded or "legal" in folded:
        return True
    parent_drive = str(meta.get("parents_drive_id") or "")
    if parent_drive in EXCLUDED_SHARED_DRIVES:
        return True
    if folder_label == "employee_hub" and "payroll" in folded:
        return True
    return False


def _dedupe_key(meta: dict[str, Any]) -> str:
    digest = str(meta.get("md5Checksum") or meta.get("md5") or "").strip()
    if digest:
        return f"md5:{digest}"
    name = _COPY_SUFFIX.sub(" ", str(meta.get("name") or "")).casefold()
    name = " ".join(name.split())
    size = str(meta.get("size") or "")
    return f"name:{name}|{size}"


def dedupe_documents(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the first copy. Callers pass core SOPs before the other folders."""
    seen: set[str] = set()
    kept: list[dict[str, Any]] = []
    for item in files:
        key = _dedupe_key(item)
        if key in seen:
            continue
        seen.add(key)
        kept.append(item)
    return kept


def collect_documents(drive: DrivePort) -> list[dict[str, Any]]:
    """List configured folders, drop excludes, and drop duplicate copies."""
    collected: list[dict[str, Any]] = []
    for label, folder_id in sop_folders():
        for meta in drive.list_folder(folder_id) or []:
            row = dict(meta)
            row["folder_label"] = label
            row["folder_id"] = folder_id
            if is_excluded_drive_file(row, folder_label=label):
                continue
            collected.append(row)
    return dedupe_documents(collected)


def ingest_sops(drive: DrivePort, index_path: str | None = None) -> dict[str, Any]:
    """Write a local index of procedure text. Folder ids only; no secret values."""
    path = index_path if index_path is not None else sop_index_path()
    if not path:
        raise RuntimeError("ROBIE_PLAYGROUND_SOP_INDEX is not set")
    docs = []
    for meta in collect_documents(drive):
        file_id = str(meta.get("id") or "").strip()
        if not file_id:
            continue
        mime = str(meta.get("mimeType") or meta.get("mime") or "")
        text = redact_text(drive.export_text(file_id, mime) or "")
        docs.append(
            {
                "doc_id": file_id,
                "title": str(meta.get("name") or "Untitled procedure"),
                "folder": str(meta.get("folder_label") or ""),
                "text": text,
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }
        )
    payload = {"documents": docs}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    return {"count": len(docs), "path": path}


def load_index(index_path: str | None = None) -> list[dict[str, Any]]:
    path = index_path if index_path is not None else sop_index_path()
    if not path:
        return []
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return []
    docs = payload.get("documents") if isinstance(payload, dict) else None
    if not isinstance(docs, list):
        return []
    return [item for item in docs if isinstance(item, dict)]


def retrieve_sop(question: str, docs: list[dict[str, Any]] | None = None, *, limit: int = 1) -> list[SopHit]:
    """Keyword overlap. No model call. Empty when nothing loaded matches."""
    corpus = docs if docs is not None else load_index()
    words = [word for word in _WORD.findall(question.casefold()) if word not in _STOPWORDS and len(word) > 2]
    if not words or not corpus:
        return []
    scored: list[tuple[int, SopHit]] = []
    for doc in corpus:
        text = str(doc.get("text") or "")
        title = str(doc.get("title") or "Procedure")
        haystack = f"{title}\n{text}".casefold()
        score = sum(1 for word in words if word in haystack)
        if score <= 0:
            continue
        excerpt = _excerpt(text, words) or title
        scored.append(
            (
                score,
                SopHit(
                    title=title,
                    folder=str(doc.get("folder") or "procedures"),
                    excerpt=excerpt,
                    doc_id=str(doc.get("doc_id") or ""),
                ),
            )
        )
    scored.sort(key=lambda item: item[0], reverse=True)
    return [hit for _, hit in scored[: max(1, limit)]]


def build_drive_port() -> DrivePort:
    """Read-only Drive client from Application Default Credentials.

    Folder ids come from config. No secret values are read or stored here.
    """
    from google.auth import default as google_auth_default
    from googleapiclient.discovery import build

    credentials, _project = google_auth_default(
        scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    service = build("drive", "v3", credentials=credentials, cache_discovery=False)
    return _GoogleDrivePort(service)


class _GoogleDrivePort:
    def __init__(self, service: Any) -> None:
        self._service = service

    def list_folder(self, folder_id: str) -> list[dict[str, Any]]:
        query = f"'{folder_id}' in parents and trashed=false"
        listed: list[dict[str, Any]] = []
        page_token = None
        while True:
            response = (
                self._service.files()
                .list(
                    q=query,
                    fields="nextPageToken, files(id, name, mimeType, md5Checksum, size, driveId)",
                    pageSize=100,
                    pageToken=page_token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
            listed.extend(response.get("files") or [])
            page_token = response.get("nextPageToken")
            if not page_token:
                return listed

    def export_text(self, file_id: str, mime: str) -> str:
        if mime == "application/vnd.google-apps.document":
            data = self._service.files().export(fileId=file_id, mimeType="text/plain").execute()
            if isinstance(data, bytes):
                return data.decode("utf-8", errors="replace")
            return str(data or "")
        data = self._service.files().get_media(fileId=file_id).execute()
        if isinstance(data, bytes):
            return data.decode("utf-8", errors="replace")
        return str(data or "")


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Ingest Playground SOP folders")
    parser.add_argument("--index", default="")
    args = parser.parse_args(argv)
    try:
        result = ingest_sops(build_drive_port(), index_path=args.index or None)
    except Exception as exc:
        print(f"SOP ingest did not finish ({type(exc).__name__}).")
        return 1
    print(f"Loaded {result['count']} procedures.")
    return 0


def _excerpt(text: str, words: list[str]) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines:
        folded = line.casefold()
        if any(word in folded for word in words):
            return line[:400]
    if lines:
        return lines[0][:400]
    return ""


if __name__ == "__main__":
    raise SystemExit(main())
