"""Post-job close-out audit. Observes only; never authorizes COMPLETE.

Runs on Chat/Job Engine terminal close-out and answers four factual checks
from jobs.db, destination evidence rows, and the published recording. Worker
prose is never treated as destination evidence. A missing jobs.db, recording,
or session is UNKNOWN/FAIL, not a skip-as-pass.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import struct
import subprocess
from pathlib import Path
from typing import Any, Callable, Iterable

from .models import TERMINAL_STATUSES, JobStatus
from .recording import RecordingStore
from .recording_tab import load_attach_log, recorder_tab_mismatch
from .store import JobStore, utc_now


DEFAULT_JOBS_DB = "/opt/streetsmart-hermes/robie-job-engine/data/jobs.db"
AUDIT_CHECKPOINT = "post_job_audit"
MOTION_FAIL_MAX_MEAN_ABS = 2.5
PLAYWRIGHT_MUTATION_RE = re.compile(
    r"("
    r"playwright_exec|"
    r"page\.(goto|click|fill|type|press|check|select_option|set_input_files)|"
    r"\.(goto|click|fill|type|press|check|select_option)\("
    r")",
    re.IGNORECASE,
)
NAVIGATION_CLAIM_RE = re.compile(
    r"("
    r"page\.goto|"
    r"navigat(?:e|ed|ion)|"
    r"clicked|"
    r"filled|"
    r"typed|"
    r"edited|"
    r"saved and continue|"
    r"select_option"
    r")",
    re.IGNORECASE,
)


class PostJobAuditError(RuntimeError):
    """Fail-closed audit construction error. Never used to authorize COMPLETE."""


def _verdict(*parts: str) -> str:
    if any(part == "FAIL" for part in parts):
        return "FAIL"
    if any(part == "UNKNOWN" for part in parts):
        return "UNKNOWN"
    if any(part == "MISMATCH" for part in parts):
        return "FAIL"
    return "PASS"


def _unknown(reason: str) -> dict[str, Any]:
    return {
        "result": "UNKNOWN",
        "reason": reason,
    }


def _fail(reason: str, **extra: Any) -> dict[str, Any]:
    payload = {"result": "FAIL", "reason": reason}
    payload.update(extra)
    return payload


def _pass(**extra: Any) -> dict[str, Any]:
    payload = {"result": "PASS"}
    payload.update(extra)
    return payload


def frames_show_motion(
    frames: Iterable[bytes],
    *,
    max_mean_abs: float = MOTION_FAIL_MAX_MEAN_ABS,
) -> dict[str, Any]:
    """Compare consecutive RGB frames. Identical / near-identical = frozen."""
    samples = [bytes(frame) for frame in frames if frame]
    if not samples:
        return _unknown("no frames; cannot measure motion")
    if len(samples) == 1:
        return {
            "result": "FAIL",
            "reason": "frozen / no-motion",
            "frame_count": 1,
            "bytes_per_frame": len(samples[0]),
            "max_mean_abs": 0.0,
            "threshold": max_mean_abs,
        }
    width = None
    diffs: list[float] = []
    for previous, current in zip(samples, samples[1:]):
        if len(previous) != len(current) or len(previous) % 3 != 0:
            return _unknown("frame sizes do not match; cannot measure motion")
        total = 0
        for left, right in zip(previous, current):
            total += abs(left - right)
        diffs.append(total / len(previous))
        width = len(previous)
    peak = max(diffs)
    moving = peak > max_mean_abs
    return {
        "result": "PASS" if moving else "FAIL",
        "reason": "motion detected" if moving else "frozen / no-motion",
        "frame_count": len(samples),
        "bytes_per_frame": width,
        "max_mean_abs": round(peak, 4),
        "threshold": max_mean_abs,
    }


def _read_ppm(payload: bytes) -> tuple[bytes, int]:
    """Return (rgb_bytes, bytes_consumed) for one binary PPM at the start of payload."""
    if not payload.startswith(b"P6"):
        raise ValueError("not a binary PPM")
    offset = 2
    if payload[offset:offset + 1] == b"\n":
        offset += 1
    while payload[offset:offset + 1] == b"#":
        newline = payload.find(b"\n", offset)
        if newline < 0:
            raise ValueError("truncated PPM comment")
        offset = newline + 1
    header_end = payload.find(b"\n", offset)
    if header_end < 0:
        raise ValueError("truncated PPM header")
    dims = payload[offset:header_end].split()
    if len(dims) < 2:
        raise ValueError("missing PPM dimensions")
    width, height = int(dims[0]), int(dims[1])
    offset = header_end + 1
    max_end = payload.find(b"\n", offset)
    if max_end < 0:
        raise ValueError("truncated PPM maxval")
    if int(payload[offset:max_end]) != 255:
        raise ValueError("only maxval 255 PPM is supported")
    offset = max_end + 1
    expected = width * height * 3
    if len(payload) < offset + expected:
        raise ValueError("truncated PPM")
    return payload[offset:offset + expected], offset + expected


def extract_video_frames(
    path: str | Path,
    *,
    ffmpeg: str | None = None,
    max_frames: int = 12,
) -> list[bytes]:
    """Extract evenly spaced RGB frames. Missing ffmpeg is UNKNOWN, not pass."""
    video = Path(path)
    if not video.is_file() or video.stat().st_size <= 0:
        raise PostJobAuditError("recording file is missing or empty")
    binary = ffmpeg or shutil.which("ffmpeg")
    if not binary:
        raise PostJobAuditError("ffmpeg is not installed; motion check is UNKNOWN")
    probe = subprocess.run(
        [
            binary,
            "-hide_banner",
            "-i",
            str(video),
            "-vframes",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "ppm",
            "pipe:1",
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if probe.returncode != 0 or not probe.stdout:
        raise PostJobAuditError(
            f"ffmpeg could not decode recording: {(probe.stderr or b'')[-400:].decode('utf-8', 'replace')}"
        )
    first, _ = _read_ppm(probe.stdout)
    command = [
        binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video),
        "-vsync",
        "0",
        "-vf",
        "scale=160:90",
        "-frames:v",
        str(max_frames),
        "-f",
        "image2pipe",
        "-vcodec",
        "ppm",
        "pipe:1",
    ]
    extracted = subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if extracted.returncode != 0:
        raise PostJobAuditError(
            f"ffmpeg frame extract failed: {(extracted.stderr or b'')[-400:].decode('utf-8', 'replace')}"
        )
    frames = _split_ppm_stream(extracted.stdout)
    if not frames:
        frames = [first]
    return frames


def _split_ppm_stream(payload: bytes) -> list[bytes]:
    frames: list[bytes] = []
    cursor = 0
    data = payload
    while cursor < len(data):
        start = data.find(b"P6", cursor)
        if start < 0:
            break
        try:
            rgb, consumed = _read_ppm(data[start:])
        except ValueError:
            break
        frames.append(rgb)
        cursor = start + consumed
        if len(frames) > 64:
            break
    return frames


def analyze_recording_motion(
    path: str | Path | None,
    *,
    extract_frames: Callable[[str | Path], list[bytes]] | None = None,
) -> dict[str, Any]:
    if path is None:
        return _fail("missing recording")
    recording = Path(path)
    if not recording.is_file():
        return _fail("missing recording", path=str(recording))
    if recording.stat().st_size <= 0:
        return _fail("recording file is empty", path=str(recording))
    extractor = extract_frames or extract_video_frames
    try:
        frames = extractor(recording)
    except PostJobAuditError as exc:
        return _unknown(str(exc))
    except Exception as exc:
        return _unknown(f"{type(exc).__name__}: {exc}")
    motion = frames_show_motion(frames)
    motion["path"] = str(recording)
    if motion["result"] == "FAIL":
        motion["reason"] = "frozen / no-motion"
    return motion


def _tool_text_from_job(store: JobStore, job_id: str) -> str:
    chunks: list[str] = []
    worker = store.get_checkpoint(job_id, "worker_response") or {}
    chunks.append(json.dumps(worker, default=str))
    action = store.get_checkpoint(job_id, "action") or {}
    chunks.append(json.dumps(action, default=str))
    for attempt in store.list_attempts(job_id):
        chunks.append(json.dumps(attempt.get("detail") or {}, default=str))
        chunks.append(str(attempt.get("outcome") or ""))
    return "\n".join(chunks)


def _session_text(session_root: str | Path | None, job_id: str) -> tuple[str, dict[str, Any]]:
    if session_root is None:
        return "", _unknown("session root is not configured")
    root = Path(session_root)
    if not root.exists():
        return "", _unknown("missing session")
    if not root.is_dir():
        return "", _unknown("session path is not a directory")
    texts: list[str] = []
    matched = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".json", ".jsonl", ".log", ".txt", ".md"}:
            continue
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if job_id in body or "playwright_exec" in body:
            texts.append(body)
            matched += 1
        if matched >= 20:
            break
    if not texts:
        return "", _unknown("missing session")
    return "\n".join(texts), _pass(files=matched)


def tool_calls_claim_mutation(text: str) -> bool:
    blob = str(text or "")
    if not blob.strip():
        return False
    return bool(PLAYWRIGHT_MUTATION_RE.search(blob) or NAVIGATION_CLAIM_RE.search(blob))


def audit_heartbeat(store: JobStore, job_id: str) -> dict[str, Any]:
    record = store.get_checkpoint_record(job_id, "gateway_progress")
    if record is None:
        return {
            "present": False,
            "result": "FAIL",
            "first_at": None,
            "last_at": None,
            "reason": "no checkpoints.kind=gateway_progress row",
        }
    data = dict(record.get("data") or {})
    first_at = data.get("first_at") or record.get("created_at")
    last_at = data.get("last_at") or record.get("created_at")
    return {
        "present": True,
        "result": "PASS",
        "first_at": first_at,
        "last_at": last_at,
        "source": data.get("source"),
    }


def audit_destination_evidence(store: JobStore, job_id: str) -> dict[str, Any]:
    action = store.get_checkpoint(job_id, "action")
    evidence = store.list_evidence(job_id)
    action_count = 1 if action else 0
    evidence_count = len(evidence)
    authoritative = sum(1 for item in evidence if item.get("authoritative"))
    verified = sum(1 for item in evidence if item.get("verified"))
    if action_count == 0 and evidence_count == 0:
        return {
            "result": "FAIL",
            "destination_action_count": 0,
            "evidence_count": 0,
            "authoritative_count": 0,
            "verified_count": 0,
            "summary": "ZERO",
            "reason": "no destination-action checkpoint and no evidence rows",
        }
    return {
        "result": "PASS",
        "destination_action_count": action_count,
        "evidence_count": evidence_count,
        "authoritative_count": authoritative,
        "verified_count": verified,
        "summary": f"action={action_count} evidence={evidence_count}",
    }


def audit_recording_motion(
    db_path: str,
    job_id: str,
    *,
    extract_frames: Callable[[str | Path], list[bytes]] | None = None,
) -> dict[str, Any]:
    try:
        recordings = RecordingStore(db_path)
        segments = recordings.list_for_job(job_id)
    except Exception as exc:
        return _unknown(f"recording ledger unavailable: {type(exc).__name__}: {exc}")
    if not segments:
        return _fail("missing recording")
    published = [
        item
        for item in segments
        if item.get("status") == "READY" and item.get("drive_url")
    ]
    candidate = published[-1] if published else segments[-1]
    local_path = candidate.get("local_path")
    if candidate.get("status") != "READY":
        return _fail(
            f"published recording is not READY ({candidate.get('status')})",
            recording_id=candidate.get("id"),
            status=candidate.get("status"),
        )
    motion = analyze_recording_motion(local_path, extract_frames=extract_frames)
    motion["recording_id"] = candidate.get("id")
    motion["drive_url"] = candidate.get("drive_url")
    motion["status"] = candidate.get("status")
    motion["attach"] = load_attach_log(local_path)
    return motion


def audit_tool_vs_recording(
    store: JobStore,
    job_id: str,
    motion: dict[str, Any],
    *,
    session_root: str | Path | None,
) -> dict[str, Any]:
    session_text, session = _session_text(session_root, job_id)
    ledger_text = _tool_text_from_job(store, job_id)
    combined = f"{ledger_text}\n{session_text}"
    claims = tool_calls_claim_mutation(combined)
    motion_result = str(motion.get("result") or "UNKNOWN")
    tab = recorder_tab_mismatch(motion.get("attach"), combined)
    if tab.get("result") == "MISMATCH":
        return {
            "result": "MISMATCH",
            "reason": tab.get("reason"),
            "tool_claims_mutation": claims,
            "recording_motion": motion_result,
            "recorder_tab": tab,
            "session": session,
        }
    if session["result"] == "UNKNOWN" and not claims and tab.get("result") != "MATCH":
        return {
            "result": "UNKNOWN",
            "reason": session.get("reason") or "missing session",
            "tool_claims_mutation": False,
            "recording_motion": motion_result,
            "recorder_tab": tab,
            "session": session,
        }
    if motion_result == "FAIL" and claims:
        missing = "missing" in str(motion.get("reason") or "").casefold()
        return {
            "result": "MISMATCH",
            "reason": (
                "recording file is missing but playwright_exec / tool results claim navigation or edits"
                if missing
                else "recording is frozen but playwright_exec / tool results claim navigation or edits"
            ),
            "tool_claims_mutation": True,
            "recording_motion": motion_result,
            "recorder_tab": tab,
            "session": session,
        }
    if motion_result == "UNKNOWN":
        return {
            "result": "UNKNOWN",
            "reason": f"recording motion is UNKNOWN ({motion.get('reason')})",
            "tool_claims_mutation": claims,
            "recording_motion": motion_result,
            "session": session,
        }
    return {
        "result": "MATCH",
        "reason": (
            "tool-call results do not claim navigation/edits against a frozen recording"
            if not claims
            else "recording shows motion and tool-call results claim browser work"
        ),
        "tool_claims_mutation": claims,
        "recording_motion": motion_result,
        "session": session,
    }


def run_post_job_audit(
    db_path: str | Path,
    job_id: str,
    *,
    session_root: str | Path | None = None,
    extract_frames: Callable[[str | Path], list[bytes]] | None = None,
) -> dict[str, Any]:
    """Inspect one Job. Never transitions status and never authorizes COMPLETE."""
    path = Path(db_path)
    if not path.is_file():
        audit = {
            "job_id": job_id,
            "job_status": "UNKNOWN",
            "verdict": "FAIL",
            "heartbeat": _unknown("missing jobs.db"),
            "destination_evidence": _unknown("missing jobs.db"),
            "recording_motion": _fail("missing jobs.db"),
            "tool_vs_recording": _unknown("missing jobs.db"),
            "authorizes_complete": False,
        }
        return audit
    try:
        store = JobStore(path)
        job = store.get_job(job_id)
    except (KeyError, sqlite3.Error, OSError) as exc:
        return {
            "job_id": job_id,
            "job_status": "UNKNOWN",
            "verdict": "FAIL",
            "heartbeat": _unknown(f"jobs.db unreadable: {type(exc).__name__}"),
            "destination_evidence": _unknown(f"jobs.db unreadable: {type(exc).__name__}"),
            "recording_motion": _fail(f"jobs.db unreadable: {type(exc).__name__}"),
            "tool_vs_recording": _unknown(f"jobs.db unreadable: {type(exc).__name__}"),
            "authorizes_complete": False,
        }
    hermes_home = session_root
    if hermes_home is None:
        hermes_home = os.environ.get("HERMES_HOME") or os.environ.get(
            "ROBIE_HERMES_SESSION_ROOT"
        )
    heartbeat = audit_heartbeat(store, job_id)
    evidence = audit_destination_evidence(store, job_id)
    motion = audit_recording_motion(str(path), job_id, extract_frames=extract_frames)
    session_text, _session = _session_text(hermes_home, job_id)
    tool_text = f"{_tool_text_from_job(store, job_id)}\n{session_text}"
    tab = recorder_tab_mismatch(motion.get("attach"), tool_text)
    if tab.get("result") == "MISMATCH":
        motion = dict(motion)
        motion["result"] = "FAIL"
        motion["reason"] = f"frozen / wrong-tab: {tab.get('reason')}"
        motion["recorder_tab"] = tab
    mismatch = audit_tool_vs_recording(
        store, job_id, motion, session_root=hermes_home
    )
    mismatch_result = str(mismatch.get("result") or "UNKNOWN")
    verdict = _verdict(
        str(heartbeat.get("result") or "UNKNOWN"),
        str(evidence.get("result") or "UNKNOWN"),
        str(motion.get("result") or "UNKNOWN"),
        "FAIL" if mismatch_result == "MISMATCH" else mismatch_result,
    )
    return {
        "job_id": job["id"],
        "job_status": job["status"],
        "action_type": job.get("action_type"),
        "verdict": verdict,
        "heartbeat": heartbeat,
        "destination_evidence": evidence,
        "recording_motion": motion,
        "tool_vs_recording": mismatch,
        "authorizes_complete": False,
        "audited_at": utc_now(),
    }


def format_audit_chat_message(audit: dict[str, Any]) -> str:
    """Short factual Chat APP post. Four answers + job id + status."""
    heartbeat = dict(audit.get("heartbeat") or {})
    if heartbeat.get("present"):
        hb = (
            f"yes first={heartbeat.get('first_at')} last={heartbeat.get('last_at')}"
        )
    elif heartbeat.get("result") == "UNKNOWN":
        hb = f"UNKNOWN ({heartbeat.get('reason')})"
    else:
        hb = "no"
    evidence = dict(audit.get("destination_evidence") or {})
    if evidence.get("summary") == "ZERO" or (
        evidence.get("destination_action_count") == 0
        and evidence.get("evidence_count") == 0
    ):
        ev = "ZERO (action=0 evidence=0)"
    elif evidence.get("result") == "UNKNOWN":
        ev = f"UNKNOWN ({evidence.get('reason')})"
    else:
        ev = (
            f"{evidence.get('summary')} "
            f"authoritative={evidence.get('authoritative_count', 0)}"
        )
    motion = dict(audit.get("recording_motion") or {})
    motion_line = f"{motion.get('result')} ({motion.get('reason')})"
    tools = dict(audit.get("tool_vs_recording") or {})
    tool_line = f"{tools.get('result')} ({tools.get('reason')})"
    return (
        f"ROBIE post-job audit — {audit.get('job_id')} — {audit.get('job_status')}\n"
        f"1. Heartbeat gateway_progress: {hb}\n"
        f"2. Destination evidence: {ev}\n"
        f"3. Recording motion: {motion_line}\n"
        f"4. Tool vs recording: {tool_line}\n"
        f"Audit verdict: {audit.get('verdict')} (does not authorize COMPLETE)"
    )


def persist_post_job_audit(
    db_path: str | Path,
    audit: dict[str, Any],
) -> dict[str, Any]:
    """Store the audit as a checkpoint. Does not change Job status."""
    store = JobStore(db_path)
    store.checkpoint(str(audit["job_id"]), AUDIT_CHECKPOINT, dict(audit))
    return audit


def audit_terminal_job(
    db_path: str | Path,
    job_id: str,
    *,
    session_root: str | Path | None = None,
    extract_frames: Callable[[str | Path], list[bytes]] | None = None,
    chat_poster: Callable[[dict[str, Any], str], Any] | None = None,
) -> dict[str, Any]:
    """Run, persist, and optionally post. Never marks the Job COMPLETE."""
    audit = run_post_job_audit(
        db_path,
        job_id,
        session_root=session_root,
        extract_frames=extract_frames,
    )
    if Path(db_path).is_file():
        try:
            persist_post_job_audit(db_path, audit)
        except Exception:
            audit = dict(audit)
            audit["persist_error"] = "failed to persist post_job_audit checkpoint"
    message = format_audit_chat_message(audit)
    audit["chat_message"] = message
    try:
        from .tab_cleanup import maybe_cleanup_terminal_job_tabs

        cleanup = maybe_cleanup_terminal_job_tabs(db_path, job_id)
        if cleanup is not None:
            audit["tab_cleanup"] = cleanup
    except Exception as exc:
        audit = dict(audit)
        audit["tab_cleanup_error"] = f"{type(exc).__name__}: {exc}"
    if chat_poster is not None:
        try:
            posted = chat_poster(audit, message)
            audit["chat_posted"] = posted is not False
        except Exception as exc:
            audit["chat_posted"] = False
            audit["chat_post_error"] = f"{type(exc).__name__}: {exc}"
    return audit


def maybe_audit_terminal_job(
    db_path: str | Path,
    job_id: str | None,
    **kwargs: Any,
) -> dict[str, Any] | None:
    if not job_id:
        return None
    path = Path(db_path)
    if not path.is_file():
        return audit_terminal_job(db_path, job_id, **kwargs)
    try:
        job = JobStore(path).get_job(job_id)
    except (KeyError, sqlite3.Error, OSError):
        return audit_terminal_job(db_path, job_id, **kwargs)
    if JobStatus(job["status"]) not in TERMINAL_STATUSES:
        return None
    return audit_terminal_job(db_path, job_id, **kwargs)


def rgb_frame(width: int, height: int, color: tuple[int, int, int]) -> bytes:
    """Test helper: one packed RGB24 frame."""
    pixel = struct.pack("BBB", *color)
    return pixel * (width * height)


def write_ppm(path: str | Path, width: int, height: int, rgb: bytes) -> Path:
    target = Path(path)
    target.write_bytes(f"P6\n{width} {height}\n255\n".encode("ascii") + rgb)
    return target
