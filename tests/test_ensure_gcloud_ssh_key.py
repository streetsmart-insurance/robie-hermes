"""Contract tests for ensure-gcloud-ssh-key.sh and Cloud Shell proof."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENSURE = ROOT / "scripts" / "ensure-gcloud-ssh-key.sh"
CLOUD = ROOT / "scripts" / "cloud-shell-restart-ssh-proof.sh"
GRANT_DEPLOYER = ROOT / "scripts" / "grant-test-deployer-iap-ssh.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "diagnose-test-iap-ssh.yml"


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

    def test_deployer_grant_script_has_rollback(self):
        text = GRANT_DEPLOYER.read_text(encoding="utf-8")
        self.assertIn("roles/iap.tunnelResourceAccessor", text)
        self.assertIn("roles/compute.osAdminLogin", text)
        self.assertIn("roles/iam.serviceAccountUser", text)
        self.assertIn("rollback", text)
        self.assertNotIn("keys create", text)
        self.assertNotIn("0.0.0.0/0", text)
        # IAP must be instance-scoped; project-level would open hermes-poc-01.
        self.assertIn(
            'gcloud compute instances add-iam-policy-binding "${VM}"',
            text,
        )
        self.assertIn('"${VM}" != "hermes-test-01"', text)
        for i, line in enumerate(text.splitlines()):
            if "projects add-iam-policy-binding" in line:
                window = "\n".join(text.splitlines()[i : i + 5])
                self.assertNotIn("iap.tunnelResourceAccessor", window)

    def test_iap_ssh_proof_workflow_is_main_only(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("PROVE_TEST_IAP_SSH", text)
        self.assertIn("refs/heads/main", text)
        self.assertIn("id-token: write", text)
        self.assertIn("hostname -s", text)
        self.assertIn("sha256sum -c", text)
        self.assertIn("robie-test-deployer@streetsmart-robie-test.iam.gserviceaccount.com", text)
        self.assertNotIn("pull_request", text)
