"""Tests for scripts/read-login-secret-versions.py.

The script resolves pinned Secret Manager version resource names on the VM
via the engine's inspect_login_secrets (version states only, never payloads).
"""

import importlib.util
import io
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import ModuleType
from unittest import TestCase
from unittest.mock import patch


def load_script():
    module_path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "read-login-secret-versions.py"
    )
    spec = importlib.util.spec_from_file_location(
        "read_login_secret_versions", module_path
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def fake_engine(report=None, exc=None):
    """Fake robie_job_engine.login_secret_health module."""
    pkg = ModuleType("robie_job_engine")
    mod = ModuleType("robie_job_engine.login_secret_health")

    def inspect_login_secrets(*, project=None):
        if exc is not None:
            raise exc
        return report

    mod.inspect_login_secrets = inspect_login_secrets
    pkg.login_secret_health = mod
    return {
        "robie_job_engine": pkg,
        "robie_job_engine.login_secret_health": mod,
    }


OK_REPORT = {
    "result": "OK",
    "secrets": [
        {"secret_id": "ezlynx-username", "newest_enabled_version": "versions/4"},
        {"secret_id": "ezlynx-password", "newest_enabled_version": "versions/7"},
    ],
}


class ReadLoginSecretVersionsTests(TestCase):
    def run_script(self, report=None, exc=None, argv=None):
        module = load_script()
        modules = fake_engine(report=report, exc=exc)
        argv = argv or ["prog", "--project", "streetsmart-hermes-poc"]
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(sys.modules, modules), patch.object(
            sys, "argv", argv
        ), redirect_stdout(out), redirect_stderr(err):
            rc = module.main()
        return rc, out.getvalue(), err.getvalue()

    def test_ok_prints_pinned_resource_names(self):
        rc, out, _ = self.run_script(report=OK_REPORT)
        self.assertEqual(rc, 0)
        self.assertEqual(
            out,
            "ROBIE_EZLYNX_USERNAME_SECRET="
            "projects/streetsmart-hermes-poc/secrets/ezlynx-username/versions/4\n"
            "ROBIE_EZLYNX_PASSWORD_SECRET="
            "projects/streetsmart-hermes-poc/secrets/ezlynx-password/versions/7\n",
        )

    def test_unknown_fails_closed(self):
        rc, _, err = self.run_script(
            report={"result": "UNKNOWN", "reason": "no creds", "secrets": []}
        )
        self.assertEqual(rc, 2)
        self.assertIn("refusing", err)

    def test_alert_fails_closed(self):
        rc, _, err = self.run_script(
            report={"result": "ALERT", "reason": "no ENABLED", "secrets": []}
        )
        self.assertEqual(rc, 2)
        self.assertIn("refusing", err)

    def test_missing_secret_fails_closed(self):
        rc, _, err = self.run_script(
            report={
                "result": "OK",
                "secrets": [
                    {
                        "secret_id": "ezlynx-username",
                        "newest_enabled_version": "versions/4",
                    }
                ],
            }
        )
        self.assertEqual(rc, 2)
        self.assertIn("ezlynx-password", err)

    def test_non_numeric_version_fails_closed(self):
        rc, _, err = self.run_script(
            report={
                "result": "OK",
                "secrets": [
                    {
                        "secret_id": "ezlynx-username",
                        "newest_enabled_version": "versions/latest",
                    },
                    {
                        "secret_id": "ezlynx-password",
                        "newest_enabled_version": "versions/7",
                    },
                ],
            }
        )
        self.assertEqual(rc, 2)
        self.assertIn("refusing", err)

    def test_inspector_exception_fails_closed(self):
        rc, _, err = self.run_script(exc=RuntimeError("boom"))
        self.assertEqual(rc, 2)
        self.assertIn("refusing", err)
