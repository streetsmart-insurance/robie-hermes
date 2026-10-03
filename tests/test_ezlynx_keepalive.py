"""Lease-gated EZLynx keepalive. No browser, no metadata server, no login."""

from __future__ import annotations

import contextlib
import inspect
import os
import re
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from robie_job_engine import ezlynx_keepalive as ka
from robie_job_engine import session_preflight
from robie_job_engine.ezlynx_driver_gate import DriverDecision
from robie_job_engine.session_preflight import LOGGED_OUT, SESSION_PRESENT

ROOT = Path(__file__).resolve().parents[1]


def _allowed() -> DriverDecision:
    return DriverDecision(True, "TEST", "driver is IN")


def _blocked() -> DriverDecision:
    return DriverDecision(False, "TEST", "driver belongs to PRODUCTION")


@contextlib.contextmanager
def _free_lock(_path: str):
    yield True


class EzlynxKeepaliveGateTests(unittest.TestCase):
    def test_session_preflight_module_is_the_cdp_classifier(self):
        self.assertTrue(callable(session_preflight.check))
        self.assertTrue(str(session_preflight.__file__).endswith("session_preflight.py"))
        self.assertIn("from .session_preflight import", inspect.getsource(ka))

    def test_skips_when_gate_is_not_allowed(self):
        def forbidden(*_args, **_kwargs):
            raise AssertionError("browser was touched")

        result = ka.run_keepalive(
            profile="TEST",
            gate=_blocked,
            holds_browser=forbidden,
            checker=forbidden,
            touch=forbidden,
            lock=forbidden,
        )
        self.assertEqual(result["gate"], "BLOCKED")
        self.assertEqual(result["action"], "skip_driver_gate")
        self.assertEqual(result["verdict"], "DRIVER_NOT_IN")
        self.assertEqual(result["exit_code"], ka.EXIT_OK)
        self.assertFalse(result["logged_in"])
        self.assertFalse(result["posted_chat"])
        self.assertEqual(result["closed_tabs"], 0)

    def test_skips_when_a_job_holds_the_browser(self):
        def touch(*_args, **_kwargs):
            raise AssertionError("touch")

        result = ka.run_keepalive(
            profile="PRODUCTION",
            gate=_allowed,
            holds_browser=lambda _path: True,
            checker=touch,
            touch=touch,
            lock=touch,
        )
        self.assertEqual(result["action"], "skip_busy_lease")
        self.assertEqual(result["exit_code"], ka.EXIT_OK)
        self.assertFalse(result["logged_in"])

    def test_skips_when_the_session_lock_is_held(self):
        def touch(*_args, **_kwargs):
            raise AssertionError("touch")

        @contextlib.contextmanager
        def busy(_path: str):
            yield False

        result = ka.run_keepalive(
            profile="TEST",
            gate=_allowed,
            holds_browser=lambda _path: False,
            lock=busy,
            checker=touch,
            touch=touch,
        )
        self.assertEqual(result["action"], "skip_browser_lock")
        self.assertFalse(result["logged_in"])
        self.assertEqual(result["closed_tabs"], 0)

    def test_logged_out_tab_never_logs_in_or_touches(self):
        def touch(*_args, **_kwargs):
            raise AssertionError("touch")

        def login(*_args, **_kwargs):
            raise AssertionError("login")

        with patch("robie_job_engine.ezlynx_session.ensure_ezlynx_session", login), patch(
            "robie_job_engine.ezlynx_session.PlaywrightEzlynxSession.login", login
        ):
            result = ka.run_keepalive(
                profile="PRODUCTION",
                gate=_allowed,
                holds_browser=lambda _path: False,
                lock=_free_lock,
                checker=lambda _url: {"state": LOGGED_OUT, "pages": []},
                touch=touch,
            )
        self.assertEqual(result["verdict"], "LOGGED_OUT")
        self.assertEqual(result["exit_code"], ka.EXIT_LOGGED_OUT)
        self.assertFalse(result["logged_in"])
        self.assertFalse(result["posted_chat"])
        self.assertEqual(result["closed_tabs"], 0)
        self.assertIn("did not log in", result["hint"])

    def test_authenticated_touch_does_not_close_tabs_or_post_chat(self):
        def login(*_args, **_kwargs):
            raise AssertionError("login")

        def touch(_url, _dashboard):
            return {
                "ok": True,
                "logged_out": False,
                "closed_tabs": 0,
                "created_page": False,
                "final_url": ka.DASHBOARD_URL,
            }

        with patch("robie_job_engine.ezlynx_session.ensure_ezlynx_session", login):
            result = ka.run_keepalive(
                profile="TEST",
                gate=_allowed,
                holds_browser=lambda _path: False,
                lock=_free_lock,
                checker=lambda _url: {"state": SESSION_PRESENT, "pages": []},
                touch=touch,
            )
        self.assertEqual(result["action"], "touch_existing_page")
        self.assertEqual(result["verdict"], "AUTHENTICATED")
        self.assertEqual(result["exit_code"], ka.EXIT_OK)
        self.assertFalse(result["logged_in"])
        self.assertFalse(result["posted_chat"])
        self.assertEqual(result["closed_tabs"], 0)
        self.assertEqual(result["gate"], "ALLOWED")

    def test_touch_that_lands_on_login_does_not_log_in(self):
        result = ka.run_keepalive(
            profile="TEST",
            gate=_allowed,
            holds_browser=lambda _path: False,
            lock=_free_lock,
            checker=lambda _url: {"state": SESSION_PRESENT, "pages": []},
            touch=lambda *_args, **_kwargs: {
                "ok": False,
                "logged_out": True,
                "closed_tabs": 0,
                "final_url": "https://app.ezlynx.com/auth/account/login",
            },
        )
        self.assertEqual(result["verdict"], "LOGGED_OUT")
        self.assertFalse(result["logged_in"])
        self.assertEqual(result["closed_tabs"], 0)

    def test_touch_implementation_does_not_close_or_create_tabs(self):
        source = inspect.getsource(ka.touch_existing_page)
        for banned in ("page.close", "new_page", ".close(", "fill(", "ensure_ezlynx_session", "urlopen"):
            self.assertNotIn(banned, source)

    def test_legacy_test_entrypoint_selects_the_test_profile(self):
        from robie_job_engine import ssrobie_keepalive

        with patch.dict(os.environ, {}, clear=False), patch(
            "robie_job_engine.ezlynx_keepalive.main", return_value=0
        ) as shared:
            os.environ.pop("ROBIE_ENV", None)
            code = ssrobie_keepalive.main(["--json"])
            self.assertEqual(os.environ.get("ROBIE_ENV"), "TEST")
        self.assertEqual(code, 0)
        shared.assert_called_once()


class EzlynxKeepaliveLeaseTests(unittest.TestCase):
    def _db(self, path: Path) -> None:
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE jobs (status TEXT, lease_owner TEXT, lease_expires_at TEXT)"
        )
        conn.commit()
        conn.close()

    def test_running_job_holds_the_browser(self):
        with self._temp() as path:
            self._db(path)
            conn = sqlite3.connect(path)
            conn.execute(
                "INSERT INTO jobs VALUES ('RUNNING', 'owner', '2000-01-01T00:00:00+00:00')"
            )
            conn.commit()
            conn.close()
            self.assertTrue(ka.job_holds_browser_or_lease(str(path)))

    def test_expired_lease_on_a_finished_job_does_not_hold(self):
        with self._temp() as path:
            self._db(path)
            conn = sqlite3.connect(path)
            conn.execute(
                "INSERT INTO jobs VALUES ('COMPLETE', 'owner', '2099-01-01T00:00:00+00:00')"
            )
            conn.commit()
            conn.close()
            self.assertFalse(ka.job_holds_browser_or_lease(str(path)))

    def test_live_lease_holds_even_when_status_is_queued(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        with self._temp() as path:
            self._db(path)
            conn = sqlite3.connect(path)
            conn.execute("INSERT INTO jobs VALUES (?, ?, ?)", ("QUEUED", "owner", future))
            conn.commit()
            conn.close()
            self.assertTrue(ka.job_holds_browser_or_lease(str(path)))

    def test_missing_database_is_not_a_hold(self):
        self.assertFalse(ka.job_holds_browser_or_lease(os.path.join(os.getcwd(), "missing-robie-jobs.db")))

    @contextlib.contextmanager
    def _temp(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            yield Path(tmp) / "jobs.db"


class EzlynxKeepaliveUnitTests(unittest.TestCase):
    def test_timers_are_at_most_twenty_minutes_and_units_do_not_log_in(self):
        for service_rel, profile, timer_rel in (
            (
                "deploy/systemd/robie-ezlynx-keepalive.service",
                "PRODUCTION",
                "deploy/systemd/robie-ezlynx-keepalive.timer",
            ),
            (
                "deploy/systemd/robie-ezlynx-keepalive-test.service",
                "TEST",
                "deploy/systemd/robie-ezlynx-keepalive-test.timer",
            ),
        ):
            service = (ROOT / service_rel).read_text()
            timer = (ROOT / timer_rel).read_text()
            self.assertIn("-m robie_job_engine.ezlynx_keepalive", service)
            self.assertIn("ezlynx_driver_gate check", service)
            self.assertIn(f"ROBIE_ENV={profile}", service)
            self.assertNotIn("ezlynx_session", service)
            match = re.search(r"OnUnitActiveSec=(\d+)min", timer)
            self.assertIsNotNone(match)
            self.assertLessEqual(int(match.group(1)), 20)


if __name__ == "__main__":
    unittest.main()
