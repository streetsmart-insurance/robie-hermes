"""Unit tests for skills/zapier (SKILL.md + bin/zap-trigger).

No live Catch Hook call is ever made: every test either dry-runs, fails
before the network, or patches urllib. The URL used here is a fake
placeholder built at runtime, not a real Catch Hook.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO / "skills" / "zapier"
SCRIPT = SKILL_DIR / "bin" / "zap-trigger"
FAKE_URL = "https://hooks.zapier.com/" + "unit-test-placeholder/"
URL_ENV_KEYS = ("ZAPIER_CATCH_HOOK_URL", "CUSTOM_ZAPIER_WEBHOOK", "HERMES_CUSTOM_ZAPIER_WEBHOOK")


def _payload(**overrides):
    base = {
        "applicant_id": "220250093",
        "task_title": "Review filed mail",
        "assignee": "SSNicole",
        "source": "unit-test",
        "due_date": "2026-10-01",
    }
    base.update(overrides)
    return base


def _load_script_module():
    loader = importlib.machinery.SourceFileLoader("zap_trigger_under_test", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class ZapTriggerCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {k: v for k, v in os.environ.items() if k not in URL_ENV_KEYS}
        # Isolate from any real ~/.hermes secret on the machine running tests.
        self.env["HERMES_HOME"] = self.tmp.name
        self.env["HOME"] = self.tmp.name

    def run_cli(self, payload, *extra, env=None):
        arg = payload if isinstance(payload, str) else json.dumps(payload)
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--payload", arg, *extra],
            capture_output=True, text=True, timeout=30, env=env or self.env,
        )
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1, f"expected one JSON line, got: {proc.stdout!r}")
        return proc.returncode, json.loads(lines[0]), proc.stdout + proc.stderr

    def test_skill_layout(self):
        self.assertTrue((SKILL_DIR / "SKILL.md").is_file())
        self.assertTrue(SCRIPT.is_file())
        self.assertTrue(os.access(SCRIPT, os.X_OK), "zap-trigger must be executable")
        self.assertTrue(SCRIPT.read_text().startswith("#!/usr/bin/env python3"))
        self.assertIn("name: zapier", (SKILL_DIR / "SKILL.md").read_text())

    def test_dry_run_ok_with_env_url(self):
        env = dict(self.env, ZAPIER_CATCH_HOOK_URL=FAKE_URL)
        code, out, raw = self.run_cli(_payload(), "--dry-run", "--applicant-verified", env=env)
        self.assertEqual(code, 0)
        self.assertEqual(out["ok"], True)
        self.assertEqual(out["dry_run"], True)
        self.assertEqual(out["applicant_verified"], True)
        self.assertNotIn("hooks.zapier.com", raw)

    def test_dry_run_reads_hermes_home_secret_file(self):
        secrets = Path(self.tmp.name) / "secrets"
        secrets.mkdir()
        (secrets / "custom.zapier-webhook").write_text(FAKE_URL + "\n")
        code, out, raw = self.run_cli(_payload(), "--dry-run")
        self.assertEqual(code, 0)
        self.assertTrue(out["ok"])
        self.assertNotIn("hooks.zapier.com", raw)

    def test_missing_url_is_config_error(self):
        code, out, _ = self.run_cli(_payload(), "--dry-run")
        self.assertEqual(code, 2)
        self.assertFalse(out["ok"])
        self.assertIn("URL missing", out["error"])

    def test_non_zapier_host_refused(self):
        env = dict(self.env, ZAPIER_CATCH_HOOK_URL="https://example.com/hook")
        code, out, raw = self.run_cli(_payload(), "--dry-run", env=env)
        self.assertEqual(code, 2)
        self.assertIn("host not allowed", out["error"])
        self.assertNotIn("example.com", raw)

    def test_missing_required_keys(self):
        env = dict(self.env, ZAPIER_CATCH_HOOK_URL=FAKE_URL)
        for key in ("applicant_id", "task_title", "assignee", "source", "due_date"):
            with self.subTest(key=key):
                payload = _payload()
                payload.pop(key)
                code, out, _ = self.run_cli(payload, "--dry-run", env=env)
                self.assertEqual(code, 2)
                self.assertIn(key, out["error"])

    def test_bad_due_date(self):
        env = dict(self.env, ZAPIER_CATCH_HOOK_URL=FAKE_URL)
        for bad in ("10/01/2026", "2026-13-01", "tomorrow"):
            with self.subTest(due=bad):
                code, out, _ = self.run_cli(_payload(due_date=bad), "--dry-run", env=env)
                self.assertEqual(code, 2)
                self.assertIn("due_date", out["error"])

    def test_display_name_assignee_refused(self):
        env = dict(self.env, ZAPIER_CATCH_HOOK_URL=FAKE_URL)
        code, out, _ = self.run_cli(_payload(assignee="Nicole Segovia"), "--dry-run", env=env)
        self.assertEqual(code, 2)
        self.assertIn("SSNicole", out["error"])

    def test_invalid_json_and_non_object(self):
        env = dict(self.env, ZAPIER_CATCH_HOOK_URL=FAKE_URL)
        code, out, _ = self.run_cli("{not json", "--dry-run", env=env)
        self.assertEqual(code, 2)
        code, out, _ = self.run_cli("[1, 2]", "--dry-run", env=env)
        self.assertEqual(code, 2)
        self.assertIn("JSON object", out["error"])

    def test_validation_runs_before_url_lookup(self):
        # A bad payload must fail as a payload error even with no URL configured.
        code, out, _ = self.run_cli(_payload(due_date=""), "--dry-run")
        self.assertEqual(code, 2)
        self.assertIn("due_date", out["error"])


class ZapTriggerPostPathTests(unittest.TestCase):
    """Exercise the non-dry-run branch with urllib patched. Nothing leaves the process."""

    def setUp(self):
        self.mod = _load_script_module()
        patcher = mock.patch.dict(os.environ, {"ZAPIER_CATCH_HOOK_URL": FAKE_URL})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _main(self, payload):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = self.mod.main(["--payload", json.dumps(payload)])
        return code, json.loads(buf.getvalue()), buf.getvalue()

    def test_http_error_is_exit_1_and_hides_url(self):
        err = urllib.error.HTTPError(FAKE_URL, 500, "boom", {}, io.BytesIO(b"nope"))
        with mock.patch.object(self.mod.urllib.request, "urlopen", side_effect=err):
            code, out, raw = self._main(_payload())
        self.assertEqual(code, 1)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "HTTP 500")
        self.assertNotIn("unit-test-placeholder", raw)

    def test_success_is_exit_0(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value = resp
        resp.read.return_value = b'{"status": "success"}'
        resp.status = 200
        with mock.patch.object(self.mod.urllib.request, "urlopen", return_value=resp) as urlopen:
            code, out, raw = self._main(_payload())
        self.assertEqual(code, 0)
        self.assertTrue(out["ok"])
        self.assertEqual(out["response"], {"status": "success"})
        sent = json.loads(urlopen.call_args[0][0].data)
        self.assertEqual(sent["assignee"], "SSNicole")
        self.assertNotIn("unit-test-placeholder", raw)


class FireTaskIntegrationTests(unittest.TestCase):
    """robie_job_engine.zapier_tasks.fire_task against the in-tree skill (dry-run only)."""

    def setUp(self):
        try:
            from robie_job_engine import zapier_tasks  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            self.skipTest(f"robie_job_engine not importable: {exc}")
        from robie_job_engine import zapier_tasks
        self.zt = zapier_tasks

    def test_missing_script_raises(self):
        with mock.patch.dict(os.environ, {"ROBIE_ZAP_TRIGGER": "/nonexistent/zap-trigger"}):
            with self.assertRaises(RuntimeError):
                self.zt.fire_task(_payload(), dry_run=True)

    def test_dry_run_through_in_tree_script(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"ROBIE_ZAP_TRIGGER": str(SCRIPT),
                                             "ZAPIER_CATCH_HOOK_URL": FAKE_URL, "HERMES_HOME": tmp}):
            result = self.zt.fire_task(_payload(), dry_run=True)
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["dry_run"], True)


if __name__ == "__main__":
    unittest.main()
