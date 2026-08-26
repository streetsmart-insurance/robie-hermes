"""Playwright-only browser tool for Robie's persistent Chrome session."""

import importlib.util
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


def playwright_exec(code: str, timeout_s: int = _DEFAULT_TIMEOUT_S, **_kwargs):
    from tools.registry import tool_error, tool_result

    if not code or not code.strip():
        return tool_error("No Playwright code provided.")
    try:
        timeout = max(10, min(int(timeout_s), _MAX_TIMEOUT_S))
    except (TypeError, ValueError):
        timeout = _DEFAULT_TIMEOUT_S

    wrapper = r'''
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
    exec(compile(source, "<playwright_exec>", "exec"), scope, scope)
finally:
    pw.stop()
'''
    env = os.environ.copy()
    env["ROBIE_PLAYWRIGHT_CDP_URL"] = _CDP_URL
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
        return tool_error(f"PLAYWRIGHT_BLOCKED: {detail}")
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
