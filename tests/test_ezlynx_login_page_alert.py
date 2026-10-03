from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from durable_temp import durable_temporary_directory
except ModuleNotFoundError:
    from tests.durable_temp import durable_temporary_directory

from robie_job_engine.ezlynx_login_page_alert import evaluate
from robie_job_engine.session_preflight import LOGGED_OUT


NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


class LoginPageAlertTests(unittest.TestCase):
    def setUp(self):
        self.tmp = durable_temporary_directory()
        self.state = Path(self.tmp.name) / "state.json"
        self.posts: list[str] = []

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def logged_out(_url: str) -> dict:
        return {"state": LOGGED_OUT}

    @staticmethod
    def authenticated(_url: str) -> dict:
        return {"state": "SESSION_PRESENT"}

    def test_alerts_once_after_fifteen_minutes(self):
        first = evaluate(
            state_path=self.state, now=NOW, checker=self.logged_out, poster=self.posts.append
        )
        self.assertEqual(first["status"], "PENDING")
        alert = evaluate(
            state_path=self.state,
            now=NOW + timedelta(minutes=15),
            checker=self.logged_out,
            poster=self.posts.append,
        )
        again = evaluate(
            state_path=self.state,
            now=NOW + timedelta(minutes=20),
            checker=self.logged_out,
            poster=self.posts.append,
        )
        self.assertEqual(alert["status"], "ALERTED")
        self.assertEqual(again["status"], "ALREADY_ALERTED")
        self.assertEqual(len(self.posts), 1)

    def test_recovery_clears_episode(self):
        evaluate(state_path=self.state, now=NOW, checker=self.logged_out, poster=self.posts.append)
        result = evaluate(
            state_path=self.state,
            now=NOW + timedelta(minutes=2),
            checker=self.authenticated,
            poster=self.posts.append,
        )
        self.assertEqual(result["status"], "CLEAR")
        self.assertFalse(self.state.exists())


if __name__ == "__main__":
    unittest.main()
