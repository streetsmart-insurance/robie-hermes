"""Nightly consistent snapshot of playground_memory.db to a private GCS bucket.

The copy is sqlite's backup API, then an upload with the VM's attached
service account. A key file is refused. This module does not install the
timer and does not create the bucket.

Env:
  ROBIE_PLAYGROUND_MEMORY_BUCKET          bucket name; empty skips the upload
  ROBIE_PLAYGROUND_MEMORY_BACKUP_KEEP_DAYS  how many days of snapshots to keep
  ROBIE_PLAYGROUND_MEMORY_BACKUP_PREFIX   object prefix, default playground-memory
  ROBIE_JOB_DB                            jobs.db path; the memory file sits beside it
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .playground_memory import MEMORY_DB_MODE, _restrict_memory_files, memory_db_path

BUCKET_ENV = "ROBIE_PLAYGROUND_MEMORY_BUCKET"
KEEP_DAYS_ENV = "ROBIE_PLAYGROUND_MEMORY_BACKUP_KEEP_DAYS"
PREFIX_ENV = "ROBIE_PLAYGROUND_MEMORY_BACKUP_PREFIX"
DEFAULT_KEEP_DAYS = 14
DEFAULT_PREFIX = "playground-memory"
_KEY_ENVS = (
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CHAT_SERVICE_ACCOUNT_JSON",
)
_SCOPE = "https://www.googleapis.com/auth/devstorage.read_write"


def backup_bucket() -> str:
    raw = os.environ.get(BUCKET_ENV, "").strip()
    if raw.startswith("gs://"):
        raw = raw[5:]
    return raw.strip("/")


def keep_days() -> int:
    raw = os.environ.get(KEEP_DAYS_ENV, "").strip()
    if not raw:
        return DEFAULT_KEEP_DAYS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_KEEP_DAYS
    return value if value > 0 else DEFAULT_KEEP_DAYS


def backup_prefix() -> str:
    raw = os.environ.get(PREFIX_ENV, "").strip().strip("/")
    return raw or DEFAULT_PREFIX


def object_name(*, now: datetime, hostname: str, prefix: str | None = None) -> str:
    stamp = _aware(now).astimezone(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    host = "".join(ch if ch.isalnum() or ch in "-." else "-" for ch in hostname) or "host"
    return f"{prefix or backup_prefix()}/{host}/{stamp}.sqlite"


def assert_no_key_file() -> None:
    """The attached service account is the only accepted credential."""
    for name in _KEY_ENVS:
        if os.environ.get(name, "").strip():
            raise RuntimeError(
                "Memory backup refuses key files. Unset "
                f"{name} and use the VM attached service account."
            )


def snapshot_memory_db(jobs_db: str, dest: Path) -> None:
    """Consistent copy via sqlite3.Connection.backup. Does not upload."""
    source_path = memory_db_path(jobs_db)
    if not Path(source_path).exists():
        raise FileNotFoundError(source_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(source_path)
    target = sqlite3.connect(dest)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    os.chmod(dest, MEMORY_DB_MODE)


def snapshots_to_delete(
    objects: list[tuple[str, datetime]],
    *,
    now: datetime,
    days: int,
) -> list[str]:
    """Names whose snapshot time is at least ``days`` old."""
    cutoff = _aware(now) - timedelta(days=days)
    stale: list[str] = []
    for name, created in objects:
        if _aware(created) <= cutoff:
            stale.append(name)
    return stale


def restore_snapshot(jobs_db: str, snapshot: Path) -> str:
    """Replace the live memory file with a consistent copy of ``snapshot``."""
    dest = memory_db_path(jobs_db)
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(snapshot)
    target = sqlite3.connect(dest)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    _restrict_memory_files(dest)
    return dest


def run_backup(
    jobs_db: str,
    *,
    now: datetime,
    hostname: str,
    snapshot_dir: Path,
    upload,
    list_objects,
    delete_object,
) -> dict[str, object]:
    """Snapshot, upload, then delete snapshots older than the keep window.

    ``upload``, ``list_objects``, and ``delete_object`` are injected so tests
    do not talk to GCS. An upload failure leaves older snapshots in place.
    """
    bucket = backup_bucket()
    if not bucket:
        return {"status": "skipped", "reason": "bucket unset"}
    if not Path(memory_db_path(jobs_db)).exists():
        return {"status": "skipped", "reason": "no memory database"}
    assert_no_key_file()
    name = object_name(now=now, hostname=hostname)
    dest = snapshot_dir / Path(name).name
    snapshot_memory_db(jobs_db, dest)
    upload(bucket, name, dest.read_bytes())
    present = list(list_objects(bucket, backup_prefix() + "/"))
    stale = snapshots_to_delete(present, now=now, days=keep_days())
    for old in stale:
        delete_object(bucket, old)
    return {"status": "uploaded", "object": name, "deleted": stale}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Back up or restore Playground memory")
    parser.add_argument(
        "--db",
        default=os.environ.get(
            "ROBIE_JOB_DB",
            "/opt/streetsmart-hermes-test/robie-job-engine/data/jobs.db",
        ),
    )
    sub = parser.add_subparsers(dest="command")
    restore = sub.add_parser("restore", help="Copy one GCS snapshot back over the memory file")
    restore.add_argument("--object", required=True, help="Object name inside the bucket")
    restore.add_argument("--confirm", action="store_true")
    sub.add_parser("list", help="List snapshot object names")
    args = parser.parse_args(argv)
    if args.command == "restore":
        return _restore_command(args)
    if args.command == "list":
        return _list_command()
    return _backup_command(args.db)


def _backup_command(jobs_db: str) -> int:
    if not backup_bucket():
        print("Memory backup bucket is unset. No snapshot uploaded.")
        return 0
    import tempfile

    now = datetime.now(timezone.utc)
    hostname = os.uname().nodename
    with tempfile.TemporaryDirectory() as tmp:
        result = run_backup(
            jobs_db,
            now=now,
            hostname=hostname,
            snapshot_dir=Path(tmp),
            upload=_upload,
            list_objects=_list_objects,
            delete_object=_delete_object,
        )
    print(result["status"])
    if result["status"] == "uploaded":
        print(result["object"])
    return 0


def _restore_command(args: argparse.Namespace) -> int:
    if not args.confirm:
        print("Refusing to restore without --confirm.")
        return 2
    if not backup_bucket():
        print("Memory backup bucket is unset. Nothing restored.")
        return 1
    import tempfile

    data = _download(backup_bucket(), args.object)
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / "restore.sqlite"
        snapshot.write_bytes(data)
        os.chmod(snapshot, MEMORY_DB_MODE)
        dest = restore_snapshot(args.db, snapshot)
    print(f"Restored {args.object} into {dest}")
    return 0


def _list_command() -> int:
    bucket = backup_bucket()
    if not bucket:
        print("Memory backup bucket is unset.")
        return 0
    for name, _created in _list_objects(bucket, backup_prefix() + "/"):
        print(name)
    return 0


def _credentials():
    assert_no_key_file()
    import google.auth
    from google.auth.transport.requests import Request

    credentials, _project = google.auth.default(scopes=[_SCOPE])
    module = type(credentials).__module__
    if "service_account" in module:
        raise RuntimeError(
            "Memory backup refuses key files. Use the VM attached service account."
        )
    credentials.refresh(Request())
    token = getattr(credentials, "token", "")
    if not token:
        raise RuntimeError("Attached service account did not return a token.")
    return token


def _request(method: str, url: str, data: bytes | None = None, content_type: str | None = None) -> bytes:
    token = _credentials()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if content_type:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"GCS {method} failed ({exc.code}): {detail}") from exc


def _upload(bucket: str, name: str, data: bytes) -> None:
    quoted = urllib.parse.quote(name, safe="")
    url = (
        "https://storage.googleapis.com/upload/storage/v1/b/"
        f"{urllib.parse.quote(bucket, safe='')}/o?uploadType=media&name={quoted}"
    )
    _request("POST", url, data=data, content_type="application/vnd.sqlite3")


def _download(bucket: str, name: str) -> bytes:
    quoted = urllib.parse.quote(name, safe="")
    url = (
        "https://storage.googleapis.com/storage/v1/b/"
        f"{urllib.parse.quote(bucket, safe='')}/o/{quoted}?alt=media"
    )
    return _request("GET", url)


def _list_objects(bucket: str, prefix: str) -> list[tuple[str, datetime]]:
    import json

    url = (
        "https://storage.googleapis.com/storage/v1/b/"
        f"{urllib.parse.quote(bucket, safe='')}/o?prefix="
        f"{urllib.parse.quote(prefix, safe='')}"
    )
    payload = json.loads(_request("GET", url).decode("utf-8"))
    found: list[tuple[str, datetime]] = []
    for item in payload.get("items") or []:
        name = str(item.get("name") or "")
        created = _parse_time(str(item.get("timeCreated") or "")) or _stamp_from_name(name)
        if name and created is not None:
            found.append((name, created))
    return found


def _delete_object(bucket: str, name: str) -> None:
    quoted = urllib.parse.quote(name, safe="")
    url = (
        "https://storage.googleapis.com/storage/v1/b/"
        f"{urllib.parse.quote(bucket, safe='')}/o/{quoted}"
    )
    _request("DELETE", url)


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _aware(parsed)


def _stamp_from_name(name: str) -> datetime | None:
    stem = Path(name).stem
    if len(stem) < 16 or "T" not in stem:
        return None
    try:
        parsed = datetime.strptime(stem, "%Y-%m-%dT%H%M%SZ")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc)


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment
