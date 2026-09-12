"""Contract tests for the sole EZLynx session owner.

Carlo 2026-09-12: collapse three partial owners into
``.github/workflows/monitor-ezlynx-session.yml``. These tests prove the
workflow shape in-repo. They do not SSH, deploy, or touch the live browser.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "monitor-ezlynx-session.yml"
SYSTEMD = ROOT / "deploy" / "systemd"
PASSWORD_LITERAL = re.compile(
    r"""(?i)(?:password|passwd|secret)\s*[:=]\s*['\"][^'\"]+['\"]"""
)


class MonitorEzlynxSessionWorkflowTests(unittest.TestCase):
    def test_workflow_is_hourly_seven_days_and_heals_logged_out(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn('cron: "0 * * * *"', text)
        self.assertIn("check_ezlynx_session.py", text)
        self.assertIn("--json", text)
        self.assertIn("--probe", text)
        self.assertIn("ezlynx_login_bootstrap.py", text)
        self.assertIn("exit_code == '2'", text)
        self.assertIn("exit_code == '1'", text)
        self.assertIn("Do not login-guess", text)
        self.assertIn("scripts/ezlynx_session_monitor.py", text)
        self.assertIn("/var/tmp/robie-ezlynx-session-monitor/last-check.json", text)
        self.assertIn("ActiveEnterTimestamp", text)
        self.assertIn("ExecMainStartTimestamp", text)
        self.assertIn("MainPID", text)
        self.assertIn("attempt_login", text)
        self.assertIn("fail_consecutive", text)
        self.assertIn("One login attempt per check", text)
        self.assertIn("logout_cause is UNVERIFIED", text)
        self.assertIn("LOGIN CAP UNEVALUATED", text)
        self.assertIn("cap_state", text)
        self.assertNotIn("systemctl restart", text)
        self.assertNotIn("chrome_refresh_if_idle", text)

    def test_workflow_keeps_existing_iap_ssh_and_pins_gcloud(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("environment: Production", text)
        self.assertIn("id-token: write", text)
        self.assertIn(
            "service_account: robie-production-deployer@streetsmart-hermes-poc",
            text,
        )
        self.assertIn(
            "workloadIdentityPools/github-production/providers/github-main",
            text,
        )
        self.assertIn("hermes-poc-01", text)
        self.assertIn("streetsmart-hermes-poc", text)
        self.assertIn("us-east1-b", text)
        self.assertIn("--tunnel-through-iap", text)
        self.assertIn("gcloud compute ssh", text)
        self.assertIn("584.0.0", text)
        self.assertIn("quote_from_bytes", text)
        self.assertIn("127.0.0.1:9222", text)

    def test_workflow_has_no_password_literals_or_secret_payloads(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIsNone(PASSWORD_LITERAL.search(text))
        self.assertNotIn("secretmanager.access", text)
        self.assertNotIn("gcloud secrets versions access", text)
        self.assertNotIn("ROBIE_EZLYNX_PASSWORD", text)
        # Secret names may appear as documentation; values must not.
        self.assertIn("ezlynx-username", text)
        self.assertIn("ezlynx-password", text)
        self.assertNotRegex(text, r"ezlynx-password['\"]?\s*[:=]\s*['\"][^'\"]+")

    def test_retired_owners_in_tree_or_never_installed(self):
        chrome_timer = (SYSTEMD / "robie-chrome-refresh.timer").read_text(encoding="utf-8")
        chrome_service = (SYSTEMD / "robie-chrome-refresh.service").read_text(encoding="utf-8")
        for text in (chrome_timer, chrome_service):
            self.assertIn("RETIRED. Do not enable.", text)
            self.assertIn("restart without a trailing login is the bug", text)
            self.assertIn("systemctl disable --now", text)
            self.assertIn("Do not delete the live unit", text)
        self.assertFalse((SYSTEMD / "robie-ezlynx-session.timer").exists())
        self.assertFalse((SYSTEMD / "robie-ezlynx-session.service").exists())
        scheduler = (SYSTEMD / "robie-scheduler.service").read_text(encoding="utf-8")
        self.assertNotIn("Environment=ROBIE_ENABLE_EZLYNX_SESSION_REFRESH=1", scheduler)


if __name__ == "__main__":
    unittest.main()
