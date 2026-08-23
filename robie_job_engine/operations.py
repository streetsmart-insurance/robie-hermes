from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


UTC = timezone.utc
DEFAULT_MAX_BYTES = 25 * 1024 * 1024
SAFE_MIME_PREFIXES = ("application/", "image/", "text/")
SAFE_EXACT_MIMES = {"audio/mpeg", "audio/mp4", "video/mp4"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _safe_filename(value: str) -> str:
    name = Path(value or "attachment").name
    name = re.sub(r"[^A-Za-z0-9._() -]+", "_", name).strip(" .")
    return (name or "attachment")[:180]


class OperationsStore:
    """Durable operational records layered on the authoritative Job DB."""

    def __init__(self, db_path: str, artifact_root: str | None = None) -> None:
        self.db_path = db_path
        self.artifact_root = Path(
            artifact_root
            or os.environ.get("ROBIE_ARTIFACT_ROOT")
            or "/opt/streetsmart-hermes/robie-job-engine/data/artifacts"
        )
        self.artifact_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.artifact_root, 0o700)
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _migrate(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    source_platform TEXT NOT NULL,
                    source_external_id TEXT NOT NULL,
                    original_name TEXT NOT NULL,
                    stored_path TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'STAGED',
                    destination_ref TEXT,
                    verification_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source_platform, source_external_id, sha256)
                );
                CREATE INDEX IF NOT EXISTS idx_artifacts_job ON artifacts(job_id);

                CREATE TABLE IF NOT EXISTS schedules (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    interval_minutes INTEGER NOT NULL CHECK(interval_minutes >= 5),
                    enabled INTEGER NOT NULL DEFAULT 1,
                    next_run_at TEXT NOT NULL,
                    last_run_at TEXT,
                    last_job_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_schedules_due
                    ON schedules(enabled, next_run_at);

                CREATE TABLE IF NOT EXISTS assignments (
                    id TEXT PRIMARY KEY,
                    external_id TEXT UNIQUE,
                    requested_by TEXT,
                    account_id TEXT,
                    task_text TEXT NOT NULL,
                    skill TEXT,
                    priority TEXT NOT NULL DEFAULT 'NORMAL',
                    due_at TEXT,
                    attachment_refs_json TEXT NOT NULL DEFAULT '[]',
                    schedule_text TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    job_id TEXT,
                    status TEXT NOT NULL DEFAULT 'NEW',
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS model_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    outcome TEXT NOT NULL,
                    error_class TEXT,
                    latency_ms INTEGER,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS report_runs (
                    id TEXT PRIMARY KEY,
                    report_type TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    window_start TEXT,
                    window_end TEXT,
                    status TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS job_recordings (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    segment_number INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    local_path TEXT NOT NULL,
                    stop_file TEXT NOT NULL,
                    capture_pid INTEGER,
                    drive_file_id TEXT,
                    drive_url TEXT,
                    sha256 TEXT,
                    size_bytes INTEGER,
                    final_job_status TEXT,
                    failure TEXT,
                    reference_approved INTEGER NOT NULL DEFAULT 0,
                    training_approved INTEGER NOT NULL DEFAULT 0,
                    redacted INTEGER NOT NULL DEFAULT 0,
                    review_notes TEXT,
                    approved_by TEXT,
                    started_at TEXT NOT NULL,
                    stopped_at TEXT,
                    uploaded_at TEXT,
                    UNIQUE(job_id, segment_number)
                );
                """
            )

    def ingest_cached_file(
        self,
        *,
        job_id: str,
        source_path: str,
        source_external_id: str,
        mime_type: str | None = None,
        source_platform: str = "google_chat",
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> dict[str, Any]:
        src = Path(source_path).resolve(strict=True)
        if not src.is_file():
            raise ValueError("attachment is not a regular file")
        size = src.stat().st_size
        if size <= 0 or size > max_bytes:
            raise ValueError(f"attachment size {size} is outside the allowed range")
        mime = mime_type or mimetypes.guess_type(src.name)[0] or "application/octet-stream"
        if not (mime.startswith(SAFE_MIME_PREFIXES) or mime in SAFE_EXACT_MIMES):
            raise ValueError(f"attachment MIME type is not allowed: {mime}")
        digest = hashlib.sha256()
        with src.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        sha = digest.hexdigest()
        artifact_id = str(uuid.uuid4())
        safe_name = _safe_filename(src.name)
        job_dir = self.artifact_root / job_id
        job_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(job_dir, 0o700)
        final = job_dir / f"{artifact_id}-{safe_name}"
        fd, temp_name = tempfile.mkstemp(prefix=".ingest-", dir=job_dir)
        os.close(fd)
        try:
            shutil.copyfile(src, temp_name)
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, final)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        now = _now()
        with self._connect() as conn:
            try:
                conn.execute(
                    """INSERT INTO artifacts
                    (id,job_id,source_platform,source_external_id,original_name,stored_path,
                     mime_type,size_bytes,sha256,status,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,'STAGED',?,?)""",
                    (artifact_id, job_id, source_platform, source_external_id,
                     safe_name, str(final), mime, size, sha, now, now),
                )
            except sqlite3.IntegrityError:
                final.unlink(missing_ok=True)
                row = conn.execute(
                    """SELECT * FROM artifacts
                       WHERE source_platform=? AND source_external_id=? AND sha256=?""",
                    (source_platform, source_external_id, sha),
                ).fetchone()
                return dict(row)
        return self.get_artifact(artifact_id)

    def get_artifact(self, artifact_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
        if row is None:
            raise KeyError(artifact_id)
        return dict(row)

    def list_artifacts(self, job_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM artifacts WHERE job_id=? ORDER BY created_at", (job_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_artifact_uploaded(
        self, artifact_id: str, destination_ref: str, verification: dict[str, Any]
    ) -> None:
        if not verification.get("verified"):
            raise ValueError("uploaded artifact requires independent verification evidence")
        with self._connect() as conn:
            conn.execute(
                """UPDATE artifacts SET status='VERIFIED', destination_ref=?,
                   verification_json=?, updated_at=? WHERE id=?""",
                (destination_ref, _json(verification), _now(), artifact_id),
            )

    def create_schedule(
        self, name: str, action_type: str, payload: dict[str, Any],
        interval_minutes: int, *, next_run_at: str | None = None,
    ) -> dict[str, Any]:
        if interval_minutes < 5:
            raise ValueError("interval must be at least 5 minutes")
        sid, now = str(uuid.uuid4()), _now()
        next_at = next_run_at or now
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO schedules
                (id,name,action_type,payload_json,interval_minutes,next_run_at,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?)""",
                (sid, name, action_type, _json(payload), interval_minutes, next_at, now, now),
            )
        return self.get_schedule(sid)

    def ensure_schedule(
        self, name: str, action_type: str, payload: dict[str, Any], interval_minutes: int
    ) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT id FROM schedules WHERE name=?", (name,)).fetchone()
        if row:
            return self.get_schedule(row["id"])
        return self.create_schedule(name, action_type, payload, interval_minutes)

    def get_schedule(self, schedule_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
        if row is None:
            raise KeyError(schedule_id)
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def claim_due_schedules(self, limit: int = 25, now: str | None = None) -> list[dict[str, Any]]:
        at = now or _now()
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM schedules WHERE enabled=1 AND next_run_at<=?
                   ORDER BY next_run_at LIMIT ?""", (at, limit)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def advance_schedule(self, schedule_id: str, job_id: str, *, from_time: str | None = None) -> None:
        schedule = self.get_schedule(schedule_id)
        base = datetime.fromisoformat(from_time or _now())
        if base.tzinfo is None:
            base = base.replace(tzinfo=UTC)
        next_at = (base + timedelta(minutes=schedule["interval_minutes"])).isoformat()
        with self._connect() as conn:
            conn.execute(
                """UPDATE schedules SET last_run_at=?, last_job_id=?, next_run_at=?, updated_at=?
                   WHERE id=?""", (_now(), job_id, next_at, _now(), schedule_id)
            )

    def dashboard_rows(self) -> dict[str, list[dict[str, Any]]]:
        with self._connect() as conn:
            jobs = [dict(r) for r in conn.execute(
                """SELECT id,action_type,payload_json,status,attempt_count,verification_count,next_wakeup_at,
                          created_at,updated_at,completed_at,last_error
                   FROM jobs ORDER BY created_at DESC LIMIT 1000"""
            ).fetchall()]
            for item in jobs:
                item["payload"] = json.loads(item.pop("payload_json") or "{}")
                counts = conn.execute(
                    """SELECT COUNT(*) AS verified_count,
                              COALESCE(SUM(CASE WHEN authoritative=1 AND verified=1 THEN 1 ELSE 0 END),0) AS authoritative_count
                       FROM verification_evidence WHERE job_id=? AND verified=1""",
                    (item["id"],),
                ).fetchone()
                item["verified_evidence_count"] = int(counts["verified_count"] or 0)
                item["authoritative_evidence_count"] = int(counts["authoritative_count"] or 0)
                recording = conn.execute(
                    """SELECT status,drive_url,failure,reference_approved,training_approved,redacted
                       FROM job_recordings WHERE job_id=? ORDER BY segment_number DESC LIMIT 1""",
                    (item["id"],),
                ).fetchone()
                if recording:
                    item.update({f"recording_{key}": value for key, value in dict(recording).items()})
            artifacts = [dict(r) for r in conn.execute(
                """SELECT id,job_id,source_platform,original_name,mime_type,size_bytes,sha256,
                          status,destination_ref,created_at,updated_at
                   FROM artifacts ORDER BY created_at DESC LIMIT 1000"""
            ).fetchall()]
            schedules = [dict(r) for r in conn.execute(
                """SELECT id,name,action_type,interval_minutes,enabled,next_run_at,last_run_at,
                          last_job_id,updated_at FROM schedules ORDER BY name"""
            ).fetchall()]
            evidence = [dict(r) for r in conn.execute(
                """SELECT job_id,verified,method,source,authoritative,expected_json,
                          observed_json,locator,evidence_sha256,captured_at,created_at
                   FROM verification_evidence ORDER BY created_at DESC LIMIT 1000"""
            ).fetchall()]
            recordings = [dict(r) for r in conn.execute(
                """SELECT id,job_id,segment_number,status,drive_url,sha256,size_bytes,
                          final_job_status,failure,reference_approved,training_approved,
                          redacted,review_notes,approved_by,started_at,stopped_at,uploaded_at
                   FROM job_recordings ORDER BY started_at DESC LIMIT 1000"""
            ).fetchall()]
        return {"jobs": jobs, "artifacts": artifacts, "schedules": schedules,
                "evidence": evidence, "recordings": recordings}


def ingest_chat_attachments(
    db_path: str,
    job_id: str,
    message_id: str,
    attachments: Iterable[tuple[str, str]],
    *,
    artifact_root: str | None = None,
) -> list[dict[str, Any]]:
    store = OperationsStore(db_path, artifact_root)
    records = []
    for index, (path, mime) in enumerate(attachments):
        records.append(store.ingest_cached_file(
            job_id=job_id,
            source_path=path,
            source_external_id=f"{message_id}:{index}",
            mime_type=mime,
        ))
    return records
