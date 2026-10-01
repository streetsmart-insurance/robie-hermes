"""Unit tests for Test SSRobie keep-alive classification (no live CDP)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from robie_job_engine import ssrobie_keepalive as ka
from robie_job_engine.ezlynx_driver_gate import EzlynxDriverGateRefused
from robie_job_engine.session_preflight import LOGGED_OUT, NO_EZLYNX_TAB, SESSION_PRESENT


class TestSsrobieKeepaliveTests(unittest.TestCase):
    def test_driver_gate_refusal_prevents_even_cdp_check(self):
        with patch.object(
            ka,
            "require_driver_in",
            side_effect=EzlynxDriverGateRefused("EZLYNX_DRIVER_NOT_IN: Production owns it"),
        ), patch.object(ka, "check") as preflight:
            result = ka.run_keepalive()
        self.assertEqual(result["verdict"], "DRIVER_NOT_IN")
        self.assertEqual(result["exit_code"], ka.EXIT_DRIVER_NOT_IN)
        preflight.assert_not_called()

    def test_login_tab_is_needs_auth(self):
        report = {
            "state": LOGGED_OUT,
            "pages": [
                {
                    "title": "Login",
                    "url": "https://app.ezlynx.com/auth/account/login",
                }
            ],
        }
        self.assertEqual(ka.cdp_auth_verdict(report), "NEEDS_AUTH")

    def test_web_dashboard_is_authenticated(self):
        report = {
            "state": SESSION_PRESENT,
            "pages": [
                {
                    "title": "Dashboard",
                    "url": "https://app.ezlynx.com/web/dashboard",
                }
            ],
        }
        self.assertEqual(ka.cdp_auth_verdict(report), "AUTHENTICATED")

    def test_blank_only_should_restore(self):
        report = {
            "state": NO_EZLYNX_TAB,
            "pages": [{"title": "", "url": "about:blank"}],
        }
        self.assertEqual(ka.cdp_auth_verdict(report), "BLANK")
        self.assertTrue(ka.should_restore_blank(report))

    def test_keepalive_skips_when_lease_busy(self):
        with patch.object(ka, "check", return_value={
            "state": SESSION_PRESENT,
            "pages": [
                {
                    "title": "Dashboard",
                    "url": "https://app.ezlynx.com/web/dashboard",
                }
            ],
        }), patch.object(ka, "has_blocking_test_leases", return_value=True), patch.object(
            ka, "navigate_existing_page"
        ) as nav:
            result = ka.run_keepalive(warm=True)
        self.assertEqual(result["action"], "skip_busy_lease")
        self.assertEqual(result["exit_code"], ka.EXIT_OK)
        nav.assert_not_called()

    def test_keepalive_restores_blank(self):
        blank = {
            "state": NO_EZLYNX_TAB,
            "pages": [{"title": "", "url": "about:blank"}],
        }
        after = {
            "state": SESSION_PRESENT,
            "pages": [
                {
                    "title": "Dashboard",
                    "url": "https://app.ezlynx.com/web/dashboard",
                }
            ],
        }
        with patch.object(ka, "check", side_effect=[blank, after]), patch.object(
            ka, "has_blocking_test_leases", return_value=False
        ), patch.object(
            ka,
            "navigate_existing_page",
            return_value={
                "ok": True,
                "logged_out": False,
                "final_url": "https://app.ezlynx.com/web/dashboard",
                "title": "Dashboard",
            },
        ):
            result = ka.run_keepalive(warm=False)
        self.assertEqual(result["action"], "restore_from_blank")
        self.assertEqual(result["verdict"], "AUTHENTICATED")
        self.assertEqual(result["exit_code"], ka.EXIT_OK)

    def test_main_exit_zero_when_authenticated(self):
        with patch.object(
            ka,
            "run_keepalive",
            return_value={"exit_code": 0, "verdict": "AUTHENTICATED", "action": "none"},
        ):
            self.assertEqual(ka.main(["--json"]), 0)


if __name__ == "__main__":
    unittest.main()
