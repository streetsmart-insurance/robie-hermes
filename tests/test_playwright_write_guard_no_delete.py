"""Cardinal rule: ROBIE never deletes.

Regression tests for the deletion-shaped click refusal in
``robie_job_engine.playwright_write_guard`` (Carlo 2026-09-10: a browser job
once deleted a policy). The block must be absolute — no Gemini fallback, no
HITL override — and benign controls must keep working.
"""

from __future__ import annotations

import unittest

from robie_job_engine.playwright_write_guard import (
    destructive_action_block_reason,
    install_playwright_write_guards,
)


class _FakeLocator:
    """Minimal Playwright-locator stand-in for guard unit tests."""

    def __init__(self, selector="", *, aria_label=None, text=None):
        self._selector = selector
        self._aria_label = aria_label
        self._text = text
        self.clicked = False

    def get_attribute(self, name):
        if name == "aria-label":
            return self._aria_label
        return None

    def inner_text(self):
        if self._text is None:
            raise RuntimeError("strict mode violation: resolves to 0 elements")
        return self._text

    def text_content(self):
        return self._text

    def count(self):
        return 1

    def click(self):  # patched by install_playwright_write_guards in one test
        self.clicked = True
        return "clicked"


class NoDeleteGuardTests(unittest.TestCase):
    def test_named_delete_button_is_refused(self):
        locator = _FakeLocator("button", aria_label="Delete", text="Delete")
        reason = destructive_action_block_reason(locator, method_name="click")
        self.assertIsNotNone(reason)
        self.assertIn("never deletes", reason)

    def test_delete_policy_context_is_refused(self):
        locator = _FakeLocator("button.danger", text="Delete policy")
        reason = destructive_action_block_reason(locator, method_name="click")
        self.assertIsNotNone(reason)

    def test_remove_client_context_is_refused(self):
        locator = _FakeLocator("a", text="Remove client")
        reason = destructive_action_block_reason(locator, method_name="click")
        self.assertIsNotNone(reason)

    def test_void_coverage_context_is_refused(self):
        locator = _FakeLocator("button", text="Void coverage")
        reason = destructive_action_block_reason(locator, method_name="click")
        self.assertIsNotNone(reason)

    def test_selector_named_delete_is_refused(self):
        locator = _FakeLocator("button")
        reason = destructive_action_block_reason(
            locator, method_name="click", selector='button:has-text("Delete")'
        )
        self.assertIsNotNone(reason)

    def test_benign_save_click_is_allowed(self):
        locator = _FakeLocator("button.save", text="Save changes")
        self.assertIsNone(
            destructive_action_block_reason(locator, method_name="click")
        )

    def test_benign_navigation_click_is_allowed(self):
        locator = _FakeLocator("a.policies", text="View policies")
        self.assertIsNone(
            destructive_action_block_reason(locator, method_name="click")
        )

    def test_delete_word_without_context_is_allowed(self):
        # "deleted items" folder navigation is not a deletion action.
        locator = _FakeLocator("a", text="Open deleted items folder")
        self.assertIsNone(
            destructive_action_block_reason(locator, method_name="click")
        )

    def test_non_click_methods_are_untouched(self):
        locator = _FakeLocator("button", aria_label="Delete", text="Delete")
        self.assertIsNone(
            destructive_action_block_reason(locator, method_name="fill")
        )

    def test_wrapped_click_raises_hard_with_no_override(self):
        class FakeLocatorClass:
            def click(self):
                return "clicked"

        scope: dict = {"Locator": FakeLocatorClass}
        install_playwright_write_guards(scope)
        locator = FakeLocatorClass()
        locator._selector = "button"  # noqa: SLF001 - test double shaping
        locator.get_attribute = lambda name: "Delete" if name == "aria-label" else None
        locator.inner_text = lambda: "Delete"
        locator.text_content = lambda: "Delete"
        with self.assertRaisesRegex(RuntimeError, "never deletes"):
            locator.click()


if __name__ == "__main__":
    unittest.main()
