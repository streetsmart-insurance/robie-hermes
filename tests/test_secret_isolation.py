"""Tests for Test/Production Secret Manager isolation."""

from __future__ import annotations

import os
import socket
from unittest import TestCase
from unittest.mock import patch

from robie_job_engine.secret_isolation import (
    forbid_test_reading_production_secrets,
    is_test_runtime,
    production_secret_id,
    SecretIsolationError,
)
from robie_job_engine.secret_resolution import access_newest_enabled_secret


class SecretIsolationTests(TestCase):
    def test_production_secret_id_detection(self):
        secret_id = production_secret_id(
            "projects/p/secrets/ezlynx-password/versions/latest"
        )
        self.assertEqual(secret_id, "ezlynx-password")

    def test_test_runtime_blocks_production_secret_access(self):
        with (
            patch.dict(os.environ, {"ROBIE_ENV": "TEST"}, clear=False),
            patch.object(socket, "gethostname", return_value="hermes-poc-01"),
        ):
            with self.assertRaises(SecretIsolationError):
                forbid_test_reading_production_secrets(
                    "projects/p/secrets/ezlynx-username"
                )

    def test_test_hostname_blocks_even_without_robie_env(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(socket, "gethostname", return_value="hermes-test-01"),
        ):
            self.assertTrue(is_test_runtime())
            with self.assertRaises(SecretIsolationError):
                forbid_test_reading_production_secrets(
                    "projects/p/secrets/ezlynx-password/versions/1"
                )

    def test_production_host_allows_production_secret_reference(self):
        with (
            patch.dict(os.environ, {"ROBIE_ENV": "PRODUCTION"}, clear=False),
            patch.object(socket, "gethostname", return_value="hermes-poc-01"),
        ):
            forbid_test_reading_production_secrets(
                "projects/p/secrets/ezlynx-username"
            )

    def test_resolution_calls_isolation_guard_before_api(self):
        client = object()
        with (
            patch.dict(os.environ, {"ROBIE_ENV": "TEST"}, clear=False),
            patch.object(socket, "gethostname", return_value="hermes-test-01"),
        ):
            with self.assertRaises(SecretIsolationError):
                access_newest_enabled_secret(
                    client,
                    "projects/p/secrets/ezlynx-password",
                )
