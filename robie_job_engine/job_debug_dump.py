"""Read-only destination-of-truth dump for one Job Engine job.

Paste a job UUID or id prefix. Prints the jobs.db row, related
durable_work_items, Playwright / gateway evidence pointers, and known
destination ids so operators do not reconstruct incidents from Chat.

Does not write, retry, kill, bind a port, deploy, or authorize COMPLETE.

    PYTHONPATH=. python3 -m robie_job_engine.job_debug_dump <job-id>
    PYTHONPATH=. python3 scripts/job_debug_dump.py <job-id> --db /path/to/jobs.db
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urlsplit

from .request_routing import WORKER_FOR_ACTION
from .secrets import redact_text


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROD_JOBS_DB = "/opt/streetsmart-hermes/robie-job-engine/data/jobs.db"
DEFAULT_ARTIFACT_ROOT = "/opt/streetsmart-hermes/robie-job-engine/data/artifacts"
DEFAULT_RECORDING_ROOT = "/opt/streetsmart-hermes/robie-job-engine/data/recordings"
CHECKOUT_JOBS_DB = REPO_ROOT / "data" / "jobs.db"
INSTALL_SIBLING_JOBS_DB = REPO_ROOT.parent / "robie-job-engine" / "data" / "jobs.db"

DESTINATION_KEYS = (
    "note_id",
    "ezlynx_note_id",
    "doc_id",
    "document_id",
    "document_path",
    "discussion_id",
    "discussion_title",
    "applicant_id",
    "account_id",
)
DESTINATION_ALIASES = {
    "noteid": "note_id",
    "ezlynxnoteid": "ezlynx_note_id",
    "docid": "doc_id",
    "documentid": "document_id",
    "documentpath": "document_path",
    "discussionid": "discussion_id",
    "discussiontitle": "discussion_title",
    "applicantid": "applicant_id",
    "accountid": "account_id",
}
CDP_KEYS = ("cdp_url", "cdp_port", "browser_cdp_url", "ROBIE_BROWSER_CDP_URL")
LOG_LINE_LIMIT = 40
LOG_TAIL_BYTES = 262_144
WORK_ITEM_LIMIT = 50


def default_jobs_db() -> str:
    """Prefer env, then a checkout/install file that exists, else Production."""
    env = str(os.environ.get("ROBIE_JOB_DB") or "").strip()
    if env:
        return env
    for candidate in (CHECKOUT_JOBS_DB, INSTALL_SIBLING_JOBS_DB):
        if candidate.is_file():
            return str(candidate)
    return DEFAULT_PROD_JOBS_DB


def default_artifact_root(db_path: str | Path | None = None) -> str:
    env = str(os.environ.get("ROBIE_ARTIFACT_ROOT") or "").strip()
    if env:
        return env
    if db_path is not None:
        sibling = Path(db_path).expanduser().resolve().parent / "artifacts"
        if sibling.is_dir():
            return str(sibling)
    return DEFAULT_ARTIFACT_ROOT


def default_recording_root(db_path: str | Path | None = None) -> str:
    env = str(os.environ.get("ROBIE_RECORDING_ROOT") or "").strip()
    if env:
        return env
    if db_path is not None:
        sibling = Path(db_path).expanduser().resolve().parent / "recordings"
        if sibling.is_dir():
            return str(sibling)
    return DEFAULT_RECORDING_ROOT


def open_readonly(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"jobs.db is missing: {path}")
    uri = f"file:{quote(str(path), safe='/')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _json_object(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_unparsed": redact_text(text[:2000])}


def _walk(value: Any) -> Iterable[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key), item
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _normalize_dest_key(key: str) -> str | None:
    raw = str(key or "").strip()
    if raw in DESTINATION_KEYS:
        return raw
    folded = raw.casefold().replace("-", "").replace("_", "")
    return DESTINATION_ALIASES.get(folded)


def extract_destination_ids(*blobs: Any) -> dict[str, str]:
    found: dict[str, str] = {}
    for blob in blobs:
        parsed = _json_object(blob) if not isinstance(blob, (dict, list)) else blob
        for key, item in _walk(parsed):
            dest = _normalize_dest_key(key)
            if dest is None or dest in found:
                continue
            if item is None:
                continue
            text = redact_text(str(item)).strip()
            if text:
                found[dest] = text
    return found


def extract_cdp(*blobs: Any) -> dict[str, str]:
    found: dict[str, str] = {}
    for blob in blobs:
        parsed = _json_object(blob) if not isinstance(blob, (dict, list)) else blob
        for key, item in _walk(parsed):
            if str(key) not in CDP_KEYS or item is None:
                continue
            text = redact_text(str(item)).strip()
            if not text:
                continue
            if str(key) == "cdp_port" and "CDP_PORT" not in found:
                found["CDP_PORT"] = text
            elif "CDP_URL" not in found:
                found["CDP_URL"] = text
                parsed_url = urlsplit(text)
                if parsed_url.port and "CDP_PORT" not in found:
                    found["CDP_PORT"] = str(parsed_url.port)
    return found


def resolve_worker(action_type: str, payload: dict[str, Any]) -> str:
    claimed = str((payload or {}).get("worker") or "").strip()
    registry = str(WORKER_FOR_ACTION.get(action_type) or "").strip()
    return claimed or registry or "(unknown)"


def find_jobs(conn: sqlite3.Connection, job_ref: str) -> list[sqlite3.Row]:
    ref = str(job_ref or "").strip()
    if not ref:
        return []
    exact = conn.execute("SELECT * FROM jobs WHERE id=?", (ref,)).fetchall()
    if exact:
        return list(exact)
    if len(ref) < 8:
        return []
    return list(
        conn.execute(
            "SELECT * FROM jobs WHERE id LIKE ? ORDER BY created_at, id",
            (f"{ref}%",),
        ).fetchall()
    )


def _job_needles(job_id: str) -> tuple[str, ...]:
    job_id = str(job_id or "").strip()
    needles = [job_id]
    short = job_id.split("-", 1)[0]
    if short and short != job_id:
        needles.append(short)
    if len(job_id) >= 8:
        needles.append(job_id[:8])
    return tuple(dict.fromkeys(n for n in needles if n))


def load_durable_work_items(
    conn: sqlite3.Connection, job_id: str, action_type: str
) -> list[dict[str, Any]]:
    if not table_exists(conn, "durable_work_items"):
        return []
    needles = _job_needles(job_id)
    clauses = []
    params: list[str] = []
    for needle in needles:
        like = f"%{needle}%"
        clauses.append("work_item_key LIKE ?")
        clauses.append("IFNULL(outcome,'') LIKE ?")
        clauses.append("IFNULL(lease_owner,'') LIKE ?")
        params.extend((like, like, like))
    if action_type:
        clauses.append("namespace=?")
        params.append(action_type)
    sql = f"""
        SELECT namespace, work_item_key, outcome, verified, lease_owner,
               created_at, updated_at
        FROM durable_work_items
        WHERE {" OR ".join(clauses)}
        ORDER BY updated_at DESC, namespace, work_item_key
        LIMIT {WORK_ITEM_LIMIT}
    """
    rows = []
    for row in conn.execute(sql, params).fetchall():
        item = dict(row)
        item["outcome_parsed"] = _json_object(item.get("outcome"))
        rows.append(item)
    return rows


def load_checkpoints(conn: sqlite3.Connection, job_id: str) -> list[dict[str, Any]]:
    if not table_exists(conn, "checkpoints"):
        return []
    rows = conn.execute(
        """SELECT kind, data_json, created_at
           FROM checkpoints WHERE job_id=? ORDER BY id""",
        (job_id,),
    ).fetchall()
    result = []
    for row in rows:
        result.append(
            {
                "kind": row["kind"],
                "created_at": row["created_at"],
                "data": _json_object(row["data_json"]),
            }
        )
    return result


def load_playwright_exec(conn: sqlite3.Connection, job_id: str) -> list[dict[str, Any]]:
    if not table_exists(conn, "playwright_exec"):
        return []
    rows = conn.execute(
        """SELECT id, tool, status, code_preview, result_json, created_at, updated_at
           FROM playwright_exec WHERE job_id=? ORDER BY id""",
        (job_id,),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["result"] = _json_object(item.pop("result_json"))
        item["code_preview"] = redact_text(str(item.get("code_preview") or ""))
        result.append(item)
    return result


def load_evidence(conn: sqlite3.Connection, job_id: str) -> list[dict[str, Any]]:
    if not table_exists(conn, "verification_evidence"):
        return []
    rows = conn.execute(
        """SELECT id, verified, method, source, authoritative, expected_json,
                  observed_json, locator, evidence_sha256, captured_at
           FROM verification_evidence WHERE job_id=? ORDER BY id""",
        (job_id,),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["expected"] = _json_object(item.pop("expected_json"))
        item["observed"] = _json_object(item.pop("observed_json"))
        result.append(item)
    return result


def _table_columns(conn: sqlite3.Connection, name: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({name})")}


def load_recordings(conn: sqlite3.Connection, job_id: str) -> list[dict[str, Any]]:
    if not table_exists(conn, "job_recordings"):
        return []
    order = "segment_number" if "segment_number" in _table_columns(conn, "job_recordings") else "rowid"
    rows = conn.execute(
        f"SELECT * FROM job_recordings WHERE job_id=? ORDER BY {order}",
        (job_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def load_artifacts(conn: sqlite3.Connection, job_id: str) -> list[dict[str, Any]]:
    if not table_exists(conn, "artifacts"):
        return []
    rows = conn.execute(
        """SELECT id, original_name, stored_path, destination_ref, status,
                  verification_json, created_at
           FROM artifacts WHERE job_id=? ORDER BY created_at""",
        (job_id,),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["verification"] = _json_object(item.pop("verification_json"))
        result.append(item)
    return result


def _present_path(path: str | Path | None) -> dict[str, Any]:
    if not path:
        return {"path": None, "present": False}
    target = Path(str(path))
    try:
        present = target.is_file() and target.stat().st_size > 0
    except OSError:
        present = False
    return {"path": str(target), "present": present}


def filesystem_recordings(job_id: str, recording_root: str | Path) -> list[dict[str, Any]]:
    root = Path(recording_root)
    if not root.is_dir():
        return []
    hits = []
    for path in sorted(root.glob(f"*{job_id}*"))[:20]:
        if path.is_file():
            hits.append(_present_path(path))
    job_dir = root / job_id
    if job_dir.is_dir():
        for path in sorted(job_dir.iterdir())[:20]:
            if path.is_file():
                hits.append(_present_path(path))
    return hits


def collect_log_lines(
    job_id: str,
    *,
    log_path: str | Path | None,
    artifact_root: str | Path,
    extra_paths: Iterable[str | Path] = (),
    limit: int = LOG_LINE_LIMIT,
) -> dict[str, Any]:
    needles = _job_needles(job_id)
    candidates: list[Path] = []
    env_log = str(os.environ.get("ROBIE_JOB_LOG") or "").strip()
    for raw in (log_path, env_log, *extra_paths):
        if raw:
            candidates.append(Path(str(raw)))
    art_dir = Path(artifact_root) / job_id
    if art_dir.is_dir():
        candidates.extend(sorted(art_dir.glob("*.log")))
    seen: set[str] = set()
    files: list[str] = []
    matches: list[str] = []
    for path in candidates:
        resolved = str(path)
        if resolved in seen or not path.is_file():
            continue
        seen.add(resolved)
        files.append(resolved)
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if len(data) > LOG_TAIL_BYTES:
            data = data[-LOG_TAIL_BYTES:]
        text = data.decode("utf-8", errors="replace")
        for line in text.splitlines():
            if any(needle in line for needle in needles):
                matches.append(redact_text(line.rstrip()))
    return {
        "files": files,
        "lines": matches[-limit:],
        "truncated": len(matches) > limit,
    }


def dump_job(
    job_ref: str,
    *,
    db_path: str | Path | None = None,
    artifact_root: str | Path | None = None,
    recording_root: str | Path | None = None,
    log_path: str | Path | None = None,
    log_limit: int = LOG_LINE_LIMIT,
) -> dict[str, Any]:
    """Read-only structured dump. Never opens jobs.db for write."""
    resolved_db = str(db_path or default_jobs_db())
    artifacts = str(artifact_root or default_artifact_root(resolved_db))
    recordings_root = str(recording_root or default_recording_root(resolved_db))
    report: dict[str, Any] = {
        "ok": False,
        "read_only": True,
        "db_path": resolved_db,
        "job_ref": job_ref,
        "artifact_root": artifacts,
        "recording_root": recordings_root,
    }
    try:
        conn = open_readonly(resolved_db)
    except FileNotFoundError as exc:
        report["error"] = str(exc)
        return report
    try:
        matches = find_jobs(conn, job_ref)
        if not matches:
            report["error"] = f"job not found: {job_ref}"
            return report
        if len(matches) > 1:
            report["error"] = f"ambiguous job prefix: {job_ref}"
            report["matches"] = [
                {
                    "id": row["id"],
                    "status": row["status"],
                    "action_type": row["action_type"],
                    "created_at": row["created_at"],
                }
                for row in matches
            ]
            return report
        row = matches[0]
        payload = _json_object(row["payload_json"])
        if not isinstance(payload, dict):
            payload = {"_payload": payload}
        job_id = str(row["id"])
        action_type = str(row["action_type"] or "")
        checkpoints = load_checkpoints(conn, job_id)
        work_items = load_durable_work_items(conn, job_id, action_type)
        playwright_rows = load_playwright_exec(conn, job_id)
        evidence_rows = load_evidence(conn, job_id)
        recording_rows = load_recordings(conn, job_id)
        artifact_rows = load_artifacts(conn, job_id)
        dest = extract_destination_ids(
            payload,
            [item.get("data") for item in checkpoints],
            [item.get("outcome_parsed") for item in work_items],
            [item.get("expected") for item in evidence_rows],
            [item.get("observed") for item in evidence_rows],
            [item.get("verification") for item in artifact_rows],
            [{"destination_ref": item.get("destination_ref")} for item in artifact_rows],
        )
        cdp = extract_cdp(payload, [item.get("data") for item in checkpoints])
        trace = _present_path(Path(artifacts) / job_id / "playwright-trace.zip")
        recording_paths = []
        seen_recordings: set[str] = set()
        for rec in recording_rows:
            info = _present_path(rec.get("local_path"))
            info["drive_url"] = rec.get("drive_url")
            info["status"] = rec.get("status")
            info["segment"] = rec.get("segment_number")
            path_key = str(info.get("path") or "")
            if path_key:
                seen_recordings.add(path_key)
            recording_paths.append(info)
        for info in filesystem_recordings(job_id, recordings_root):
            path_key = str(info.get("path") or "")
            if path_key and path_key in seen_recordings:
                continue
            if path_key:
                seen_recordings.add(path_key)
            recording_paths.append(info)
        gateway = next(
            (item for item in checkpoints if item.get("kind") == "gateway_progress"),
            None,
        )
        report.update(
            {
                "ok": True,
                "job": {
                    "id": job_id,
                    "status": row["status"],
                    "action_type": action_type,
                    "worker": resolve_worker(action_type, payload),
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "completed_at": row["completed_at"],
                    "attempt_count": row["attempt_count"],
                    "lease_owner": row["lease_owner"],
                    "last_error": row["last_error"],
                },
                "durable_work_items": work_items,
                "checkpoints": [
                    {"kind": item["kind"], "created_at": item["created_at"]}
                    for item in checkpoints
                ],
                "playwright_exec_count": len(playwright_rows),
                "playwright_exec": [
                    {
                        "id": item.get("id"),
                        "tool": item.get("tool"),
                        "status": item.get("status"),
                        "created_at": item.get("created_at"),
                        "updated_at": item.get("updated_at"),
                    }
                    for item in playwright_rows
                ],
                "gateway_progress": None if gateway is None else gateway.get("data"),
                "trace": trace,
                "recordings": recording_paths,
                "cdp": cdp,
                "destination": dest,
                "logs": collect_log_lines(
                    job_id,
                    log_path=log_path,
                    artifact_root=artifacts,
                    limit=log_limit,
                ),
            }
        )
        return report
    finally:
        conn.close()


def _kv(key: str, value: Any) -> str:
    if value is None or value == "":
        return f"{key}=(not found)"
    if isinstance(value, bool):
        return f"{key}={'yes' if value else 'no'}"
    return f"{key}={value}"


def format_dump(report: dict[str, Any]) -> str:
    lines = [
        "ROBIE job debug dump — read-only. Does not retry, kill, or write.",
        _kv("DB", report.get("db_path")),
        _kv("QUERY", report.get("job_ref")),
    ]
    if not report.get("ok"):
        lines.append("")
        lines.append(f"ERROR={report.get('error') or 'dump failed'}")
        for match in report.get("matches") or []:
            lines.append(
                "  "
                + " ".join(
                    (
                        _kv("JOB_ID", match.get("id")),
                        _kv("STATUS", match.get("status")),
                        _kv("TYPE", match.get("action_type")),
                    )
                )
            )
        return "\n".join(lines)

    job = dict(report.get("job") or {})
    lines.extend(
        [
            "",
            "== JOB ==",
            _kv("JOB_ID", job.get("id")),
            _kv("STATUS", job.get("status")),
            _kv("TYPE", job.get("action_type")),
            _kv("WORKER", job.get("worker")),
            _kv("CREATED_AT", job.get("created_at")),
            _kv("UPDATED_AT", job.get("updated_at")),
            _kv("COMPLETED_AT", job.get("completed_at")),
            _kv("ATTEMPT_COUNT", job.get("attempt_count")),
            _kv("LEASE_OWNER", job.get("lease_owner")),
            "LAST_ERROR:",
        ]
    )
    error = job.get("last_error")
    if error:
        lines.append(str(error))
    else:
        lines.append("(empty)")

    items = list(report.get("durable_work_items") or [])
    lines.extend(["", "== DURABLE WORK ITEMS ==", _kv("COUNT", len(items))])
    if not items:
        lines.append("(none)")
    for item in items:
        outcome = item.get("outcome")
        if isinstance(item.get("outcome_parsed"), dict):
            outcome = json.dumps(item["outcome_parsed"], sort_keys=True, default=str)
        if outcome is not None:
            outcome = redact_text(str(outcome))
        lines.append(
            "  "
            + " ".join(
                (
                    _kv("NAMESPACE", item.get("namespace")),
                    _kv("KEY", item.get("work_item_key")),
                    _kv("VERIFIED", item.get("verified")),
                    _kv("UPDATED_AT", item.get("updated_at")),
                )
            )
        )
        lines.append(f"  OUTCOME={outcome if outcome not in (None, '') else '(empty)'}")

    lines.extend(["", "== PLAYWRIGHT / GATEWAY EVIDENCE =="])
    trace = dict(report.get("trace") or {})
    lines.append(
        _kv("TRACE_PATH", trace.get("path"))
        + f" present={'yes' if trace.get('present') else 'no'}"
    )
    recordings = list(report.get("recordings") or [])
    if not recordings:
        lines.append("RECORDING_PATH=(not found) present=no")
    for rec in recordings:
        extra = []
        if rec.get("drive_url"):
            extra.append(_kv("DRIVE_URL", rec.get("drive_url")))
        if rec.get("status"):
            extra.append(_kv("STATUS", rec.get("status")))
        suffix = (" " + " ".join(extra)) if extra else ""
        lines.append(
            _kv("RECORDING_PATH", rec.get("path"))
            + f" present={'yes' if rec.get('present') else 'no'}"
            + suffix
        )
    cdp = dict(report.get("cdp") or {})
    lines.append(_kv("CDP_URL", cdp.get("CDP_URL")))
    lines.append(_kv("CDP_PORT", cdp.get("CDP_PORT")))
    lines.append(_kv("PLAYWRIGHT_EXEC_COUNT", report.get("playwright_exec_count")))
    for row in report.get("playwright_exec") or []:
        lines.append(
            "  "
            + " ".join(
                (
                    _kv("ID", row.get("id")),
                    _kv("TOOL", row.get("tool")),
                    _kv("STATUS", row.get("status")),
                    _kv("UPDATED_AT", row.get("updated_at")),
                )
            )
        )
    gateway = report.get("gateway_progress")
    if gateway:
        lines.append(f"GATEWAY_PROGRESS={json.dumps(gateway, sort_keys=True, default=str)}")
    else:
        lines.append("GATEWAY_PROGRESS=(not found)")
    kinds = [item.get("kind") for item in report.get("checkpoints") or []]
    lines.append(_kv("CHECKPOINT_KINDS", ",".join(str(k) for k in kinds) or "(none)"))

    dest = dict(report.get("destination") or {})
    lines.extend(["", "== DESTINATION EVIDENCE =="])
    printed = set()
    for key in (
        "note_id",
        "ezlynx_note_id",
        "doc_id",
        "document_id",
        "discussion_id",
        "discussion_title",
        "applicant_id",
        "account_id",
        "document_path",
    ):
        lines.append(_kv(key.upper(), dest.get(key)))
        printed.add(key)
    for key, value in dest.items():
        if key not in printed:
            lines.append(_kv(key.upper(), value))

    logs = dict(report.get("logs") or {})
    lines.extend(["", "== RECENT LOG LINES =="])
    files = list(logs.get("files") or [])
    log_lines = list(logs.get("lines") or [])
    if not files:
        lines.append("(no standard log path present; pass --log or ROBIE_JOB_LOG)")
    else:
        for path in files:
            lines.append(_kv("LOG_PATH", path))
        if not log_lines:
            lines.append("(no matching lines in bounded tail)")
        for line in log_lines:
            lines.append(f"  {line}")
        if logs.get("truncated"):
            lines.append(f"(truncated to last {LOG_LINE_LIMIT} matching lines)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only job dump: jobs.db row, durable work items, "
            "Playwright/gateway pointers, and destination ids. "
            "Does not write, retry, kill, or deploy."
        )
    )
    parser.add_argument("job_id", help="Job UUID or unique id prefix")
    parser.add_argument(
        "--db",
        default=None,
        help="jobs.db path (default ROBIE_JOB_DB, checkout data/jobs.db, or Production ledger)",
    )
    parser.add_argument(
        "--artifact-root",
        default=None,
        help="Artifact root for playwright-trace.zip (default ROBIE_ARTIFACT_ROOT or <db>/artifacts)",
    )
    parser.add_argument(
        "--recording-root",
        default=None,
        help="Recording root (default ROBIE_RECORDING_ROOT or <db>/recordings)",
    )
    parser.add_argument(
        "--log",
        default=None,
        help="Optional log file to scan for matching job-id lines",
    )
    parser.add_argument(
        "--log-limit",
        type=int,
        default=LOG_LINE_LIMIT,
        help=f"Max matching log lines (default {LOG_LINE_LIMIT})",
    )
    args = parser.parse_args(argv)
    report = dump_job(
        args.job_id,
        db_path=args.db,
        artifact_root=args.artifact_root,
        recording_root=args.recording_root,
        log_path=args.log,
        log_limit=max(1, int(args.log_limit)),
    )
    print(format_dump(report))
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
