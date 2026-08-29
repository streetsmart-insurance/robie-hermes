from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "deploy-test.yml"
INSTALLER = ROOT / "scripts" / "deploy-test-release.sh"


class TestDeployWorkflowContractTests(unittest.TestCase):
    def test_credentials_are_main_only_and_target_is_exact_test_vm(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", text)
        self.assertNotIn("pull_request:", text)
        self.assertNotIn("pull_request_target:", text)
        self.assertIn("github.ref == 'refs/heads/main'", text)
        self.assertIn("inputs.confirmation == 'DEPLOY_TO_HERMES_TEST_01'", text)
        self.assertIn("TEST_VM: hermes-test-01", text)
        self.assertIn("id-token: write", text)
        self.assertNotIn("hermes-poc-01", text)
        self.assertNotIn("systemctl restart hermes-gateway", text)

    def test_installer_fails_closed_and_preserves_rollback(self):
        text = INSTALLER.read_text(encoding="utf-8")
        self.assertIn('EXPECTED_HOST="hermes-test-01"', text)
        self.assertIn('GATEWAY_UNIT="robie-gateway"', text)
        self.assertIn('OPT_ROOT="/opt/streetsmart-hermes-test"', text)
        self.assertIn("mode=ro", text)
        self.assertIn("active Test jobs or leases exist; refuse deploy", text)
        self.assertIn("rollback_test", text)
        self.assertIn("atomic_pointer", text)
        self.assertIn('"production_touched": False', text)
        self.assertNotIn("/opt/streetsmart-hermes/", text)
        self.assertNotIn("hermes-poc-01", text)
        self.assertNotIn("rm -rf /opt", text)

    def test_test_proof_uses_test_gateway_and_cannot_complete_jobs(self):
        text = INSTALLER.read_text(encoding="utf-8")
        self.assertIn('--gateway-unit "${GATEWAY_UNIT}"', text)
        self.assertIn('data.get("authorizes_complete") is False', text)
        self.assertIn("TEST VERIFIED", text)


if __name__ == "__main__":
    unittest.main()
