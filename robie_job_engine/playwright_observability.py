"""Structured Playwright observability for Chat / EZLynx jobs.

Production job 1df9740b finished UNVERIFIED in ~90s with gateway_progress,
zero attempts, zero verification_evidence, zero artifacts, zero
playwright_exec rows, and a leftover Ascend recorder tab. Operators could
not see whether the VM robot used Playwright at all.

This module:
1. Persists every playwright_exec call as a jobs.db row at start (committed)
   and again with the tool result, so a dead worker still leaves a row.
2. Snapshots Chrome CDP ``/json/list`` (url + title only) at job start/end.
3. Fail-closes a Chat/Playwright job that ends with zero tool rows instead
   of hiding behind UNVERIFIED prose.

PR 57 ``playwright_tracing.py`` stays the live-trace path. This is the
ledger operators query after the job.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import TERMINAL_STATUSES, JobStatus
from .recording_tab import (
    DEFAULT_CDP_URL,
    list_cdp_page_candidates,
    tab_candidates_from_cdp_payload,
)
from .secrets import REDACTED, is_secret_key, redact_mapping, redact_text
from .store import JobStore, utc_now


logger = logging.getLogger(__name__)

DEFAULT_JOBS_DB = "/opt/streetsmart-hermes/robie-job-engine/data/jobs.db"
DEFAULT_ARTIFACT_ROOT = "/opt/streetsmart-hermes/robie-job-engine/data/artifacts"
CDP_START_CHECKPOINT = "cdp_tabs_start"
CDP_END_CHECKPOINT = "cdp_tabs_end"
PLAYWRIGHT_TOOL = "playwright_exec"
CODE_PREVIEW_LIMIT = 800
RESULT_PREVIEW_LIMIT = 4000
CHAT_PLAYWRIGHT_ACTIONS = frozenset(
    {"hermes.google_chat_task", "hermes.plain_english"}
)
PLAYWRIGHT_REQUEST_MARKERS = (
    "ezlynx",
    "playwright",
    "commercial auto",
    "form entry",
    "formentry",
    "app.ezlynx",
    "useascend.com",
)
CONVERSATION_ONLY_MARKERS = (
    "what does this mean",
    "how is it going",
    "how are we doing",
    "what are you working on",
    "where did we leave off",
    "what did you do",
    "give me a rundown",
    "can you give me a rundown",
    "can you tell me what was done",
    "is this good or bad",
)
ZERO_PLAYWRIGHT_TOOL_ROWS = (
    "PLAYWRIGHT_SILENT: zero playwright_exec rows; this Chat/Playwright "
    "job never recorded a Playwright tool call (1df9740b silent-gap). "
    "Fail closed; not UNVERIFIED."
)
SECRET_TAB_KEYS = frozenset(
    {
        "websocketdebuggerurl",
        "devtoolsfrontendurl",
        "faviconurl",
        "parentid",
        "cookie",
        "cookies",
        "header",
        "headers",
        "authorization",
        "token",
        "secret",
    }
)


def bind_current_playwright_job(db_path: str | Path | None, job_id: str | None) -> None:
    """Expose the live Chat job to playwright_exec / PR 57 tracing.

    Does not overwrite ``ROBIE_JOB_DB`` — production already sets that to the
    live ledger. Tests share one process and must not point later tools at a
    deleted temp database.
    """
    if not job_id:
        return
    os.environ["ROBIE_JOB_ID"] = job_id
    os.environ["ROBIE_CURRENT_JOB_ID"] = job_id
    os.environ.setdefault("ROBIE_JOB_ACTION", "hermes.google_chat_task")
    _ = db_path


def resolve_playwright_job_binding(
    *,
    job_id: str | None = None,
    db_path: str | Path | None = None,
) -> tuple[str | None, str]:
    """Return (job_id, jobs.db). Env / latest RUNNING Chat job if omitted."""
    resolved_db = str(
        db_path
        or os.environ.get("ROBIE_JOB_DB")
        or DEFAULT_JOBS_DB
    )
    resolved_id = (
        job_id
        or os.environ.get("ROBIE_CURRENT_JOB_ID")
        or os.environ.get("ROBIE_JOB_ID")
        or os.environ.get("JOB_ID")
        or ""
    ).strip() or None
    if resolved_id:
        return resolved_id, resolved_db
    if not Path(resolved_db).is_file():
        return None, resolved_db
    try:
        store = JobStore(resolved_db)
        resolved_id = store.latest_running_chat_job_id()
    except Exception:
        resolved_id = None
    return resolved_id, resolved_db


def job_text(job: dict[str, Any]) -> str:
    payload = dict(job.get("payload") or {})
    return " ".join(
        str(part or "")
        for part in (
            payload.get("text"),
            payload.get("task"),
            job.get("action_type"),
        )
    )


_API_NOT_UI_MARKERS = (
    "mailing address",
    "certificate of insurance",
    "discussion note",
    "ezlynx_discussion_note",
)

_API_NOT_UI_ACTIONS = frozenset(
    {
        "ezlynx.policy_change",
        "ezlynx.certificate",
        "ezlynx.reassign",
    }
)


def deterministic_path_succeeded(store: JobStore, job: dict[str, Any] | None) -> bool:
    """True when readback or an answer-only close already proved the job."""
    if not job:
        return False
    from .answer_only import is_answer_only_job

    if is_answer_only_job(job):
        return True
    job_id = str(job.get("id") or "")
    if not job_id:
        return False
    try:
        if store.get_checkpoint(job_id, "answer_only_close"):
            return True
    except Exception:
        pass
    try:
        evidence = store.list_evidence(job_id)
    except Exception:
        evidence = []
    for item in evidence:
        if item.get("verified") and item.get("authoritative"):
            return True
    for kind in ("address_readback", "destination_readback"):
        try:
            note = store.get_checkpoint(job_id, kind) or {}
        except Exception:
            note = {}
        if note.get("passed") is True or note.get("proved") is True:
            return True
        if str(note.get("status") or "").casefold() == "passed":
            return True
    return False


def job_expected_to_drive_ui(job: dict[str, Any] | None) -> bool:
    """True only when this job was supposed to drive a browser screen.

    Notes, certificates, mailing-address changes, and answers use the API
    or a plain reply. A missing Playwright row is not a failure for those.
    """
    if not job_requires_playwright(job):
        return False
    action = str((job or {}).get("action_type") or "")
    if action in _API_NOT_UI_ACTIONS:
        return False
    payload = dict((job or {}).get("payload") or {})
    if payload.get("answer_only"):
        return False
    text = job_text(job or {}).casefold()
    if any(marker in text for marker in _API_NOT_UI_MARKERS):
        return False
    return True


def job_requires_playwright(job: dict[str, Any] | None) -> bool:
    """True when a Chat job asked for EZLynx / Playwright browser work."""
    if not job:
        return False
    action = str(job.get("action_type") or "")
    if action not in CHAT_PLAYWRIGHT_ACTIONS:
        return False
    text = " ".join(job_text(job).casefold().split())
    if any(text.startswith(marker) or marker == text for marker in CONVERSATION_ONLY_MARKERS):
        return False
    return any(marker in text for marker in PLAYWRIGHT_REQUEST_MARKERS)


def sanitize_tab_url(url: str) -> str:
    """Keep scheme/host/path. Redact secret-looking query keys. Drop fragment."""
    raw = redact_text(str(url or "").strip())
    if not raw:
        return ""
    parts = urlsplit(raw)
    kept: list[tuple[str, str]] = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if is_secret_key(key):
            kept.append((key, REDACTED))
        else:
            kept.append((key, redact_text(value)))
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(kept, safe="[]"), "")
    )


def tabs_from_cdp_list(payload: Any) -> list[dict[str, str]]:
    """url + title only. No cookies, websocket debugger URLs, or secrets."""
    tabs: list[dict[str, str]] = []
    for candidate in tab_candidates_from_cdp_payload(payload):
        tabs.append(
            {
                "url": sanitize_tab_url(candidate.url),
                "title": redact_text(str(candidate.title or "")),
            }
        )
    if tabs:
        return tabs
    if isinstance(payload, dict):
        payload = (
            payload.get("targetInfos")
            or payload.get("value")
            or payload.get("items")
            or []
        )
    if not isinstance(payload, list):
        return []
    for item in payload:
        if not isinstance(item, dict):
            continue
        target_type = str(item.get("type") or "page").casefold()
        if target_type and target_type not in {"page", "tab"}:
            continue
        leaked = [key for key in item if str(key).casefold().replace("_", "") in SECRET_TAB_KEYS]
        if leaked:
            # Drop the secret fields; still keep url/title.
            pass
        tabs.append(
            {
                "url": sanitize_tab_url(str(item.get("url") or "")),
                "title": redact_text(str(item.get("title") or "")),
            }
        )
    return tabs


def snapshot_cdp_tabs(
    *,
    cdp_url: str | None = None,
    http_get: Callable[[str], tuple[int, bytes]] | None = None,
    payload: Any = None,
) -> dict[str, Any]:
    """Snapshot Chrome ``/json/list``. Fixture payload needs no live browser."""
    captured_at = utc_now()
    endpoint = (cdp_url or os.environ.get("ROBIE_BROWSER_CDP_URL") or DEFAULT_CDP_URL).rstrip(
        "/"
    )
    if payload is not None:
        return {
            "ok": True,
            "source": "fixture",
            "cdp_url": endpoint,
            "captured_at": captured_at,
            "tabs": tabs_from_cdp_list(payload),
        }
    try:
        candidates = list_cdp_page_candidates(cdp_url=endpoint, http_get=http_get)
    except Exception as exc:
        return {
            "ok": False,
            "source": "cdp",
            "cdp_url": endpoint,
            "captured_at": captured_at,
            "error": f"{type(exc).__name__}: {exc}",
            "tabs": [],
        }
    return {
        "ok": True,
        "source": "cdp",
        "cdp_url": endpoint,
        "captured_at": captured_at,
        "tabs": [
            {
                "url": sanitize_tab_url(item.url),
                "title": redact_text(str(item.title or "")),
            }
            for item in candidates
        ],
    }


def persist_cdp_snapshot(
    store: JobStore | str | Path,
    job_id: str,
    phase: str,
    *,
    cdp_url: str | None = None,
    http_get: Callable[[str], tuple[int, bytes]] | None = None,
    payload: Any = None,
) -> dict[str, Any]:
    """Write start/end CDP tab checkpoint. Never raises into the job path."""
    kind = CDP_START_CHECKPOINT if phase == "start" else CDP_END_CHECKPOINT
    jobs = store if isinstance(store, JobStore) else JobStore(store)
    snapshot = snapshot_cdp_tabs(cdp_url=cdp_url, http_get=http_get, payload=payload)
    snapshot["phase"] = "start" if phase == "start" else "end"
    if not job_id:
        return snapshot
    try:
        jobs.get_job(job_id)
        jobs.checkpoint(job_id, kind, snapshot)
    except KeyError:
        return snapshot
    except Exception:
        logger.exception("cdp snapshot persist failed job=%s phase=%s", job_id, phase)
    return snapshot


def _code_preview(code: str) -> str:
    text = redact_text(str(code or ""))
    if len(text) > CODE_PREVIEW_LIMIT:
        return text[:CODE_PREVIEW_LIMIT] + "…"
    return text


def _result_preview(result: Any) -> dict[str, Any]:
    if result is None:
        return {}
    if isinstance(result, dict):
        payload = redact_mapping(result)
        output = payload.get("output")
        if isinstance(output, str) and len(output) > RESULT_PREVIEW_LIMIT:
            payload["output"] = output[:RESULT_PREVIEW_LIMIT] + "…"
        error = payload.get("error")
        if isinstance(error, str) and len(error) > RESULT_PREVIEW_LIMIT:
            payload["error"] = error[:RESULT_PREVIEW_LIMIT] + "…"
        return payload
    text = redact_text(str(result))
    if len(text) > RESULT_PREVIEW_LIMIT:
        text = text[:RESULT_PREVIEW_LIMIT] + "…"
    return {"text": text}


def persist_playwright_exec_start(
    db_path: str | Path,
    job_id: str,
    code: str,
    *,
    tool: str = PLAYWRIGHT_TOOL,
) -> int | None:
    """Insert a started row and commit before the runner runs."""
    path = Path(db_path)
    if not path.is_file():
        return None
    try:
        store = JobStore(path)
        store.get_job(job_id)
        return store.add_playwright_exec(
            job_id,
            tool,
            "started",
            code_preview=_code_preview(code),
            result={"status": "started"},
        )
    except KeyError:
        return None
    except Exception:
        logger.exception("playwright_exec start row failed job=%s", job_id)
        return None


def persist_playwright_exec_finish(
    db_path: str | Path,
    row_id: int | None,
    result: Any,
    *,
    status: str | None = None,
) -> None:
    if row_id is None:
        return
    payload = _result_preview(result)
    if status is None:
        if isinstance(result, dict) and result.get("ok") is False:
            status = "error"
        elif isinstance(result, dict) and (
            result.get("success") or result.get("ok") is True
        ):
            status = "ok"
        elif isinstance(result, dict) and result.get("error"):
            status = "error"
        else:
            status = "ok"
    try:
        JobStore(db_path).update_playwright_exec(row_id, status=status, result=payload)
    except Exception:
        logger.exception("playwright_exec finish row failed id=%s", row_id)


def record_playwright_exec(
    code: str,
    result: Any,
    *,
    job_id: str | None = None,
    db_path: str | Path | None = None,
    status: str = "ok",
) -> int | None:
    """One-shot persist for tests and non-wrapped callers."""
    resolved_id, resolved_db = resolve_playwright_job_binding(
        job_id=job_id, db_path=db_path
    )
    if not resolved_id:
        return None
    row_id = persist_playwright_exec_start(resolved_db, resolved_id, code)
    persist_playwright_exec_finish(resolved_db, row_id, result, status=status)
    return row_id


def list_playwright_exec(store: JobStore | str | Path, job_id: str) -> list[dict[str, Any]]:
    jobs = store if isinstance(store, JobStore) else JobStore(store)
    return jobs.list_playwright_exec(job_id)


def fail_closed_zero_playwright_rows(
    store: JobStore,
    job: dict[str, Any],
    *,
    expected: set[JobStatus] | None = None,
) -> dict[str, Any]:
    """FAILED when a Playwright-required Chat job has zero tool rows.

    Heartbeat + leftover recorder tab is not evidence the robot used
    Playwright. UNVERIFIED prose is not an allowed hide.
    """
    current = store.get_job(job["id"])
    if deterministic_path_succeeded(store, current):
        return current
    if not job_expected_to_drive_ui(current):
        return current
    if not job_requires_playwright(current):
        return current
    if list_playwright_exec(store, current["id"]):
        return current
    status = JobStatus(current["status"])
    if status not in {
        JobStatus.RUNNING,
        JobStatus.VERIFYING,
        JobStatus.UNVERIFIED,
        JobStatus.PENDING,
    }:
        return current
    allowed = expected or {
        JobStatus.RUNNING,
        JobStatus.VERIFYING,
        JobStatus.UNVERIFIED,
        JobStatus.PENDING,
    }
    if status not in allowed:
        return current
    store.checkpoint(
        current["id"],
        "playwright_silent",
        {
            "reason": ZERO_PLAYWRIGHT_TOOL_ROWS,
            "tool_rows": 0,
            "incident": "1df9740b",
        },
    )
    return store.transition(
        current["id"],
        JobStatus.FAILED,
        expected=allowed,
        error=ZERO_PLAYWRIGHT_TOOL_ROWS,
        release_lease=True,
    )


def maybe_snapshot_and_bind(
    db_path: str | Path,
    job_id: str | None,
    *,
    phase: str,
    http_get: Callable[[str], tuple[int, bytes]] | None = None,
    payload: Any = None,
) -> dict[str, Any] | None:
    if not job_id:
        return None
    bind_current_playwright_job(db_path, job_id)
    try:
        return persist_cdp_snapshot(
            db_path, job_id, phase, http_get=http_get, payload=payload
        )
    except Exception:
        logger.exception("cdp snapshot failed job=%s phase=%s", job_id, phase)
        return None


def audit_playwright_tool_log(store: JobStore, job: dict[str, Any]) -> dict[str, Any]:
    rows = list_playwright_exec(store, job["id"])
    required = job_requires_playwright(job)
    if required and not rows:
        return {
            "result": "FAIL",
            "required": True,
            "row_count": 0,
            "reason": ZERO_PLAYWRIGHT_TOOL_ROWS,
        }
    return {
        "result": "PASS" if rows or not required else "FAIL",
        "required": required,
        "row_count": len(rows),
        "statuses": [str(row.get("status") or "") for row in rows],
    }


def encode_json_list_fixture(tabs: list[dict[str, Any]]) -> bytes:
    """Test helper: Chrome-shaped ``/json/list`` bytes."""
    return json.dumps(tabs).encode("utf-8")


def operator_tab_views(snapshot: dict[str, Any] | None) -> list[dict[str, str]]:
    """url + title only from a stored CDP checkpoint. Drops cookies/secrets."""
    tabs: list[dict[str, str]] = []
    for item in list((snapshot or {}).get("tabs") or []):
        if not isinstance(item, dict):
            continue
        tabs.append(
            {
                "url": sanitize_tab_url(str(item.get("url") or "")),
                "title": redact_text(str(item.get("title") or "")),
            }
        )
    return tabs


def operator_exec_views(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Structured playwright_exec rows for operators. Already redacted at write."""
    views: list[dict[str, Any]] = []
    for row in rows:
        result = dict(row.get("result") or {})
        views.append(
            {
                "id": row.get("id"),
                "tool": redact_text(str(row.get("tool") or PLAYWRIGHT_TOOL)),
                "status": str(row.get("status") or ""),
                "code_preview": redact_text(str(row.get("code_preview") or "")),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
                "error": redact_text(str(result.get("error") or "")),
                "success": bool(result.get("success") or result.get("ok") is True),
            }
        )
    return views


def trace_zip_for_job(
    job_id: str,
    *,
    artifact_root: str | Path | None = None,
) -> dict[str, Any]:
    """Report the PR 57 trace zip path without creating folders."""
    root = Path(
        artifact_root
        or os.environ.get("ROBIE_ARTIFACT_ROOT")
        or DEFAULT_ARTIFACT_ROOT
    )
    path = root / job_id / "playwright-trace.zip"
    return {
        "path": str(path),
        "present": path.is_file() and path.stat().st_size > 0,
    }


def lookup_playwright_job(
    job_id: str,
    *,
    db_path: str | Path | None = None,
    artifact_root: str | Path | None = None,
) -> dict[str, Any]:
    """Read-only operator view of what Playwright actually did.

    Standing diagnosis path. Chat prose and a frozen recording are not
    substitutes. Does not bind a port, write rows, or authorize COMPLETE.
    """
    resolved_db = str(db_path or os.environ.get("ROBIE_JOB_DB") or DEFAULT_JOBS_DB)
    if not Path(resolved_db).is_file():
        return {
            "ok": False,
            "job_id": job_id,
            "error": f"jobs.db is missing: {resolved_db}",
            "playwright_exec": [],
            "cdp_tabs_start": [],
            "cdp_tabs_end": [],
            "trace": {"path": None, "present": False},
        }
    store = JobStore(resolved_db)
    try:
        job = store.get_job(job_id)
    except KeyError:
        return {
            "ok": False,
            "job_id": job_id,
            "error": f"job not found: {job_id}",
            "playwright_exec": [],
            "cdp_tabs_start": [],
            "cdp_tabs_end": [],
            "trace": trace_zip_for_job(job_id, artifact_root=artifact_root),
        }
    start = store.get_checkpoint(job_id, CDP_START_CHECKPOINT) or {}
    end = store.get_checkpoint(job_id, CDP_END_CHECKPOINT) or {}
    rows = operator_exec_views(list_playwright_exec(store, job_id))
    return {
        "ok": True,
        "job_id": job["id"],
        "status": job["status"],
        "action_type": job.get("action_type"),
        "requires_playwright": job_requires_playwright(job),
        "playwright_exec": rows,
        "playwright_exec_count": len(rows),
        "cdp_tabs_start": operator_tab_views(start),
        "cdp_tabs_end": operator_tab_views(end),
        "trace": trace_zip_for_job(job_id, artifact_root=artifact_root),
        "zero_tool_rows": job_requires_playwright(job) and not rows,
    }


def format_playwright_job_lookup(report: dict[str, Any]) -> str:
    """Plain-text operator card. No cookies, secrets, or passwords."""
    lines = [
        f"ROBIE Playwright lookup — {report.get('job_id')} — "
        f"{report.get('status') or report.get('error') or 'UNKNOWN'}"
    ]
    if not report.get("ok"):
        lines.append(str(report.get("error") or "lookup failed"))
        return "\n".join(lines)
    rows = list(report.get("playwright_exec") or [])
    lines.append(f"playwright_exec rows: {len(rows)}")
    if not rows:
        if report.get("zero_tool_rows"):
            lines.append(f"  {ZERO_PLAYWRIGHT_TOOL_ROWS}")
        else:
            lines.append("  (none)")
    for index, row in enumerate(rows, start=1):
        preview = str(row.get("code_preview") or "").replace("\n", " ")
        if len(preview) > 120:
            preview = preview[:120] + "…"
        extra = f" error={row['error']}" if row.get("error") else ""
        lines.append(f"  {index}. {row.get('status')} {preview}{extra}")
    for phase, key in (("start", "cdp_tabs_start"), ("end", "cdp_tabs_end")):
        tabs = list(report.get(key) or [])
        lines.append(f"CDP tabs {phase}: {len(tabs)}")
        if not tabs:
            lines.append("  (none)")
        for tab in tabs:
            title = tab.get("title") or "(no title)"
            lines.append(f"  - {title} | {tab.get('url')}")
    trace = dict(report.get("trace") or {})
    present = "present" if trace.get("present") else "missing"
    lines.append(f"trace zip: {trace.get('path') or '(none)'} ({present})")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI: ``python -m robie_job_engine.playwright_observability <job-id>``."""
    parser = argparse.ArgumentParser(
        description=(
            "Read-only Playwright/EZLynx job lookup. Prints playwright_exec "
            "rows, CDP url+title snapshots, and the trace zip path. "
            "Does not bind a port, deploy, or authorize COMPLETE."
        )
    )
    parser.add_argument("job_id", help="Job id to inspect")
    parser.add_argument(
        "--db",
        default=os.environ.get("ROBIE_JOB_DB") or DEFAULT_JOBS_DB,
        help="jobs.db path (default ROBIE_JOB_DB or Production ledger path)",
    )
    parser.add_argument(
        "--artifact-root",
        default=os.environ.get("ROBIE_ARTIFACT_ROOT") or DEFAULT_ARTIFACT_ROOT,
        help="Artifact root used by PR 57 playwright-trace.zip",
    )
    args = parser.parse_args(argv)
    report = lookup_playwright_job(
        args.job_id, db_path=args.db, artifact_root=args.artifact_root
    )
    print(format_playwright_job_lookup(report))
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
