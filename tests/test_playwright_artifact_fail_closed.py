"""Empty PDF / missing artifact must fail closed once — not retryable PLAYWRIGHT_BLOCKED.

No live EZLynx. No real customer, policy, coverage, or payment data.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from robie_job_engine.playwright_write_guard import (
    install_playwright_write_guards,
    locator_is_positional_guess,
    require_unique_write_target,
)


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "deploy" / "hermes" / "tools" / "playwright_tool.py"

EMPTY_PDF_TRACE = """
Traceback (most recent call last):
  File "<playwright_exec>", line 12, in <module>
  File "/opt/streetsmart-hermes/.hermes/hermes-agent/venv/lib/python3.12/site-packages/pypdf/_reader.py", line 133, in __init__
    self.read(stream)
  File "/opt/streetsmart-hermes/.hermes/hermes-agent/venv/lib/python3.12/site-packages/pypdf/_reader.py", line 449, in read
    raise EmptyFileError("Cannot read an empty file")
pypdf.errors.EmptyFileError: Cannot read an empty file
""".strip()

MISSING_ARTIFACT_TRACE = """
Traceback (most recent call last):
  File "<playwright_exec>", line 18, in <module>
  File "/usr/lib/python3.12/shutil.py", line 435, in copy
    copyfile(src, dst, follow_symlinks=follow_symlinks)
  File "/usr/lib/python3.12/shutil.py", line 260, in copyfile
    with open(src, 'rb') as fsrc:
         ^^^^^^^^^^^^^^^
FileNotFoundError: [Errno 2] No such file or directory: '/tmp/playwright-artifacts-abc123/shot.png'
""".strip()

UNIQUE_WRITE_TRACE = """
Traceback (most recent call last):
  File "<playwright_exec>", line 4, in <module>
RuntimeError: PLAYWRIGHT_BLOCKED: write target was chosen by position, not unique identity; .first/.nth/.last guesses are refused
""".strip()

SUCCESS_OUTPUT = "named_insured=ROBIE Test LLC\n"


def _install_hermes_registry_stub():
    tools_pkg = ModuleType("tools")
    tools_pkg.__path__ = []
    registry_mod = ModuleType("tools.registry")

    class DummyRegistry:
        def register(self, **_kwargs):
            return None

    def tool_error(message):
        return {"ok": False, "error": message}

    def tool_result(payload):
        return payload

    registry_mod.registry = DummyRegistry()
    registry_mod.tool_error = tool_error
    registry_mod.tool_result = tool_result
    previous = {name: sys.modules.get(name) for name in ("tools", "tools.registry")}
    sys.modules["tools"] = tools_pkg
    sys.modules["tools.registry"] = registry_mod
    return previous


def _restore_modules(previous: dict[str, ModuleType | None]) -> None:
    for name, module in previous.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


def _load_playwright_tool():
    previous = _install_hermes_registry_stub()
    spec = importlib.util.spec_from_file_location("robie_playwright_tool_under_test", TOOL)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    try:
        spec.loader.exec_module(module)
    except Exception:
        _restore_modules(previous)
        raise
    return module, previous


class FakeLocator:
    def __init__(self, matches: int, selector: str):
        self.matches = matches
        self.selector = selector
        self.fills: list[str] = []

    def count(self):
        return self.matches

    @property
    def first(self):
        return FakeLocator(1, f"{self.selector} >> nth=0")

    def nth(self, index: int):
        return FakeLocator(1, f"{self.selector} >> nth={index}")

    @property
    def last(self):
        return FakeLocator(1, f"{self.selector} >> nth=-1")

    def fill(self, value: str):
        require_unique_write_target(self)
        self.fills.append(value)


class FakePlaywrightLocator:
    fill = FakeLocator.fill


class _FakeProc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.pid = 4242

    def communicate(self, input=None, timeout=None):
        return self._stdout, self._stderr


class PlaywrightArtifactFailClosedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool, cls._registry_modules = _load_playwright_tool()

    @classmethod
    def tearDownClass(cls):
        _restore_modules(cls._registry_modules)

    def test_empty_pdf_is_fail_closed_once_not_retryable_blocked(self):
        mapped = self.tool.empty_or_missing_artifact_error(EMPTY_PDF_TRACE)
        self.assertIsNotNone(mapped)
        self.assertIn("PLAYWRIGHT_FAIL_CLOSED", mapped)
        self.assertNotIn("PLAYWRIGHT_BLOCKED", mapped)
        self.assertIn("do not retry the same download/screenshot/PDF parse", mapped)
        self.assertIn("EZLynx file", mapped)
        self.assertIn("HITL Carlo", mapped)
        self.assertEqual(self.tool.runner_failure_error(EMPTY_PDF_TRACE), mapped)

        with patch.object(
            self.tool.subprocess,
            "Popen",
            return_value=_FakeProc(1, stderr=EMPTY_PDF_TRACE),
        ) as popen:
            result = self.tool.playwright_exec(
                "from pypdf import PdfReader; PdfReader('/tmp/empty.pdf')"
            )
        self.assertEqual(popen.call_count, 1)
        self.assertFalse(result.get("ok", True))
        self.assertEqual(result["error"], mapped)
        self.assertNotIn("PLAYWRIGHT_BLOCKED", result["error"])

    def test_missing_playwright_artifact_is_fail_closed_once(self):
        mapped = self.tool.empty_or_missing_artifact_error(MISSING_ARTIFACT_TRACE)
        self.assertIsNotNone(mapped)
        self.assertIn("PLAYWRIGHT_FAIL_CLOSED", mapped)
        self.assertNotIn("PLAYWRIGHT_BLOCKED", mapped)
        self.assertEqual(self.tool.runner_failure_error(MISSING_ARTIFACT_TRACE), mapped)

        missing = FileNotFoundError(
            2,
            "No such file or directory",
            "/tmp/playwright-artifacts-xyz/doc.pdf",
        )
        self.assertEqual(
            self.tool.empty_or_missing_artifact_error(
                f"{type(missing).__name__}: {missing}"
            ),
            mapped,
        )

        with patch.object(
            self.tool.subprocess,
            "Popen",
            return_value=_FakeProc(1, stderr=MISSING_ARTIFACT_TRACE),
        ) as popen:
            result = self.tool.playwright_exec("page.screenshot(path='shot.png')")
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(result["error"], mapped)

    def test_unrelated_missing_file_and_cdp_timeout_stay_playwright_blocked(self):
        other = (
            "FileNotFoundError: [Errno 2] No such file or directory: "
            "'/tmp/other-downloads/quote.pdf'"
        )
        self.assertIsNone(self.tool.empty_or_missing_artifact_error(other))
        self.assertTrue(
            self.tool.runner_failure_error(other).startswith("PLAYWRIGHT_BLOCKED:")
        )
        timeout = "PLAYWRIGHT_BLOCKED: execution exceeded 45 seconds"
        self.assertIsNone(self.tool.empty_or_missing_artifact_error(timeout))
        self.assertEqual(self.tool.runner_failure_error(timeout), f"PLAYWRIGHT_BLOCKED: {timeout}")

    def test_unique_write_still_blocks_first_nth_last(self):
        target = FakeLocator(2, "textbox:effective-date")
        self.assertTrue(locator_is_positional_guess(target.first))
        self.assertTrue(locator_is_positional_guess(target.nth(0)))
        self.assertTrue(locator_is_positional_guess(target.last))
        for locator in (target.first, target.nth(1), target.last):
            with self.assertRaisesRegex(RuntimeError, "PLAYWRIGHT_BLOCKED"):
                locator.fill("2026-09-01")
            self.assertEqual(locator.fills, [])
        self.assertEqual(target.fills, [])

        scope = {"Locator": FakePlaywrightLocator}
        install_playwright_write_guards(scope)
        self.assertTrue(scope["_robie_unique_write_guard"])
        with self.assertRaisesRegex(RuntimeError, r"\.first/\.nth/\.last|position"):
            FakePlaywrightLocator.fill(target.first, "guess")

        with patch.object(
            self.tool.subprocess,
            "Popen",
            return_value=_FakeProc(1, stderr=UNIQUE_WRITE_TRACE),
        ):
            result = self.tool.playwright_exec("page.get_by_label('Date').first.fill('x')")
        self.assertIn("PLAYWRIGHT_BLOCKED", result["error"])
        self.assertNotIn("PLAYWRIGHT_FAIL_CLOSED", result["error"])
        self.assertIn(".first/.nth/.last", result["error"])

    def test_successful_exec_payload_is_unchanged(self):
        with patch.object(
            self.tool.subprocess,
            "Popen",
            return_value=_FakeProc(0, stdout=SUCCESS_OUTPUT),
        ) as popen:
            result = self.tool.playwright_exec("print('named_insured=ROBIE Test LLC')")
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(
            result,
            {
                "success": True,
                "exit_code": 0,
                "output": SUCCESS_OUTPUT,
                "engine": "playwright",
                "destination_verified": False,
                "authorizes_complete": False,
            },
        )

    def test_exec_helper_installs_the_same_fail_closed_relabel(self):
        wrapper = self.tool._playwright_exec_wrapper()
        compile(wrapper, "<playwright_exec_wrapper>", "exec")
        self.assertIn("relabel_user_exec_exception", wrapper)
        self.assertIn("empty_or_missing_artifact_error", wrapper)
        self.assertIn("PLAYWRIGHT_FAIL_CLOSED", wrapper)
        self.assertIn("install_playwright_write_guards", wrapper)
        self.assertIn(".first/.nth/.last", TOOL.read_text())

        class EmptyFileError(Exception):
            pass

        with self.assertRaisesRegex(RuntimeError, "PLAYWRIGHT_FAIL_CLOSED"):
            self.tool.relabel_user_exec_exception(
                EmptyFileError("Cannot read an empty file")
            )
        with self.assertRaisesRegex(RuntimeError, "PLAYWRIGHT_FAIL_CLOSED"):
            self.tool.relabel_user_exec_exception(
                FileNotFoundError(
                    2,
                    "No such file or directory",
                    "/tmp/playwright-artifacts-id/shot.png",
                )
            )
        with self.assertRaises(RuntimeError) as raised:
            self.tool.relabel_user_exec_exception(
                RuntimeError(
                    "PLAYWRIGHT_BLOCKED: write target was chosen by position, "
                    "not unique identity; .first/.nth/.last guesses are refused"
                )
            )
        self.assertIn("PLAYWRIGHT_BLOCKED", str(raised.exception))
        self.assertIn(".first/.nth/.last", str(raised.exception))
        self.assertNotIn("PLAYWRIGHT_FAIL_CLOSED", str(raised.exception))

        class PlaywrightTimeoutError(Exception):
            """playwright.sync_api.TimeoutError is not builtin TimeoutError."""

        PlaywrightTimeoutError.__name__ = "TimeoutError"
        PlaywrightTimeoutError.__module__ = "playwright.sync_api"
        with self.assertRaisesRegex(RuntimeError, "PLAYWRIGHT_BLOCKED") as timeout_raised:
            self.tool.relabel_user_exec_exception(
                PlaywrightTimeoutError(
                    "Timeout 30000ms exceeded.\n"
                    "waiting for locator(\"input[name='quotes.0.carrier_id']\")\n"
                    "attempting fill action; element is hidden / combobox"
                )
            )
        self.assertIn("ask Gemini then HITL Carlo", str(timeout_raised.exception))
        self.assertIn("do not retry-loop", str(timeout_raised.exception))
        self.assertNotIn("PLAYWRIGHT_FAIL_CLOSED", str(timeout_raised.exception))


if __name__ == "__main__":
    unittest.main()
