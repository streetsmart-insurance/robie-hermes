"""Unit tests for JE-KILL live preflight blockers."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from robie_job_engine.je_kill_preflight import (
    ezlynx_auth_tab_errors,
    fixture_approval_errors,
)


class JeKillPreflightTests(unittest.TestCase):
    def _fixture(self, *, approved_at: str) -> dict:
        return {
            "test_only": True,
            "disposable": True,
            "approved_by": "Carlo Ferrara",
            "approval_scope": "JE-KILL-01",
            "approved_at": approved_at,
            "scenarios": {
                "before_action": {"account_id": "220250093", "resource_id": "1"},
                "after_action": {"account_id": "220250093", "resource_id": "2"},
                "during_verification": {"account_id": "220250093", "resource_id": "3"},
            },
        }

    def test_fresh_approval_passes(self):
        now = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
        data = self._fixture(approved_at="2026-09-12T12:00:00Z")
        self.assertEqual(fixture_approval_errors(data, now=now), [])

    def test_expired_approval_blocks(self):
        now = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
        data = self._fixture(approved_at="2026-08-30T12:00:00Z")
        errors = fixture_approval_errors(data, now=now)
        self.assertTrue(any("expired" in e for e in errors))
        self.assertTrue(any("refresh-je-kill-fixture-approval.sh" in e for e in errors))

    def test_login_tab_blocks(self):
        tabs = [
            {
                "type": "page",
                "url": "https://app.ezlynx.com/auth/account/login?redirectURL=x",
                "title": "Login",
            }
        ]
        errors = ezlynx_auth_tab_errors(tabs)
        self.assertTrue(any("login page" in e for e in errors))

    def test_authenticated_tab_passes(self):
        tabs = [
            {
                "type": "page",
                "url": "https://app.ezlynx.com/web/account/220250093/documents",
                "title": "Documents",
            }
        ]
        self.assertEqual(ezlynx_auth_tab_errors(tabs), [])


if __name__ == "__main__":
    unittest.main()
