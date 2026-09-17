"""Tests for Stage 4 JE-KILL readiness + Test-aware Gmail token path."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest import mock

from robie_job_engine.je_kill_preflight import ezlynx_auth_tab_errors

ROOT = Path(__file__).resolve().parents[1]
READINESS = ROOT / "scripts" / "je-kill-test-readiness.py"


def _load_bootstrap():
    google = ModuleType("google")
    google_cloud = ModuleType("google.cloud")
    secretmanager = ModuleType("google.cloud.secretmanager")
    secretmanager.SecretManagerServiceClient = mock.Mock()
    google_cloud.secretmanager = secretmanager
    modules = {
        "google": google,
        "google.cloud": google_cloud,
        "google.cloud.secretmanager": secretmanager,
        "google.auth": ModuleType("google.auth"),
        "google.auth.transport": ModuleType("google.auth.transport"),
        "google.auth.transport.requests": ModuleType("google.auth.transport.requests"),
        "google.oauth2": ModuleType("google.oauth2"),
        "google.oauth2.credentials": ModuleType("google.oauth2.credentials"),
        "googleapiclient": ModuleType("googleapiclient"),
        "googleapiclient.discovery": ModuleType("googleapiclient.discovery"),
        "playwright": ModuleType("playwright"),
        "playwright.sync_api": ModuleType("playwright.sync_api"),
    }
    modules["google.auth.transport.requests"].Request = object
    modules["google.oauth2.credentials"].Credentials = object
    modules["googleapiclient.discovery"].build = mock.Mock()
    modules["playwright.sync_api"].sync_playwright = mock.Mock()
    path = ROOT / "ezlynx_login_bootstrap.py"
    spec = importlib.util.spec_from_file_location("bootstrap_token_path", path)
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, modules):
        assert spec.loader is not None
        spec.loader.exec_module(mod)
    return mod


class GmailTokenPathTests(unittest.TestCase):
    def test_defaults_to_production_path(self):
        mod = _load_bootstrap()
        with mock.patch.dict(os.environ, {}, clear=True):
            # Keep PATH-like essentials empty for these keys only.
            for key in (
                "ROBIE_GOOGLE_TOKEN_FILE",
                "HERMES_HOME",
                "ROBIE_OPT_ROOT",
            ):
                os.environ.pop(key, None)
            self.assertEqual(
                str(mod.gmail_token_path()),
                "/opt/streetsmart-hermes/.hermes/robie_google_token.json",
            )

    def test_uses_hermes_home_on_test(self):
        mod = _load_bootstrap()
        with mock.patch.dict(
            os.environ,
            {"HERMES_HOME": "/opt/streetsmart-hermes-test/.hermes"},
            clear=False,
        ):
            os.environ.pop("ROBIE_GOOGLE_TOKEN_FILE", None)
            self.assertEqual(
                str(mod.gmail_token_path()),
                "/opt/streetsmart-hermes-test/.hermes/robie_google_token.json",
            )

    def test_explicit_env_wins(self):
        mod = _load_bootstrap()
        with mock.patch.dict(
            os.environ,
            {
                "ROBIE_GOOGLE_TOKEN_FILE": "/tmp/robie_google_token.json",
                "HERMES_HOME": "/opt/streetsmart-hermes-test/.hermes",
            },
            clear=False,
        ):
            self.assertEqual(
                str(mod.gmail_token_path()), "/tmp/robie_google_token.json"
            )


class AboutBlankAuthMessageTests(unittest.TestCase):
    def test_about_blank_calls_out_missing_ezlynx_tab(self):
        errors = ezlynx_auth_tab_errors(
            [{"type": "page", "url": "about:blank", "title": ""}]
        )
        self.assertTrue(errors)
        self.assertIn("about:blank", errors[0])
        self.assertIn("CDP AUTHENTICATED required", errors[0])


class JeKillTestReadinessScriptTests(unittest.TestCase):
    def test_refuses_without_expected_sha(self):
        proc = subprocess.run(
            [sys.executable, str(READINESS), "--expected-sha", ""],
            cwd=str(ROOT),
            env={**os.environ, "PYTHONPATH": str(ROOT)},
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("JE-KILL TEST READINESS: BLOCKED", proc.stderr)
        self.assertIn("EXPECTED_SHA not set", proc.stderr)

    def test_reports_json_ready_false_on_missing_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc = subprocess.run(
                [
                    sys.executable,
                    str(READINESS),
                    "--test-root",
                    str(root),
                    "--fixture",
                    str(root / "missing.json"),
                    "--job-db",
                    str(root / "missing.db"),
                    "--expected-sha",
                    "dcfda9850ae1",
                    "--cdp-url",
                    "http://127.0.0.1:9",
                ],
                cwd=str(ROOT),
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 2)
            payload = json.loads(proc.stdout)
            self.assertFalse(payload["ready"])
            self.assertTrue(payload["blocking"])


if __name__ == "__main__":
    unittest.main()
