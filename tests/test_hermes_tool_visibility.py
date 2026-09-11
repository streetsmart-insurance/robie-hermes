from types import SimpleNamespace
import unittest
from unittest.mock import patch
from robie_job_engine.hermes_tool_visibility import expose_guarded_browser


class ToolVisibilityTests(unittest.TestCase):
    def test_browser_direct_visibility_is_narrow_and_idempotent(self):
        runtime = SimpleNamespace(_HERMES_CORE_TOOLS=['terminal', 'read_file'])
        original = runtime._HERMES_CORE_TOOLS
        expose_guarded_browser(runtime)
        expose_guarded_browser(runtime)
        self.assertIs(runtime._HERMES_CORE_TOOLS, original)
        self.assertEqual(original, ['terminal', 'read_file', 'playwright_exec'])

    def test_incompatible_visibility_interface_fails_explicitly(self):
        for core in (None, {}, (), {'terminal'}, ['terminal', 1]):
            with self.subTest(core=core), self.assertRaisesRegex(RuntimeError, 'Unsupported Hermes'):
                expose_guarded_browser(SimpleNamespace(_HERMES_CORE_TOOLS=core))

    def test_unavailable_browser_does_not_pin_schema(self):
        from test_playwright_artifact_fail_closed import _load_playwright_tool, _restore_modules
        tool, previous = _load_playwright_tool()
        try:
            with patch.object(tool.importlib.util, 'find_spec', return_value=None):
                self.assertFalse(tool._available())
        finally:
            _restore_modules(previous)

    def test_registered_availability_pins_only_browser(self):
        import sys
        from test_playwright_artifact_fail_closed import _load_playwright_tool, _restore_modules
        tool, previous = _load_playwright_tool()
        runtime = SimpleNamespace(_HERMES_CORE_TOOLS=['terminal'])
        try:
            with patch.dict(sys.modules, {'toolsets': runtime}), patch.object(tool.importlib.util, 'find_spec', return_value=object()):
                self.assertTrue(tool._available())
                self.assertEqual(runtime._HERMES_CORE_TOOLS, ['terminal', 'playwright_exec'])
        finally:
            _restore_modules(previous)
