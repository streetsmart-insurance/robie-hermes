from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol


UTC = timezone.utc
DEFAULT_RECORDING_ROOT = "/opt/streetsmart-hermes/robie-job-engine/data/recordings"
DEFAULT_CDP_URL = "http://127.0.0.1:9222"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _safe(value: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in value)[:120]


def _enabled(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def recording_health(
    *,
    environ: dict[str, str] | None = None,
    check_cdp: bool = True,
) -> dict[str, Any]:
    """Return a secret-free readiness report for production recording."""
    env = dict(os.environ if environ is None else environ)
    issues: list[str] = []
    warnings: list[str] = []
    enabled = _enabled(env.get("ROBIE_RECORD_ALL_JOBS", "0"))
    root = Path(env.get("ROBIE_RECORDING_ROOT") or DEFAULT_RECORDING_ROOT)
    cdp_url = (env.get("ROBIE_BROWSER_CDP_URL") or DEFAULT_CDP_URL).rstrip("/")
    parsed_cdp = urllib.parse.urlsplit(cdp_url)
    cdp_host = parsed_cdp.hostname or ""
    if parsed_cdp.port:
        cdp_host = f"{cdp_host}:{parsed_cdp.port}"
    cdp_display = urllib.parse.urlunsplit((
        parsed_cdp.scheme,
        cdp_host,
        parsed_cdp.path,
        "",
        "",
    ))
    folder_configured = bool(env.get("ROBIE_RECORDINGS_DRIVE_FOLDER_ID", "").strip())
    token_file = env.get("ROBIE_GOOGLE_TOKEN_FILE", "").strip()

    if not enabled:
        issues.append("ROBIE_RECORD_ALL_JOBS is not enabled")
    if not folder_configured:
        issues.append("ROBIE_RECORDINGS_DRIVE_FOLDER_ID is not configured")
    if shutil.which("ffmpeg") is None:
        issues.append("ffmpeg is not installed or is not on PATH")
    if importlib.util.find_spec("playwright") is None:
        issues.append("Python Playwright is not installed")
    if token_file and not Path(token_file).is_file():
        issues.append("ROBIE_GOOGLE_TOKEN_FILE does not exist")
    elif not token_file:
        warnings.append("Drive upload will use application-default credentials")

    root_parent = root if root.exists() else root.parent
    if not root_parent.exists() or not os.access(root_parent, os.W_OK):
        issues.append("recording directory is not writable")

    if check_cdp:
        try:
            with urllib.request.urlopen(f"{cdp_url}/json/version", timeout=2) as response:
                if int(getattr(response, "status", 200)) >= 400:
                    raise RuntimeError(f"HTTP {response.status}")
        except Exception as exc:
            issues.append(f"Chrome CDP is unavailable at {cdp_display}: {type(exc).__name__}")

    return {
        "ready": not issues,
        "enabled": enabled,
        "recording_root": str(root),
        "cdp_url": cdp_display,
        "drive_folder_configured": folder_configured,
        "google_token_configured": bool(token_file),
        "issues": issues,
        "warnings": warnings,
    }


class CaptureBackend(Protocol):
    def start(self, output_path: Path, stop_file: Path) -> int: ...
    def stop(self, pid: int, stop_file: Path, output_path: Path) -> None: ...


class RecordingUploader(Protocol):
    def upload(self, path: Path, file_name: str) -> tuple[str, str]: ...


class RecordingRequiredError(RuntimeError):
    """Raised before executable work when required capture is unavailable."""


class SubprocessTabCapture:
    """Capture only the persistent Chrome tab through CDP, never the desktop."""

    def __init__(
        self,
        *,
        cdp_url: str = DEFAULT_CDP_URL,
        fps: int = 4,
        ready_timeout: float = 20.0,
    ) -> None:
        self.cdp_url = cdp_url
        self.fps = fps
        self.ready_timeout = ready_timeout

    def start(self, output_path: Path, stop_file: Path) -> int:
        ready_file = output_path.with_suffix(".ready")
        ready_file.unlink(missing_ok=True)
        command = [
            sys.executable,
            "-m",
            "robie_job_engine.browser_capture",
            "--cdp-url", self.cdp_url,
            "--output", str(output_path),
            "--stop-file", str(stop_file),
            "--ready-file", str(ready_file),
            "--fps", str(self.fps),
            "--hint-file", str(output_path.with_suffix(".hint.json")),
        ]
        log_path = output_path.with_suffix(".capture.log")
        log_handle = log_path.open("ab")
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        finally:
            log_handle.close()

        deadline = time.monotonic() + self.ready_timeout
        while time.monotonic() < deadline:
            if ready_file.is_file():
                return int(process.pid)
            if process.poll() is not None:
                raise RuntimeError(
                    f"browser capture exited before readiness (code {process.returncode})"
                )
            time.sleep(0.05)

        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        raise RuntimeError("browser capture did not become ready before timeout")

    def stop(self, pid: int, stop_file: Path, output_path: Path) -> None:
        stop_file.touch(mode=0o600, exist_ok=True)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                waited, _ = os.waitpid(pid, os.WNOHANG)
                if waited == pid:
                    break
            except ChildProcessError:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
            except ProcessLookupError:
                break
            time.sleep(0.25)
        else:
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            time.sleep(1)
        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError("browser capture produced no video")
        output_path.with_suffix(".ready").unlink(missing_ok=True)


class GoogleDriveUploader:
    def __init__(self, folder_id: str) -> None:
        if not folder_id:
            raise ValueError("recordings Drive folder ID is required")
        self.folder_id = folder_id

    def upload(self, path: Path, file_name: str) -> tuple[str, str]:
        import google.auth
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload

        scope = "https://www.googleapis.com/auth/drive"
        token_file = os.environ.get("ROBIE_GOOGLE_TOKEN_FILE", "").strip()
        if token_file:
            from google.oauth2.credentials import Credentials
            credentials = Credentials.from_authorized_user_file(token_file)
            granted = set(credentials.scopes or ())
            if scope not in granted:
                raise PermissionError("Google token lacks required Drive scope")
        else:
            credentials, _ = google.auth.default(scopes=[scope])
        drive = build("drive", "v3", credentials=credentials, cache_discovery=False)
        result = drive.files().create(
            body={"name": file_name, "parents": [self.folder_id]},
            media_body=MediaFileUpload(str(path), mimetype="video/webm", resumable=True),
            fields="id,webViewLink",
            supportsAllDrives=True,
        ).execute()
        file_id = str(result["id"])
        return file_id, str(
            result.get("webViewLink")
            or f"https://drive.google.com/file/d/{file_id}/view"
        )


class RecordingStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _migrate(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
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
                    failure_stage TEXT,
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
                CREATE INDEX IF NOT EXISTS idx_job_recordings_job
                    ON job_recordings(job_id, segment_number DESC);
                """
            )
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(job_recordings)")}
            if "failure_stage" not in columns:
                conn.execute("ALTER TABLE job_recordings ADD COLUMN failure_stage TEXT")

    def create(self, job_id: str, local_path: Path, stop_file: Path) -> dict[str, Any]:
        with self._connect() as conn:
            segment = int(conn.execute(
                "SELECT COALESCE(MAX(segment_number),0)+1 FROM job_recordings WHERE job_id=?",
                (job_id,),
            ).fetchone()[0])
            recording_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO job_recordings
                (id,job_id,segment_number,status,local_path,stop_file,started_at)
                VALUES(?,?,?,'STARTING',?,?,?)""",
                (recording_id, job_id, segment, str(local_path), str(stop_file), _now()),
            )
        return self.get(recording_id)

    def update(self, recording_id: str, **fields: Any) -> dict[str, Any]:
        allowed = {
            "status", "capture_pid", "drive_file_id", "drive_url", "sha256",
            "size_bytes", "final_job_status", "failure", "stopped_at", "uploaded_at",
            "reference_approved", "training_approved", "redacted", "review_notes",
            "approved_by", "failure_stage",
        }
        invalid = set(fields) - allowed
        if invalid:
            raise ValueError(f"unsupported recording fields: {sorted(invalid)}")
        if not fields:
            return self.get(recording_id)
        values = list(fields.values()) + [recording_id]
        assignments = ",".join(f"{name}=?" for name in fields)
        with self._connect() as conn:
            conn.execute(f"UPDATE job_recordings SET {assignments} WHERE id=?", values)
        return self.get(recording_id)

    def get(self, recording_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM job_recordings WHERE id=?", (recording_id,)).fetchone()
        if row is None:
            raise KeyError(recording_id)
        return dict(row)

    def active(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM job_recordings WHERE job_id=?
                   AND status IN ('STARTING','RECORDING','STOPPING','UPLOADING')
                   ORDER BY segment_number DESC LIMIT 1""",
                (job_id,),
            ).fetchone()
        return dict(row) if row else None

    def latest(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM job_recordings WHERE job_id=? ORDER BY segment_number DESC LIMIT 1",
                (job_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_for_job(self, job_id: str) -> list[dict[str, Any]]:
        """Return every recording segment for a Job, oldest segment first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM job_recordings WHERE job_id=? ORDER BY segment_number",
                (job_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def approve_reference(
        self, recording_id: str, *, approved_by: str, notes: str, redacted: bool
    ) -> dict[str, Any]:
        if not redacted:
            raise ValueError("a recording must be reviewed and redacted before reference approval")
        if not notes.strip() or not approved_by.strip():
            raise ValueError("reference approval requires reviewer and notes")
        return self.update(
            recording_id,
            reference_approved=1,
            redacted=1,
            review_notes=notes.strip(),
            approved_by=approved_by.strip(),
        )

    def approve_training(self, recording_id: str, *, approved_by: str) -> dict[str, Any]:
        current = self.get(recording_id)
        if not current["reference_approved"] or not current["redacted"]:
            raise ValueError("training approval requires a redacted, reference-approved recording")
        return self.update(recording_id, training_approved=1, approved_by=approved_by.strip())

    def reference_manifest(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT id,job_id,drive_url,review_notes,approved_by,final_job_status
                   FROM job_recordings WHERE reference_approved=1 AND redacted=1
                   ORDER BY started_at DESC"""
            ).fetchall()
        return [dict(row) for row in rows]


class RecordingManager:
    def __init__(
        self,
        db_path: str | Path,
        *,
        root: str | Path | None = None,
        capture: CaptureBackend | None = None,
        uploader: RecordingUploader | None = None,
        enabled: bool | None = None,
        keep_local: bool | None = None,
    ) -> None:
        self.store = RecordingStore(db_path)
        self.root = Path(root or os.environ.get("ROBIE_RECORDING_ROOT") or DEFAULT_RECORDING_ROOT)
        if enabled is None:
            enabled = _enabled(os.environ.get("ROBIE_RECORD_ALL_JOBS", "0"))
        self.enabled = enabled
        if keep_local is None:
            delete = os.environ.get("ROBIE_DELETE_LOCAL_RECORDING_AFTER_UPLOAD", "1").lower() in {
                "1",
                "true",
                "yes",
            }
            keep_local = not delete
        self.keep_local = keep_local
        if self.enabled:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.root, 0o700)
        self.capture = capture or SubprocessTabCapture(
            cdp_url=os.environ.get("ROBIE_BROWSER_CDP_URL", DEFAULT_CDP_URL),
            fps=int(os.environ.get("ROBIE_RECORDING_FPS", "4")),
        )
        folder_id = os.environ.get("ROBIE_RECORDINGS_DRIVE_FOLDER_ID", "")
        self.uploader = uploader or (GoogleDriveUploader(folder_id) if folder_id else None)

    def start(self, job_id: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        active = self.store.active(job_id)
        if active:
            return active
        job_dir = self.root / _safe(job_id)
        job_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        unique = uuid.uuid4().hex
        output = job_dir / f"{_safe(job_id)}-{stamp}-{unique}.webm"
        stop_file = output.with_suffix(".stop")
        os.environ["ROBIE_RECORDING_HINT_FILE"] = str(output.with_suffix(".hint.json"))
        os.environ["ROBIE_RECORDING_JOB_ID"] = str(job_id)
        recording = self.store.create(job_id, output, stop_file)
        try:
            pid = self.capture.start(output, stop_file)
            return self.store.update(recording["id"], status="RECORDING", capture_pid=pid)
        except Exception as exc:
            return self.store.update(
                recording["id"], status="FAILED", failure_stage="START",
                failure=f"{type(exc).__name__}: {exc}"
            )

    def start_required(self, job_id: str) -> dict[str, Any]:
        """Start capture and fail closed before any executable work."""
        if not self.enabled:
            raise RecordingRequiredError("recording is required but disabled")
        recording = self.start(job_id)
        if not recording or recording.get("status") != "RECORDING":
            detail = (recording or {}).get("failure") or "capture did not enter RECORDING"
            raise RecordingRequiredError(f"recording start failed: {detail}")
        return recording

    def stop_and_upload(self, job_id: str, final_job_status: str) -> dict[str, Any] | None:
        recording = self.store.active(job_id)
        if not recording:
            return self.store.latest(job_id)
        recording = self.store.update(
            recording["id"], status="STOPPING", final_job_status=final_job_status
        )
        output = Path(recording["local_path"])
        try:
            self.capture.stop(
                int(recording["capture_pid"]), Path(recording["stop_file"]), output
            )
            digest = hashlib.sha256(output.read_bytes()).hexdigest()
            recording = self.store.update(
                recording["id"], status="UPLOADING", sha256=digest,
                size_bytes=output.stat().st_size, stopped_at=_now(),
            )
            if self.uploader is None:
                raise RuntimeError("ROBIE recordings Drive folder is not configured")
            name = f"ROBIE Job {job_id} — segment {recording['segment_number']} — {final_job_status}.webm"
            file_id, url = self.uploader.upload(output, name)
            ready = self.store.update(
                recording["id"], status="READY", drive_file_id=file_id,
                drive_url=url, uploaded_at=_now(),
            )
            if not self.keep_local:
                output.unlink(missing_ok=True)
                Path(recording["stop_file"]).unlink(missing_ok=True)
                output.with_suffix(".ready").unlink(missing_ok=True)
            return ready
        except Exception as exc:
            stage = "UPLOAD" if recording.get("status") == "UPLOADING" else "FINALIZE"
            return self.store.update(
                recording["id"], status="FAILED", failure_stage=stage,
                failure=f"{type(exc).__name__}: {exc}",
                stopped_at=_now(),
            )

    def safe_start(self, job_id: str) -> None:
        try:
            self.start(job_id)
        except Exception:
            pass

    def safe_stop(self, job_id: str, final_job_status: str) -> None:
        try:
            self.stop_and_upload(job_id, final_job_status)
        except Exception:
            pass

    def list_for_job(self, job_id: str) -> list[dict[str, Any]]:
        return self.store.list_for_job(job_id)

    def completion_error(self, job_id: str) -> str | None:
        """Explain why recording evidence cannot authorize COMPLETE."""
        segments = self.list_for_job(job_id)
        if not segments:
            return "Recording failed: no recording segment exists"
        for segment in segments:
            if segment.get("status") != "READY" or not segment.get("drive_url"):
                if segment.get("failure_stage") == "UPLOAD":
                    return (
                        "Recording upload failed for segment "
                        f"{segment['segment_number']}: {segment.get('failure') or 'unknown error'}"
                    )
                return (
                    "Recording failed for segment "
                    f"{segment['segment_number']}: {segment.get('failure') or segment.get('status')}"
                )
        return None
