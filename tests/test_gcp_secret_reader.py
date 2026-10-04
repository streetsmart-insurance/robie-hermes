"""Unit tests for robie_job_engine.gcp_secret_reader. Fakes only."""

from __future__ import annotations

import os
import sys
import types
import unittest
import unittest.mock

sys.modules.pop("robie_job_engine.verification_common", None)

from robie_job_engine import gcp_secret_reader as gsr


def _install_fake_secret_manager(*, error=None, payload=b"top-secret-value"):
    """Inject fake google.cloud.secretmanager + google.api_core.exceptions."""
    google = types.ModuleType("google")
    cloud = types.ModuleType("google.cloud")
    secret_manager = types.ModuleType("google.cloud.secretmanager")
    api_core = types.ModuleType("google.api_core")
    api_exceptions = types.ModuleType("google.api_core.exceptions")

    class PermissionDenied(Exception):
        pass

    class NotFound(Exception):
        pass

    api_exceptions.PermissionDenied = PermissionDenied
    api_exceptions.NotFound = NotFound

    class _FakePayload:
        def __init__(self, data):
            self.data = data

    class _FakeResponse:
        def __init__(self, data):
            self.payload = _FakePayload(data)

    class SecretManagerServiceClient:
        def __init__(self):
            self.paths = []

        def secret_version_path(self, project, name, version):
            path = f"projects/{project}/secrets/{name}/versions/{version}"
            self.paths.append(path)
            return path

        def access_secret_version(self, request=None):
            import sys as _sys

            module = _sys.modules.get("google.cloud.secretmanager")
            error = getattr(module, "_next_error", None)
            if error is not None:
                raise error
            return _FakeResponse(payload)

    secret_manager.SecretManagerServiceClient = SecretManagerServiceClient
    secret_manager._next_error = error
    cloud.secretmanager = secret_manager
    google.cloud = cloud
    api_core.exceptions = api_exceptions

    sys.modules["google"] = google
    sys.modules["google.cloud"] = cloud
    sys.modules["google.cloud.secretmanager"] = secret_manager
    sys.modules["google.api_core"] = api_core
    sys.modules["google.api_core.exceptions"] = api_exceptions
    return PermissionDenied, NotFound


def _remove_fake_secret_manager():
    for name in (
        "google",
        "google.cloud",
        "google.cloud.secretmanager",
        "google.api_core",
        "google.api_core.exceptions",
    ):
        sys.modules.pop(name, None)


class SecretReaderTests(unittest.TestCase):
    def tearDown(self):
        _remove_fake_secret_manager()

    def test_happy_path_reads_latest(self):
        _install_fake_secret_manager()
        env = {
            key: value
            for key, value in os.environ.items()
            if key != "GOOGLE_APPLICATION_CREDENTIALS"
        }
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            value = gsr.get_secret("ezlynx-username")
        self.assertEqual(value, "top-secret-value")

    def test_permission_denied_names_iam_grant(self):
        permission_denied, _ = _install_fake_secret_manager()
        sys.modules["google.cloud.secretmanager"]._next_error = permission_denied(
            "denied"
        )
        env = {
            key: value
            for key, value in os.environ.items()
            if key != "GOOGLE_APPLICATION_CREDENTIALS"
        }
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(gsr.SecretManagerAccessError) as ctx:
                gsr.get_secret("ezlynx-password")
        message = str(ctx.exception)
        self.assertIn("NEEDS_AUTH", message)
        self.assertIn("secretmanager.secretAccessor", message)
        self.assertIn("ezlynx-password", message)

    def test_not_found(self):
        _, not_found_cls = _install_fake_secret_manager()
        sys.modules["google.cloud.secretmanager"]._next_error = not_found_cls(
            "missing"
        )
        env = {
            key: value
            for key, value in os.environ.items()
            if key != "GOOGLE_APPLICATION_CREDENTIALS"
        }
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(gsr.SecretManagerAccessError) as ctx:
                gsr.get_secret("nope")
        self.assertIn("not found", str(ctx.exception))

    def test_key_file_credentials_refused(self):
        _install_fake_secret_manager()
        with unittest.mock.patch.dict(
            os.environ,
            {"GOOGLE_APPLICATION_CREDENTIALS": "/tmp/some-key.json"},
            clear=True,
        ):
            with self.assertRaises(gsr.SecretManagerAccessError) as ctx:
                gsr.get_secret("ezlynx-username")
        self.assertIn("key-file", str(ctx.exception))

    def test_inventory_key_file_refused_explicitly(self):
        _install_fake_secret_manager()
        with unittest.mock.patch.dict(
            os.environ,
            {"GOOGLE_APPLICATION_CREDENTIALS": gsr.FORBIDDEN_KEY_FILE},
            clear=True,
        ):
            with self.assertRaises(gsr.SecretManagerAccessError) as ctx:
                gsr.get_secret("ezlynx-username")
        self.assertIn("inventory key file", str(ctx.exception))

    def test_empty_name_rejected(self):
        _install_fake_secret_manager()
        with self.assertRaises(gsr.SecretManagerAccessError):
            gsr.get_secret("  ")

    def test_missing_dependency_fails_closed(self):
        _remove_fake_secret_manager()
        # A plain google.cloud module with no secretmanager submodule, so the
        # import fails even when google-cloud-secret-manager is installed.
        google = types.ModuleType("google")
        cloud = types.ModuleType("google.cloud")
        google.cloud = cloud
        sys.modules["google"] = google
        sys.modules["google.cloud"] = cloud
        env = {
            key: value
            for key, value in os.environ.items()
            if key != "GOOGLE_APPLICATION_CREDENTIALS"
        }
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(gsr.SecretManagerAccessError) as ctx:
                gsr.get_secret("ezlynx-username")
        self.assertIn("not installed", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()


# The fakes above are installed under the name the reader imports, so on their
# own they cannot catch a wrong module name (google.cloud.secret_manager passed
# CI while every Production read failed with NEEDS_AUTH). These tests check the
# import path itself.
REAL_MODULE = "google.cloud.secretmanager"
WRONG_MODULE = "google.cloud." + "secret_" + "manager"


def _imports_in(path):
    import ast

    tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.extend(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
    return found


class SecretManagerImportPathTests(unittest.TestCase):
    def test_reader_imports_the_real_package_name(self):
        imports = _imports_in(gsr.__file__)
        self.assertIn(REAL_MODULE, imports)
        self.assertNotIn(WRONG_MODULE, imports)

    def test_no_python_file_imports_the_wrong_name(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        offenders = []
        for base, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in {".git", "node_modules", ".venv", "venv", "__pycache__"}]
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(base, name)
                try:
                    imports = _imports_in(path)
                except (SyntaxError, UnicodeDecodeError, ValueError):
                    continue
                if any(item == WRONG_MODULE or item.startswith(WRONG_MODULE + ".") for item in imports):
                    offenders.append(os.path.relpath(path, root))
        self.assertEqual(offenders, [])

    def test_installed_package_resolves_and_reader_uses_it(self):
        import importlib
        import importlib.util

        _remove_fake_secret_manager()
        try:
            spec = importlib.util.find_spec(REAL_MODULE)
        except ModuleNotFoundError:
            spec = None
        if spec is None:
            self.skipTest("google-cloud-secret-manager is not installed")
        self.assertIsNone(importlib.util.find_spec("google.cloud." + "secret_" + "manager"))
        real = importlib.import_module(REAL_MODULE)

        class _Payload:
            data = b"value-from-real-module-path"

        class _Response:
            payload = _Payload()

        class _Client:
            def secret_version_path(self, project, secret, version):
                return f"projects/{project}/secrets/{secret}/versions/{version}"

            def access_secret_version(self, request=None):
                return _Response()

        env = {
            key: value
            for key, value in os.environ.items()
            if key != "GOOGLE_APPLICATION_CREDENTIALS"
        }
        with unittest.mock.patch.object(real, "SecretManagerServiceClient", _Client), \
                unittest.mock.patch.dict(os.environ, env, clear=True):
            value = gsr.get_secret("bland-api-key")
        self.assertEqual(value, "value-from-real-module-path")
