"""Regression: ezlynx_policy_setup must attach to the job tab, not pages[0].

Live failure (job c1ffb79a, 2026-09-12): policy_setup_tool.py used
contexts[0]/pages[0] (first-ezlynx-wins). The live Chrome had Submission
Center in front, so the tool drove the wrong tab and the job finished
UNVERIFIED. playwright_exec already refuses that pattern; this tool must use
the same recorder-hint + scored selection and raise PLAYWRIGHT_BLOCKED instead
of silently driving the wrong tab.

No live EZLynx. No real customer, policy, coverage, or payment data.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "deploy" / "hermes" / "tools" / "policy_setup_tool.py"

SUBMISSION_CENTER = "https://app.ezlynx.com/web/submission-center/overview/submissions"
EDIT_POLICY = "https://app.ezlynx.com/applicantportal/Policy/Actions/Edit/220250093/83651751"


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


def _restore_modules(previous):
    for name, module in previous.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


def _load_policy_setup_tool():
    previous = _install_hermes_registry_stub()
    spec = importlib.util.spec_from_file_location("policy_setup_tool", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, previous


class FakePage:
    def __init__(self, url, guid):
        self.url = url
        self.title = ""
        self._guid = guid


class FakeContext:
    def __init__(self, pages):
        self.pages = pages


class FakeBrowser:
    def __init__(self, contexts):
        self.contexts = contexts


class JobTabSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool, cls.previous = _load_policy_setup_tool()

    @classmethod
    def tearDownClass(cls):
        _restore_modules(cls.previous)

    def _browser(self, *page_urls):
        pages = [FakePage(url, f"guid-{i}") for i, url in enumerate(page_urls)]
        return FakeBrowser([FakeContext(pages)])

    def test_submission_center_in_front_does_not_win(self):
        """Reproduces job c1ffb79a: Submission Center enumerated first."""
        browser = self._browser(SUBMISSION_CENTER, EDIT_POLICY)
        chosen = self.tool._select_job_page(browser)
        self.assertIsNot(chosen, browser.contexts[0].pages[0])
        self.assertEqual(chosen.url, EDIT_POLICY)

    def test_hinted_job_tab_wins_over_enumeration_order(self):
        browser = self._browser(SUBMISSION_CENTER, EDIT_POLICY)
        with patch(
            "robie_job_engine.recording_tab.read_page_hint",
            return_value={"url": EDIT_POLICY},
        ):
            chosen = self.tool._select_job_page(browser)
        self.assertEqual(chosen.url, EDIT_POLICY)

    def test_no_contexts_raises_playwright_blocked(self):
        browser = FakeBrowser([])
        with self.assertRaisesRegex(RuntimeError, "PLAYWRIGHT_BLOCKED"):
            self.tool._select_job_page(browser)

    def test_no_selectable_tab_raises_playwright_blocked(self):
        browser = self._browser("")
        with self.assertRaisesRegex(
            RuntimeError, "refusing pages\\[0\\] / first-ezlynx-wins"
        ):
            self.tool._select_job_page(browser)

    def test_tool_module_never_indexes_first_page(self):
        """The old first-ezlynx-wins indexing must not come back."""
        source = TOOL.read_text()
        self.assertNotIn("pages[0] if", source)
        self.assertNotIn("contexts[0] if", source)
        self.assertNotIn("= ctx.pages[0]", source)


class EmailRoutingTests(unittest.TestCase):
    def test_homeowners_policy_creation_routes_to_tool_first(self):
        """The email worker never called ezlynx_policy_setup in job c1ffb79a.

        The task prompt must route homeowners policy creation to the tool
        first, ahead of the generic 'execute the required skill' fallback.
        """
        source = (ROOT / "scripts" / "robie_email_agent.py").read_text()
        self.assertIn("'ezlynx_policy_setup' tool FIRST", source)
        self.assertIn("homeowners policy on EZLynx", source)
        # The routing must come before the generic fallback instruction.
        self.assertLess(
            source.index("'ezlynx_policy_setup' tool FIRST"),
            source.index("For any other request"),
        )


if __name__ == "__main__":
    unittest.main()
