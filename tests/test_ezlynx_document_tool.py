"""ezlynx_document_upload worker tool: routing, fail-closed wiring, allowlist.

No live EZLynx. The engine client is mocked; the write-allowlist refusal is
simulated by raising EzlynxWriteScopeError from the mocked client.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from robie_job_engine.ezlynx_write_scope import EzlynxWriteScopeError
from robie_job_engine.hermes_tool_visibility import (
    email_chat_job_schema,
    expose_guarded_browser,
    is_email_or_chat_worker,
)
from test_playwright_artifact_fail_closed import (
    _install_hermes_registry_stub,
    _restore_modules,
)

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "deploy" / "hermes" / "tools" / "ezlynx_document_tool.py"


def _load_document_tool():
    previous = _install_hermes_registry_stub()
    spec = importlib.util.spec_from_file_location("robie_document_tool_under_test", TOOL)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    try:
        spec.loader.exec_module(module)
    except Exception:
        _restore_modules(previous)
        raise
    return module, previous


class DocumentToolVisibilityTests(unittest.TestCase):
    def test_worker_schema_includes_document_upload(self):
        offered = ["terminal", "read_file", "execute_code", "playwright_exec"]
        for action in ("hermes.email_task", "hermes.google_chat_task"):
            with self.subTest(action=action):
                schema = email_chat_job_schema(offered)
                self.assertIn("ezlynx_document_upload", schema)
                self.assertIn("ezlynx_discussion_note", schema)
                self.assertIn("playwright_exec", schema)
                self.assertIn("ezlynx_policy_setup", schema)
                self.assertNotIn("execute_code", schema)
                self.assertNotIn("terminal", schema)

    def test_worker_core_gets_document_upload(self):
        from types import SimpleNamespace

        runtime = SimpleNamespace(_HERMES_CORE_TOOLS=["terminal", "execute_code"])
        expose_guarded_browser(
            runtime, action_type="hermes.email_task", env={}, argv=["hermes", "chat"]
        )
        self.assertIn("ezlynx_document_upload", runtime._HERMES_CORE_TOOLS)
        self.assertIn("ezlynx_discussion_note", runtime._HERMES_CORE_TOOLS)
        self.assertNotIn("execute_code", runtime._HERMES_CORE_TOOLS)
        self.assertNotIn("terminal", runtime._HERMES_CORE_TOOLS)

    def test_desktop_core_never_gets_document_upload(self):
        from types import SimpleNamespace

        # Interactive desktop: no job action, plain `hermes chat` argv.
        runtime = SimpleNamespace(_HERMES_CORE_TOOLS=["terminal", "execute_code"])
        self.assertFalse(is_email_or_chat_worker(env={}, argv=["hermes", "chat"]))
        expose_guarded_browser(runtime, env={}, argv=["hermes", "chat"])
        self.assertNotIn("ezlynx_document_upload", runtime._HERMES_CORE_TOOLS)
        self.assertNotIn("ezlynx_policy_setup", runtime._HERMES_CORE_TOOLS)
        self.assertEqual(
            runtime._HERMES_CORE_TOOLS, ["terminal", "execute_code", "playwright_exec"]
        )

    def test_visibility_is_idempotent(self):
        from types import SimpleNamespace

        runtime = SimpleNamespace(_HERMES_CORE_TOOLS=[])
        env = {"ROBIE_JOB_ACTION": "hermes.email_task"}
        expose_guarded_browser(runtime, env=env, argv=[])
        expose_guarded_browser(runtime, env=env, argv=[])
        self.assertEqual(
            runtime._HERMES_CORE_TOOLS.count("ezlynx_document_upload"), 1
        )


class DocumentToolHandlerTests(unittest.TestCase):
    def _write_tmp_file(self, tmp_dir, name, data: bytes) -> str:
        path = os.path.join(str(tmp_dir), name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def test_schema_requires_applicant_file_and_name(self):
        tool, previous = _load_document_tool()
        try:
            required = tool.DOCUMENT_UPLOAD_SCHEMA["parameters"]["required"]
            self.assertEqual(
                sorted(required), ["applicant_id", "document_name", "file_path"]
            )
            self.assertEqual(tool.DOCUMENT_UPLOAD_SCHEMA["name"], "ezlynx_document_upload")
        finally:
            _restore_modules(previous)

    def test_handler_fails_closed_on_missing_args(self):
        tool, previous = _load_document_tool()
        try:
            for args in (
                {},
                {"applicant_id": "220250093"},
                {"applicant_id": "220250093", "file_path": "/tmp/x.pdf"},
                {
                    "applicant_id": "220250093",
                    "file_path": "/tmp/does-not-exist-robie.pdf",
                    "document_name": "dec.pdf",
                },
            ):
                with self.subTest(args=args):
                    result = tool.ezlynx_document_upload_handler(dict(args))
                    self.assertFalse(result["ok"])
                    self.assertIn("error", result)
        finally:
            _restore_modules(previous)

    def test_handler_fails_closed_on_empty_file(self):
        import tempfile

        tool, previous = _load_document_tool()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                empty = self._write_tmp_file(tmp, "empty.pdf", b"")
                result = tool.ezlynx_document_upload_handler(
                    {
                        "applicant_id": "220250093",
                        "file_path": empty,
                        "document_name": "empty.pdf",
                    }
                )
                self.assertFalse(result["ok"])
                self.assertIn("empty", result["error"])
        finally:
            _restore_modules(previous)

    def test_handler_uploads_through_engine_client(self):
        import tempfile

        tool, previous = _load_document_tool()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                pdf = self._write_tmp_file(tmp, "dec.pdf", b"%PDF-1.4 fake")
                calls = {}

                class FakeClient:
                    def __init__(self, config):
                        calls["config"] = config

                    def upload_applicant_document(
                        self, applicant_id, document_name, file_bytes, **kwargs
                    ):
                        calls["upload"] = {
                            "applicant_id": applicant_id,
                            "document_name": document_name,
                            "file_bytes": file_bytes,
                            "kwargs": kwargs,
                        }
                        return "987654321"

                    def search_applicant_documents(self, applicant_id):
                        return {
                            "results": [
                                {"id": "987654321", "name": "dec.pdf"}
                            ]
                        }

                with (
                    patch(
                        "robie_job_engine.ezlynx_api.EzlynxApiClient", FakeClient
                    ),
                    patch(
                        "robie_job_engine.ezlynx_api.load_ezlynx_api_config",
                        return_value=object(),
                    ),
                ):
                    result = tool.ezlynx_document_upload_handler(
                        {
                            "applicant_id": "220250093",
                            "file_path": pdf,
                            "document_name": "dec.pdf",
                            "file_content_type": "application/pdf",
                        }
                    )
                self.assertTrue(result["ok"])
                self.assertEqual(result["document_id"], "987654321")
                self.assertTrue(result["read_back"])
                self.assertEqual(result["applicant_id"], "220250093")
                upload = calls["upload"]
                self.assertEqual(upload["applicant_id"], "220250093")
                self.assertEqual(upload["document_name"], "dec.pdf")
                self.assertEqual(upload["file_bytes"], b"%PDF-1.4 fake")
                self.assertEqual(
                    upload["kwargs"].get("file_content_type"), "application/pdf"
                )
                # Default policy_master_id is left to the engine (applicant-level).
                self.assertNotIn("policy_master_id", upload["kwargs"])
        finally:
            _restore_modules(previous)

    def test_handler_surfaces_allowlist_refusal_as_error(self):
        import tempfile

        tool, previous = _load_document_tool()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                pdf = self._write_tmp_file(tmp, "dec.pdf", b"%PDF-1.4 fake")

                class RefusingClient:
                    def __init__(self, config):
                        pass

                    def upload_applicant_document(self, *args, **kwargs):
                        raise EzlynxWriteScopeError(
                            "EZLYNX_WRITE_SCOPE_REFUSED: applicant 999 is not authorized"
                        )

                with (
                    patch(
                        "robie_job_engine.ezlynx_api.EzlynxApiClient", RefusingClient
                    ),
                    patch(
                        "robie_job_engine.ezlynx_api.load_ezlynx_api_config",
                        return_value=object(),
                    ),
                ):
                    result = tool.ezlynx_document_upload_handler(
                        {
                            "applicant_id": "999",
                            "file_path": pdf,
                            "document_name": "dec.pdf",
                        }
                    )
                self.assertFalse(result["ok"])
                self.assertIn("EzlynxWriteScopeError", result["error"])
        finally:
            _restore_modules(previous)

    def test_available_when_engine_imports(self):
        tool, previous = _load_document_tool()
        try:
            self.assertTrue(tool._available())
        finally:
            _restore_modules(previous)


if __name__ == "__main__":
    unittest.main()
