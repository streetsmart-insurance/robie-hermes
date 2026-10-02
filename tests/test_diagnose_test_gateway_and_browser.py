from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "diagnose-test-gateway-and-browser.yml"


class TestGatewayBrowserWorkflow(unittest.TestCase):
    def test_workflow_is_protected_main_test_only_and_read_only(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", text)
        self.assertNotIn("pull_request:", text)
        self.assertIn("github.ref == 'refs/heads/main'", text)
        self.assertIn("DIAGNOSE_TEST_GATEWAY_AND_BROWSER", text)
        self.assertIn("TEST_VM: hermes-test-01", text)
        self.assertNotIn("hermes-poc-01", text)
        self.assertNotIn("systemctl restart", text)
        self.assertNotIn("systemctl stop", text)
        self.assertNotIn("kill ", text)
        self.assertNotIn("rm -", text)

    def test_workflow_streams_bounded_collector_and_retains_failed_evidence(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertNotIn("systemctl cat", text)
        self.assertNotIn("pgrep -fa", text)
        self.assertNotIn("exit 0", text)
        self.assertIn("set -euo pipefail", text)
        self.assertIn("< scripts/diagnose_test_release_readonly.py", text)
        self.assertIn("python3 -I -B -", text)
        self.assertIn("OSLOGIN_SSH_KEY_TTL: 1h", text)
        self.assertIn("inputs.temporary_ssh_key_approved == true", text)
        self.assertIn("if: always()", text)
        self.assertIn("test-release-snapshot.json", text)
