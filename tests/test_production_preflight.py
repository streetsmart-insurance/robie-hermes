"""Production pre-flight: seven yes/no checks. Chat on the first no only."""

from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

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
    check_chat_intake,
    check_conversation_job_links,
    check_ezlynx_tab,
    check_hermes_gateway,
    check_login_secrets,
    ezlynx_web_tab_ok,
    format_failure,
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
            result = check_chat_intake(db, journal="")
            self.assertTrue(result["ok"])
            self.assertEqual(result["name"], CHECK_CHAT_INTAKE)
            self.assertIn("last Chat inbound", result["evidence"])

    def test_recent_chat_job_without_queue_row_is_yes(self):
        with durable_temporary_directory() as tmp:
            db = _empty_jobs_db(tmp)
            JobStore(db).create_job("hermes.google_chat_task", {"text": "work"})
            result = check_chat_intake(db, journal="")
            self.assertTrue(result["ok"])
            self.assertIn("last Chat inbound", result["evidence"])

    def test_connected_idle_without_recent_inbound_is_yes(self):
        with durable_temporary_directory() as tmp:
            db = _empty_jobs_db(tmp)
            _enqueue_chat_inbound(db)
            _age_chat_inbound(db, hours=20)
            result = check_chat_intake(
                db,
                journal="[GoogleChat] Connected; inbound=pubsub\n",
            )
            self.assertTrue(result["ok"])
            self.assertIn("connected", result["evidence"].casefold())

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


if __name__ == "__main__":
    unittest.main()
