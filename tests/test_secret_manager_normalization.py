from __future__ import annotations

import os
import unittest
from unittest.mock import Mock, patch

from robie_job_engine.secret_manager import (
    GoogleSecretManagerAccessor,
    normalize_secret_version_ref,
    load_ezlynx_credentials,
)


class TestSecretManagerNormalization(unittest.TestCase):
    def test_normalize_full_reference_unchanged(self):
        full = "projects/my-proj/secrets/my-sec/versions/1"
        self.assertEqual(normalize_secret_version_ref(full), full)

    def test_normalize_secret_level_adds_latest(self):
        secret_level = "projects/my-proj/secrets/my-sec"
        self.assertEqual(
            normalize_secret_version_ref(secret_level),
            "projects/my-proj/secrets/my-sec/versions/latest",
        )

    def test_normalize_bare_secret_name(self):
        bare = "ezlynx-username"
        self.assertEqual(
            normalize_secret_version_ref(bare, project_id="streetsmart-hermes-poc"),
            "projects/streetsmart-hermes-poc/secrets/ezlynx-username/versions/latest",
        )

    def test_normalize_empty_raises(self):
        with self.assertRaises(ValueError):
            normalize_secret_version_ref("")

    def test_accessor_resolves_bare_name(self):
        mock_client = Mock()
        mock_response = Mock()
        mock_response.payload.data = b"secret-pass-123"
        mock_client.access_secret_version.return_value = mock_response

        accessor = GoogleSecretManagerAccessor(client=mock_client)
        with patch.dict(os.environ, {"PROJECT_ID": "test-proj"}):
            val = accessor.access("ezlynx-password")
        self.assertEqual(val, "secret-pass-123")
        mock_client.access_secret_version.assert_called_once_with(
            request={"name": "projects/test-proj/secrets/ezlynx-password/versions/latest"}
        )

    def test_load_ezlynx_credentials_bare_env(self):
        mock_accessor = Mock()
        mock_accessor.access.side_effect = lambda ref: f"val-for-{ref}"
        env = {
            "ROBIE_EZLYNX_USERNAME_SECRET": "ezlynx-username",
            "ROBIE_EZLYNX_PASSWORD_SECRET": "ezlynx-password",
        }
        with patch.dict(os.environ, env):
            creds = load_ezlynx_credentials(mock_accessor)
        self.assertEqual(creds.username, "val-for-ezlynx-username")
        self.assertEqual(creds.password, "val-for-ezlynx-password")


if __name__ == "__main__":
    unittest.main()
