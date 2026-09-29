"""SMALL Production pre-flight. Yes/no only. Chat on the first no.

Complement to post-job audit. Not a dashboard. The seven checks observe
only: no Chrome, hermes-gateway, or browser restart; no bind; no
client-file navigation. After the checks, a leftover-tab flush may close
any unclaimed page via CDP Target.closeTarget. A zip pointer match is
not health. Chat-runtime is the seventh check: adapter / Playwright
tools loaded from .hermes must equal the zip (bytes or zip-load shim).

Hooked after the zip pointer flip + hermes-gateway restart (ExecStartPost
on the PYTHONPATH drop-in) and by an every-day 7am-midnight ET oneshot timer.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .login_secret_health import DEFAULT_PROJECT, DEFAULT_SECRETS, inspect_login_secrets
from .models import JobStatus, TERMINAL_STATUSES


DEFAULT_CDP_URL = "http://127.0.0.1:9222"
DEFAULT_JOBS_DB = "/opt/streetsmart-hermes/robie-job-engine/data/jobs.db"
DEFAULT_CHAT_SPACE = "spaces/AAQAZbLJO78"
CANONICAL_JOB_ENGINE_ROOT = "/opt/streetsmart-hermes/releases/current"
GATEWAY_UNIT = "hermes-gateway"
AUTHENTICATED_APP_PREFIX = "https://app.ezlynx.com/web/"
LOGIN_PATH = "/auth/account/login"
# Job Engine enums plus the lowercase wording used for this check.
TERMINAL_BIND_STATUSES = {status.value for status in TERMINAL_STATUSES} | {
    "completed",
    JobStatus.COMPLETE.value.lower(),
}

CHECK_GATEWAY = "hermes-gateway"
CHECK_CDP = "cdp"
CHECK_EZLYNX_TAB = "ezlynx-tab"
CHECK_SECRETS = "login-secrets"
CHECK_LINKS = "conversation-job-links"
CHECK_CHAT_INTAKE = "chat-intake"
CHECK_CHAT_RUNTIME = "chat-runtime"
CHAT_JOB_ACTION = "hermes.google_chat_task"
CHAT_PAYLOAD_SOURCE = "Google Chat"
# Gateway writes "[GoogleChat] Connected" here, not to the systemd journal.
GATEWAY_AGENT_LOG = "/opt/streetsmart-hermes/.hermes/logs/agent.log"
DEFAULT_CHAT_INTAKE_FRESH_SECONDS = 6 * 60 * 60
LISTENER_CONNECTED_MARKER = "[GoogleChat] Connected"
LISTENER_HANDOFF_MARKER = "durable executable handoff"
LISTENER_FATAL_MARKERS = (
    "pubsub_reconnect_exhausted",
    "Pub/Sub reconnect failed",
    "subscription_not_found",
)
LISTENER_STALL_MARKERS = (
    "bound Job is not awaiting human input",
    "active human-input correlation is missing",
    "conversation is not awaiting human input",
)


def _chat_space() -> str:
    for key in (
        "ROBIE_PREFLIGHT_CHAT_SPACE",
        "ROBIE_OPS_CHAT_SPACE",
        "GOOGLE_CHAT_HOME_CHANNEL",
    ):
        value = os.environ.get(key, "").strip()
        if value.startswith("spaces/"):
            return value
    return DEFAULT_CHAT_SPACE


def _cdp_url() -> str:
    return (
        os.environ.get("ROBIE_BROWSER_CDP_URL") or DEFAULT_CDP_URL
    ).rstrip("/")


def _jobs_db() -> str:
    return os.environ.get("ROBIE_JOB_DB") or DEFAULT_JOBS_DB


def _result(name: str, ok: bool, evidence: str) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "evidence": evidence}


def _http_get(url: str, *, timeout: float = 2.0) -> tuple[int, bytes]:
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        status = int(getattr(response, "status", 200) or 200)
        return status, response.read()


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )


def _parse_environment(text: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("Environment="):
            line = line[len("Environment=") :]
        for part in line.split():
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            env[key] = value
    return env


def _job_engine_on_pythonpath(pythonpath: str) -> bool:
    for entry in pythonpath.split(":"):
        root = Path(entry)
        if (root / "robie_job_engine" / "__init__.py").is_file():
            return True
    return False


def probe_hermes_gateway(
    *,
    runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    """Read hermes-gateway ActiveState + PYTHONPATH. Never probes a Job Engine unit."""
    run = runner or _run
    active_proc = run(["systemctl", "is-active", GATEWAY_UNIT])
    show_proc = run(
        [
            "systemctl",
            "show",
            GATEWAY_UNIT,
            "-p",
            "ActiveState",
            "-p",
            "Environment",
            "-p",
            "MainPID",
            "--no-pager",
        ]
    )
    show_text = (show_proc.stdout or "") + "\n" + (active_proc.stdout or "")
    env = _parse_environment(show_proc.stdout or "")
    pythonpath = env.get("PYTHONPATH") or os.environ.get("PYTHONPATH") or ""
    canonical = (
        env.get("ROBIE_CANONICAL_JOB_ENGINE_ROOT")
        or os.environ.get("ROBIE_CANONICAL_JOB_ENGINE_ROOT")
        or ""
    )
    active = (active_proc.stdout or "").strip() == "active" or "ActiveState=active" in show_text
    return {
        "active": active,
        "active_state": (active_proc.stdout or "").strip()
        or env.get("ActiveState")
        or "unknown",
        "pythonpath": pythonpath,
        "canonical_root": canonical,
        "job_engine_present": _job_engine_on_pythonpath(pythonpath),
        "show": show_text.strip(),
    }


def check_hermes_gateway(
    probe: dict[str, Any] | None = None,
    *,
    runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    data = dict(probe) if probe is not None else probe_hermes_gateway(runner=runner)
    pythonpath = str(data.get("pythonpath") or "")
    present = data.get("job_engine_present")
    if present is None:
        present = _job_engine_on_pythonpath(pythonpath)
    if not data.get("active"):
        return _result(
            CHECK_GATEWAY,
            False,
            f"{GATEWAY_UNIT} ActiveState={data.get('active_state') or 'inactive'}",
        )
    if CANONICAL_JOB_ENGINE_ROOT not in pythonpath.split(":"):
        return _result(
            CHECK_GATEWAY,
            False,
            "hermes-gateway PYTHONPATH does not load Job Engine from "
            f"{CANONICAL_JOB_ENGINE_ROOT}",
        )
    if not present:
        return _result(
            CHECK_GATEWAY,
            False,
            "Job Engine package missing on PYTHONPATH "
            f"{CANONICAL_JOB_ENGINE_ROOT}/robie_job_engine",
        )
    return _result(
        CHECK_GATEWAY,
        True,
        f"{GATEWAY_UNIT} active; Job Engine on PYTHONPATH {CANONICAL_JOB_ENGINE_ROOT}",
    )


def check_cdp(
    *,
    cdp_url: str | None = None,
    http_get: Callable[[str], tuple[int, bytes]] | None = None,
) -> dict[str, Any]:
    url = (cdp_url or _cdp_url()).rstrip("/")
    version = f"{url}/json/version"
    getter = http_get or _http_get
    try:
        status, _body = getter(version)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return _result(
            CHECK_CDP,
            False,
            f"{version} error {type(exc).__name__}",
        )
    if int(status) != 200:
        return _result(CHECK_CDP, False, f"{version} HTTP {status}")
    return _result(CHECK_CDP, True, f"{version} HTTP 200")


def _tab_urls(payload: Any) -> list[str]:
    if isinstance(payload, dict):
        payload = payload.get("value") or payload.get("items") or []
    if not isinstance(payload, list):
        return []
    urls: list[str] = []
    for item in payload:
        if isinstance(item, str):
            urls.append(item)
            continue
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if url:
            urls.append(url)
    return urls


def ezlynx_web_tab_ok(url: str) -> bool:
    raw = str(url or "").strip()
    if not raw:
        return False
    folded = raw.casefold()
    if LOGIN_PATH in urlsplit(folded).path:
        return False
    return urlsplit(folded).geturl().split("?", 1)[0].startswith(AUTHENTICATED_APP_PREFIX)


def check_ezlynx_tab(
    *,
    cdp_url: str | None = None,
    http_get: Callable[[str], tuple[int, bytes]] | None = None,
    tabs: list[str] | None = None,
) -> dict[str, Any]:
    """Read CDP tab URLs only. Never navigates. Never opens a client file."""
    if tabs is None:
        url = (cdp_url or _cdp_url()).rstrip("/")
        getter = http_get or _http_get
        listed: list[str] = []
        last_error = "no CDP tab list"
        for suffix in ("/json/list", "/json"):
            try:
                status, body = getter(f"{url}{suffix}")
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                last_error = f"{url}{suffix} {type(exc).__name__}"
                continue
            if int(status) != 200:
                last_error = f"{url}{suffix} HTTP {status}"
                continue
            try:
                listed = _tab_urls(json.loads(body.decode("utf-8", errors="replace")))
            except json.JSONDecodeError:
                last_error = f"{url}{suffix} invalid JSON"
                continue
            last_error = f"{url}{suffix} empty tab list"
            break
        tabs = listed
        if not tabs:
            if "empty tab list" in last_error:
                return _result(
                    CHECK_EZLYNX_TAB,
                    False,
                    "empty CDP target list; session is not fine",
                )
            return _result(
                CHECK_EZLYNX_TAB,
                False,
                last_error or "empty CDP target list; session is not fine",
            )
    if not tabs:
        return _result(
            CHECK_EZLYNX_TAB,
            False,
            "empty CDP target list; session is not fine",
        )
    matching = [item for item in tabs if ezlynx_web_tab_ok(item)]
    if matching:
        return _result(CHECK_EZLYNX_TAB, True, matching[0].split("?", 1)[0])
    sample = (tabs[0].split("?", 1)[0] if tabs else "none")
    return _result(
        CHECK_EZLYNX_TAB,
        False,
        f"no EZLynx tab on {AUTHENTICATED_APP_PREFIX} (saw {sample})",
    )


def check_login_secrets(
    *,
    inspector: Callable[..., dict[str, Any]] | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    inspect = inspector or (lambda: inspect_login_secrets(client=client, project=DEFAULT_PROJECT))
    report = dict(inspect())
    parts: list[str] = []
    missing: list[str] = []
    for secret_id in DEFAULT_SECRETS:
        row = next(
            (
                item
                for item in (report.get("secrets") or [])
                if item.get("secret_id") == secret_id
            ),
            None,
        )
        enabled = (row or {}).get("newest_enabled_version") or ""
        if row is None or row.get("missing_enabled") or not enabled:
            missing.append(secret_id)
            parts.append(f"{secret_id} no ENABLED version")
        else:
            parts.append(f"{secret_id} ENABLED {enabled}")
    evidence = "; ".join(parts) or str(report.get("reason") or "secret version states")
    ok = report.get("result") == "OK" and not missing
    return _result(CHECK_SECRETS, ok, evidence)


def _status_is_terminal(status: str) -> bool:
    return str(status or "").strip().casefold() in {item.casefold() for item in TERMINAL_BIND_STATUSES}


def check_conversation_job_links(
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    path = Path(db_path or _jobs_db())
    if not path.is_file():
        return _result(CHECK_LINKS, False, f"jobs.db missing at {path}")
    try:
        uri = f"file:{path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "conversation_job_links" not in tables:
                return _result(
                    CHECK_LINKS, False, "jobs.db has no conversation_job_links table"
                )
            rows = conn.execute(
                """
                SELECT links.conversation_id, links.job_id, jobs.status
                FROM conversation_job_links AS links
                LEFT JOIN jobs ON jobs.id = links.job_id
                WHERE links.active=1
                """
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        return _result(CHECK_LINKS, False, f"jobs.db unreadable: {type(exc).__name__}")
    stale = [
        row
        for row in rows
        if row["status"] is None or _status_is_terminal(str(row["status"]))
    ]
    if stale:
        row = stale[0]
        status = row["status"] or "missing"
        return _result(
            CHECK_LINKS,
            False,
            f"active=1 job {row['job_id']} status={status}",
        )
    return _result(
        CHECK_LINKS,
        True,
        f"no active=1 terminal bind ({len(rows)} live link(s))",
    )


def _parse_utc(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _fresh_seconds() -> int:
    raw = os.environ.get("ROBIE_PREFLIGHT_CHAT_INTAKE_MAX_AGE_SECONDS", "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = DEFAULT_CHAT_INTAKE_FRESH_SECONDS
        if value > 0:
            return value
    return DEFAULT_CHAT_INTAKE_FRESH_SECONDS


def last_chat_inbound_at(db_path: str | Path | None = None) -> datetime | None:
    """Latest DurableChatEventQueue or Chat-job timestamp. Read-only."""
    path = Path(db_path or _jobs_db())
    if not path.is_file():
        return None
    stamps: list[datetime] = []
    try:
        uri = f"file:{path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "chat_event_queue" in tables:
                row = conn.execute(
                    """
                    SELECT MAX(created_at), MAX(updated_at)
                    FROM chat_event_queue
                    """
                ).fetchone()
                for value in row or ():
                    parsed = _parse_utc(value)
                    if parsed is not None:
                        stamps.append(parsed)
            if "jobs" in tables:
                # Chat messages now arrive as hermes.plain_english with
                # payload source "Google Chat", not only hermes.google_chat_task.
                row = conn.execute(
                    """
                    SELECT MAX(created_at)
                    FROM jobs
                    WHERE action_type=?
                       OR json_extract(payload_json, '$.source') = ?
                    """,
                    (CHAT_JOB_ACTION, CHAT_PAYLOAD_SOURCE),
                ).fetchone()
                parsed = _parse_utc(row[0] if row else None)
                if parsed is not None:
                    stamps.append(parsed)
        finally:
            conn.close()
    except Exception:
        return max(stamps) if stamps else None
    return max(stamps) if stamps else None


def classify_listener_journal(text: str) -> dict[str, Any]:
    """Observe gateway logs only. Does not send a Chat job."""
    blob = str(text or "")
    folded = blob.casefold()
    fatal = next((item for item in LISTENER_FATAL_MARKERS if item.casefold() in folded), None)
    stall = next((item for item in LISTENER_STALL_MARKERS if item.casefold() in folded), None)
    return {
        "connected": LISTENER_CONNECTED_MARKER in blob,
        "handoff": LISTENER_HANDOFF_MARKER in blob,
        "fatal": fatal,
        "stall": stall,
        "wedged": bool(fatal or stall),
    }


def _read_agent_log(path: str | Path | None = None) -> str:
    """Gateway agent log. Missing or unreadable is empty, never an error."""
    log_path = Path(path or GATEWAY_AGENT_LOG)
    try:
        return log_path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return ""


def _read_gateway_journal(
    *,
    runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
) -> str:
    run = runner or _run
    proc = run(
        [
            "journalctl",
            "-u",
            GATEWAY_UNIT,
            "-n",
            "400",
            "--no-pager",
            "-o",
            "cat",
            "--since",
            "24 hours ago",
        ]
    )
    text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    agent_log = _read_agent_log()
    if agent_log:
        text = f"{text}\n{agent_log}"
    return text


def check_chat_intake(
    db_path: str | Path | None = None,
    *,
    now: datetime | None = None,
    journal: str | None = None,
    journal_reader: Callable[[], str] | None = None,
    fresh_seconds: int | None = None,
) -> dict[str, Any]:
    """Yes if Chat intake is live. Silent/wedged listener + active gateway is no.

    Recent DurableChatEventQueue / Chat-job activity is yes. Recency alone is
    not the wedge signal (#26 can leave a still-fresh prior job). A stall or
    fatal marker in hermes-gateway logs is no. Idle Connected without a stall
    is yes so a quiet morning does not page. No inbound and no Connected is
    no: a silent listener while hermes-gateway is active.
    Does not @robie. Does not send a test Chat job.
    """
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    window = fresh_seconds if fresh_seconds is not None else _fresh_seconds()
    inbound_at = last_chat_inbound_at(db_path)
    age = (clock - inbound_at).total_seconds() if inbound_at is not None else None
    recent = age is not None and age >= 0 and age <= window
    if journal is None:
        reader = journal_reader or _read_gateway_journal
        try:
            journal = reader()
        except Exception:
            journal = ""
    state = classify_listener_journal(journal or "")
    if state["wedged"]:
        reason = state["fatal"] or state["stall"] or "Pub/Sub stall"
        return _result(
            CHECK_CHAT_INTAKE,
            False,
            f"{GATEWAY_UNIT} active; Chat listener wedged ({reason})",
        )
    if recent:
        stamp = inbound_at.isoformat() if inbound_at else "recent"
        return _result(
            CHECK_CHAT_INTAKE,
            True,
            f"last Chat inbound {stamp} ({int(age)}s ago)",
        )
    if state["connected"]:
        return _result(
            CHECK_CHAT_INTAKE,
            True,
            f"{GATEWAY_UNIT} active; Chat Pub/Sub listener connected (idle)",
        )
    if inbound_at is not None:
        return _result(
            CHECK_CHAT_INTAKE,
            False,
            f"{GATEWAY_UNIT} active; Chat listener silent "
            f"(last inbound {inbound_at.isoformat()})",
        )
    path = Path(db_path or _jobs_db())
    return _result(
        CHECK_CHAT_INTAKE,
        False,
        f"{GATEWAY_UNIT} active; Chat listener silent "
        f"(no Chat inbound in {path})",
    )


def check_chat_runtime(
    *,
    opt_root: str | Path | None = None,
    release_root: str | Path | None = None,
    hermes_home: str | Path | None = None,
    prover: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Yes only when Chat-loaded dests equal the zip this pointer names.

    Pointer-only is a no. Skills are not part of this check.
    """
    from .deploy_truth import (
        DEFAULT_OPT_ROOT,
        prove_chat_runtime_matches_zip,
        pointer_targets,
    )

    root = Path(opt_root) if opt_root else DEFAULT_OPT_ROOT
    dest = Path(hermes_home) if hermes_home else root / ".hermes"
    zip_root = Path(release_root) if release_root else None
    if zip_root is None:
        pointers = pointer_targets(opt_root=root)
        target = pointers.get("releases_current_target") or pointers.get(
            "current_target"
        )
        if not target:
            return _result(
                CHECK_CHAT_RUNTIME,
                False,
                "zip pointer missing; Chat load path cannot equal the zip",
            )
        zip_root = Path(str(target))
    prove = prover or prove_chat_runtime_matches_zip
    report = prove(release_root=zip_root, hermes_home=dest)
    return _result(
        CHECK_CHAT_RUNTIME,
        bool(report.get("ok")),
        str(report.get("evidence") or "Chat load path does not equal the zip"),
    )


CHECKS: tuple[Callable[..., dict[str, Any]], ...] = (
    check_hermes_gateway,
    check_cdp,
    check_ezlynx_tab,
    check_login_secrets,
    check_conversation_job_links,
    check_chat_intake,
    check_chat_runtime,
)


def format_failure(check_name: str, evidence: str) -> str:
    """One check name and one evidence line. Never @robie."""
    return (
        f"ROBIE Production pre-flight: no — {check_name}\n"
        f"{evidence.strip() or 'no evidence'}"
    )


def _post_failure(
    text: str,
    *,
    poster: Callable[..., Any] | None,
    dm_finder: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Post the Robie space message, then the same text to Carlo/Jake DMs.

    Keeps the existing space post. There is no outbound email API on
    hermes-poc-01; operator notify reuses ``post_as_chat_app`` against an
    existing DM space from ``find_direct_message_space``. Never @robie.
    """
    if "@robie" in text.casefold():
        raise ValueError("pre-flight Chat must not @robie")
    from .chat_app_post import fail_notify_emails, find_direct_message_space, post_as_chat_app

    send = poster if poster is not None else post_as_chat_app
    finder = dm_finder if dm_finder is not None else find_direct_message_space
    targets: list[str] = []
    dm_errors: list[str] = []
    space = _chat_space()
    space_posted = False
    if space.startswith("spaces/"):
        send(space, text)
        targets.append(space)
        space_posted = True
    for email in fail_notify_emails():
        try:
            dm_space = finder(email)
        except Exception as exc:
            dm_errors.append(f"{email}: {type(exc).__name__}")
            continue
        if not str(dm_space or "").startswith("spaces/"):
            dm_errors.append(f"{email}: no existing DM space")
            continue
        if dm_space in targets:
            continue
        try:
            send(dm_space, text)
            targets.append(dm_space)
        except Exception as exc:
            dm_errors.append(f"{email}: {type(exc).__name__}")
    return {
        "space_posted": space_posted,
        "targets": targets,
        "dm_errors": dm_errors,
    }


def _attach_tab_flush(
    payload: dict[str, Any],
    *,
    db_path: str | Path | None,
    ezlynx_tabs: list[str] | None,
    cdp_http_get: Callable[[str], tuple[int, bytes]] | None,
) -> None:
    """Best-effort leftover flush. Never fails the yes/no pre-flight."""
    try:
        from .tab_cleanup import maybe_flush_orphaned_tabs, tabs_from_cdp_payload

        flush_tabs = None
        if ezlynx_tabs is not None:
            flush_tabs = tabs_from_cdp_payload(ezlynx_tabs)
        flushed = maybe_flush_orphaned_tabs(
            db_path=db_path,
            tabs=flush_tabs,
            http_get=cdp_http_get,
        )
        payload["tab_flush"] = flushed
        payload["tab_sweep"] = flushed
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        payload["tab_flush_error"] = error
        payload["tab_sweep_error"] = error


def run_production_preflight(
    *,
    gateway_probe: dict[str, Any] | None = None,
    cdp_http_get: Callable[[str], tuple[int, bytes]] | None = None,
    ezlynx_tabs: list[str] | None = None,
    secret_inspector: Callable[..., dict[str, Any]] | None = None,
    db_path: str | Path | None = None,
    poster: Callable[..., Any] | None = None,
    dm_finder: Callable[[str], str] | None = None,
    journal: str | None = None,
    journal_reader: Callable[[], str] | None = None,
    now: datetime | None = None,
    chat_runtime_probe: dict[str, Any] | Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run checks in order. First no posts Chat (space + operator DMs) and stops."""
    runners: list[tuple[str, Callable[[], dict[str, Any]]]] = [
        (CHECK_GATEWAY, lambda: check_hermes_gateway(gateway_probe)),
        (CHECK_CDP, lambda: check_cdp(http_get=cdp_http_get)),
        (
            CHECK_EZLYNX_TAB,
            lambda: check_ezlynx_tab(http_get=cdp_http_get, tabs=ezlynx_tabs),
        ),
        (CHECK_SECRETS, lambda: check_login_secrets(inspector=secret_inspector)),
        (CHECK_LINKS, lambda: check_conversation_job_links(db_path)),
        (
            CHECK_CHAT_INTAKE,
            lambda: check_chat_intake(
                db_path,
                now=now,
                journal=journal,
                journal_reader=journal_reader,
            ),
        ),
        (
            CHECK_CHAT_RUNTIME,
            lambda: (
                chat_runtime_probe()
                if callable(chat_runtime_probe)
                else chat_runtime_probe
                if chat_runtime_probe is not None
                else check_chat_runtime()
            ),
        ),
    ]
    completed: list[dict[str, Any]] = []
    for _name, runner in runners:
        result = runner()
        completed.append(result)
        if result["ok"]:
            continue
        message = format_failure(result["name"], result["evidence"])
        posted = False
        post_error = None
        notify: dict[str, Any] = {}
        try:
            notify = _post_failure(message, poster=poster, dm_finder=dm_finder)
            posted = bool(notify.get("space_posted"))
        except Exception as exc:
            post_error = f"{type(exc).__name__}: {exc}"
        payload = {
            "ok": False,
            "failed_check": result["name"],
            "checks": completed,
            "chat_posted": posted,
            "fail_notify_targets": list(notify.get("targets") or []),
            "fail_notify_dm_errors": list(notify.get("dm_errors") or []),
            "message": message,
        }
        if post_error:
            payload["chat_post_error"] = post_error
        _attach_tab_flush(
            payload,
            db_path=db_path,
            ezlynx_tabs=ezlynx_tabs,
            cdp_http_get=cdp_http_get,
        )
        return payload
    report = {
        "ok": True,
        "failed_check": None,
        "checks": completed,
        "chat_posted": False,
        "fail_notify_targets": [],
        "fail_notify_dm_errors": [],
        "message": None,
    }
    _attach_tab_flush(
        report,
        db_path=db_path,
        ezlynx_tabs=ezlynx_tabs,
        cdp_http_get=cdp_http_get,
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Production pre-flight. Yes/no only. Posts to the Robie Chat "
            "space and Carlo/Jake Chat APP DMs on the first no. "
            "Does not @robie. Infra only: not a job-type gate."
        )
    )
    parser.add_argument("--db", default="")
    args = parser.parse_args()
    report = run_production_preflight(db_path=args.db or None)
    print(json.dumps(
        {
            "ok": report["ok"],
            "failed_check": report.get("failed_check"),
            "chat_posted": report.get("chat_posted"),
            "checks": [
                {"name": item["name"], "ok": item["ok"], "evidence": item["evidence"]}
                for item in report.get("checks") or []
            ],
        },
        sort_keys=True,
    ))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
