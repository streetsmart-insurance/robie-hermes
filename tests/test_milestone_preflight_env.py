"""The pre-flight must read the gateway's real environment, not invent one.

milestone_preflight.py runs over an ad-hoc ssh session, which inherits none of
the unit's Environment= or EnvironmentFile=. Two wrong answers are available:
set our own values (green on a box that would fail in production) or set none
(red on a box that is fine). The script reads the unit's own configuration out
of systemd and uses exactly that, and restores os.environ afterwards so the
overlay cannot leak into the checks that follow.

Stdlib unittest: runs without pytest.
"""
from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "milestone_preflight.py"

_spec = importlib.util.spec_from_file_location("milestone_preflight", SCRIPT)
preflight = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(preflight)

SECRET_KEY = "ROBIE_EZLYNX_API_UAT_SECRET"


def _fake_properties(mapping):
    """Stand in for "systemctl show <unit> -p <prop> --value"."""
    def _show(unit: str, prop: str) -> str:
        return mapping.get((unit, prop), "")
    return _show


def _tmpdir(testcase) -> Path:
    handle = tempfile.TemporaryDirectory()
    testcase.addCleanup(handle.cleanup)
    return Path(handle.name)


class ReadsTheUnitsOwnEnvironment(unittest.TestCase):
    def setUp(self):
        self.env_file = _tmpdir(self) / "accountability.env"
        self.env_file.write_text(
            "# written by configure-ezlynx-api-env-test.yml\n"
            "\n"
            "ROBIE_EZLYNX_API_UAT_SECRET="
            "projects/751771086524/secrets/ezlynx-api-uat/versions/latest\n"
            'ROBIE_ACCOUNTABILITY_MANIFEST="/etc/streetsmart-hermes-test/manifest.json"\n'
        )
        self.props = {
            ("robie-gateway", "LoadState"): "loaded",
            ("robie-gateway", "EnvironmentFiles"): f"{self.env_file} (ignore_errors=no)",
            ("robie-gateway", "Environment"): "ROBIE_ENV=TEST",
        }

    def test_env_file_values_are_read(self):
        with mock.patch.object(preflight, "_unit_property", _fake_properties(self.props)):
            env, sources = preflight._unit_environment("robie-gateway")
        self.assertEqual(
            env[SECRET_KEY],
            "projects/751771086524/secrets/ezlynx-api-uat/versions/latest",
        )
        self.assertEqual(env["ROBIE_ENV"], "TEST")
        # Quotes stripped, comments and blank lines ignored.
        self.assertEqual(
            env["ROBIE_ACCOUNTABILITY_MANIFEST"],
            "/etc/streetsmart-hermes-test/manifest.json",
        )
        self.assertIn(str(self.env_file), sources)

    def test_missing_env_file_is_named_not_silently_skipped(self):
        props = dict(self.props)
        props[("robie-gateway", "EnvironmentFiles")] = "/etc/nope/absent.env (ignore_errors=no)"
        with mock.patch.object(preflight, "_unit_property", _fake_properties(props)):
            env, sources = preflight._unit_environment("robie-gateway")
        self.assertNotIn(SECRET_KEY, env)
        self.assertTrue(any("missing" in s for s in sources), sources)

    def test_gateway_env_check_uses_the_env_file_not_just_environment(self):
        props = dict(self.props)
        props[("robie-gateway", "Environment")] = ""
        self.env_file.write_text("ROBIE_ENV=TEST\n")
        with mock.patch.object(preflight, "_unit_property", _fake_properties(props)):
            unit, seen = preflight._loaded_gateway_unit(["robie-gateway", "hermes-gateway"])
            env, _ = preflight._unit_environment(unit)
            state, detail = preflight._check_gateway_env(unit, seen, env)
        self.assertEqual(state, preflight.OK, detail)

    def test_no_gateway_unit_says_so_rather_than_guessing(self):
        with mock.patch.object(preflight, "_unit_property", _fake_properties({})):
            unit, seen = preflight._loaded_gateway_unit(["robie-gateway", "hermes-gateway"])
            state, detail = preflight._check_gateway_env(unit, seen, {})
        self.assertIsNone(unit)
        self.assertEqual(state, preflight.BAD)
        self.assertIn("robie-gateway=absent", detail)
        self.assertIn("hermes-gateway=absent", detail)


class SecretCheckAppliesAndRestores(unittest.TestCase):
    @staticmethod
    def _loader(sink):
        class _Module:
            @staticmethod
            def load_ezlynx_api_config():
                sink.append(os.environ.get(SECRET_KEY))
        return _Module

    def test_overlay_is_visible_to_the_loader(self):
        seen = []
        module = self._loader(seen)
        with mock.patch.dict("sys.modules", {"robie_job_engine.ezlynx_api": module}):
            state, detail = preflight._check_secret_manager(
                {SECRET_KEY: "projects/1/secrets/x/versions/latest"}, ["/etc/x.env"]
            )
        self.assertEqual(state, preflight.OK, detail)
        self.assertEqual(seen, ["projects/1/secrets/x/versions/latest"])
        self.assertIn("/etc/x.env", detail)

    def test_environment_is_restored_afterwards(self):
        original = os.environ.get(SECRET_KEY)

        def _restore():
            if original is None:
                os.environ.pop(SECRET_KEY, None)
            else:
                os.environ[SECRET_KEY] = original

        self.addCleanup(_restore)
        os.environ.pop(SECRET_KEY, None)
        with mock.patch.dict("sys.modules", {"robie_job_engine.ezlynx_api": self._loader([])}):
            preflight._check_secret_manager({SECRET_KEY: "overlay"}, [])
        self.assertNotIn(SECRET_KEY, os.environ)

    def test_failure_names_the_source_it_used(self):
        class _Boom:
            @staticmethod
            def load_ezlynx_api_config():
                raise RuntimeError("ROBIE_EZLYNX_API_UAT_SECRET must be configured")

        with mock.patch.dict("sys.modules", {"robie_job_engine.ezlynx_api": _Boom}):
            state, detail = preflight._check_secret_manager(
                {}, ["/etc/streetsmart-hermes-test/accountability.env"]
            )
        self.assertEqual(state, preflight.BAD)
        self.assertIn("/etc/streetsmart-hermes-test/accountability.env", detail)
        self.assertIn("must be configured", detail)


class RunningServiceMustHaveTheCurrentConfig(unittest.TestCase):
    """Config on disk is not config in the process until the unit restarts."""

    def setUp(self):
        self.env_file = _tmpdir(self) / "accountability.env"
        self.env_file.write_text("ROBIE_ENV=TEST\n")

    def _props(self, started: str):
        return {
            ("robie-gateway", "LoadState"): "loaded",
            ("robie-gateway", "EnvironmentFiles"): f"{self.env_file} (ignore_errors=no)",
            ("robie-gateway", "ExecMainStartTimestamp"): started,
        }

    def test_service_older_than_its_config_is_a_problem(self):
        os.utime(self.env_file, (2_000_000_000, 2_000_000_000))
        with mock.patch.object(preflight, "_unit_property", _fake_properties(self._props("long ago"))), \
             mock.patch.object(preflight, "_to_epoch", lambda _s: 1_000_000_000.0):
            state, detail = preflight._check_config_is_live("robie-gateway")
        self.assertEqual(state, preflight.BAD)
        self.assertIn("old values", detail)

    def test_service_newer_than_its_config_is_fine(self):
        os.utime(self.env_file, (1_000_000_000, 1_000_000_000))
        with mock.patch.object(preflight, "_unit_property", _fake_properties(self._props("recently"))), \
             mock.patch.object(preflight, "_to_epoch", lambda _s: 2_000_000_000.0):
            state, detail = preflight._check_config_is_live("robie-gateway")
        self.assertEqual(state, preflight.OK, detail)

    def test_unparseable_start_time_is_informational_not_a_failure(self):
        with mock.patch.object(preflight, "_unit_property", _fake_properties(self._props(""))), \
             mock.patch.object(preflight, "_to_epoch", lambda _s: None):
            state, detail = preflight._check_config_is_live("robie-gateway")
        self.assertEqual(state, preflight.OK, detail)
        self.assertIn("informational", detail)

    def test_no_gateway_unit_is_informational(self):
        state, detail = preflight._check_config_is_live(None)
        self.assertEqual(state, preflight.OK, detail)


if __name__ == "__main__":
    unittest.main()
