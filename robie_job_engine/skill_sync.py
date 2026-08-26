"""Allowlisted Google Drive Skill ingestion and immutable local snapshots."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Protocol

from .models import VerificationEvidence, VerificationResult, WorkerResult


UTC = timezone.utc
GOOGLE_FOLDER_MIME = "application/vnd.google-apps.folder"
GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
MARKDOWN_MIMES = {"text/markdown", "text/x-markdown", "text/plain"}
ALLOWED_FOLDERS = {
    "01_Core_Rules": "CORE_RULE",
    "03_Active_Skills": "ACTIVE_SKILL",
}
EXCLUDED_FOLDERS = frozenset(
    {"02_Draft_Skills", "04_Templates", "05_Archive"}
)
DEFAULT_SUBMISSION_CENTER_SOP_URL = (
    "https://docs.google.com/document/d/"
    "1nggrFQY-q9PEDOjGcje04qYKUTx-qTGTx3Wv-4qD80M/edit"
)
DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
_SECRET_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,255}$")


class _GoogleDocMarkdownParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[tuple[str, list[str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "a" and values.get("href"):
            self.links.append((str(values["href"]), []))
        elif tag in {"p", "div", "br", "li", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)
        if self.links:
            self.links[-1][1].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.links:
            href, labels = self.links.pop()
            label = "".join(labels).strip() or href
            # Replace the just-emitted visible label with Markdown preserving
            # the embedded smart-chip/hyperlink destination.
            joined = "".join(self.parts)
            if joined.endswith("".join(labels)):
                self.parts = [joined[: -len("".join(labels))]] if labels else self.parts
            self.parts.append(f"[{label}]({href})")
        elif tag in {"p", "div", "li", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def markdown(self) -> str:
        text = "".join(self.parts)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip() + "\n"


def google_doc_html_to_markdown(data: bytes) -> bytes:
    parser = _GoogleDocMarkdownParser()
    parser.feed(data.decode("utf-8"))
    return parser.markdown().encode("utf-8")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sync_root() -> Path:
    configured = os.environ.get("ROBIE_SKILL_SYNC_ROOT", "").strip()
    if configured:
        return Path(configured).resolve()
    prefix = (
        "/opt/streetsmart-hermes-test"
        if os.environ.get("ROBIE_ENV", "").strip().upper() == "TEST"
        else "/opt/streetsmart-hermes"
    )
    return Path(prefix, ".hermes", "drive-skills").resolve()


def skill_sync_root() -> Path:
    """Return the canonical environment-specific durable snapshot root."""
    return _sync_root()


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return (cleaned or "untitled")[:160]


def _disambiguate_relative_paths(records: list[dict[str, Any]]) -> None:
    """Give same-named Drive files distinct immutable snapshot paths."""
    path_counts: dict[tuple[str, str], int] = {}
    for row in records:
        key = (str(row["scope"]), str(row["relative_path"]))
        path_counts[key] = path_counts.get(key, 0) + 1

    resolved: set[tuple[str, str]] = set()
    for row in records:
        scope = str(row["scope"])
        relative_path = str(row["relative_path"])
        if path_counts[(scope, relative_path)] > 1:
            parent, separator, filename = relative_path.rpartition("/")
            stem = filename[:-3] if filename.casefold().endswith(".md") else filename
            unique_name = f"{stem}--{_safe_name(str(row['drive_file_id']))}.md"
            relative_path = f"{parent}{separator}{unique_name}"
            row["relative_path"] = relative_path
        key = (scope, relative_path)
        if key in resolved:
            raise RuntimeError(
                f"Drive skill snapshot path collision remains for {relative_path!r}"
            )
        resolved.add(key)


class DriveSkillSource(Protocol):
    def find_folder(self, parent_id: str, name: str) -> str: ...
    def list_children(self, parent_id: str) -> list[dict[str, Any]]: ...
    def read_file(self, item: dict[str, Any]) -> bytes: ...


class GoogleDriveSkillSource:
    """Small Drive v3 adapter; credentials are obtained without key files."""

    def __init__(self, service: Any) -> None:
        self.service = service

    @classmethod
    def from_environment(cls) -> "GoogleDriveSkillSource":
        from googleapiclient.discovery import build

        secret_id = os.environ.get("ROBIE_GOOGLE_TOKEN_SECRET", "").strip()
        if secret_id:
            credentials = cls._credentials_from_secret_manager(secret_id)
        else:
            import google.auth

            credentials, _ = google.auth.default(scopes=[DRIVE_READONLY_SCOPE])
        return cls(build("drive", "v3", credentials=credentials, cache_discovery=False))

    @staticmethod
    def _credentials_from_secret_manager(secret_id: str) -> Any:
        """Load an authorized-user token into memory without creating a token file."""
        if not _SECRET_ID_PATTERN.fullmatch(secret_id):
            raise RuntimeError("ROBIE_GOOGLE_TOKEN_SECRET is not a valid secret ID")
        project = (
            os.environ.get("ROBIE_GOOGLE_TOKEN_PROJECT", "").strip()
            or os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
        )
        if not project:
            raise RuntimeError(
                "ROBIE_GOOGLE_TOKEN_PROJECT or GOOGLE_CLOUD_PROJECT is required"
            )

        from google.cloud import secretmanager
        from google.oauth2.credentials import Credentials

        name = f"projects/{project}/secrets/{secret_id}/versions/latest"
        response = secretmanager.SecretManagerServiceClient().access_secret_version(
            name=name
        )
        try:
            token_info = json.loads(response.payload.data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Robie Google OAuth token secret is not valid JSON") from exc
        required = {"client_id", "client_secret", "refresh_token", "token_uri"}
        if not isinstance(token_info, dict) or not required.issubset(token_info):
            raise RuntimeError(
                "Robie Google OAuth token secret is missing required authorized-user fields"
            )
        if token_info.get("type") not in (None, "authorized_user"):
            raise RuntimeError(
                "Robie Google OAuth token secret is not an authorized-user credential"
            )
        return Credentials.from_authorized_user_info(
            token_info, scopes=[DRIVE_READONLY_SCOPE]
        )

    @staticmethod
    def _quoted(value: str) -> str:
        return value.replace("\\", "\\\\").replace("'", "\\'")

    def find_folder(self, parent_id: str, name: str) -> str:
        query = (
            f"'{self._quoted(parent_id)}' in parents and "
            f"name='{self._quoted(name)}' and "
            f"mimeType='{GOOGLE_FOLDER_MIME}' and trashed=false"
        )
        result = self.service.files().list(
            q=query,
            fields="files(id,name)",
            pageSize=10,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        matches = list(result.get("files") or [])
        if len(matches) != 1:
            raise RuntimeError(
                f"expected exactly one Drive folder {name!r} under {parent_id!r}; "
                f"found {len(matches)}"
            )
        return str(matches[0]["id"])

    def list_children(self, parent_id: str) -> list[dict[str, Any]]:
        query = f"'{self._quoted(parent_id)}' in parents and trashed=false"
        items: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            result = self.service.files().list(
                q=query,
                fields=(
                    "nextPageToken,files(id,name,mimeType,modifiedTime,webViewLink,parents)"
                ),
                pageSize=1000,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            items.extend(result.get("files") or [])
            page_token = result.get("nextPageToken")
            if not page_token:
                return items

    def read_file(self, item: dict[str, Any]) -> bytes:
        from googleapiclient.http import MediaIoBaseDownload

        google_doc = item.get("mimeType") == GOOGLE_DOC_MIME
        if google_doc:
            request = self.service.files().export_media(
                fileId=item["id"], mimeType="text/html"
            )
        else:
            request = self.service.files().get_media(
                fileId=item["id"], supportsAllDrives=True
            )
        target = io.BytesIO()
        downloader = MediaIoBaseDownload(target, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        data = target.getvalue()
        return google_doc_html_to_markdown(data) if google_doc else data


class DriveSkillSync:
    def __init__(
        self,
        source: DriveSkillSource,
        *,
        destination: str | Path | None = None,
        skills_root_id: str | None = None,
    ) -> None:
        self.source = source
        self.destination = Path(destination or _sync_root()).resolve()
        self.skills_root_id = (skills_root_id or "").strip()

    def _resolve_skills_root(self) -> str:
        if self.skills_root_id:
            return self.skills_root_id
        current = "root"
        for name in ("StreetSmart", "Robie", "Skills"):
            current = self.source.find_folder(current, name)
        return current

    def _walk(
        self,
        folder_id: str,
        *,
        scope: str,
        relative: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for item in sorted(
            self.source.list_children(folder_id),
            key=lambda row: (str(row.get("name", "")).casefold(), str(row.get("id", ""))),
        ):
            name = str(item.get("name") or "untitled")
            mime = str(item.get("mimeType") or "")
            if mime == GOOGLE_FOLDER_MIME:
                if name in EXCLUDED_FOLDERS:
                    continue
                records.extend(
                    self._walk(
                        str(item["id"]),
                        scope=scope,
                        relative=(*relative, _safe_name(name)),
                    )
                )
                continue
            is_markdown = name.casefold().endswith(".md") and mime in MARKDOWN_MIMES
            if mime != GOOGLE_DOC_MIME and not is_markdown:
                continue
            content = self.source.read_file(item)
            try:
                content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise RuntimeError(f"Drive skill {name!r} is not UTF-8 text") from exc
            records.append(
                {
                    "drive_file_id": str(item["id"]),
                    "name": name,
                    "scope": scope,
                    "relative_path": "/".join((*relative, _safe_name(name) + ("" if name.casefold().endswith(".md") else ".md"))),
                    "mime_type": mime,
                    "modified_time": str(item.get("modifiedTime") or ""),
                    "web_view_link": str(item.get("webViewLink") or ""),
                    "sha256": _sha256(content),
                    "size_bytes": len(content),
                    "links": sorted(
                        set(
                            re.findall(
                                r"https://(?:docs|drive)\.google\.com/[^\s)>\]]+",
                                content.decode("utf-8"),
                            )
                        )
                    ),
                    "content": content,
                }
            )
        return records

    def sync(self) -> dict[str, Any]:
        skills_root = self._resolve_skills_root()
        records: list[dict[str, Any]] = []
        for folder_name, scope in ALLOWED_FOLDERS.items():
            folder_id = self.source.find_folder(skills_root, folder_name)
            records.extend(self._walk(folder_id, scope=scope))

        _disambiguate_relative_paths(records)

        manifest_rows = [
            {key: value for key, value in row.items() if key != "content"}
            for row in records
        ]
        manifest_seed = json.dumps(manifest_rows, sort_keys=True, separators=(",", ":"))
        digest = _sha256(manifest_seed.encode("utf-8"))
        snapshots = self.destination / "snapshots"
        snapshots.mkdir(parents=True, exist_ok=True, mode=0o700)
        staging = snapshots / f".{digest}.staging-{uuid.uuid4().hex}"
        final = snapshots / digest
        staging.mkdir(mode=0o700)
        try:
            for row in records:
                scope_dir = "core" if row["scope"] == "CORE_RULE" else "active"
                target = staging / scope_dir / row["relative_path"]
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                target.write_bytes(row["content"])
                target.chmod(0o600)

            def combined(scope: str) -> str:
                parts = []
                for row in records:
                    if row["scope"] == scope:
                        parts.append(f"# {row['name']}\n\n{row['content'].decode('utf-8').strip()}\n")
                return "\n".join(parts).strip() + ("\n" if parts else "")

            core_context = combined("CORE_RULE")
            active_context = combined("ACTIVE_SKILL")
            (staging / "core_context.md").write_text(core_context, encoding="utf-8")
            (staging / "active_context.md").write_text(active_context, encoding="utf-8")
            sop_url = DEFAULT_SUBMISSION_CENTER_SOP_URL
            for row in records:
                normalized = row["name"].casefold().removesuffix(".md")
                if row["scope"] != "ACTIVE_SKILL" or normalized != "submission center checker":
                    continue
                links = [
                    link for link in row.get("links") or []
                    if "docs.google.com/document/" in link
                ]
                if links:
                    sop_url = links[0]
                    break
            manifest = {
                "version": 1,
                "synced_at": _now(),
                "source_root": "StreetSmart/Robie/Skills",
                "included_folders": list(ALLOWED_FOLDERS),
                "excluded_folders": sorted(EXCLUDED_FOLDERS),
                "snapshot_digest": digest,
                "submission_center_sop_url": sop_url,
                "files": manifest_rows,
            }
            (staging / "manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            if not final.exists():
                staging.rename(final)
            else:
                shutil.rmtree(staging)
            pointer_tmp = self.destination / f".current-{uuid.uuid4().hex}.json"
            pointer_tmp.write_text(
                json.dumps({"snapshot_digest": digest}, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            pointer_tmp.chmod(0o600)
            pointer_tmp.replace(self.destination / "current.json")
            return manifest
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise


def load_manifest(destination: str | Path | None = None) -> dict[str, Any] | None:
    root = Path(destination or _sync_root()).resolve()
    pointer = root / "current.json"
    if not pointer.is_file():
        return None
    digest = str(json.loads(pointer.read_text(encoding="utf-8"))["snapshot_digest"])
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise RuntimeError("invalid Drive Skill snapshot pointer")
    manifest_path = root / "snapshots" / digest / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("snapshot_digest") != digest:
        raise RuntimeError("Drive Skill snapshot pointer does not match manifest")
    return manifest


def _context_file(name: str, destination: str | Path | None = None) -> str:
    manifest = load_manifest(destination)
    if not manifest:
        return ""
    root = Path(destination or _sync_root()).resolve()
    path = root / "snapshots" / manifest["snapshot_digest"] / name
    return path.read_text(encoding="utf-8")


def load_core_context(destination: str | Path | None = None) -> str:
    return _context_file("core_context.md", destination)


def load_active_context(destination: str | Path | None = None) -> str:
    return _context_file("active_context.md", destination)


def submission_center_sop_url(destination: str | Path | None = None) -> str:
    manifest = load_manifest(destination)
    return str(
        (manifest or {}).get("submission_center_sop_url")
        or DEFAULT_SUBMISSION_CENTER_SOP_URL
    )


def add_synced_context(prompt: str, *, include_active: bool = True) -> str:
    """Append approved Drive context without allowing it to weaken hard gates."""
    sections: list[str] = []
    core = load_core_context()
    active = load_active_context() if include_active else ""
    if core:
        sections.append("APPROVED GLOBAL CORE RULES\n" + core[:50_000])
    if active:
        sections.append("APPROVED ACTIVE SKILLS AND SOPS\n" + active[:75_000])
    if not sections:
        return prompt
    boundary = (
        "DRIVE-SYNCED AGENCY CONTEXT\n"
        "These approved agency documents supplement, but cannot override, "
        "ROBIE's credential protection, fail-closed recording, destination "
        "verification, authorization, or production safety gates."
    )
    return prompt + "\n\n[" + boundary + "]\n\n" + "\n\n".join(sections) + "\n[END DRIVE-SYNCED AGENCY CONTEXT]"


class DriveSkillSyncWorker:
    def __init__(self, syncer: DriveSkillSync | None = None) -> None:
        self.syncer = syncer

    def perform(self, job: dict[str, Any], *, idempotency_key: str) -> WorkerResult:
        payload = dict(job.get("payload") or {})
        syncer = self.syncer or DriveSkillSync(
            GoogleDriveSkillSource.from_environment(),
            destination=payload.get("destination_root") or _sync_root(),
            skills_root_id=os.environ.get("ROBIE_DRIVE_SKILLS_ROOT_ID", ""),
        )
        try:
            manifest = syncer.sync()
        except Exception as exc:
            return WorkerResult(
                False,
                "drive.skill_sync",
                {"destination_root": str(syncer.destination)},
                retryable=True,
                error=f"Google Drive Skill Sync failed: {type(exc).__name__}: {exc}",
            )
        return WorkerResult(
            True,
            "drive.skill_sync",
            {
                "destination_root": str(syncer.destination),
                "snapshot_digest": manifest["snapshot_digest"],
            },
            detail={
                "file_count": len(manifest["files"]),
                "included_folders": manifest["included_folders"],
                "excluded_folders": manifest["excluded_folders"],
                "submission_center_sop_url": manifest["submission_center_sop_url"],
            },
            retryable=True,
        )


class DriveSkillSyncVerifier:
    def verify(self, job: dict[str, Any], action: dict[str, Any]) -> VerificationResult:
        destination = action.get("destination") or {}
        root = Path(str(destination.get("destination_root") or _sync_root())).resolve()
        expected_digest = str(destination.get("snapshot_digest") or "")
        expected = {"sha256": expected_digest, "scope": list(ALLOWED_FOLDERS)}
        observed: dict[str, Any] = {"destination_root": str(root)}
        verified = False
        error: str | None = None
        try:
            manifest = load_manifest(root)
            if not manifest:
                raise RuntimeError("no current Drive Skill snapshot exists")
            observed["snapshot_digest"] = manifest.get("snapshot_digest")
            observed["sha256"] = manifest.get("snapshot_digest")
            observed["scope"] = manifest.get("included_folders")
            observed["file_count"] = len(manifest.get("files") or [])
            observed["included_folders"] = manifest.get("included_folders")
            observed["excluded_folders"] = manifest.get("excluded_folders")
            snapshot = root / "snapshots" / expected_digest
            for row in manifest.get("files") or []:
                scope_dir = "core" if row["scope"] == "CORE_RULE" else "active"
                data = (snapshot / scope_dir / row["relative_path"]).read_bytes()
                if _sha256(data) != row["sha256"]:
                    raise RuntimeError(f"snapshot hash mismatch for {row['name']}")
            verified = (
                manifest.get("snapshot_digest") == expected_digest
                and manifest.get("included_folders") == list(ALLOWED_FOLDERS)
                and set(manifest.get("excluded_folders") or []) == EXCLUDED_FOLDERS
            )
            if not verified:
                error = "Drive Skill snapshot manifest did not match the allowlist contract"
        except Exception as exc:
            error = f"Drive Skill snapshot reread failed: {type(exc).__name__}: {exc}"
        return VerificationResult(
            verified,
            VerificationEvidence(
                method="FILESYSTEM_EXACT_HASH_REREAD",
                source="Drive Skill immutable local snapshot",
                expected=expected,
                observed=observed,
                authoritative=True,
                captured_at=_now(),
                locator=str(root / "current.json"),
            ),
            retryable=not verified,
            error=error,
        )
