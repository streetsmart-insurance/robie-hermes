"""Playwright-only browser tool for Robie's persistent Chrome session.

Production Hermes loads this file from
``/opt/streetsmart-hermes/.hermes/hermes-agent/tools/playwright_tool.py``.
The repo overlay is ``deploy/hermes/tools/playwright_tool.py``. A zip-only
deploy does not install this tool; copy the overlay onto the .hermes path or
the next install will keep relabeling empty-PDF / missing-artifact errors as
retryable ``PLAYWRIGHT_BLOCKED``.
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


def runner_failure_error(detail: str) -> str:
    """Map a failed exec to fail-closed artifact text or PLAYWRIGHT_BLOCKED."""
    return empty_or_missing_artifact_error(detail) or f"PLAYWRIGHT_BLOCKED: {detail}"


def relabel_user_exec_exception(exc: BaseException) -> None:
    """Re-raise empty/missing artifact errors as a non-retryable fail-closed stop."""
    name = type(exc).__name__
    text = f"{name}: {exc}"
    path = str(getattr(exc, "filename", "") or "")
    mapped = empty_or_missing_artifact_error(f"{path} {text}")
    if mapped:
        raise RuntimeError(mapped) from exc
    raise exc


def _job_engine_root() -> Path | None:
    here = Path(__file__).resolve()
    candidates = [
        here.parents[2],
        here.parents[3] if len(here.parents) >= 4 else None,
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
    candidates = [
        here.with_name("playwright_write_guard.py"),
        here.parents[2] / "robie_job_engine" / "playwright_write_guard.py",
    ]
    for path in candidates:
        if path.is_file():
            return path
    raise RuntimeError(
        "PLAYWRIGHT_BLOCKED: unique-write guard source is missing; "
        "refuse to run unconstrained Playwright writes"
    )


def _available():
    return importlib.util.find_spec("playwright") is not None


def _playwright_exec_wrapper() -> str:
    """Return the subprocess helper that installs unique-write and fail-closed artifacts."""
    helpers = (
        f"_ARTIFACT_FAIL_CLOSED = {_ARTIFACT_FAIL_CLOSED!r}\n\n"
        + inspect.getsource(empty_or_missing_artifact_error)
        + "\n"
        + inspect.getsource(relabel_user_exec_exception)
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
pw = sync_playwright().start()
try:
    browser = pw.chromium.connect_over_cdp(cdp_url, timeout=15000)
    contexts = browser.contexts
    if not contexts:
        raise RuntimeError("PLAYWRIGHT_BLOCKED: Chrome has no browser context")
    context = contexts[0]
    pages = [page for item in contexts for page in item.pages]
    page = pages[0] if pages else context.new_page()
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
    engine_root = os.environ.get("ROBIE_JOB_ENGINE_ROOT", "")
    if engine_root and engine_root not in sys.path:
        sys.path.insert(0, engine_root)
    try:
        from robie_job_engine.gemini_field_helper import ask_gemini_unique_field
        scope["ask_gemini_unique_field"] = ask_gemini_unique_field
    except Exception:
        scope["ask_gemini_unique_field"] = None
    try:
        exec(compile(source, "<playwright_exec>", "exec"), scope, scope)
    except Exception as exc:
        relabel_user_exec_exception(exc)
    finally:
        try:
            from robie_job_engine.recording_tab import write_page_hint
            hint = os.environ.get("ROBIE_RECORDING_HINT_FILE", "").strip()
            page = scope.get("page")
            if hint and page is not None and getattr(page, "url", None):
                write_page_hint(hint, url=page.url)
        except Exception:
            pass
finally:
    pw.stop()
'''


def playwright_exec(code: str, timeout_s: int = _DEFAULT_TIMEOUT_S, **_kwargs):
    from tools.registry import tool_error, tool_result

    if not code or not code.strip():
        return tool_error("No Playwright code provided.")
    try:
        timeout = max(10, min(int(timeout_s), _MAX_TIMEOUT_S))
    except (TypeError, ValueError):
        timeout = _DEFAULT_TIMEOUT_S

    wrapper = _playwright_exec_wrapper()
    env = os.environ.copy()
    env["ROBIE_PLAYWRIGHT_CDP_URL"] = _CDP_URL
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
        return tool_error(f"PLAYWRIGHT_BLOCKED: {exc}")
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
        return tool_error(
            f"PLAYWRIGHT_BLOCKED: execution exceeded {timeout} seconds; "
            "the runner was cancelled and the persistent browser was preserved; "
            "the browser state was not verified"
        )
    except OSError as exc:
        return tool_error(f"PLAYWRIGHT_BLOCKED: runner failed to start: {exc}")

    if proc.returncode != 0:
        detail = (stderr or stdout or "runner exited without details")[-12000:]
        return tool_error(runner_failure_error(detail))
    return tool_result(
        {
            "success": True,
            "exit_code": 0,
            "output": stdout,
            "engine": "playwright",
            "destination_verified": False,
            "authorizes_complete": False,
        }
    )


PLAYWRIGHT_EXEC_SCHEMA = {
    "name": "playwright_exec",
    "description": (
        "Control Robie's existing signed-in Chrome session using Python Playwright only. "
        "The code runs with sync Playwright bindings already available: browser, context, "
        "pages, page, expect, and playwright. Reuse a matching page from pages before "
        "opening or navigating another tab. Writes fail closed unless the locator uniquely "
        "identifies exactly one field; .first/.nth/.last guesses are PLAYWRIGHT_BLOCKED. "
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
