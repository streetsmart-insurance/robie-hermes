"""Login-secret version-state preflight. No payloads. No live EZLynx."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_guard import open_chat_job
from robie_job_engine.login_secret_health import (
    format_alert,
    format_leftover_note,
    inspect_login_secrets,
    maybe_periodic_login_secret_check,
    maybe_preflight_login_secrets,
    summarize_secret_versions,
)
from robie_job_engine.models import JobStatus
from robie_job_engine.store import JobStore


def _version(name: str, state: str, created: float) -> SimpleNamespace:
    return SimpleNamespace(name=name, state=state, create_time=created)


PASSWORD_DESTROYED_NEWEST = [
    _version(
        "projects/streetsmart-hermes-poc/secrets/ezlynx-password/versions/1",
        "ENABLED",
        1.0,
    ),
    _version(
        "projects/streetsmart-hermes-poc/secrets/ezlynx-password/versions/2",
        "DESTROYED",
        2.0,
    ),
]
USERNAME_ENABLED = [
    _version(
        "projects/streetsmart-hermes-poc/secrets/ezlynx-username/versions/3",
        "ENABLED",
        3.0,
    ),
]


class VersionStateTests(unittest.TestCase):
    def test_newest_destroyed_with_older_enabled_is_healthy(self):
        summary = summarize_secret_versions(
            PASSWORD_DESTROYED_NEWEST, secret_id="ezlynx-password"
        )
        self.assertFalse(summary["alert"])
        self.assertTrue(summary["newest_destroyed"])
        self.assertTrue(summary["leftover_destroyed"])
        self.assertFalse(summary["missing_enabled"])
        self.assertEqual(summary["newest_version"], "versions/2")
        self.assertEqual(summary["newest_enabled_version"], "versions/1")
        self.assertEqual(summary["enabled_versions"], ["versions/1"])

    def test_no_enabled_version_holds(self):
        summary = summarize_secret_versions(
            [
                _version("secrets/ezlynx-password/versions/2", "DESTROYED", 2.0),
            ],
            secret_id="ezlynx-password",
        )
        self.assertTrue(summary["missing_enabled"])
        self.assertTrue(summary["alert"])

    def test_inspect_lists_states_and_never_accesses_payload(self):
        client = Mock()

        def list_versions(request):
            parent = request["parent"]
            if parent.endswith("ezlynx-password"):
                return PASSWORD_DESTROYED_NEWEST
            return USERNAME_ENABLED

        client.list_secret_versions.side_effect = list_versions
        report = inspect_login_secrets(
            client=client, project="streetsmart-hermes-poc"
        )
        self.assertEqual(report["result"], "OK")
        self.assertFalse(report["should_hold"])
        leftover = format_leftover_note(report)
        self.assertIn("using ENABLED versions/1", leftover)
        self.assertIn("versions/2 is DESTROYED leftover", leftover)
        self.assertNotIn("password is destroyed", leftover.casefold())
        client.access_secret_version.assert_not_called()
        self.assertEqual(client.list_secret_versions.call_count, 2)
        text = format_alert(report)
        self.assertIn("ezlynx-password", text)
        self.assertIn("versions/2", text)
        self.assertIn("RETRY", text)
        self.assertNotIn("[REDACTED]", text)
        self.assertNotIn("password=", text)
        self.assertNotIn("password is destroyed", text.casefold())

    def test_all_enabled_newest_is_ok(self):
        client = Mock()
        client.list_secret_versions.return_value = USERNAME_ENABLED
        report = inspect_login_secrets(
            client=client,
            project="streetsmart-hermes-poc",
            secret_ids=("ezlynx-username",),
        )
        self.assertEqual(report["result"], "OK")
        self.assertFalse(report["should_hold"])

    def test_unavailable_secret_manager_is_unknown_not_hold(self):
        report = inspect_login_secrets(client=None)
        if report["result"] != "UNKNOWN":
            # A live client in this VM is still fail-closed on payloads.
            self.assertNotIn("payload", str(report).casefold())
            return
        self.assertFalse(report["should_hold"])


class PreflightAndSchedulerTests(unittest.TestCase):
    def test_preflight_holds_before_running_when_no_enabled(self):
        with durable_temporary_directory() as tmp:
            db = str(__import__("pathlib").Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "hermes.google_chat_task",
                {"conversation_id": "spaces/hitl", "text": "login"},
            )
            posted: list[tuple[str, str]] = []
            report = {
                "result": "ALERT",
                "reason": "ezlynx-password: no ENABLED version",
                "project": "streetsmart-hermes-poc",
                "should_hold": True,
                "secrets": [
                    {
                        "secret_id": "ezlynx-password",
                        "versions": [{"version": "versions/2", "state": "DESTROYED"}],
                        "enabled_versions": [],
                        "newest_version": "versions/2",
                        "newest_state": "DESTROYED",
                        "missing_enabled": True,
                        "newest_destroyed": True,
                        "alert": True,
                    }
                ],
            }
            maybe_preflight_login_secrets(
                store,
                job,
                inspector=lambda: report,
                poster=lambda space, text: posted.append((space, text)),
            )
            held = store.get_job(job["id"])
            self.assertEqual(held["status"], JobStatus.NEEDS_AUTH.value)
            self.assertIn("DESTROYED", held["last_error"])
            self.assertEqual(posted[0][0], "spaces/hitl")
            self.assertIn("RETRY", posted[0][1])
            self.assertNotIn("SSRobie", posted[0][1])

    def test_open_chat_job_holds_on_destroyed_only_secret(self):
        with durable_temporary_directory() as tmp:
            db = str(__import__("pathlib").Path(tmp) / "jobs.db")
            report = {
                "result": "ALERT",
                "reason": "ezlynx-password: no ENABLED version",
                "project": "streetsmart-hermes-poc",
                "should_hold": True,
                "secrets": [
                    {
                        "secret_id": "ezlynx-password",
                        "enabled_versions": [],
                        "newest_version": "versions/2",
                        "newest_state": "DESTROYED",
                        "missing_enabled": True,
                        "newest_destroyed": True,
                        "alert": True,
                    }
                ],
            }
            with patch(
                "robie_job_engine.login_secret_health.inspect_login_secrets",
                return_value=report,
            ), patch(
                "robie_job_engine.chat_app_post.post_as_chat_app",
                return_value={"name": "ok"},
            ):
                job_id = open_chat_job(
                    db,
                    "spaces/s/messages/secret-hold",
                    "Perform the destination workflow",
                    conversation_id="spaces/s",
                )
            job = JobStore(db).get_job(job_id)
            self.assertEqual(job["status"], JobStatus.NEEDS_AUTH.value)
            self.assertNotEqual(job["status"], JobStatus.RUNNING.value)

    def test_open_chat_job_stays_running_when_inspect_unknown(self):
        with durable_temporary_directory() as tmp:
            db = str(__import__("pathlib").Path(tmp) / "jobs.db")
            with patch(
                "robie_job_engine.login_secret_health.inspect_login_secrets",
                return_value={
                    "result": "UNKNOWN",
                    "reason": "secret manager unavailable: ImportError",
                    "should_hold": False,
                    "secrets": [],
                },
            ):
                job_id = open_chat_job(
                    db,
                    "spaces/s/messages/unknown-sm",
                    "Perform the destination workflow",
                    conversation_id="spaces/s",
                )
            job = JobStore(db).get_job(job_id)
            self.assertEqual(job["status"], JobStatus.RUNNING.value)

    def test_periodic_check_alerts_once_then_cools_down(self):
        with durable_temporary_directory() as tmp:
            db = str(__import__("pathlib").Path(tmp) / "jobs.db")
            report = {
                "result": "ALERT",
                "reason": "ezlynx-password has no ENABLED version",
                "project": "streetsmart-hermes-poc",
                "should_hold": True,
                "secrets": [
                    {
                        "secret_id": "ezlynx-password",
                        "enabled_versions": [],
                        "newest_enabled_version": None,
                        "newest_version": "versions/2",
                        "newest_state": "DESTROYED",
                        "missing_enabled": True,
                        "newest_destroyed": True,
                        "leftover_destroyed": False,
                        "alert": True,
                    }
                ],
            }
            posted: list[str] = []
            os.environ["ROBIE_LOGIN_SECRET_ALERT_SPACE"] = "spaces/ops"
            try:
                first = maybe_periodic_login_secret_check(
                    db,
                    inspector=lambda: report,
                    poster=lambda space, text: posted.append(text),
                    cooldown_seconds=1800,
                )
                second = maybe_periodic_login_secret_check(
                    db,
                    inspector=lambda: report,
                    poster=lambda space, text: posted.append(text),
                    cooldown_seconds=1800,
                )
            finally:
                os.environ.pop("ROBIE_LOGIN_SECRET_ALERT_SPACE", None)
            self.assertEqual(first["result"], "ALERT")
            self.assertEqual(second["result"], "ALERT")
            self.assertEqual(len(posted), 1)
            self.assertIn("DESTROYED", posted[0])

    def test_periodic_unknown_does_not_create_sentinel(self):
        with durable_temporary_directory() as tmp:
            db = str(__import__("pathlib").Path(tmp) / "jobs.db")
            maybe_periodic_login_secret_check(
                db,
                inspector=lambda: {
                    "result": "UNKNOWN",
                    "reason": "secret manager unavailable",
                    "should_hold": False,
                    "secrets": [],
                },
            )
            store = JobStore(db)
            with store.connect() as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
