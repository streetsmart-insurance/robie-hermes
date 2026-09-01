"""Contract tests for prove-test-production-secret-denial.sh."""

from __future__ import annotations

import unittest
from pathlib import Path


PROVE = Path(__file__).resolve().parents[1] / "scripts" / "prove-test-production-secret-denial.sh"
ENSURE = Path(__file__).resolve().parents[1] / "scripts" / "ensure-gcloud-ssh-key.sh"
CLOUD = Path(__file__).resolve().parents[1] / "scripts" / "cloud-shell-restart-ssh-proof.sh"


class ProveTestSecretDenialContractTests(unittest.TestCase):
    def test_uses_mktemp_and_captures_denied_exit_in_else(self):
        text = PROVE.read_text(encoding="utf-8")
        self.assertIn("mktemp", text)
        self.assertNotIn("/tmp/robie-deny-test.err", text)
        self.assertIn("else", text)
        self.assertIn("code=$?", text)
        self.assertIn("DENIED ${secret_id}: exit=${code}", text)

    def test_ensure_rotates_passphrase_keys_and_sets_gcloud_key_file(self):
        text = ENSURE.read_text(encoding="utf-8")
        self.assertIn("passphrase-backup", text)
        self.assertIn("google_compute_engine", text)

    def test_cloud_shell_proof_script_is_non_interactive(self):
        text = CLOUD.read_text(encoding="utf-8")
        self.assertIn("ensure-gcloud-ssh-key.sh", text)
        self.assertIn("BatchMode=yes", text)
        self.assertIn("ROBIE_CLOUD_SHELL_SSH_PROOF_BEGIN", text)
