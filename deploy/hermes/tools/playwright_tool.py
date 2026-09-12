"""Playwright-only browser tool for Robie's persistent Chrome session.

Production Hermes still discovers this filename at
``/opt/streetsmart-hermes/.hermes/hermes-agent/tools/playwright_tool.py``.
The official install writes a zip-load shim there so the running tool is
this zip file (``deploy/hermes/tools/playwright_tool.py``). Write-guard and
Job Engine helpers prefer ``ROBIE_CANONICAL_JOB_ENGINE_ROOT`` / PYTHONPATH
over a stale .hermes sibling. Pointer-only is not live.
"""

import importlib.util
import inspect
import os
import signal
import subprocess
import sys
from pathlib import Path

from tools.registry import registry


_DEFAULT_TIMEOUT_S = 45
_MAX_TIMEOUT_S = 180
_CDP_URL = os.environ.get("ROBIE_PLAYWRIGHT_CDP_URL", "http://127.0.0.1:9222")
_USER_CODE_SEPARATOR = "\n##ROBIE_PLAYWRIGHT_USER_CODE##\n"
_CDP_VERSION_ATTEMPTS = 12
_CDP_VERSION_DELAY_S = 0.5
_ARTIFACT_FAIL_CLOSED = (
    "PLAYWRIGHT_FAIL_CLOSED: empty or missing browser artifact; "
    "do not retry the same download/screenshot/PDF parse; "
    "use the document already on the EZLynx file / HITL Carlo"
)


def empty_or_missing_artifact_error(detail: object) -> str | None:
    """Return a non-retryable stop if this is an empty PDF or missing artifact.

    EmptyFileError / pypdf empty-file and FileNotFoundError on
    ``/tmp/playwright-artifacts-*`` are not unique-write or CDP failures.
    Relabeling them as generic ``PLAYWRIGHT_BLOCKED`` makes the model retry
    the same download/screenshot/PDF path and burn Vertex quota.
    """
    text = str(detail or "")
    if not text.strip():
        return None
    if "PLAYWRIGHT_FAIL_CLOSED" in text:
        return _ARTIFACT_FAIL_CLOSED
    lowered = text.casefold()
    if "emptyfileerror" in lowered or "cannot read an empty file" in lowered:
        return _ARTIFACT_FAIL_CLOSED
    if "pypdf" in lowered and "empty file" in lowered:
        return _ARTIFACT_FAIL_CLOSED
    if "filenotfounderror" in lowered and "playwright-artifacts" in lowered:
        return _ARTIFACT_FAIL_CLOSED
    return None


_LOCATOR_WRITE_TOKENS = (
    "locator.fill",
    "locator.click",
    "locator.dblclick",
    "locator.check",
    "locator.uncheck",
    "locator.select_option",
    "locator.set_checked",
    "locator.type",
    "locator.press",
    "locator.clear",
    ".fill(",
    ".click(",
    ".dblclick(",
    ".check(",
    ".uncheck(",
    ".select_option(",
    ".set_checked(",
    ".type(",
    ".press(",
    ".clear(",
    "attempting fill",
    "attempting click",
    "attempting check",
    "attempting select_option",
    "element is not visible",
    "element is hidden",
    "aria-hidden",
    "combobox",
)


def runner_timeout_error(detail: object) -> str | None:
    """Return PLAYWRIGHT_TIMEOUT text if this is a wait/load state expiration.

    A wait expiring (e.g. networkidle, wait_for_load_state, goto timeout,
    locator.wait_for, locator.text_content) is not a security guard refusing.
    Only write/input control interactions (fill, click, select_option, combobox)
    are treated as guard / control blockers.
    """
    text = str(detail or "")
    if not text.strip():
        return None
    if "PLAYWRIGHT_BLOCKED" in text:
        return None
    if "PLAYWRIGHT_TIMEOUT" in text:
        clean = text.strip()
        return clean if clean.startswith("PLAYWRIGHT_TIMEOUT:") else f"PLAYWRIGHT_TIMEOUT: {clean}"
    lowered = text.casefold()
    if (
        "networkidle" in lowered
        or "waiting for load state" in lowered
        or "page.goto: timeout" in lowered
        or "page.wait_for" in lowered
        or "locator.wait_for" in lowered
        or (
            "timeouterror" in lowered
            and not any(token in lowered for token in _LOCATOR_WRITE_TOKENS)
        )
    ):
        return f"PLAYWRIGHT_TIMEOUT: {text}"
    return None


def runner_failure_error(detail: str) -> str:
    """Map a failed exec to fail-closed artifact text, timeout, or PLAYWRIGHT_BLOCKED."""
    return (
        empty_or_missing_artifact_error(detail)
        or runner_timeout_error(detail)
        or f"PLAYWRIGHT_BLOCKED: {detail}"
    )


def wait_for_cdp_json_version(
    cdp_url,
    *,
    http_get=None,
    attempts=_CDP_VERSION_ATTEMPTS,
    delay_s=_CDP_VERSION_DELAY_S,
    sleeper=None,
):
    """Block until Chrome ``/json/version`` is healthy. Bounded retries only."""
    import json
    import time
    from urllib.error import URLError
    from urllib.request import Request, urlopen

    base = str(cdp_url or "http://127.0.0.1:9222").rstrip("/")
    url = f"{base}/json/version"
    tries = max(1, int(attempts))
    pause = max(0.0, float(delay_s))
    sleep = sleeper or time.sleep

    def _default_get(target):
        request = Request(target, method="GET")
        with urlopen(request, timeout=2.0) as response:
            status = int(getattr(response, "status", 200) or 200)
            return status, response.read()

    getter = http_get or _default_get
    last = f"{url} not checked"
    for attempt in range(1, tries + 1):
        try:
            status, body = getter(url)
            if int(status) == 200:
                raw = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body)
                payload = json.loads(raw)
                if isinstance(payload, dict) and (
                    payload.get("Browser") or payload.get("webSocketDebuggerUrl")
                ):
                    return {"ok": True, "url": url, "attempts": attempt}
                last = f"{url} missing Browser/webSocketDebuggerUrl"
            else:
                last = f"{url} HTTP {status}"
        except (URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError) as exc:
            last = f"{url} {type(exc).__name__}: {exc}"
        if attempt < tries:
            sleep(pause)
    raise RuntimeError(
        f"PLAYWRIGHT_BLOCKED: CDP {url} not healthy after {tries} attempt(s): {last}"
    )


def apply_playwright_stealth(browser, *, stealth_apply=None, log=None):
    """Apply playwright-stealth to attached contexts/pages. Soft-fail if missing.

    Mirrors renewal-automation-system session_manager: stealth after attach
    on the persistent Chrome CDP path only. Missing package logs and continues.
    """
    def _log(message):
        if log is not None:
            log(message)
        else:
            print(message, file=sys.stderr)

    apply_one = stealth_apply
    api_name = "injected"
    if apply_one is None:
        try:
            from playwright_stealth import stealth_sync

            apply_one = stealth_sync
            api_name = "stealth_sync"
        except ImportError:
            try:
                from playwright_stealth import Stealth

                stealth = Stealth()
                apply_sync = getattr(stealth, "apply_stealth_sync", None)
                if not callable(apply_sync):
                    _log(
                        "playwright-stealth is installed but has no sync apply API; "
                        "EZLynx Chat attach continues without stealth"
                    )
                    return {"ok": False, "applied": 0, "error": "no sync apply API"}
                apply_one = apply_sync
                api_name = "Stealth.apply_stealth_sync"
            except ImportError as exc:
                _log(
                    "playwright-stealth not installed; EZLynx Chat attach "
                    f"continues without it: {exc}"
                )
                return {"ok": False, "applied": 0, "error": "missing playwright-stealth"}
            except Exception as exc:
                _log(f"playwright-stealth unavailable; continuing without it: {exc}")
                return {"ok": False, "applied": 0, "error": str(exc)}
        except Exception as exc:
            _log(f"playwright-stealth stealth_sync failed: {type(exc).__name__}: {exc}")
            return {"ok": False, "applied": 0, "error": str(exc)}

    applied = 0
    contexts = list(getattr(browser, "contexts", None) or [])
    for context in contexts:
        try:
            apply_one(context)
            applied += 1
        except Exception as exc:
            _log(f"playwright-stealth skipped a context: {type(exc).__name__}: {exc}")
        for page in list(getattr(context, "pages", None) or []):
            try:
                apply_one(page)
                applied += 1
            except Exception as exc:
                _log(f"playwright-stealth skipped a page: {type(exc).__name__}: {exc}")
        listener = getattr(context, "on", None)
        if callable(listener):
            try:
                listener("page", apply_one)
            except Exception:
                pass
    return {"ok": True, "applied": applied, "api": api_name}


def relabel_user_exec_exception(exc: BaseException) -> None:
    """Re-raise empty/missing artifact or leaked control-action timeouts.

    Empty/missing artifacts stay PLAYWRIGHT_FAIL_CLOSED. A TimeoutError from
    fill/click/select_option/type (or a hidden / combobox control) is the
    same PLAYWRIGHT_BLOCKED class as unique-write: ask Gemini then HITL
    Carlo; do not retry-loop. Unique-write PLAYWRIGHT_BLOCKED is left intact.
    A wait expiring (such as networkidle, wait_for_load_state, goto timeout)
    is PLAYWRIGHT_TIMEOUT, not a guard refusing.
    """
    name = type(exc).__name__
    text = f"{name}: {exc}"
    path = str(getattr(exc, "filename", "") or "")
    mapped = empty_or_missing_artifact_error(f"{path} {text}")
    if mapped:
        raise RuntimeError(mapped) from exc
    if "PLAYWRIGHT_BLOCKED" in text or "PLAYWRIGHT_FAIL_CLOSED" in text or "PLAYWRIGHT_TIMEOUT" in text:
        raise exc
    timeout = (
        isinstance(exc, TimeoutError)
        or name == "TimeoutError"
        or (
            "playwright" in (getattr(type(exc), "__module__", "") or "")
            and "timeout" in name.casefold()
        )
    )
    if timeout:
        blob = text.casefold()
        control = any(token in blob for token in _LOCATOR_WRITE_TOKENS)
        if control:
            raise RuntimeError(
                f"PLAYWRIGHT_BLOCKED: {text}; "
                "ask Gemini then HITL Carlo; do not retry-loop"
            ) from exc
        raise RuntimeError(f"PLAYWRIGHT_TIMEOUT: {text}") from exc
    raise exc


def _job_engine_root() -> Path | None:
    here = Path(__file__).resolve()
    try:
        from robie_job_engine.deploy_truth import resolve_job_engine_root

        found = resolve_job_engine_root(here=here)
        if found is not None:
            return found
    except ImportError:
        pass
    candidates = [
        here.parents[2],
        here.parents[3] if len(here.parents) >= 4 else None,
        Path("/opt/streetsmart-hermes/releases/current"),
        Path("/opt/streetsmart-hermes/robie-job-engine"),
        Path("/opt/streetsmart-hermes-test/robie-job-engine"),
    ]
    for path in candidates:
        if path is None:
            continue
        if (path / "robie_job_engine" / "gemini_field_helper.py").is_file():
            return path
    return None


def _write_guard_path() -> Path:
    here = Path(__file__).resolve()
    try:
        from robie_job_engine.deploy_truth import resolve_write_guard_path

        return resolve_write_guard_path(here=here)
    except ImportError:
        pass
    except RuntimeError:
        pass
    candidates = [
        Path(os.environ["ROBIE_CANONICAL_JOB_ENGINE_ROOT"])
        / "robie_job_engine"
        / "playwright_write_guard.py"
        if os.environ.get("ROBIE_CANONICAL_JOB_ENGINE_ROOT")
        else None,
        here.parents[2] / "robie_job_engine" / "playwright_write_guard.py"
        if len(here.parents) >= 3
        else None,
        here.with_name("playwright_write_guard.py"),
    ]
    for path in candidates:
        if path is not None and path.is_file():
            return path
    raise RuntimeError(
        "PLAYWRIGHT_BLOCKED: unique-write guard source is missing; "
        "refuse to run unconstrained Playwright writes"
    )


def _available():
    if importlib.util.find_spec("playwright") is None:
        return False
    from robie_job_engine.hermes_tool_visibility import expose_guarded_browser
    expose_guarded_browser()
    return True


def _playwright_exec_wrapper() -> str:
    """Return the subprocess helper that installs unique-write and fail-closed artifacts."""
    helpers = (
        f"_ARTIFACT_FAIL_CLOSED = {_ARTIFACT_FAIL_CLOSED!r}\n"
        f"_CDP_VERSION_ATTEMPTS = {_CDP_VERSION_ATTEMPTS!r}\n"
        f"_CDP_VERSION_DELAY_S = {_CDP_VERSION_DELAY_S!r}\n\n"
        + inspect.getsource(empty_or_missing_artifact_error)
        + "\n"
        + inspect.getsource(relabel_user_exec_exception)
        + "\n"
        + inspect.getsource(wait_for_cdp_json_version)
        + "\n"
        + inspect.getsource(apply_playwright_stealth)
        + "\n"
    )
    return helpers + r'''
import os, sys
from playwright.sync_api import Locator, Page, sync_playwright, expect

raw = sys.stdin.read()
separator = "\n##ROBIE_PLAYWRIGHT_USER_CODE##\n"
if separator not in raw:
    raise RuntimeError(
        "PLAYWRIGHT_BLOCKED: unique-write guard was not installed; "
        "refuse to run unconstrained Playwright writes"
    )
guard_source, source = raw.split(separator, 1)
cdp_url = os.environ.get("ROBIE_PLAYWRIGHT_CDP_URL", "http://127.0.0.1:9222")
wait_for_cdp_json_version(cdp_url)
pw = sync_playwright().start()
try:
    browser = pw.chromium.connect_over_cdp(cdp_url, timeout=15000)
    apply_playwright_stealth(browser)
    contexts = browser.contexts
    if not contexts:
        raise RuntimeError("PLAYWRIGHT_BLOCKED: Chrome has no browser context")
    context = contexts[0]
    pages = [page for item in contexts for page in item.pages]
    engine_root = os.environ.get("ROBIE_JOB_ENGINE_ROOT", "")
    if engine_root and engine_root not in sys.path:
        sys.path.insert(0, engine_root)
    page = None
    try:
        from robie_job_engine.recording_tab import (
            publish_live_playwright_hint,
            read_page_hint,
            resolve_hint_file,
            select_playwright_page,
        )
        hinted = read_page_hint(resolve_hint_file()) or {}
        page = select_playwright_page(pages, hint_url=hinted.get("url") or None)
        publish_live_playwright_hint(pages, page=page)
    except Exception:
        page = None
    if page is None and not pages:
        page = context.new_page()
    if page is None:
        raise RuntimeError(
            "PLAYWRIGHT_BLOCKED: could not select the current job tab; "
            "refusing pages[0] / first-ezlynx-wins"
        )
    scope = {
        "playwright": pw,
        "browser": browser,
        "context": context,
        "pages": pages,
        "page": page,
        "expect": expect,
        "Locator": Locator,
        "Page": Page,
    }
    exec(compile(guard_source, "<playwright_write_guard>", "exec"), scope, scope)
    installer = scope.get("install_playwright_write_guards")
    if not callable(installer):
        raise RuntimeError(
            "PLAYWRIGHT_BLOCKED: unique-write guard installer is missing"
        )
    installer(scope)
    if not scope.get("_robie_unique_write_guard"):
        raise RuntimeError(
            "PLAYWRIGHT_BLOCKED: unique-write guard did not install"
        )
    try:
        from robie_job_engine.gemini_field_helper import ask_gemini_unique_field
        scope["ask_gemini_unique_field"] = ask_gemini_unique_field
    except Exception:
        scope["ask_gemini_unique_field"] = None
    _trace_mgr = None
    _job_id = os.environ.get("ROBIE_JOB_ID") or os.environ.get("JOB_ID")
    if _job_id:
        try:
            from robie_job_engine.playwright_tracing import PlaywrightTraceManager
            _trace_mgr = PlaywrightTraceManager()
            _trace_mgr.start_tracing(context)
        except Exception:
            _trace_mgr = None
    try:
        exec(compile(source, "<playwright_exec>", "exec"), scope, scope)
    except Exception as exc:
        relabel_user_exec_exception(exc)
    finally:
        if _trace_mgr and _job_id:
            try:
                _trace_mgr.stop_tracing(context, _job_id)
            except Exception:
                pass
        try:
            from robie_job_engine.recording_tab import (
                publish_live_playwright_hint,
                select_playwright_page,
            )
            live_pages = [item for ctx in browser.contexts for item in ctx.pages]
            scope["pages"] = live_pages
            live = select_playwright_page(live_pages) or scope.get("page")
            if live is not None:
                scope["page"] = live
            publish_live_playwright_hint(live_pages, page=scope.get("page"))
        except Exception:
            pass
finally:
    pw.stop()
'''


def _persist_playwright_exec_start(code: str, **kwargs):
    """Commit a started jobs.db row before the runner. Worker death still leaves it."""
    try:
        from robie_job_engine.playwright_observability import (
            persist_playwright_exec_start,
            resolve_playwright_job_binding,
        )

        job_id, db_path = resolve_playwright_job_binding(
            job_id=kwargs.get("job_id"),
            db_path=kwargs.get("db_path"),
        )
        if not job_id:
            return None, None, None
        row_id = persist_playwright_exec_start(db_path, job_id, code)
        return row_id, job_id, db_path
    except Exception:
        return None, None, None


def _persist_playwright_exec_finish(db_path, row_id, result) -> None:
    if row_id is None or not db_path:
        return
    try:
        from robie_job_engine.playwright_observability import persist_playwright_exec_finish

        persist_playwright_exec_finish(db_path, row_id, result)
    except Exception:
        pass


def playwright_exec(code: str, timeout_s: int = _DEFAULT_TIMEOUT_S, **kwargs):
    from tools.registry import tool_error, tool_result

    row_id, job_id, db_path = _persist_playwright_exec_start(code, **kwargs)

    def _finish(result):
        _persist_playwright_exec_finish(db_path, row_id, result)
        return result

    job = None
    bound_job_id = job_id or os.environ.get("ROBIE_JOB_ID") or os.environ.get("JOB_ID")
    bound_db = db_path or os.environ.get("ROBIE_JOB_DB")
    if bound_job_id and bound_db:
        try:
            from robie_job_engine.store import JobStore

            job = JobStore(bound_db).get_job(bound_job_id)
        except Exception:
            job = None
    try:
        from robie_job_engine.tab_cleanup import flush_tabs_at_job_start

        flush_tabs_at_job_start()
    except Exception:
        pass
    try:
        from robie_job_engine.tab_cleanup import refuse_wrong_host_at_job_start

        verdict = refuse_wrong_host_at_job_start(
            db_path=bound_db, job=job, code=code
        )
        if verdict.get("refused") and verdict.get("reason"):
            return _finish(tool_error(verdict["reason"]))
    except Exception:
        pass
    if not code or not code.strip():
        return _finish(tool_error("No Playwright code provided."))
    try:
        from robie_job_engine.action_gate import refuse_playwright_start

        refused = refuse_playwright_start(code)
        if refused:
            return _finish(tool_error(refused))
    except ImportError:
        pass
    try:
        timeout = max(10, min(int(timeout_s), _MAX_TIMEOUT_S))
    except (TypeError, ValueError):
        timeout = _DEFAULT_TIMEOUT_S

    wrapper = _playwright_exec_wrapper()
    env = os.environ.copy()
    env["ROBIE_PLAYWRIGHT_CDP_URL"] = _CDP_URL
    if job_id:
        env["ROBIE_JOB_ID"] = job_id
        env["ROBIE_CURRENT_JOB_ID"] = job_id
    payload = dict((job or {}).get("payload") or {})
    from robie_job_engine.ezlynx_write_scope import requested_message_applicant
    env["ROBIE_EZLYNX_WRITE_APPLICANT_ID"] = str(
        payload.get("applicant_id") or payload.get("account_id") or requested_message_applicant(payload) or ""
    ).strip()
    if bound_job_id:
        env["ROBIE_CURRENT_JOB_ID"] = bound_job_id
        env["ROBIE_JOB_ID"] = bound_job_id
        env["JOB_ID"] = bound_job_id
    if bound_db:
        env["ROBIE_JOB_DB"] = str(bound_db)
    engine_root = _job_engine_root()
    if engine_root is not None:
        env["ROBIE_JOB_ENGINE_ROOT"] = str(engine_root)
        current = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            str(engine_root) if not current else str(engine_root) + os.pathsep + current
        )
    try:
        payload = _write_guard_path().read_text() + _USER_CODE_SEPARATOR + code
    except Exception as exc:
        return _finish(tool_error(f"PLAYWRIGHT_BLOCKED: {exc}"))
    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", "-c", wrapper],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            text=True,
            env=env,
        )
        stdout, stderr = proc.communicate(
            input=payload,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.communicate(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
        return _finish(
            tool_error(
                f"PLAYWRIGHT_TIMEOUT: execution exceeded {timeout} seconds; "
                "the runner was cancelled and the persistent browser was preserved; "
                "the browser state was not verified"
            )
        )
    except OSError as exc:
        return _finish(tool_error(f"PLAYWRIGHT_BLOCKED: runner failed to start: {exc}"))

    if proc.returncode != 0:
        detail = (stderr or stdout or "runner exited without details")[-12000:]
        return _finish(tool_error(runner_failure_error(detail)))
    return _finish(
        tool_result(
            {
                "success": True,
                "exit_code": 0,
                "output": stdout,
                "engine": "playwright",
                "destination_verified": False,
                "authorizes_complete": False,
            }
        )
    )


PLAYWRIGHT_EXEC_SCHEMA = {
    "name": "playwright_exec",
    "description": (
        "Control Robie's existing signed-in Chrome session using Python Playwright only. "
        "The code runs with sync Playwright bindings already available: browser, context, "
        "pages, page, expect, and playwright. Select the current job tab with "
        "select_playwright_page / the recorder hint — never pages[0] or the first "
        "EZLynx tab. Reuse a matching page from pages before opening or navigating "
        "another tab. Writes fail closed unless the locator uniquely "
        "identifies exactly one field; .first/.nth/.last guesses are PLAYWRIGHT_BLOCKED. "
        "A TimeoutError on fill/click/select_option/type, or a hidden / aria-hidden / "
        "not-visible / combobox-hidden control, is the same PLAYWRIGHT_BLOCKED: ask "
        "Gemini then HITL Carlo; do not retry-loop or invent the value. "
        "An empty PDF (EmptyFileError) or missing /tmp/playwright-artifacts file is "
        "PLAYWRIGHT_FAIL_CLOSED once: do not retry the same download/screenshot/PDF "
        "parse; use the document already on the EZLynx file / HITL Carlo. "
        "After PLAYWRIGHT_BLOCKED or an unnamed modal, stop, describe the dialog title "
        "and visible labels only, call gemini_unique_field for one unique locator, and "
        "HITL Carlo if Gemini is unsure. Never guess a field. "
        "A zero exit code is not destination evidence and does not authorize Job Engine "
        "COMPLETE. Print structured data needed for the final answer. If Playwright cannot "
        "attach or verify state, stop; do not use another browser engine."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Python code using the prebound synchronous Playwright objects.",
            },
            "timeout_s": {
                "type": "integer",
                "description": "Execution timeout in seconds (10-180).",
                "default": _DEFAULT_TIMEOUT_S,
            },
        },
        "required": ["code"],
    },
}


registry.register(
    name="playwright_exec",
    toolset="playwright",
    schema=PLAYWRIGHT_EXEC_SCHEMA,
    handler=lambda args, **kwargs: playwright_exec(
        code=args.get("code", ""),
        timeout_s=args.get("timeout_s", _DEFAULT_TIMEOUT_S),
        **kwargs,
    ),
    check_fn=_available,
    emoji="🎭",
)
