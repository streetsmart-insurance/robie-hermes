"""Production pre-flight: seven yes/no checks. Chat on the first no only."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_app_post import (
    DEFAULT_FAIL_NOTIFY_EMAILS,
    fail_notify_emails,
    find_direct_message_space,
)
from robie_job_engine.chat_queue import DurableChatEventQueue
from robie_job_engine.models import JobStatus
from robie_job_engine.production_preflight import (
    AUTHENTICATED_APP_PREFIX,
    CANONICAL_JOB_ENGINE_ROOT,
    CHECK_CDP,
    CHECK_CHAT_INTAKE,
    CHECK_CHAT_RUNTIME,
    CHECK_EZLYNX_TAB,
    CHECK_GATEWAY,
    CHECK_LINKS,
    CHECK_SECRETS,
    DEFAULT_CHAT_SPACE,
    check_cdp,
    DEFAULT_GATEWAY_LOG,
    DEFAULT_PREFLIGHT_ENV_FILE,
    EXIT_ALERT_DELIVERY_FAILED,
    EXIT_CHECK_FAILED,
    EXIT_OK,
    check_chat_intake,
    check_conversation_job_links,
    check_ezlynx_tab,
    check_hermes_gateway,
    check_login_secrets,
    ezlynx_web_tab_ok,
    format_failure,
    format_preflight_startup,
    main,
    parse_preflight_alert_state,
    resolve_preflight_env_files,
    run_production_preflight,
)
from robie_job_engine.store import JobStore


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "robie_job_engine" / "production_preflight.py").read_text(
    encoding="utf-8"
)
DROP_IN = (
    ROOT / "deploy/systemd/zz-hermes-gateway-job-engine-path.conf"
).read_text(encoding="utf-8")
SERVICE = (
    ROOT / "deploy/systemd/robie-production-preflight.service"
).read_text(encoding="utf-8")
TIMER = (
    ROOT / "deploy/systemd/robie-production-preflight.timer"
).read_text(encoding="utf-8")


def _gateway_ok(**overrides) -> dict:
    payload = {
        "active": True,
        "active_state": "active",
        "pythonpath": f"{CANONICAL_JOB_ENGINE_ROOT}:/srv/robie/current",
        "canonical_root": CANONICAL_JOB_ENGINE_ROOT,
        "job_engine_present": True,
    }
    payload.update(overrides)
    return payload


def _secret_ok() -> dict:
    return {
        "result": "OK",
        "reason": "each watched secret has an ENABLED version",
        "project": "streetsmart-hermes-poc",
        "secrets": [
            {
                "secret_id": "ezlynx-username",
                "newest_enabled_version": "versions/3",
                "missing_enabled": False,
            },
            {
                "secret_id": "ezlynx-password",
                "newest_enabled_version": "versions/1",
                "missing_enabled": False,
            },
        ],
    }


def _secret_missing_password() -> dict:
    return {
        "result": "ALERT",
        "reason": "ezlynx-password has no ENABLED version",
        "project": "streetsmart-hermes-poc",
        "secrets": [
            {
                "secret_id": "ezlynx-username",
                "newest_enabled_version": "versions/3",
                "missing_enabled": False,
            },
            {
                "secret_id": "ezlynx-password",
                "newest_enabled_version": None,
                "missing_enabled": True,
            },
        ],
    }


def _http(mapping: dict[str, tuple[int, bytes]]):
    def getter(url: str) -> tuple[int, bytes]:
        if url not in mapping:
            raise AssertionError(f"unexpected CDP URL {url}")
        return mapping[url]

    return getter


class CheckContractTests(unittest.TestCase):
    def test_source_does_not_look_for_job_engine_unit_or_display(self):
        folded = SOURCE.casefold()
        self.assertNotIn("robie-job-engine.service", folded)
        self.assertNotIn("os.environ.get(\"display\")", folded)
        self.assertNotIn("computer_use", folded)
        self.assertNotIn("access_secret_version", folded)
        self.assertNotIn("versions/latest", folded)
        self.assertNotIn("page.goto", folded)
        self.assertNotIn("systemctl restart", folded)
        self.assertNotIn("<users/", folded)
        self.assertIn("mode=ro", SOURCE)

    def test_systemd_units_match_scheduler_oneshot_and_daily_window(self):
        self.assertIn("Type=oneshot", SERVICE)
        self.assertIn(
            "ExecStart=/opt/streetsmart-hermes/.hermes/hermes-agent/venv/bin/python "
            "-m robie_job_engine.production_preflight",
            SERVICE,
        )
        self.assertIn(
            "Environment=PYTHONPATH=/opt/streetsmart-hermes/releases/current",
            SERVICE,
        )
        self.assertIn("ROBIE_PREFLIGHT_CHAT_SPACE=spaces/AAQAZbLJO78", SERVICE)
        self.assertIn(
            "ROBIE_PREFLIGHT_FAIL_NOTIFY=carlo@streetsmart.insurance,"
            "jake@streetsmart.insurance",
            SERVICE,
        )
        self.assertNotIn("robie-job-engine.service", SERVICE)
        self.assertNotIn("systemctl restart", SERVICE)
        self.assertIn("OnCalendar=*-*-* 00,07..23:00:00 America/New_York", TIMER)
        self.assertIn("Unit=robie-production-preflight.service", TIMER)
        self.assertIn("WantedBy=timers.target", TIMER)

    def test_chat_alert_units_run_as_hermes_with_the_live_key(self):
        key_line = (
            "Environment=ROBIE_CHAT_SA_KEY_FILE="
            "/etc/streetsmart-hermes/robie-chat-sa-key.json"
        )
        wrong_key = "/etc/streetsmart-hermes/secrets/robie-chat-sa.json"
        units = {
            "deploy/systemd/robie-production-preflight.service": (
                "-m robie_job_engine.production_preflight"
            ),
            "deploy/systemd/robie-health-check.service": "scripts/robie_health_check.py",
            "deploy/systemd/robie-health-digest.service": (
                "scripts/robie_health_check.py --daily-digest"
            ),
        }
        for rel, exec_fragment in units.items():
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIn("User=streetsmart-hermes\n", text, rel)
            self.assertIn("Group=streetsmart-hermes\n", text, rel)
            self.assertIn(key_line, text, rel)
            self.assertNotIn(wrong_key, text, rel)
            self.assertNotIn("robie-recording.env", text, rel)
            self.assertNotIn("EnvironmentFile=/etc/streetsmart-hermes/robie-recording.env", text, rel)
            self.assertIn(exec_fragment, text, rel)
            self.assertNotIn("User=carlo", text, rel)
        health = (ROOT / "deploy/systemd/robie-health-check.service").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("--daily-digest", health)

    def test_gateway_drop_in_runs_preflight_after_restart_without_failing_unit(self):
        self.assertIn(
            "ExecStartPost=-/home/streetsmart-hermes/.hermes/hermes-agent/venv/bin/python "
            "-m robie_job_engine.production_preflight",
            DROP_IN,
        )
        self.assertIn("PYTHONPATH=/opt/streetsmart-hermes/releases/current:", DROP_IN)
        self.assertIn(
            "ROBIE_PREFLIGHT_FAIL_NOTIFY=carlo@streetsmart.insurance,"
            "jake@streetsmart.insurance",
            DROP_IN,
        )
        self.assertNotIn("robie-job-engine.service", DROP_IN)
        self.assertNotIn("systemctl restart", DROP_IN)

    def test_docs_keep_preflight_infra_only_and_test_n_gate(self):
        release = (ROOT / "RELEASE_PROCESS.md").read_text(encoding="utf-8")
        state = (ROOT / "CURRENT_STATE.md").read_text(encoding="utf-8")
        for text in (release, state):
            self.assertIn("infra only", text.casefold())
            self.assertIn("hermes-test-01", text)
            self.assertIn("New job types still need", text)
        self.assertIn("3 clean jobs", release)
        self.assertIn("N clean Test", state)
        self.assertIn("N = 3", state)
        self.assertIn("required Production gate", release)
        self.assertNotIn("smtp", SOURCE.casefold())
        self.assertNotIn("sendgrid", SOURCE.casefold())
        poster = (ROOT / "robie_job_engine" / "chat_app_post.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("findDirectMessage", poster)
        self.assertNotIn(".setup(", poster)
        self.assertIn("no outbound email api", poster.casefold())

    def test_failure_message_names_check_and_does_not_mention_robie(self):
        text = format_failure("cdp", "http://127.0.0.1:9222/json/version HTTP 500")
        self.assertIn("ROBIE Production pre-flight: no — cdp", text)
        self.assertIn("http://127.0.0.1:9222/json/version HTTP 500", text)
        self.assertNotIn("@robie", text.casefold())
        self.assertEqual(text.count("\n"), 1)


class GatewayCheckTests(unittest.TestCase):
    def test_active_gateway_with_job_engine_pythonpath_is_yes(self):
        result = check_hermes_gateway(_gateway_ok())
        self.assertTrue(result["ok"])
        self.assertEqual(result["name"], CHECK_GATEWAY)
        self.assertIn("PYTHONPATH", result["evidence"])

    def test_inactive_gateway_is_no(self):
        result = check_hermes_gateway(
            _gateway_ok(active=False, active_state="inactive")
        )
        self.assertFalse(result["ok"])
        self.assertIn("hermes-gateway", result["evidence"])
        self.assertNotIn("robie-job-engine.service", result["evidence"])

    def test_pointer_string_without_job_engine_package_is_no(self):
        result = check_hermes_gateway(_gateway_ok(job_engine_present=False))
        self.assertFalse(result["ok"])
        self.assertIn("missing", result["evidence"])

    def test_pythonpath_without_canonical_root_is_no(self):
        result = check_hermes_gateway(
            _gateway_ok(pythonpath="/srv/robie/current/vendor:/srv/robie/current")
        )
        self.assertFalse(result["ok"])


class CdpAndTabCheckTests(unittest.TestCase):
    def test_version_200_is_yes(self):
        result = check_cdp(
            http_get=_http({"http://127.0.0.1:9222/json/version": (200, b"{}")})
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["name"], CHECK_CDP)
        self.assertIn("HTTP 200", result["evidence"])

    def test_version_non_200_is_no(self):
        result = check_cdp(
            http_get=_http({"http://127.0.0.1:9222/json/version": (500, b"err")})
        )
        self.assertFalse(result["ok"])
        self.assertIn("HTTP 500", result["evidence"])

    def test_web_tab_is_yes_and_login_is_no(self):
        self.assertTrue(
            ezlynx_web_tab_ok("https://app.ezlynx.com/web/submission-center/overview")
        )
        self.assertFalse(
            ezlynx_web_tab_ok("https://app.ezlynx.com/auth/account/login")
        )
        yes = check_ezlynx_tab(
            tabs=[
                "https://app.ezlynx.com/auth/account/login",
                "https://app.ezlynx.com/web/policies",
            ]
        )
        no = check_ezlynx_tab(tabs=["https://app.ezlynx.com/auth/account/login"])
        empty = check_ezlynx_tab(tabs=[])
        self.assertTrue(yes["ok"])
        self.assertFalse(empty["ok"])
        self.assertIn("empty CDP target list", empty["evidence"])
        self.assertIn("session is not fine", empty["evidence"])
        self.assertEqual(yes["name"], CHECK_EZLYNX_TAB)
        self.assertTrue(yes["evidence"].startswith(AUTHENTICATED_APP_PREFIX))
        self.assertFalse(no["ok"])

    def test_lists_cdp_tabs_without_navigating(self):
        calls: list[str] = []

        def getter(url: str) -> tuple[int, bytes]:
            calls.append(url)
            if url.endswith("/json/list"):
                return (
                    200,
                    json.dumps(
                        [{"url": "https://app.ezlynx.com/web/account/1/policies"}]
                    ).encode(),
                )
            raise AssertionError(f"unexpected {url}")

        result = check_ezlynx_tab(http_get=getter)
        self.assertTrue(result["ok"])
        self.assertEqual(calls, ["http://127.0.0.1:9222/json/list"])
        self.assertNotIn("goto", SOURCE)


class SecretAndLinkCheckTests(unittest.TestCase):
    def test_enabled_versions_are_yes_and_never_read_payloads(self):
        client = Mock()
        client.list_secret_versions.side_effect = lambda request: [
            SimpleNamespace(
                name=f"{request['parent']}/versions/3",
                state="ENABLED",
                create_time=3.0,
            )
        ]
        result = check_login_secrets(client=client)
        self.assertTrue(result["ok"])
        self.assertEqual(result["name"], CHECK_SECRETS)
        self.assertIn("ezlynx-username ENABLED versions/3", result["evidence"])
        self.assertIn("ezlynx-password ENABLED versions/3", result["evidence"])
        client.access_secret_version.assert_not_called()
        self.assertNotIn("password=", result["evidence"].casefold())
        self.assertEqual(client.list_secret_versions.call_count, 2)
        parents = [
            call.kwargs["request"]["parent"]
            for call in client.list_secret_versions.call_args_list
        ]
        self.assertEqual(
            parents,
            [
                "projects/streetsmart-hermes-poc/secrets/ezlynx-username",
                "projects/streetsmart-hermes-poc/secrets/ezlynx-password",
            ],
        )

    def test_missing_enabled_version_is_no(self):
        result = check_login_secrets(inspector=_secret_missing_password)
        self.assertFalse(result["ok"])
        self.assertIn("ezlynx-password no ENABLED version", result["evidence"])

    def test_destroyed_latest_with_older_enabled_is_yes(self):
        result = check_login_secrets(
            inspector=lambda: {
                "result": "OK",
                "secrets": [
                    {
                        "secret_id": "ezlynx-username",
                        "newest_enabled_version": "versions/3",
                        "missing_enabled": False,
                    },
                    {
                        "secret_id": "ezlynx-password",
                        "newest_enabled_version": "versions/1",
                        "newest_version": "versions/2",
                        "missing_enabled": False,
                    },
                ],
            }
        )
        self.assertTrue(result["ok"])
        self.assertIn("ezlynx-password ENABLED versions/1", result["evidence"])

    def _link_db(
        self, tmp: str, *, status: JobStatus | str, active: int = 1
    ) -> tuple[str, str]:
        label = status.value if isinstance(status, JobStatus) else status
        db = str(Path(tmp) / f"jobs-{label}-{active}.db")
        store = JobStore(db)
        job = store.create_job("hermes.google_chat_task", {"text": "work"})
        stored = status.value if isinstance(status, JobStatus) else status
        with store.connect() as conn:
            conn.execute("UPDATE jobs SET status=? WHERE id=?", (stored, job["id"]))
        queue = DurableChatEventQueue(db)
        queue.link_conversation_job(
            conversation_id="spaces/AAQAZbLJO78",
            job_id=job["id"],
            message_id=f"spaces/AAQAZbLJO78/messages/{job['id']}",
            event_id=f"spaces/AAQAZbLJO78/messages/{job['id']}",
        )
        if active == 0:
            queue.deactivate_conversation("spaces/AAQAZbLJO78")
        return db, job["id"]

    def test_active_terminal_bind_is_no(self):
        with durable_temporary_directory() as tmp:
            for status in (JobStatus.FAILED, JobStatus.UNVERIFIED, "completed"):
                with self.subTest(status=status):
                    db, job_id = self._link_db(tmp, status=status)
                    result = check_conversation_job_links(db)
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["name"], CHECK_LINKS)
                    self.assertIn(job_id, result["evidence"])
                    self.assertIn("active=1", result["evidence"])

    def test_live_hitl_bind_is_yes(self):
        with durable_temporary_directory() as tmp:
            db, _job_id = self._link_db(
                tmp, status=JobStatus.AWAITING_HUMAN_INPUT
            )
            result = check_conversation_job_links(db)
            self.assertTrue(result["ok"])
            self.assertIn("no active=1 terminal bind", result["evidence"])

    def test_inactive_failed_bind_is_yes(self):
        with durable_temporary_directory() as tmp:
            db, _job_id = self._link_db(tmp, status=JobStatus.FAILED, active=0)
            result = check_conversation_job_links(db)
            self.assertTrue(result["ok"])


def _empty_jobs_db(tmp: str) -> str:
    db = str(Path(tmp) / "jobs-empty.db")
    JobStore(db)
    DurableChatEventQueue(db)
    return db


def _enqueue_chat_inbound(db: str, *, event_id: str = "spaces/s/messages/fresh") -> None:
    DurableChatEventQueue(db).enqueue(
        event_id=event_id,
        conversation_id="spaces/AAQAZbLJO78",
        message_id=event_id,
        payload={"action_type": "hermes.google_chat_task"},
    )


def _age_chat_inbound(db: str, *, hours: int) -> None:
    stamp = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE chat_event_queue SET created_at=?, updated_at=?",
            (stamp, stamp),
        )


WEDGED_JOURNAL = (
    "[GoogleChat] Connected; project=streetsmart-hermes-poc, inbound=pubsub\n"
    "RuntimeError: bound Job is not awaiting human input\n"
)


class ChatIntakeCheckTests(unittest.TestCase):
    def test_wedged_listener_is_no(self):
        with durable_temporary_directory() as tmp:
            db = _empty_jobs_db(tmp)
            result = check_chat_intake(db, journal=WEDGED_JOURNAL)
            self.assertFalse(result["ok"])
            self.assertEqual(result["name"], CHECK_CHAT_INTAKE)
            self.assertIn("wedged", result["evidence"])
            self.assertIn("hermes-gateway active", result["evidence"])
            self.assertIn("bound Job is not awaiting human input", result["evidence"])

    def test_silent_listener_without_inbound_is_no(self):
        with durable_temporary_directory() as tmp:
            db = _empty_jobs_db(tmp)
            result = check_chat_intake(db, journal="")
            self.assertFalse(result["ok"])
            self.assertEqual(result["name"], CHECK_CHAT_INTAKE)
            self.assertIn("silent", result["evidence"])
            self.assertIn("hermes-gateway active", result["evidence"])

    def test_recent_inbound_is_yes(self):
        with durable_temporary_directory() as tmp:
            db = _empty_jobs_db(tmp)
            _enqueue_chat_inbound(db)
            result = check_chat_intake(
                db,
                journal="[GoogleChat] Connected; inbound=pubsub\n",
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["name"], CHECK_CHAT_INTAKE)
            self.assertIn("last Chat inbound", result["evidence"])

    def test_recent_chat_job_without_queue_row_is_yes(self):
        with durable_temporary_directory() as tmp:
            db = _empty_jobs_db(tmp)
            JobStore(db).create_job("hermes.google_chat_task", {"text": "work"})
            result = check_chat_intake(
                db,
                journal="[GoogleChat] Connected; inbound=pubsub\n",
            )
            self.assertTrue(result["ok"])
            self.assertIn("last Chat inbound", result["evidence"])

    def test_recent_inbound_without_connect_marker_is_no(self):
        with durable_temporary_directory() as tmp:
            db = _empty_jobs_db(tmp)
            _enqueue_chat_inbound(db)
            result = check_chat_intake(db, journal="", gateway_log="")
            self.assertFalse(result["ok"])
            self.assertIn("silent", result["evidence"])
            self.assertIn(DEFAULT_GATEWAY_LOG, result["evidence"])

    def test_connected_idle_without_recent_inbound_is_info(self):
        with durable_temporary_directory() as tmp:
            db = _empty_jobs_db(tmp)
            _enqueue_chat_inbound(db)
            _age_chat_inbound(db, hours=20)
            result = check_chat_intake(
                db,
                journal="[GoogleChat] Connected; inbound=pubsub\n",
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["severity"], "INFO")
            self.assertIn("INFO: listener connected; no inbound since", result["evidence"])

    def test_quiet_inbox_uses_gateway_log_when_journal_has_no_marker(self):
        with durable_temporary_directory() as tmp:
            db = _empty_jobs_db(tmp)
            _enqueue_chat_inbound(db)
            _age_chat_inbound(db, hours=30)
            result = check_chat_intake(
                db,
                journal="",
                gateway_log=(
                    "2026-10-03 11:58:05,123 INFO [GoogleChat] Connected; "
                    "project=streetsmart-hermes-poc, inbound=pubsub\n"
                ),
                gateway_active=True,
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["severity"], "INFO")
            self.assertIn("listener connected; no inbound since", result["evidence"])

    def test_gateway_log_path_argument_and_env_are_used(self):
        with durable_temporary_directory() as tmp:
            db = _empty_jobs_db(tmp)
            log = Path(tmp) / "gateway.log"
            log.write_text(
                "11:58:05 INFO [GoogleChat] Connected; inbound=pubsub\n",
                encoding="utf-8",
            )
            by_path = check_chat_intake(
                db,
                journal="",
                gateway_log_path=log,
                gateway_active=True,
                now=datetime(2026, 10, 3, 16, 0, tzinfo=timezone.utc),
            )
            self.assertTrue(by_path["ok"])
            self.assertIn("listener connected; no inbound since never", by_path["evidence"])
            with patch.dict(os.environ, {"ROBIE_GATEWAY_LOG": str(log)}):
                by_env = check_chat_intake(
                    db,
                    journal_reader=lambda: "",
                    gateway_active=True,
                    now=datetime(2026, 10, 3, 16, 0, tzinfo=timezone.utc),
                )
            self.assertTrue(by_env["ok"])
            self.assertEqual(by_env["severity"], "INFO")

    def test_gateway_inactive_is_no_even_when_listener_connected(self):
        with durable_temporary_directory() as tmp:
            db = _empty_jobs_db(tmp)
            result = check_chat_intake(
                db,
                journal="[GoogleChat] Connected; inbound=pubsub\n",
                gateway_active=False,
            )
            self.assertFalse(result["ok"])
            self.assertIn("hermes-gateway inactive", result["evidence"])

    def test_disconnect_newer_than_connect_is_no(self):
        with durable_temporary_directory() as tmp:
            db = _empty_jobs_db(tmp)
            result = check_chat_intake(
                db,
                journal=(
                    "2026-10-03T10:00:00+00:00 [GoogleChat] Connected; "
                    "inbound=pubsub\n"
                ),
                gateway_log="2026-10-03T12:00:00+00:00 [GoogleChat] Disconnected\n",
                gateway_active=True,
            )
            self.assertFalse(result["ok"])
            self.assertIn("disconnected", result["evidence"])
            self.assertIn("newer than last connect", result["evidence"])

    def test_error_newer_than_connect_is_no(self):
        with durable_temporary_directory() as tmp:
            db = _empty_jobs_db(tmp)
            result = check_chat_intake(
                db,
                journal="",
                gateway_log=(
                    "2026-10-03T11:58:05+00:00 [GoogleChat] Connected; inbound=pubsub\n"
                    "2026-10-03T12:10:00+00:00 ERROR pubsub_reconnect_exhausted\n"
                ),
                gateway_active=True,
            )
            self.assertFalse(result["ok"])
            self.assertIn("wedged", result["evidence"])
            self.assertIn("pubsub_reconnect_exhausted", result["evidence"])

    def test_reconnect_after_disconnect_and_error_is_info(self):
        with durable_temporary_directory() as tmp:
            db = _empty_jobs_db(tmp)
            result = check_chat_intake(
                db,
                journal="2026-10-03T09:00:00+00:00 [GoogleChat] Disconnected\n",
                gateway_log=(
                    "2026-10-03T10:00:00+00:00 pubsub_reconnect_exhausted\n"
                    "2026-10-03T11:58:05+00:00 [GoogleChat] Connected; inbound=pubsub\n"
                ),
                gateway_active=True,
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["severity"], "INFO")
            self.assertIn("listener connected; no inbound since never", result["evidence"])

    def test_unreadable_gateway_log_and_no_marker_is_no(self):
        with durable_temporary_directory() as tmp:
            db = _empty_jobs_db(tmp)
            result = check_chat_intake(
                db,
                journal="",
                gateway_log_path=tmp,
                gateway_active=True,
            )
            self.assertFalse(result["ok"])
            self.assertIn("silent", result["evidence"])
            self.assertIn("unreadable", result["evidence"])

    def test_stale_inbound_plus_stall_is_no(self):
        with durable_temporary_directory() as tmp:
            db = _empty_jobs_db(tmp)
            _enqueue_chat_inbound(db)
            _age_chat_inbound(db, hours=1)
            result = check_chat_intake(db, journal=WEDGED_JOURNAL)
            self.assertFalse(result["ok"])
            self.assertIn("wedged", result["evidence"])

    def test_does_not_send_a_test_chat_job(self):
        self.assertNotIn("open_chat_job", SOURCE)
        self.assertNotIn("spaces.setup", SOURCE)
        self.assertNotIn("messages().create", SOURCE)


class FailNotifyTests(unittest.TestCase):
    def test_fail_notify_emails_are_carlo_and_jake(self):
        self.assertEqual(
            fail_notify_emails(),
            [
                "carlo@streetsmart.insurance",
                "jake@streetsmart.insurance",
            ],
        )
        self.assertEqual(
            DEFAULT_FAIL_NOTIFY_EMAILS,
            (
                "carlo@streetsmart.insurance",
                "jake@streetsmart.insurance",
            ),
        )

    def test_find_direct_message_uses_existing_poster_path(self):
        chat = Mock()
        chat.spaces.return_value.findDirectMessage.return_value.execute.return_value = {
            "name": "spaces/dm-carlo"
        }
        space = find_direct_message_space(
            "carlo@streetsmart.insurance", chat=chat
        )
        self.assertEqual(space, "spaces/dm-carlo")
        chat.spaces.return_value.findDirectMessage.assert_called_once_with(
            name="users/carlo@streetsmart.insurance"
        )
        chat.spaces.return_value.setup.assert_not_called()


class FailClosedRunTests(unittest.TestCase):
    def _dm_finder(self, email: str) -> str:
        return {
            "carlo@streetsmart.insurance": "spaces/dm-carlo",
            "jake@streetsmart.insurance": "spaces/dm-jake",
        }[email]

    def test_first_no_posts_space_and_operator_dms_and_stops(self):
        posted: list[tuple[str, str]] = []
        secrets_called = {"n": 0}

        def secrets():
            secrets_called["n"] += 1
            raise AssertionError("later checks must not run")

        report = run_production_preflight(
            gateway_probe=_gateway_ok(active=False, active_state="failed"),
            cdp_http_get=lambda url: (_ for _ in ()).throw(
                AssertionError(f"cdp {url}")
            ),
            secret_inspector=secrets,
            poster=lambda space, text: posted.append((space, text)),
            dm_finder=self._dm_finder,
        )
        self.assertFalse(report["ok"])
        self.assertEqual(report["failed_check"], CHECK_GATEWAY)
        self.assertEqual(len(report["checks"]), 1)
        self.assertEqual(secrets_called["n"], 0)
        self.assertEqual(
            [space for space, _text in posted],
            [DEFAULT_CHAT_SPACE, "spaces/dm-carlo", "spaces/dm-jake"],
        )
        self.assertEqual(posted[0][0], DEFAULT_CHAT_SPACE)
        self.assertIn("hermes-gateway", posted[0][1])
        self.assertNotIn("@robie", posted[0][1].casefold())
        self.assertTrue(report["chat_posted"])
        self.assertEqual(
            report["fail_notify_targets"],
            [DEFAULT_CHAT_SPACE, "spaces/dm-carlo", "spaces/dm-jake"],
        )
        self.assertEqual(report["fail_notify_dm_errors"], [])

    def test_space_post_survives_missing_dm(self):
        posted: list[str] = []

        def finder(email: str) -> str:
            raise RuntimeError(f"no DM for {email}")

        report = run_production_preflight(
            gateway_probe=_gateway_ok(active=False, active_state="failed"),
            poster=lambda space, text: posted.append(space),
            dm_finder=finder,
        )
        self.assertFalse(report["ok"])
        self.assertEqual(posted, [DEFAULT_CHAT_SPACE])
        self.assertTrue(report["chat_posted"])
        self.assertEqual(len(report["fail_notify_dm_errors"]), 2)

    def test_all_yes_does_not_post(self):
        posted: list[str] = []
        with durable_temporary_directory() as tmp:
            db, _job_id = SecretAndLinkCheckTests()._link_db(
                tmp, status=JobStatus.AWAITING_HUMAN_INPUT
            )
            report = run_production_preflight(
                gateway_probe=_gateway_ok(),
                cdp_http_get=_http(
                    {
                        "http://127.0.0.1:9222/json/version": (200, b"{}"),
                        "http://127.0.0.1:9222/json/list": (
                            200,
                            json.dumps(
                                [{"url": "https://app.ezlynx.com/web/policies"}]
                            ).encode(),
                        ),
                    }
                ),
                secret_inspector=_secret_ok,
                db_path=db,
                journal="[GoogleChat] Connected; inbound=pubsub\n",
                poster=lambda space, text: posted.append(text),
                dm_finder=self._dm_finder,
                chat_runtime_probe={
                    "name": CHECK_CHAT_RUNTIME,
                    "ok": True,
                    "evidence": "Chat load path equals zip",
                },
            )
        self.assertTrue(report["ok"])
        self.assertIsNone(report["failed_check"])
        self.assertEqual(len(report["checks"]), 7)
        self.assertEqual(
            [item["name"] for item in report["checks"]],
            [
                CHECK_GATEWAY,
                CHECK_CDP,
                CHECK_EZLYNX_TAB,
                CHECK_SECRETS,
                CHECK_LINKS,
                CHECK_CHAT_INTAKE,
                CHECK_CHAT_RUNTIME,
            ],
        )
        self.assertTrue(all(item["ok"] for item in report["checks"]))
        self.assertEqual(posted, [])
        self.assertFalse(report["chat_posted"])
        self.assertFalse(report["alert_delivery_failed"])
        self.assertIsNone(report["chat_post_error"])
        self.assertEqual(report["alert_targets"], [])


class AlertDeliveryTests(unittest.TestCase):
    def _dm_finder(self, email: str) -> str:
        return {
            "carlo@streetsmart.insurance": "spaces/dm-carlo",
            "jake@streetsmart.insurance": "spaces/dm-jake",
        }[email]

    def test_post_failure_records_error_targets_and_flag(self):
        def boom(space, text):
            raise RuntimeError("ROBIE_CHAT_SA_KEY_FILE is not set")

        report = run_production_preflight(
            gateway_probe=_gateway_ok(active=False, active_state="failed"),
            poster=boom,
            dm_finder=self._dm_finder,
        )
        self.assertFalse(report["ok"])
        self.assertFalse(report["chat_posted"])
        self.assertTrue(report["alert_delivery_failed"])
        self.assertIn("ROBIE_CHAT_SA_KEY_FILE is not set", report["chat_post_error"])
        self.assertIn(DEFAULT_CHAT_SPACE, report["alert_targets"])
        self.assertIn("dm:carlo@streetsmart.insurance", report["alert_targets"])
        self.assertIn("dm:jake@streetsmart.insurance", report["alert_targets"])

    def test_delivered_alert_is_not_flagged(self):
        report = run_production_preflight(
            gateway_probe=_gateway_ok(active=False, active_state="failed"),
            poster=lambda space, text: None,
            dm_finder=self._dm_finder,
        )
        self.assertFalse(report["ok"])
        self.assertTrue(report["chat_posted"])
        self.assertFalse(report["alert_delivery_failed"])
        self.assertIsNone(report["chat_post_error"])
        self.assertEqual(report["alert_targets"][0], DEFAULT_CHAT_SPACE)

    def test_startup_line_names_key_file_and_env_file(self):
        missing = "/etc/streetsmart-hermes/robie-recording.env"
        with patch.dict(os.environ, {"ROBIE_CHAT_SA_KEY_FILE": ""}, clear=False):
            os.environ.pop("ROBIE_CHAT_SA_KEY_FILE", None)
            line = format_preflight_startup([(missing, False)])
        self.assertIn("ROBIE_CHAT_SA_KEY_FILE=(unset)", line)
        self.assertIn(f"{missing} (missing)", line)
        self.assertIn("preflight startup:", line)
        present_key = "/etc/streetsmart-hermes/robie-chat-sa-key.json"
        with patch.dict(os.environ, {"ROBIE_CHAT_SA_KEY_FILE": present_key}):
            named = format_preflight_startup(
                [("/etc/streetsmart-hermes/hermes-email-watcher.env", True)]
            )
        self.assertIn(f"ROBIE_CHAT_SA_KEY_FILE={present_key}", named)
        self.assertIn("hermes-email-watcher.env (present)", named)
        with patch.dict(
            os.environ,
            {"ROBIE_CHAT_SA_KEY_FILE": '{"type":"service_account"}'},
        ):
            refused = format_preflight_startup([(DEFAULT_PREFLIGHT_ENV_FILE, False)])
        self.assertIn("(inline JSON refused)", refused)
        self.assertNotIn("service_account", refused)

    def test_empty_systemd_env_files_do_not_invent_recording_env(self):
        def runner(_cmd):
            return SimpleNamespace(stdout="", stderr="", returncode=0)

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ROBIE_PREFLIGHT_ENV_FILE", None)
            files = resolve_preflight_env_files(runner=runner)
        self.assertEqual(files, [])
        self.assertNotIn(DEFAULT_PREFLIGHT_ENV_FILE, [path for path, _exists in files])

    def test_main_prints_error_and_exits_3_when_alert_fails(self):
        report = {
            "ok": False,
            "failed_check": CHECK_CHAT_INTAKE,
            "chat_posted": False,
            "chat_post_error": "ChatAppIdentityError: ROBIE_CHAT_SA_KEY_FILE is not set",
            "alert_targets": [DEFAULT_CHAT_SPACE, "dm:carlo@streetsmart.insurance"],
            "alert_delivery_failed": True,
            "checks": [
                {
                    "name": CHECK_CHAT_INTAKE,
                    "ok": False,
                    "evidence": "hermes-gateway active; Chat listener silent",
                }
            ],
        }
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("sys.argv", ["production_preflight"]), patch(
            "robie_job_engine.production_preflight.run_production_preflight",
            return_value=report,
        ), patch(
            "robie_job_engine.production_preflight.format_preflight_startup",
            return_value=(
                "preflight startup: ROBIE_CHAT_SA_KEY_FILE=(unset); "
                "env file /etc/streetsmart-hermes/robie-recording.env (missing)"
            ),
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            code = main()
        self.assertEqual(code, EXIT_ALERT_DELIVERY_FAILED)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["chat_post_error"], report["chat_post_error"])
        self.assertEqual(payload["alert_targets"], report["alert_targets"])
        self.assertTrue(payload["alert_delivery_failed"])
        err = stderr.getvalue()
        self.assertIn("preflight startup:", err)
        self.assertIn("ROBIE_CHAT_SA_KEY_FILE=(unset)", err)
        self.assertIn("robie-recording.env (missing)", err)
        err_payload = json.loads(err.strip().splitlines()[-1])
        self.assertEqual(err_payload["chat_post_error"], report["chat_post_error"])
        self.assertEqual(err_payload["alert_targets"], report["alert_targets"])
        self.assertTrue(err_payload["alert_delivery_failed"])

    def test_main_exits_2_when_alert_is_delivered(self):
        report = {
            "ok": False,
            "failed_check": CHECK_GATEWAY,
            "chat_posted": True,
            "chat_post_error": None,
            "alert_targets": [DEFAULT_CHAT_SPACE],
            "alert_delivery_failed": False,
            "checks": [
                {"name": CHECK_GATEWAY, "ok": False, "evidence": "inactive"}
            ],
        }
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("sys.argv", ["production_preflight"]), patch(
            "robie_job_engine.production_preflight.run_production_preflight",
            return_value=report,
        ), patch(
            "robie_job_engine.production_preflight.format_preflight_startup",
            return_value="preflight startup: ROBIE_CHAT_SA_KEY_FILE=/key.json; env file /etc/x (present)",
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            code = main()
        self.assertEqual(code, EXIT_CHECK_FAILED)
        payload = json.loads(stdout.getvalue())
        self.assertIsNone(payload["chat_post_error"])
        self.assertEqual(payload["alert_targets"], [DEFAULT_CHAT_SPACE])
        self.assertFalse(payload["alert_delivery_failed"])

    def test_main_exits_0_and_still_prints_empty_alert_fields(self):
        report = {
            "ok": True,
            "failed_check": None,
            "chat_posted": False,
            "chat_post_error": None,
            "alert_targets": [],
            "alert_delivery_failed": False,
            "checks": [{"name": CHECK_GATEWAY, "ok": True, "evidence": "up"}],
        }
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("sys.argv", ["production_preflight"]), patch(
            "robie_job_engine.production_preflight.run_production_preflight",
            return_value=report,
        ), patch(
            "robie_job_engine.production_preflight.format_preflight_startup",
            return_value="preflight startup: ROBIE_CHAT_SA_KEY_FILE=(unset); env file /etc/x (missing)",
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            code = main()
        self.assertEqual(code, EXIT_OK)
        payload = json.loads(stdout.getvalue())
        self.assertIsNone(payload["chat_post_error"])
        self.assertEqual(payload["alert_targets"], [])
        self.assertFalse(payload["alert_delivery_failed"])
        self.assertIn("chat_post_error", stderr.getvalue())

    def test_health_check_sees_alert_delivery_failed(self):
        import importlib.util

        script = ROOT / "scripts" / "robie_health_check.py"
        spec = importlib.util.spec_from_file_location("robie_health_check_preflight", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        broken = json.dumps(
            {
                "ok": False,
                "chat_posted": False,
                "chat_post_error": "ChatAppIdentityError: ROBIE_CHAT_SA_KEY_FILE is not set",
                "alert_targets": [DEFAULT_CHAT_SPACE],
                "alert_delivery_failed": True,
            }
        )
        ok, detail, extra = module.check_preflight_alert_delivery(
            journal=f"Oct 03 12:00:00 host python: {broken}\n"
        )
        self.assertFalse(ok)
        self.assertIn("ROBIE_CHAT_SA_KEY_FILE is not set", detail)
        self.assertTrue(extra["alert_delivery_failed"])
        healthy = json.dumps(
            {
                "ok": False,
                "chat_posted": True,
                "chat_post_error": None,
                "alert_delivery_failed": False,
            }
        )
        ok, detail, _extra = module.check_preflight_alert_delivery(journal=healthy)
        self.assertTrue(ok)
        self.assertIn("ok", detail)
        ok, detail, _extra = module.check_preflight_alert_delivery(journal="")
        self.assertTrue(ok)
        self.assertIn("no preflight JSON", detail)
        legacy = json.dumps({"ok": False, "chat_posted": False, "failed_check": "cdp"})
        parsed = parse_preflight_alert_state(legacy)
        self.assertIsNone(parsed.get("alert_delivery_failed"))
        ok, detail, _extra = module.check_preflight_alert_delivery(journal=legacy)
        self.assertTrue(ok)
        unset = (
            "preflight startup: ROBIE_CHAT_SA_KEY_FILE=(unset); "
            "env file /etc/streetsmart-hermes/robie-recording.env (missing)\n"
            + json.dumps({"ok": True, "chat_posted": False, "alert_delivery_failed": False})
        )
        ok, detail, _extra = module.check_preflight_alert_delivery(journal=unset)
        self.assertFalse(ok)
        self.assertIn("ROBIE_CHAT_SA_KEY_FILE is unset", detail)
        later = (
            unset
            + "\npreflight startup: ROBIE_CHAT_SA_KEY_FILE=/etc/streetsmart-hermes/robie-chat-sa-key.json; "
            "env file /etc/streetsmart-hermes/hermes-email-watcher.env (present)\n"
        )
        ok, detail, _extra = module.check_preflight_alert_delivery(journal=later)
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
