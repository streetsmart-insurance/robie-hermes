import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch


class EzlynxLoginSecretVersionTests(TestCase):
    def test_secret_uses_newest_enabled_version_not_latest_alias(self):
        client = Mock()
        client.list_secret_versions.return_value = [
            SimpleNamespace(name="versions/1", create_time=1),
            SimpleNamespace(name="versions/3", create_time=3),
        ]
        client.access_secret_version.return_value = SimpleNamespace(
            payload=SimpleNamespace(data=b"credential-value\n")
        )
        google = ModuleType("google")
        google_auth = ModuleType("google.auth")
        google_auth_transport = ModuleType("google.auth.transport")
        google_auth_requests = ModuleType("google.auth.transport.requests")
        google_auth_requests.Request = object
        google_cloud = ModuleType("google.cloud")
        secretmanager = ModuleType("google.cloud.secretmanager")
        secretmanager.SecretManagerServiceClient = Mock(return_value=client)
        google_cloud.secretmanager = secretmanager
        google_oauth2 = ModuleType("google.oauth2")
        google_credentials = ModuleType("google.oauth2.credentials")
        google_credentials.Credentials = object
        google_api = ModuleType("googleapiclient")
        google_discovery = ModuleType("googleapiclient.discovery")
        google_discovery.build = Mock()
        playwright = ModuleType("playwright")
        playwright_sync = ModuleType("playwright.sync_api")
        playwright_sync.sync_playwright = Mock()
        modules = {
            "google": google,
            "google.auth": google_auth,
            "google.auth.transport": google_auth_transport,
            "google.auth.transport.requests": google_auth_requests,
            "google.cloud": google_cloud,
            "google.cloud.secretmanager": secretmanager,
            "google.oauth2": google_oauth2,
            "google.oauth2.credentials": google_credentials,
            "googleapiclient": google_api,
            "googleapiclient.discovery": google_discovery,
            "playwright": playwright,
            "playwright.sync_api": playwright_sync,
        }
        module_path = Path(__file__).resolve().parents[1] / "ezlynx_login_bootstrap.py"
        spec = importlib.util.spec_from_file_location("test_login_bootstrap", module_path)
        bootstrap = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, modules):
            assert spec.loader is not None
            spec.loader.exec_module(bootstrap)
            value = bootstrap.secret("ezlynx-password")

        self.assertEqual(value, "credential-value")
        client.list_secret_versions.assert_called_once_with(
            request={
                "parent": "projects/streetsmart-hermes-poc/secrets/ezlynx-password",
                "filter": "state:ENABLED",
            }
        )
        client.access_secret_version.assert_called_once_with(
            request={"name": "versions/3"}
        )

        page = Mock()
        bootstrap.navigate_to_submission_route(page)
        page.goto.assert_called_once_with(
            bootstrap.SUBMISSION_URL, wait_until="domcontentloaded"
        )
        page.wait_for_timeout.assert_called_once_with(2_000)

    def test_secret_ignores_stale_pin_and_uses_newest_enabled(self):
        client = Mock()
        client.list_secret_versions.return_value = [
            SimpleNamespace(name="versions/1", create_time=1),
            SimpleNamespace(name="versions/6", create_time=6),
        ]
        client.access_secret_version.return_value = SimpleNamespace(
            payload=SimpleNamespace(data=b"newest-credential\n")
        )
        google = ModuleType("google")
        google_cloud = ModuleType("google.cloud")
        secretmanager = ModuleType("google.cloud.secretmanager")
        secretmanager.SecretManagerServiceClient = Mock(return_value=client)
        google_cloud.secretmanager = secretmanager
        modules = {
            "google": google,
            "google.cloud": google_cloud,
            "google.cloud.secretmanager": secretmanager,
        }
        module_path = Path(__file__).resolve().parents[1] / "ezlynx_login_bootstrap.py"
        spec = importlib.util.spec_from_file_location("test_pinned_login_bootstrap", module_path)
        bootstrap = importlib.util.module_from_spec(spec)
        reference = (
            "projects/streetsmart-hermes-poc/secrets/"
            "ezlynx-password/versions/1"
        )
        with (
            patch.dict(sys.modules, modules),
            patch.dict(
                os.environ,
                {"ROBIE_EZLYNX_PASSWORD_SECRET": reference},
                clear=False,
            ),
        ):
            assert spec.loader is not None
            spec.loader.exec_module(bootstrap)
            value = bootstrap.secret("ezlynx-password")

        self.assertEqual(value, "newest-credential")
        client.list_secret_versions.assert_called_once()
        client.access_secret_version.assert_called_once_with(
            request={"name": "versions/6"}
        )

