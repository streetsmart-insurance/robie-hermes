"""Tests for newest ENABLED Secret Manager resolution."""

from __future__ import annotations

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from robie_job_engine.secret_resolution import (
    access_newest_enabled_secret,
    describe_newest_enabled_resolution,
    newest_enabled_version_name,
    secret_parent,
)


def _version(name: str, state: str, create_time: float) -> SimpleNamespace:
    return SimpleNamespace(name=name, state=state, create_time=create_time)


class SecretResolutionTests(TestCase):
    def test_secret_parent_strips_version_suffix(self):
        parent = secret_parent(
            "projects/p/secrets/ezlynx-password/versions/latest"
        )
        self.assertEqual(parent, "projects/p/secrets/ezlynx-password")

    def test_newest_enabled_ignores_destroyed_latest_pointer(self):
        client = Mock()
        client.list_secret_versions.side_effect = [
            [
                _version("projects/p/secrets/ezlynx-password/versions/1", "ENABLED", 1.0),
            ],
            [
                _version("projects/p/secrets/ezlynx-password/versions/2", "DESTROYED", 2.0),
                _version("projects/p/secrets/ezlynx-password/versions/1", "ENABLED", 1.0),
            ],
        ]
        client.access_secret_version.return_value = SimpleNamespace(
            payload=SimpleNamespace(data=b"enabled-password\n")
        )
        value = access_newest_enabled_secret(
            client,
            "projects/p/secrets/ezlynx-password/versions/latest",
        )
        self.assertEqual(value, "enabled-password")
        client.access_secret_version.assert_called_once_with(
            request={"name": "projects/p/secrets/ezlynx-password/versions/1"}
        )
        client.list_secret_versions.assert_any_call(
            request={
                "parent": "projects/p/secrets/ezlynx-password",
                "filter": "state:ENABLED",
            }
        )

    def test_pinned_destroyed_numeric_version_still_resolves_enabled(self):
        client = Mock()
        client.list_secret_versions.return_value = [
            _version("projects/p/secrets/ezlynx-password/versions/1", "ENABLED", 1.0),
        ]
        client.access_secret_version.return_value = SimpleNamespace(
            payload=SimpleNamespace(data=b"still-enabled\n")
        )
        value = access_newest_enabled_secret(
            client,
            "projects/p/secrets/ezlynx-password/versions/2",
        )
        self.assertEqual(value, "still-enabled")
        client.access_secret_version.assert_called_once_with(
            request={"name": "projects/p/secrets/ezlynx-password/versions/1"}
        )

    def test_describe_resolution_is_state_only(self):
        client = Mock()
        client.list_secret_versions.side_effect = [
            [
                _version("projects/p/secrets/ezlynx-password/versions/1", "ENABLED", 1.0),
            ],
            [
                _version("projects/p/secrets/ezlynx-password/versions/2", "DESTROYED", 2.0),
                _version("projects/p/secrets/ezlynx-password/versions/1", "ENABLED", 1.0),
            ],
        ]
        summary = describe_newest_enabled_resolution(
            client,
            "projects/p/secrets/ezlynx-password/versions/latest",
        )
        self.assertEqual(summary["resolved_enabled_version"], "versions/1")
        self.assertEqual(summary["newest_version_overall"], "versions/2")
        client.access_secret_version.assert_not_called()
