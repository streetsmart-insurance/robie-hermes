"""The health check imports the release it ships in, not a hard-coded Prod path.

Production cron still resolves to /opt/streetsmart-hermes/releases/current when
that is the tree beside the script. A Test tree or an explicit environment
override must not.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "robie_health_check.py")
_PROD_RELEASE = "/opt/streetsmart-hermes/releases/current"


def _load():
    spec = importlib.util.spec_from_file_location("robie_health_check_import_root", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


h = _load()


def _isolated_env(**extra):
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("ROBIE_CANONICAL_JOB_ENGINE_ROOT", "PYTHONPATH")
    }
    env.update(extra)
    return patch.dict(os.environ, env, clear=True)


def _touch_package(release: str) -> None:
    os.makedirs(os.path.join(release, "robie_job_engine"))


class ReleaseImportRootTests(unittest.TestCase):
    def test_script_inside_release_uses_that_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            release = os.path.join(tmp, "opt", "streetsmart-hermes-test", "releases", "current")
            _touch_package(release)
            script = os.path.join(release, "scripts", "robie_health_check.py")
            os.makedirs(os.path.dirname(script))
            with _isolated_env():
                root = h.release_import_root(script)
        self.assertEqual(root, release)
        self.assertNotEqual(root, _PROD_RELEASE)

    def test_cron_copy_follows_its_own_prefix(self):
        """<prefix>/scripts/ imports <prefix>/releases/current, including Test."""
        with tempfile.TemporaryDirectory() as tmp:
            test_root = os.path.join(tmp, "opt", "streetsmart-hermes-test")
            test_release = os.path.join(test_root, "releases", "current")
            _touch_package(test_release)
            test_script = os.path.join(test_root, "scripts", "robie_health_check.py")
            os.makedirs(os.path.dirname(test_script))

            prod_root = os.path.join(tmp, "opt", "streetsmart-hermes")
            prod_release = os.path.join(prod_root, "releases", "current")
            _touch_package(prod_release)
            prod_script = os.path.join(prod_root, "scripts", "robie_health_check.py")
            os.makedirs(os.path.dirname(prod_script))

            with _isolated_env():
                self.assertEqual(h.release_import_root(test_script), test_release)
                self.assertEqual(h.release_import_root(prod_script), prod_release)
        self.assertNotEqual(test_release, _PROD_RELEASE)

    def test_env_override_beats_hard_coded_prod_path(self):
        override = "/opt/streetsmart-hermes-test/releases/current"
        script = "/opt/streetsmart-hermes/scripts/robie_health_check.py"
        with _isolated_env(ROBIE_CANONICAL_JOB_ENGINE_ROOT=override):
            root = h.release_import_root(script)
        self.assertEqual(root, override)
        self.assertNotEqual(root, _PROD_RELEASE)

    def test_pythonpath_uses_release_not_gateway_runtime_or_prod(self):
        with tempfile.TemporaryDirectory() as tmp:
            release = os.path.join(tmp, "opt", "streetsmart-hermes-test", "releases", "current")
            runtime = os.path.join(release, ".gateway-runtime")
            os.makedirs(runtime)
            _touch_package(release)
            script = os.path.join(tmp, "bin", "robie_health_check.py")
            os.makedirs(os.path.dirname(script))
            pythonpath = os.pathsep.join([
                runtime,
                release,
                os.path.join(tmp, ".hermes", "hermes-agent"),
            ])
            with _isolated_env(PYTHONPATH=pythonpath):
                root = h.release_import_root(script)
        self.assertEqual(root, release)
        self.assertNotEqual(root, _PROD_RELEASE)

    def test_unresolved_prod_cron_keeps_historical_path(self):
        """No visible release tree and no override: Production cron is unchanged."""
        script = "/opt/streetsmart-hermes/scripts/robie_health_check.py"
        with _isolated_env():
            self.assertEqual(h.release_import_root(script), _PROD_RELEASE)


class EzlynxProbeImportTests(unittest.TestCase):
    def test_probe_prepends_script_release_not_prod(self):
        with tempfile.TemporaryDirectory() as tmp:
            release = os.path.join(tmp, "opt", "streetsmart-hermes-test", "releases", "current")
            _touch_package(release)
            script = os.path.join(release, "scripts", "robie_health_check.py")
            os.makedirs(os.path.dirname(script))
            inserted: list[tuple[int, str]] = []

            class _RecordingPath(list):
                def insert(self, index, value):
                    inserted.append((index, value))
                    super().insert(index, value)

            fake_api = types.ModuleType("robie_job_engine.ezlynx_api")

            def load_ezlynx_api_config():
                raise RuntimeError("config not visible")

            fake_api.load_ezlynx_api_config = load_ezlynx_api_config
            fake_api.EzlynxApiClient = object
            package = types.ModuleType("robie_job_engine")
            package.ezlynx_api = fake_api
            with _isolated_env(), \
                 patch.object(h, "__file__", script), \
                 patch.object(h.sys, "path", _RecordingPath(h.sys.path)), \
                 patch.dict(sys.modules, {
                     "robie_job_engine": package,
                     "robie_job_engine.ezlynx_api": fake_api,
                 }):
                ok, detail, extra = h.check_ezlynx_auth()
        self.assertEqual(inserted, [(0, release)])
        self.assertNotIn(_PROD_RELEASE, [value for _, value in inserted])
        self.assertTrue(ok)
        self.assertTrue(extra.get("skipped"))
        self.assertIn("config not visible", detail)


class PreflightAlertImportTests(unittest.TestCase):
    def test_probe_prepends_script_release_not_prod(self):
        with tempfile.TemporaryDirectory() as tmp:
            release = os.path.join(tmp, "opt", "streetsmart-hermes-test", "releases", "current")
            _touch_package(release)
            script = os.path.join(release, "scripts", "robie_health_check.py")
            os.makedirs(os.path.dirname(script))
            inserted: list[tuple[int, str]] = []

            class _RecordingPath(list):
                def insert(self, index, value):
                    inserted.append((index, value))
                    super().insert(index, value)

            fake_preflight = types.ModuleType("robie_job_engine.production_preflight")
            fake_preflight.parse_preflight_alert_state = lambda _text: None
            package = types.ModuleType("robie_job_engine")
            package.production_preflight = fake_preflight
            with _isolated_env(), \
                 patch.object(h, "__file__", script), \
                 patch.object(h.sys, "path", _RecordingPath(list(h.sys.path))), \
                 patch.dict(sys.modules, {
                     "robie_job_engine": package,
                     "robie_job_engine.production_preflight": fake_preflight,
                 }):
                ok, detail, _extra = h.check_preflight_alert_delivery(journal="")
        self.assertEqual(inserted, [(0, release)])
        self.assertNotIn(_PROD_RELEASE, [value for _, value in inserted])
        self.assertTrue(ok)
        self.assertIn("no preflight JSON", detail)


if __name__ == "__main__":
    unittest.main()
