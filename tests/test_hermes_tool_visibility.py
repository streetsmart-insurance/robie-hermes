from types import SimpleNamespace
import os
import sys
import unittest
from unittest.mock import patch
from robie_job_engine.chat_policy import SECURITY_GUARD_STOP_RULE
from robie_job_engine.hermes_tool_visibility import (
    email_chat_job_schema,
    expose_guarded_browser,
    filter_email_chat_schemas,
    install_email_chat_schema_filter,
    is_email_or_chat_worker,
)


class ToolVisibilityTests(unittest.TestCase):
    def test_browser_direct_visibility_is_narrow_and_idempotent(self):
        runtime = SimpleNamespace(_HERMES_CORE_TOOLS=['terminal', 'read_file'])
        original = runtime._HERMES_CORE_TOOLS
        expose_guarded_browser(runtime, env={}, argv=['hermes', 'chat'])
        expose_guarded_browser(runtime, env={}, argv=['hermes', 'chat'])
        self.assertIs(runtime._HERMES_CORE_TOOLS, original)
        self.assertEqual(original, ['terminal', 'read_file', 'playwright_exec'])

    def test_incompatible_visibility_interface_fails_explicitly(self):
        for core in (None, {}, (), {'terminal'}, ['terminal', 1]):
            with self.subTest(core=core), self.assertRaisesRegex(RuntimeError, 'Unsupported Hermes'):
                expose_guarded_browser(SimpleNamespace(_HERMES_CORE_TOOLS=core), env={}, argv=[])

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
            with patch.dict(sys.modules, {'toolsets': runtime}), patch.object(tool.importlib.util, 'find_spec', return_value=object()), patch.dict('os.environ', {}, clear=False):
                self.assertTrue(tool._available())
                self.assertEqual(runtime._HERMES_CORE_TOOLS, ['terminal', 'playwright_exec'])
        finally:
            _restore_modules(previous)

    def test_email_and_chat_job_schema_has_playwright_exec_not_execute_code(self):
        offered = ['terminal', 'read_file', 'execute_code', 'playwright_exec']
        for action in ('hermes.email_task', 'hermes.google_chat_task'):
            with self.subTest(action=action):
                schema = email_chat_job_schema(offered)
                self.assertIn('playwright_exec', schema)
                self.assertNotIn('execute_code', schema)
                runtime = SimpleNamespace(_HERMES_CORE_TOOLS=list(offered))
                expose_guarded_browser(runtime, action_type=action, env={}, argv=['hermes', 'chat'])
                self.assertIn('playwright_exec', runtime._HERMES_CORE_TOOLS)
                self.assertNotIn('execute_code', runtime._HERMES_CORE_TOOLS)
                self.assertNotIn('code_execution', runtime._HERMES_CORE_TOOLS)

    def test_interactive_desktop_keeps_execute_code(self):
        runtime = SimpleNamespace(_HERMES_CORE_TOOLS=['terminal', 'execute_code'])
        expose_guarded_browser(runtime, env={}, argv=['hermes', 'chat'])
        self.assertEqual(runtime._HERMES_CORE_TOOLS, ['terminal', 'execute_code', 'playwright_exec'])

    def test_gateway_argv_is_chat_worker_and_hides_execute_code(self):
        self.assertTrue(is_email_or_chat_worker(env={}, argv=['hermes_cli.main', 'gateway', 'run']))
        runtime = SimpleNamespace(_HERMES_CORE_TOOLS=['execute_code', 'read_file'])
        expose_guarded_browser(runtime, env={}, argv=['-m', 'hermes_cli.main', 'gateway', 'run'])
        self.assertIn('playwright_exec', runtime._HERMES_CORE_TOOLS)
        self.assertNotIn('execute_code', runtime._HERMES_CORE_TOOLS)

    def test_single_query_email_job_hides_execute_code(self):
        env = {'ROBIE_JOB_ID': 'bf4d701e', 'HERMES_SINGLE_QUERY_SESSION': '1'}
        self.assertTrue(is_email_or_chat_worker(env=env, argv=['hermes', 'chat', '-q']))
        runtime = SimpleNamespace(_HERMES_CORE_TOOLS=['execute_code', 'playwright_exec'])
        expose_guarded_browser(runtime, env=env, argv=['hermes', 'chat', '-q'])
        self.assertEqual(runtime._HERMES_CORE_TOOLS, ['playwright_exec'])

    def test_schema_dicts_drop_execute_code_keep_playwright_exec(self):
        filtered = filter_email_chat_schemas([
            {'function': {'name': 'playwright_exec'}},
            {'function': {'name': 'execute_code'}},
            {'name': 'code_execution'},
            {'function': {'name': 'read_file'}},
        ])
        names = [_schema_name(item) for item in filtered]
        self.assertEqual(names, ['playwright_exec', 'read_file'])

    def test_does_not_lift_single_query_deny_guard(self):
        from pathlib import Path
        visibility = Path('robie_job_engine/hermes_tool_visibility.py').read_text()
        runner = Path('robie_job_engine/email_agent_runner.py').read_text()
        self.assertNotIn('single_query_mode', visibility)
        self.assertNotIn('single_query_mode', runner)

    def test_security_guard_rule_requires_stop_not_negotiate(self):
        self.assertIn('NEVER ask a human to lift a security control', SECURITY_GUARD_STOP_RULE)
        self.assertIn('execute_code BLOCKED', SECURITY_GUARD_STOP_RULE)
        self.assertIn('PLAYWRIGHT_BLOCKED', SECURITY_GUARD_STOP_RULE)
        self.assertIn('and stop', SECURITY_GUARD_STOP_RULE)
        self.assertIn('Do not invent passwords', SECURITY_GUARD_STOP_RULE)
        self.assertIn('Do not ask to approve execute_code', SECURITY_GUARD_STOP_RULE)
        self.assertNotIn('approve the bypass', SECURITY_GUARD_STOP_RULE.casefold())

    def test_schema_assembly_wrap_hides_execute_code_for_email_chat_only(self):
        defs = [
            {'function': {'name': 'playwright_exec'}},
            {'function': {'name': 'execute_code'}},
        ]
        fake = SimpleNamespace(
            get_tool_definitions=lambda enabled_toolsets=None, quiet_mode=False, disabled_toolsets=None: list(defs)
        )
        with patch.dict(sys.modules, {'model_tools': fake}):
            install_email_chat_schema_filter()
            with patch('robie_job_engine.hermes_tool_visibility.is_email_or_chat_worker', return_value=True):
                names = [item['function']['name'] for item in fake.get_tool_definitions()]
            self.assertEqual(names, ['playwright_exec'])
            with patch('robie_job_engine.hermes_tool_visibility.is_email_or_chat_worker', return_value=False):
                interactive = [item['function']['name'] for item in fake.get_tool_definitions()]
            self.assertEqual(interactive, ['playwright_exec', 'execute_code'])

    def test_chat_bind_marks_google_chat_job_action(self):
        from robie_job_engine.playwright_observability import bind_current_playwright_job
        previous = os.environ.pop('ROBIE_JOB_ACTION', None)
        previous_job = os.environ.get('ROBIE_JOB_ID')
        try:
            bind_current_playwright_job(None, 'chat-job')
            self.assertEqual(os.environ['ROBIE_JOB_ACTION'], 'hermes.google_chat_task')
            self.assertEqual(os.environ['ROBIE_JOB_ID'], 'chat-job')
        finally:
            if previous is None:
                os.environ.pop('ROBIE_JOB_ACTION', None)
            else:
                os.environ['ROBIE_JOB_ACTION'] = previous
            if previous_job is None:
                os.environ.pop('ROBIE_JOB_ID', None)
            else:
                os.environ['ROBIE_JOB_ID'] = previous_job
            os.environ.pop('ROBIE_CURRENT_JOB_ID', None)


def _schema_name(item):
    function = item.get('function') if isinstance(item, dict) else None
    if isinstance(function, dict):
        return function.get('name')
    return item.get('name')
