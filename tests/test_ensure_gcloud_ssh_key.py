"""Contract tests for ensure-gcloud-ssh-key.sh and Cloud Shell proof."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENSURE = ROOT / "scripts" / "ensure-gcloud-ssh-key.sh"
CLOUD = ROOT / "scripts" / "cloud-shell-restart-ssh-proof.sh"
GRANT_DEPLOYER = ROOT / "scripts" / "grant-test-deployer-iap-ssh.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "diagnose-test-iap-ssh.yml"
BOOTSTRAP = ROOT / "docs" / "TEST_DEPLOYER_IAP_SSH_BOOTSTRAP.md"


class EnsureGcloudSshKeyContractTests(unittest.TestCase):
    def test_ensure_rotates_passphrase_keys_and_uses_short_ttl(self):
        text = ENSURE.read_text(encoding="utf-8")
        self.assertIn("passphrase-backup", text)
        self.assertIn("google_compute_engine", text)
        self.assertIn("os-login ssh-keys add", text)
        self.assertIn("OSLOGIN_SSH_KEY_TTL", text)
        self.assertIn('--ttl="${OSLOGIN_SSH_KEY_TTL}"', text)
        self.assertIn("1h", text)

    def test_cloud_shell_proof_is_non_interactive(self):
        text = CLOUD.read_text(encoding="utf-8")
        self.assertIn("ensure-gcloud-ssh-key.sh", text)
        self.assertIn("BatchMode=yes", text)
        self.assertIn("ROBIE_CLOUD_SHELL_SSH_PROOF_BEGIN", text)

    def test_deployer_grant_script_uses_iap_tunnel_api_not_compute_iap(self):
        text = GRANT_DEPLOYER.read_text(encoding="utf-8")
        self.assertIn("roles/iap.tunnelResourceAccessor", text)
        self.assertIn("roles/compute.osAdminLogin", text)
        self.assertIn("roles/iam.serviceAccountUser", text)
        self.assertIn("rollback", text)
        self.assertNotIn("keys create", text)
        self.assertNotIn("0.0.0.0/0", text)
        self.assertIn('"${VM}" != "hermes-test-01"', text)
        # IAP must be granted on the IAP tunnel instance resource.
        self.assertIn("iap.googleapis.com/v1/projects/", text)
        self.assertIn("iap_tunnel/zones/", text)
        self.assertIn("setIamPolicy", text)
        self.assertIn("HTTP 400", text)
        # Never bind IAP via Compute instance IAM (Carlo's 400).
        for i, line in enumerate(text.splitlines()):
            if "compute instances add-iam-policy-binding" in line:
                window = "\n".join(text.splitlines()[i : i + 5])
                self.assertNotIn("iap.tunnelResourceAccessor", window)

    def test_bootstrap_doc_records_fresh_runner_proof(self):
        text = BOOTSTRAP.read_text(encoding="utf-8")
        self.assertIn("34745695554", text)
        self.assertIn("github-test-deployer-ssh", text)
        self.assertIn("6971056864475829887", text)

    def test_iap_ssh_proof_workflow_is_main_only(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("PROVE_TEST_IAP_SSH", text)
        self.assertIn("refs/heads/main", text)
        self.assertIn("id-token: write", text)
        self.assertIn("hostname -s", text)
        self.assertIn("sha256sum -c", text)
        self.assertIn("robie-test-deployer@streetsmart-robie-test.iam.gserviceaccount.com", text)
        self.assertNotIn("pull_request", text)
