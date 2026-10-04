"""Test health profile skips Prod-only probes and still alerts on a real fault."""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "robie_health_check.py")


def _load():
    spec = importlib.util.spec_from_file_location("robie_health_check_profile", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


h = _load()

PROD_ONLY = {
    "worker_alive",
    "env_vars",
    "hitl_dry_run",
    "gmail_sa_key",
    "ringcentral_auth",
    "sweep_freshness",
    "service_errors",
    "phone_gmail_keys",
    "applicant_ingest_freshness",
    "eod_drive_delivery",
    "task_verifier_health",
    "tuesday_4359_proof",
    "chat_intake",
    "preflight_alert_delivery",
    "ascend_driver_stall",
}


def _completed(text: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["systemctl"], returncode=returncode, stdout=text, stderr="")


class HealthProfileSelectionTests(unittest.TestCase):
    def test_unset_env_stays_on_the_production_list(self):
        env = {key: value for key, value in os.environ.items() if key != "ROBIE_ENV"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(h.resolve_health_profile(None), "PRODUCTION")
            selected = h.checks_for_profile(None)
        self.assertEqual([name for name, _fn in selected], [name for name, _fn in h.CHECKS])
        self.assertEqual([fn for _name, fn in selected], [fn for _name, fn in h.CHECKS])

    def test_explicit_production_ignores_test_env(self):
        with patch.dict(os.environ, {"ROBIE_ENV": "TEST"}):
            self.assertEqual(h.resolve_health_profile("PRODUCTION"), "PRODUCTION")
            names = [name for name, _fn in h.checks_for_profile("PRODUCTION")]
        self.assertEqual(names, [name for name, _fn in h.CHECKS])

    def test_test_profile_skips_prod_only_and_checks_test_units(self):
        names = [name for name, _fn in h.checks_for_profile("TEST")]
        for skipped in PROD_ONLY:
            self.assertNotIn(skipped, names)
        production_names = [name for name, _fn in h.CHECKS]
        self.assertIn("preflight_alert_delivery", production_names)
        for kept in ("code_version", "disk", "ezlynx_auth", "duplicate_guard", "stuck_leases"):
            self.assertIn(kept, names)
        for required in (
            "test_gateway",
            "test_ezlynx_browser",
            "test_keepalive",
            "test_paths",
            "test_service_errors",
            "test_stuck_job_leases",
        ):
            self.assertIn(required, names)

    def test_test_profile_prefers_the_test_release_for_code_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            release = os.path.join(tmp, "releases", "current")
            os.makedirs(os.path.join(release, "robie_job_engine"))
            previous = h._REQUESTED_PROFILE
            h._REQUESTED_PROFILE = "TEST"
            try:
                with patch.object(h, "TEST_RELEASE_ROOT", release):
                    self.assertEqual(h._code_version_import_root(), release)
            finally:
                h._REQUESTED_PROFILE = previous

    def test_production_profile_does_not_import_the_test_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            release = os.path.join(tmp, "releases", "current")
            os.makedirs(os.path.join(release, "robie_job_engine"))
            previous = h._REQUESTED_PROFILE
            h._REQUESTED_PROFILE = "PRODUCTION"
            try:
                with patch.object(h, "TEST_RELEASE_ROOT", release):
                    self.assertNotEqual(h._code_version_import_root(), release)
            finally:
                h._REQUESTED_PROFILE = previous

    def test_robie_env_test_selects_the_test_profile(self):
        with patch.dict(os.environ, {"ROBIE_ENV": "TEST"}):
            self.assertEqual(h.resolve_health_profile(None), "TEST")
            names = [name for name, _fn in h.checks_for_profile(None)]
        self.assertIn("test_gateway", names)
        self.assertNotIn("phone_gmail_keys", names)


class HealthProfileOutcomeTests(unittest.TestCase):
    def test_healthy_test_is_quiet(self):
        checks = [
            ("disk", lambda: (True, "ok", {})),
            ("test_gateway", lambda: (True, "robie-gateway.service active", {})),
        ]
        with tempfile.TemporaryDirectory() as tmp, patch.object(h, "checks_for_profile", return_value=checks):
            code = h.execute(profile="TEST", status_dir=tmp, no_chat=True, daily_digest=False)
            self.assertTrue(os.path.isfile(os.path.join(tmp, "status.json")))
        self.assertEqual(code, 0)

    def test_inactive_test_gateway_alerts(self):
        with patch.object(h.subprocess, "run", return_value=_completed("inactive\n", 3)):
            ok, detail, extra = h.check_test_gateway()
        self.assertFalse(ok)
        self.assertIn("robie-gateway.service", detail)
        self.assertEqual(extra["state"], "inactive")

    def test_failed_keepalive_service_alerts(self):
        def run(args, **_kwargs):
            if args[1] == "is-active":
                return _completed("active\n")
            return _completed("failed\n")

        with patch.object(h.subprocess, "run", side_effect=run):
            ok, detail, _extra = h.check_test_keepalive()
        self.assertFalse(ok)
        self.assertIn("failed", detail)

    def test_missing_test_jobs_db_alerts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "opt")
            release = os.path.join(root, "releases", "current")
            os.makedirs(release)
            with patch.object(h, "TEST_ROOT", root), patch.object(
                h, "TEST_RELEASE_ROOT", release
            ), patch.object(h, "TEST_JOBS_DB", os.path.join(root, "missing.db")):
                ok, detail, _extra = h.check_test_paths()
        self.assertFalse(ok)
        self.assertIn("missing", detail)

    def test_present_test_paths_are_quiet(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "opt")
            release = os.path.join(root, "releases", "current")
            os.makedirs(release)
            db = os.path.join(root, "jobs.db")
            with open(db, "w"):
                pass
            with patch.object(h, "TEST_ROOT", root), patch.object(
                h, "TEST_RELEASE_ROOT", release
            ), patch.object(h, "TEST_JOBS_DB", db):
                ok, _detail, extra = h.check_test_paths()
        self.assertTrue(ok)
        self.assertEqual(extra["missing"], [])

    def test_traceback_in_test_gateway_journal_alerts(self):
        def journal(unit, _since):
            if unit == "robie-gateway.service":
                return "Traceback (most recent call last)\nModuleNotFoundError"
            return ""

        with patch.object(h, "_journal_since", side_effect=journal):
            ok, detail, _extra = h.check_test_service_errors()
        self.assertFalse(ok)
        self.assertIn("Traceback", detail)

    def test_clean_test_journals_are_quiet(self):
        with patch.object(h, "_journal_since", return_value="keepalive ok\n"):
            ok, _detail, _extra = h.check_test_service_errors()
        self.assertTrue(ok)

    def test_fresh_running_job_is_quiet_and_old_one_alerts(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "jobs.db")
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE jobs (id TEXT, status TEXT, updated_at TEXT)"
            )
            fresh = datetime.now(timezone.utc).isoformat()
            stale = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
            conn.execute("INSERT INTO jobs VALUES ('fresh', 'RUNNING', ?)", (fresh,))
            conn.commit()
            with patch.object(h, "TEST_JOBS_DB", db):
                ok, _detail, _extra = h.check_test_stuck_job_leases()
            self.assertTrue(ok)
            conn.execute("INSERT INTO jobs VALUES ('stale', 'VERIFYING', ?)", (stale,))
            conn.commit()
            conn.close()
            with patch.object(h, "TEST_JOBS_DB", db):
                ok, detail, extra = h.check_test_stuck_job_leases()
        self.assertFalse(ok)
        self.assertEqual(extra["stuck"], ["stale:VERIFYING"])
        self.assertIn("over an hour", detail)

    def test_execute_alerts_when_a_selected_check_fails(self):
        checks = [("test_gateway", lambda: (False, "robie-gateway.service is inactive", {}))]
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            h, "checks_for_profile", return_value=checks
        ):
            code = h.execute(profile="TEST", status_dir=tmp, no_chat=True, daily_digest=False)
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
