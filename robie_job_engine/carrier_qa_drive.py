"""Carrier pull Drive layouts.

Daily pull (Carlo 2026-10-10): one folder per pull day, carrier folders inside.

    Document Retrieval/<YYYY-MM-DD>/<Carrier>/<file>.pdf

``Document Retrieval`` (id below) is on the StreetSmart shared drive; robie@ is
a file organizer there. The day folder and the carrier folders are created on
the first upload of the day.

Per-module QA packs (decided 2026-10-08) keep the older top-level-per-carrier
map below. An accidental nested copy "Robie Carrier Pull QA (Nicole)/Robie
Carrier Pull QA (Nicole)/..." (created 2026-10-07, id
1_gpeEPibWrHoCuWk-7YxEuKTu9kKvn_q) is retired: never write there.

Upload: ``CarrierDriveUpload`` copies each PDF in one carrier's dated pack into
``Document Retrieval/<pull date>/<Carrier>/`` and records it in a carrier-level
``drive-ledger.json`` so a notice is uploaded once, even when a later pull
downloads it again. It runs with ROBIE_ENV=TEST off Production, or with
ROBIE_ENV=PRODUCTION on a Production host. It only writes under the
Document Retrieval folder, never changes sharing, and uses the host's Drive
token (``ROBIE_CARRIER_DRIVE_TOKEN_FILE``, else ``ROBIE_GOOGLE_TOKEN_FILE``).
Without a token with Drive scope it fails closed. The per-module
``--upload-drive`` flags still fail closed; the daily runner
(``carrier_daily_run``) is the only uploader.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import tempfile
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any

DAILY_DRIVE_ROOT_ID = "1kCvgSb1W7UZN4Jm-HCQYZCrKS3Ifw6Ry"
DAILY_DRIVE_ROOT_NAME = "Document Retrieval"

CARRIER_QA_DRIVE_ROOT_ID = "1cLEpR-0T6KdiVjcdAr0qpGTO447MetI2"
CARRIER_QA_DRIVE_ROOT_NAME = "Robie Carrier Pull QA (Nicole)"
RETIRED_NESTED_ROOT_ID = "1_gpeEPibWrHoCuWk-7YxEuKTu9kKvn_q"

# carrier key (as in carrier_dry_run.SPECS) -> (folder title, folder id)
CARRIER_QA_FOLDERS: dict[str, tuple[str, str]] = {
    "progressive": ("Progressive", "1MMojqm99ft4DgxplMuBvz-eTnKdgpY9U"),
    "progressive_bop": ("Progressive BOP", "122J0nQ85jW26eEyRReUCqyMOpLiAHuHo"),
    "guard": ("Guard", "13NlvNvTm5Urki88xkFhFuCGdnIGIWCjX"),
    "geico": ("Geico", "1mMy9nrYjN8PRRwihRLgjjDdb213WqBLt"),
    "travelers": ("Travelers", "1dAHrhxn_ksrbxqAYAxpOWjtbhjuVO39t"),
    "natgen": ("NatGen", "1jpyLNJmuRvhZ-EYnM2sGQRCXfyQjsHh9"),
    "uticafirst": ("Utica First", "1fn7q_tBK19Z-PqOs7McYzpdiaBcAogBj"),
    "farmersofsalem": ("Farmers of Salem", "1oz9aYLVxre7UMzzgG3rRe_GqVS4tZn-G"),
}


def qa_folder_path(carrier: str, day_iso: str) -> str:
    """Human path for one carrier's dated QA folder, e.g. for READMEs."""
    title, _ = CARRIER_QA_FOLDERS[carrier]
    return f"{CARRIER_QA_DRIVE_ROOT_NAME}/{title}/{day_iso}"


def daily_folder_path(carrier: str, day_iso: str) -> str:
    """Human path for one carrier's folder in the daily pull layout."""
    title, _ = CARRIER_QA_FOLDERS[carrier]
    return f"{DAILY_DRIVE_ROOT_NAME}/{day_iso}/{title}"


def drive_file_link(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view"


def drive_folder_link(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}"


DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
TOKEN_ENV = "ROBIE_CARRIER_DRIVE_TOKEN_FILE"
FALLBACK_TOKEN_ENV = "ROBIE_GOOGLE_TOKEN_FILE"
DRIVE_LEDGER_NAME = "drive-ledger.json"
FOLDER_MIME = "application/vnd.google-apps.folder"
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class DriveUploadHold(RuntimeError):
    """The Drive upload for one carrier (or one file) cannot go ahead safely."""


def is_production_host() -> bool:
    raw = f"{socket.gethostname()} {socket.getfqdn()}".lower()
    labels = [label for label in re.split(r"[\s.]+", raw) if label]
    return any(label.startswith("hermes-poc") for label in labels)


def require_upload_environment() -> None:
    """Test hosts need ROBIE_ENV=TEST; a Production host needs ROBIE_ENV=PRODUCTION."""
    from .intake_core import require_test

    if is_production_host():
        if os.environ.get("ROBIE_ENV", "").strip().upper() not in {"PRODUCTION", "PROD"}:
            raise DriveUploadHold("Carrier Drive upload on Production requires ROBIE_ENV=PRODUCTION")
        return
    require_test()


def build_drive_service() -> Any:
    """Drive v3 client from this host's Drive token. Fails closed."""
    require_upload_environment()
    path = (os.environ.get(TOKEN_ENV) or os.environ.get(FALLBACK_TOKEN_ENV) or "").strip()
    if not path:
        raise DriveUploadHold("Drive upload token is not configured on this host")
    if not os.path.isfile(path):
        raise DriveUploadHold("Drive upload token file is missing")
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    credentials = Credentials.from_authorized_user_file(path)
    granted = set(credentials.scopes or ())
    if not granted & {DRIVE_SCOPE, DRIVE_FILE_SCOPE}:
        raise DriveUploadHold("Drive upload token lacks Drive scope")
    if not credentials.valid:
        credentials.refresh(Request())
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def _q(value: str) -> str:
    """Quote a value for a Drive ``q`` string."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _media_upload(content: bytes) -> Any:
    from googleapiclient.http import MediaIoBaseUpload

    return MediaIoBaseUpload(BytesIO(content), mimetype="application/pdf", resumable=False)


def _is_pdf(blob: bytes) -> bool:
    return blob[:5] == b"%PDF-"


@dataclass
class DriveUploadResult:
    carrier: str
    day: str
    folder_id: str | None = None
    uploaded: list[dict[str, str]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    held: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "carrier": self.carrier,
            "day": self.day,
            "folder_id": self.folder_id,
            "uploaded": self.uploaded,
            "skipped": self.skipped,
            "held": self.held,
        }


def pack_pdfs(pack: Path) -> list[tuple[str, Path]]:
    """(ledger key, path) for every PDF in a dated pack, sources excluded.

    The key is ``<issued date>/<filename>`` (the pack layout every carrier
    uses), or just the filename for a PDF at the pack root.
    """
    found: list[tuple[str, Path]] = []
    if not pack.is_dir():
        return found
    for path in sorted(pack.rglob("*")):
        rel = path.relative_to(pack)
        if rel.parts and rel.parts[0] == "sources":
            continue
        if path.is_symlink() or not path.is_file() or path.suffix.lower() != ".pdf":
            continue
        found.append(("/".join(rel.parts), path))
    return found


def drive_names(keys: list[str]) -> dict[str, str]:
    """Drive file name per ledger key, unique within one date folder.

    A filename that appears under two issued dates in one pull gets the
    issued date added: ``<stem> (<issued date>).pdf``.
    """
    counts: dict[str, int] = {}
    for key in keys:
        counts[key.rsplit("/", 1)[-1]] = counts.get(key.rsplit("/", 1)[-1], 0) + 1
    names: dict[str, str] = {}
    for key in keys:
        parts = key.split("/")
        filename = parts[-1]
        if counts[filename] > 1 and len(parts) > 1:
            stem, dot, ext = filename.rpartition(".")
            names[key] = f"{stem} ({parts[-2]}).{ext}" if dot else f"{filename} ({parts[-2]})"
        else:
            names[key] = filename
    return names


class CarrierDriveLedger:
    """Carrier-level record of what is already in Drive (across pull days)."""

    def __init__(self, carrier_root: Path):
        self.path = Path(carrier_root) / DRIVE_LEDGER_NAME

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"items": {}}
        if self.path.is_symlink() or not self.path.is_file():
            raise DriveUploadHold("Drive ledger is missing or ambiguous")
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError) as exc:
            raise DriveUploadHold("Drive ledger is missing or ambiguous") from exc
        if not isinstance(data, dict) or not isinstance(data.get("items"), dict):
            raise DriveUploadHold("Drive ledger is missing or ambiguous")
        return data

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".drive-ledger-", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def verify_daily_root(drive: Any) -> None:
    """The Document Retrieval folder exists, is not trashed, and takes new files."""
    meta = drive.files().get(
        fileId=DAILY_DRIVE_ROOT_ID,
        fields="id,name,mimeType,trashed,capabilities(canAddChildren)",
        supportsAllDrives=True,
    ).execute()
    if (
        meta.get("mimeType") != FOLDER_MIME
        or meta.get("trashed")
        or meta.get("name") != DAILY_DRIVE_ROOT_NAME
        or not (meta.get("capabilities") or {}).get("canAddChildren")
    ):
        raise DriveUploadHold(f"Drive folder {DAILY_DRIVE_ROOT_NAME} is missing, renamed, or read-only")


def child_folder(drive: Any, parent_id: str, name: str) -> str:
    """Find or create one folder under ``parent_id``. Two with the same name hold."""
    query = (
        f"{_q(parent_id)} in parents and name = {_q(name)} "
        f"and mimeType = {_q(FOLDER_MIME)} and trashed = false"
    )
    found = drive.files().list(
        q=query, fields="files(id,name)", pageSize=10,
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute().get("files", [])
    if len(found) > 1:
        raise DriveUploadHold(f"Drive has {len(found)} folders named {name}")
    if found:
        return str(found[0]["id"])
    created = drive.files().create(
        body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
        fields="id", supportsAllDrives=True,
    ).execute()
    return str(created["id"])


def daily_day_folder(drive: Any, day: str) -> str:
    """``Document Retrieval/<YYYY-MM-DD>``, created on first use."""
    if not _DAY.fullmatch(day):
        raise DriveUploadHold("Drive date folder name must be YYYY-MM-DD")
    verify_daily_root(drive)
    return child_folder(drive, DAILY_DRIVE_ROOT_ID, day)


class CarrierDriveUpload:
    """Upload one carrier's dated pack to ``Document Retrieval/<YYYY-MM-DD>/<Carrier>/``."""

    def __init__(self, drive: Any, carrier: str, carrier_root: Path):
        if carrier not in CARRIER_QA_FOLDERS:
            raise DriveUploadHold(f"No Drive folder is mapped for carrier {carrier!r}")
        self.drive = drive
        self.carrier = carrier
        self.folder_title = CARRIER_QA_FOLDERS[carrier][0]
        self.ledger = CarrierDriveLedger(carrier_root)

    def date_folder(self, day: str) -> str:
        """The carrier's folder inside that day's folder."""
        return child_folder(self.drive, daily_day_folder(self.drive, day), self.folder_title)

    def _existing(self, folder_id: str, name: str) -> list[dict[str, Any]]:
        query = f"{_q(folder_id)} in parents and name = {_q(name)} and trashed = false"
        return list(self.drive.files().list(
            q=query, fields="files(id,name,md5Checksum)", pageSize=10,
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute().get("files", []))

    def _create(self, folder_id: str, name: str, content: bytes) -> str:
        created = self.drive.files().create(
            body={"name": name, "parents": [folder_id], "mimeType": "application/pdf"},
            media_body=_media_upload(content),
            fields="id,md5Checksum", supportsAllDrives=True,
        ).execute()
        md5 = created.get("md5Checksum")
        if md5 and md5 != hashlib.md5(content).hexdigest():
            raise DriveUploadHold(f"Drive copy of {name} does not match the local PDF")
        return str(created["id"])

    def upload_pack(self, pack: Path, day: str) -> DriveUploadResult:
        require_upload_environment()
        result = DriveUploadResult(carrier=self.carrier, day=day)
        files = pack_pdfs(Path(pack))
        data = self.ledger.load()
        items: dict[str, Any] = data["items"]
        by_sha = {str(v.get("sha256")): k for k, v in items.items() if isinstance(v, dict)}
        pending: list[tuple[str, Path, bytes, str]] = []
        for key, path in files:
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            if not _is_pdf(content):
                result.held.append({"file": key, "reason": "not a PDF"})
                continue
            if key in items:
                result.skipped.append({"file": key, "reason": f"already in Drive ({items[key].get('drive_day')})"})
                continue
            if digest in by_sha:
                result.skipped.append({"file": key, "reason": f"same PDF already in Drive as {by_sha[digest]}"})
                continue
            pending.append((key, path, content, digest))
        if not pending:
            return result
        folder_id = self.date_folder(day)
        result.folder_id = folder_id
        names = drive_names([key for key, _ in files])
        for key, path, content, digest in pending:
            name = names[key]
            try:
                existing = self._existing(folder_id, name)
                md5 = hashlib.md5(content).hexdigest()
                if len(existing) > 1:
                    raise DriveUploadHold(f"Drive has {len(existing)} files named {name}")
                if existing:
                    if existing[0].get("md5Checksum") != md5:
                        raise DriveUploadHold(f"A different {name} is already in Drive")
                    file_id = str(existing[0]["id"])
                    result.skipped.append({"file": key, "reason": "already in Drive (matched by name and checksum)"})
                else:
                    file_id = self._create(folder_id, name, content)
                    result.uploaded.append({"file": key, "name": name, "drive_file_id": file_id})
            except DriveUploadHold as exc:
                result.held.append({"file": key, "reason": str(exc)})
                continue
            items[key] = {
                "sha256": digest,
                "bytes": len(content),
                "drive_file_id": file_id,
                "drive_name": name,
                "drive_day": day,
                "drive_folder_id": folder_id,
            }
            by_sha[digest] = key
            self.ledger.save(data)
        return result
