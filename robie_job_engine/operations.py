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


class ModelBudgetExceeded(RuntimeError):
    """Raised before a model call would exceed a durable per-Job budget."""


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

                CREATE TABLE IF NOT EXISTS release_records (
                    environment TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    commit_sha TEXT NOT NULL,
                    artifact_uri TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PREPARED',
                    evidence_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(environment, digest)
                );
                CREATE TABLE IF NOT EXISTS release_pointers (
                    environment TEXT PRIMARY KEY,
                    current_digest TEXT,
                    previous_digest TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS release_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    environment TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    from_digest TEXT,
                    to_digest TEXT,
                    actor TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            model_attempt_columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(model_attempts)")
            }
            for name, declaration in {
                "input_tokens": "INTEGER NOT NULL DEFAULT 0",
                "output_tokens": "INTEGER NOT NULL DEFAULT 0",
                "cache_read_tokens": "INTEGER NOT NULL DEFAULT 0",
                "thinking_tokens": "INTEGER NOT NULL DEFAULT 0",
                "estimated_cost_microusd": "INTEGER NOT NULL DEFAULT 0",
            }.items():
                if name not in model_attempt_columns:
                    conn.execute(f"ALTER TABLE model_attempts ADD COLUMN {name} {declaration}")
            report_columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(report_runs)")
            }
            for name, declaration in {
                "dedupe_key": "TEXT",
                "updated_at": "TEXT",
                "error": "TEXT",
            }.items():
                if name not in report_columns:
                    conn.execute(f"ALTER TABLE report_runs ADD COLUMN {name} {declaration}")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_report_runs_dedupe ON report_runs(dedupe_key)"
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

    def record_model_attempt(
        self,
        *,
        job_id: str,
        provider: str,
        model: str,
        ordinal: int,
        outcome: str,
        error_class: str | None = None,
        latency_ms: int | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        thinking_tokens: int = 0,
        estimated_cost_usd: float = 0.0,
    ) -> None:
        token_values = (input_tokens, output_tokens, cache_read_tokens, thinking_tokens)
        if any(value < 0 for value in token_values):
            raise ValueError("token counts cannot be negative")
        if estimated_cost_usd < 0:
            raise ValueError("estimated model cost cannot be negative")
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO model_attempts
                (job_id,provider,model,ordinal,outcome,error_class,latency_ms,
                 input_tokens,output_tokens,cache_read_tokens,thinking_tokens,
                 estimated_cost_microusd,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    job_id, provider, model, ordinal, outcome, error_class, latency_ms,
                    input_tokens, output_tokens, cache_read_tokens, thinking_tokens,
                    round(estimated_cost_usd * 1_000_000), _now(),
                ),
            )

    def model_usage(self, job_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT COALESCE(SUM(input_tokens),0) AS input_tokens,
                          COALESCE(SUM(output_tokens),0) AS output_tokens,
                          COALESCE(SUM(cache_read_tokens),0) AS cache_read_tokens,
                          COALESCE(SUM(thinking_tokens),0) AS thinking_tokens,
                          COALESCE(SUM(estimated_cost_microusd),0) AS cost_microusd,
                          COUNT(*) AS model_attempt_count
                   FROM model_attempts WHERE job_id=?""",
                (job_id,),
            ).fetchone()
        result = dict(row)
        result["total_tokens"] = int(result["input_tokens"]) + int(result["output_tokens"])
        result["estimated_cost_usd"] = int(result.pop("cost_microusd")) / 1_000_000
        return result

    def enforce_model_budget(
        self,
        job_id: str,
        *,
        max_total_tokens: int,
        max_cost_usd: float,
        reserve_tokens: int = 0,
        reserve_cost_usd: float = 0.0,
    ) -> dict[str, Any]:
        if (
            max_total_tokens <= 0 or max_cost_usd <= 0
            or reserve_tokens < 0 or reserve_cost_usd < 0
        ):
            raise ValueError("model budgets must be positive and reserves cannot be negative")
        usage = self.model_usage(job_id)
        if int(usage["total_tokens"]) + reserve_tokens > max_total_tokens:
            raise ModelBudgetExceeded(
                f"Job token budget exceeded: {usage['total_tokens']} used, "
                f"{reserve_tokens} reserved, {max_total_tokens} allowed"
            )
        if float(usage["estimated_cost_usd"]) + reserve_cost_usd > max_cost_usd:
            raise ModelBudgetExceeded(
                f"Job cost budget exceeded: ${usage['estimated_cost_usd']:.6f} used, "
                f"${reserve_cost_usd:.6f} reserved, ${max_cost_usd:.6f} allowed"
            )
        return usage

    @staticmethod
    def _release_identity(environment: str, digest: str) -> tuple[str, str]:
        environment = environment.strip().upper()
        digest = digest.strip().lower()
        if environment not in {"TEST", "PRODUCTION"}:
            raise ValueError("release environment must be TEST or PRODUCTION")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("release digest must be a SHA-256 hex value")
        return environment, digest

    def register_release(
        self,
        *,
        environment: str,
        digest: str,
        commit_sha: str,
        artifact_uri: str,
    ) -> dict[str, Any]:
        environment, digest = self._release_identity(environment, digest)
        commit_sha = commit_sha.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{7,64}", commit_sha):
            raise ValueError("release commit must be a Git hex SHA")
        if not artifact_uri.strip():
            raise ValueError("release artifact URI is required")
        now = _now()
        with self._connect() as conn:
            existing = conn.execute(
                """SELECT commit_sha,artifact_uri FROM release_records
                   WHERE environment=? AND digest=?""",
                (environment, digest),
            ).fetchone()
            if existing and (
                existing["commit_sha"] != commit_sha
                or existing["artifact_uri"] != artifact_uri.strip()
            ):
                raise ValueError("an immutable release digest cannot be rebound")
            conn.execute(
                """INSERT INTO release_records
                (environment,digest,commit_sha,artifact_uri,status,created_at,updated_at)
                VALUES (?,?,?,?,'PREPARED',?,?)
                ON CONFLICT(environment,digest) DO NOTHING""",
                (environment, digest, commit_sha, artifact_uri.strip(), now, now),
            )
        return self.get_release(environment, digest)

    def get_release(self, environment: str, digest: str) -> dict[str, Any]:
        environment, digest = self._release_identity(environment, digest)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM release_records WHERE environment=? AND digest=?",
                (environment, digest),
            ).fetchone()
        if row is None:
            raise KeyError((environment, digest))
        result = dict(row)
        result["evidence"] = json.loads(result.pop("evidence_json") or "{}")
        return result

    def verify_release(
        self,
        environment: str,
        digest: str,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        environment, digest = self._release_identity(environment, digest)
        if not evidence.get("verified") or not evidence.get("authoritative"):
            raise ValueError("release verification requires authoritative evidence")
        now = _now()
        with self._connect() as conn:
            updated = conn.execute(
                """UPDATE release_records SET status='VERIFIED',evidence_json=?,updated_at=?
                   WHERE environment=? AND digest=?""",
                (_json(evidence), now, environment, digest),
            )
            if updated.rowcount != 1:
                raise KeyError((environment, digest))
        return self.get_release(environment, digest)

    def promote_verified_release(
        self,
        environment: str,
        digest: str,
        *,
        actor: str,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        environment, digest = self._release_identity(environment, digest)
        if not actor.strip():
            raise ValueError("promotion actor is required")
        if environment == "PRODUCTION" and not evidence.get("approved"):
            raise ValueError("Production promotion requires explicit approval evidence")
        release = self.get_release(environment, digest)
        if release["status"] != "VERIFIED":
            raise ValueError("only a verified release may become current")
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                pointer = conn.execute(
                    "SELECT current_digest,previous_digest FROM release_pointers WHERE environment=?",
                    (environment,),
                ).fetchone()
                current = str(pointer["current_digest"]) if pointer and pointer["current_digest"] else None
                previous = current if current and current != digest else (
                    str(pointer["previous_digest"]) if pointer and pointer["previous_digest"] else None
                )
                conn.execute(
                    """INSERT INTO release_pointers(environment,current_digest,previous_digest,updated_at)
                    VALUES (?,?,?,?) ON CONFLICT(environment) DO UPDATE SET
                    current_digest=excluded.current_digest,
                    previous_digest=excluded.previous_digest,updated_at=excluded.updated_at""",
                    (environment, digest, previous, now),
                )
                conn.execute(
                    """INSERT INTO release_events
                    (environment,event_type,from_digest,to_digest,actor,evidence_json,created_at)
                    VALUES (?,'PROMOTE',?,?,?,?,?)""",
                    (environment, current, digest, actor.strip(), _json(evidence), now),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self.release_state(environment)

    def rollback_release(
        self,
        environment: str,
        *,
        actor: str,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        environment = environment.strip().upper()
        if environment not in {"TEST", "PRODUCTION"} or not actor.strip():
            raise ValueError("valid environment and rollback actor are required")
        if not evidence.get("verified") or not evidence.get("authoritative"):
            raise ValueError("rollback requires authoritative preflight evidence")
        if environment == "PRODUCTION" and not evidence.get("approved"):
            raise ValueError("Production rollback requires explicit approval evidence")
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                pointer = conn.execute(
                    "SELECT current_digest,previous_digest FROM release_pointers WHERE environment=?",
                    (environment,),
                ).fetchone()
                if not pointer or not pointer["current_digest"] or not pointer["previous_digest"]:
                    raise ValueError("no previous verified release is available")
                target = conn.execute(
                    """SELECT status FROM release_records
                       WHERE environment=? AND digest=?""",
                    (environment, pointer["previous_digest"]),
                ).fetchone()
                if not target or target["status"] != "VERIFIED":
                    raise ValueError("rollback target is not a verified release")
                conn.execute(
                    """UPDATE release_pointers SET current_digest=?,previous_digest=?,updated_at=?
                       WHERE environment=?""",
                    (pointer["previous_digest"], pointer["current_digest"], now, environment),
                )
                conn.execute(
                    """INSERT INTO release_events
                    (environment,event_type,from_digest,to_digest,actor,evidence_json,created_at)
                    VALUES (?,'ROLLBACK',?,?,?,?,?)""",
                    (
                        environment, pointer["current_digest"], pointer["previous_digest"],
                        actor.strip(), _json(evidence), now,
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self.release_state(environment)

    def release_state(self, environment: str) -> dict[str, Any]:
        environment = environment.strip().upper()
        if environment not in {"TEST", "PRODUCTION"}:
            raise ValueError("release environment must be TEST or PRODUCTION")
        with self._connect() as conn:
            pointer = conn.execute(
                "SELECT * FROM release_pointers WHERE environment=?", (environment,)
            ).fetchone()
        return dict(pointer) if pointer else {
            "environment": environment,
            "current_digest": None,
            "previous_digest": None,
            "updated_at": None,
        }

    def record_report_run(
        self,
        *,
        report_type: str,
        destination: str,
        window_start: str,
        window_end: str,
        status: str,
        summary: dict[str, Any],
        error: str | None = None,
    ) -> dict[str, Any]:
        report_type = report_type.strip()
        destination = destination.strip()
        if not report_type or not destination or not window_start or not window_end:
            raise ValueError("report identity and window are required")
        key = hashlib.sha256(
            f"{report_type}:{destination}:{window_start}:{window_end}".encode()
        ).hexdigest()
        report_id = str(uuid.uuid4())
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO report_runs
                (id,report_type,destination,window_start,window_end,status,summary_json,
                 created_at,dedupe_key,updated_at,error)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(dedupe_key) DO NOTHING""",
                (
                    report_id, report_type, destination, window_start, window_end,
                    status.strip().upper(), _json(summary), now, key, now, error,
                ),
            )
            row = conn.execute(
                "SELECT * FROM report_runs WHERE dedupe_key=?", (key,)
            ).fetchone()
        result = dict(row)
        result["summary"] = json.loads(result.pop("summary_json") or "{}")
        return result

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
                item.update(self.model_usage(item["id"]))
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
            model_attempts = [dict(r) for r in conn.execute(
                """SELECT job_id,provider,model,ordinal,outcome,error_class,latency_ms,
                          input_tokens,output_tokens,cache_read_tokens,thinking_tokens,
                          estimated_cost_microusd,created_at
                   FROM model_attempts ORDER BY created_at DESC LIMIT 5000"""
            ).fetchall()]
            releases = [dict(r) for r in conn.execute(
                """SELECT environment,digest,commit_sha,artifact_uri,status,evidence_json,
                          created_at,updated_at
                   FROM release_records ORDER BY updated_at DESC LIMIT 1000"""
            ).fetchall()]
            reports = [dict(r) for r in conn.execute(
                """SELECT id,report_type,destination,window_start,window_end,status,
                          summary_json,error,created_at,updated_at
                   FROM report_runs ORDER BY created_at DESC LIMIT 1000"""
            ).fetchall()]
        for attempt in model_attempts:
            attempt["estimated_cost_usd"] = int(attempt.pop("estimated_cost_microusd")) / 1_000_000
        return {"jobs": jobs, "artifacts": artifacts, "schedules": schedules,
                "evidence": evidence, "recordings": recordings,
                "model_attempts": model_attempts, "releases": releases,
                "reports": reports}


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
