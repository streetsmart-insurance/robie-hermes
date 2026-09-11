"""The pre-flight must describe the service, not the ssh session it runs in.

milestone_preflight.py runs over an ad-hoc ssh session. That session is not the
service: it inherits none of the unit's Environment= or EnvironmentFile=, and
python3 on PATH is not necessarily the interpreter the unit runs, so it need not
have the same libraries. Answering from this session gives confident answers
about a box that does not exist - in both directions, green on a broken box and
red on a working one.

These tests pin the three pieces that keep the report honest: reading the unit's
environment, resolving the unit's interpreter (and admitting when it could not),
and noticing that the running service predates its own config.

Stdlib unittest: runs without pytest.
"""
from __future__ import annotations

import importlib.util
import os
import sys
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

    def test_files_override_unit_environment_in_declared_order(self):
        second = self.env_file.with_name('second.env')
        self.env_file.write_text('ROBIE_ENV=FIRST\n')
        second.write_text('ROBIE_ENV=SECOND\n')
        self.props[("robie-gateway", "EnvironmentFiles")] = f"{self.env_file} (ignore_errors=no) {second} (ignore_errors=no)"
        self.props[("robie-gateway", "Environment")] = 'ROBIE_ENV=INLINE "LABEL=two words"'
        with mock.patch.object(preflight, "_unit_property", _fake_properties(self.props)):
            env, _ = preflight._unit_environment("robie-gateway")
        self.assertEqual(env['ROBIE_ENV'], 'SECOND')
        self.assertEqual(env['LABEL'], 'two words')

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


class ResolvesTheServicesInterpreter(unittest.TestCase):
    """A venv's python has libraries this ssh session's python3 does not."""

    def setUp(self):
        self.venv_python = _tmpdir(self) / "python3.11"
        self.venv_python.write_text("#!/bin/sh\n")
        self.venv_python.chmod(0o755)

    def test_interpreter_comes_from_exec_start_path(self):
        props = {("robie-gateway", "ExecStart"):
                 f"{{ path={self.venv_python} ; argv[]={self.venv_python} -m robie_job_engine ; ignore_errors=no }}"}
        with mock.patch.object(preflight, "_unit_property", _fake_properties(props)):
            interpreter, note = preflight._service_python("robie-gateway")
        self.assertEqual(interpreter, str(self.venv_python))
        self.assertIn("ExecStart", note)

    def test_interpreter_falls_back_to_argv_when_path_is_a_wrapper(self):
        props = {("robie-gateway", "ExecStart"):
                 f"{{ path=/usr/bin/env ; argv[]=/usr/bin/env {self.venv_python} -m robie_job_engine ; ignore_errors=no }}"}
        with mock.patch.object(preflight, "_unit_property", _fake_properties(props)):
            interpreter, note = preflight._service_python("robie-gateway")
        self.assertEqual(interpreter, str(self.venv_python))
        self.assertIn("argv", note)

    def test_unknown_interpreter_admits_the_answer_may_be_about_this_session(self):
        props = {("robie-gateway", "ExecStart"):
                 "{ path=/opt/robie/bin/gateway ; argv[]=/opt/robie/bin/gateway ; ignore_errors=no }"}
        with mock.patch.object(preflight, "_unit_property", _fake_properties(props)):
            interpreter, note = preflight._service_python("robie-gateway")
        self.assertEqual(interpreter, sys.executable)
        self.assertIn("this session", note)

    def test_no_unit_at_all_says_which_python_it_used(self):
        interpreter, note = preflight._service_python(None)
        self.assertEqual(interpreter, sys.executable)
        self.assertIn("no gateway unit", note)


class ProbeRunsInThatInterpreter(unittest.TestCase):
    """The probe's answers must come from the service's runtime, not ours."""

    def test_probe_sees_the_unit_environment_and_the_release_on_path(self):
        release = _tmpdir(self)
        pkg = release / "robie_job_engine"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "ezlynx_api.py").write_text(
            "import os\n"
            "def load_ezlynx_api_config():\n"
            "    if not os.environ.get('ROBIE_EZLYNX_API_UAT_SECRET'):\n"
            "        raise RuntimeError('ROBIE_EZLYNX_API_UAT_SECRET must be configured')\n"
        )
        (pkg / "chat_guard.py").write_text(
            "class _UnavailableVerifier:\n"
            "    def __init__(self, reason):\n"
            "        self.reason = reason\n"
            "class Real:\n"
            "    pass\n"
            "def _default_chat_verifiers():\n"
            "    return {'hermes.google_chat_task': Real(), 'browser.read': Real()}\n"
        )
        probe, err = preflight._probe_service_runtime(
            sys.executable, {SECRET_KEY: "projects/1/secrets/x/versions/latest"}, release
        )
        self.assertEqual(err, "")
        self.assertEqual(probe["secret"][0], preflight.OK, probe)
        self.assertEqual(
            sorted(row[0] for row in probe["verifiers"]),
            ["browser.read", "hermes.google_chat_task"],
        )

    def test_missing_environment_reaches_the_probe_as_a_problem(self):
        release = _tmpdir(self)
        pkg = release / "robie_job_engine"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "ezlynx_api.py").write_text(
            "import os\n"
            "def load_ezlynx_api_config():\n"
            "    if not os.environ.get('ROBIE_EZLYNX_API_UAT_SECRET'):\n"
            "        raise RuntimeError('ROBIE_EZLYNX_API_UAT_SECRET must be configured')\n"
        )
        saved = os.environ.pop(SECRET_KEY, None)
        if saved is not None:
            self.addCleanup(os.environ.__setitem__, SECRET_KEY, saved)
        probe, err = preflight._probe_service_runtime(sys.executable, {}, release)
        self.assertEqual(err, "")
        self.assertEqual(probe["secret"][0], preflight.BAD)
        self.assertIn("must be configured", probe["secret"][1])

    def test_the_units_pythonpath_is_kept_not_replaced(self):
        """The gateway's libraries live on the unit's PYTHONPATH, not in the release."""
        release = _tmpdir(self)
        runtime = _tmpdir(self)
        (runtime / "only_on_the_units_path.py").write_text("VALUE = 1\n")
        pkg = release / "robie_job_engine"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "ezlynx_api.py").write_text(
            "import only_on_the_units_path\n"
            "def load_ezlynx_api_config():\n"
            "    return only_on_the_units_path.VALUE\n"
        )
        probe, err = preflight._probe_service_runtime(
            sys.executable, {"PYTHONPATH": str(runtime)}, release
        )
        self.assertEqual(err, "")
        self.assertEqual(probe["secret"][0], preflight.OK, probe)

    def test_release_is_appended_not_duplicated(self):
        release = _tmpdir(self)
        runtime = "/opt/x/.gateway-runtime"
        self.assertEqual(
            preflight._service_pythonpath({"PYTHONPATH": runtime}, release),
            f"{runtime}{os.pathsep}{release}",
        )
        already = f"{runtime}{os.pathsep}{release}"
        self.assertEqual(
            preflight._service_pythonpath({"PYTHONPATH": already}, release), already
        )
        self.assertEqual(preflight._service_pythonpath({}, release), str(release))

    def test_an_interpreter_that_cannot_run_is_an_error_not_a_pass(self):
        probe, err = preflight._probe_service_runtime(
            "/nonexistent/python", {}, _tmpdir(self)
        )
        self.assertIsNone(probe)
        self.assertTrue(err)


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
