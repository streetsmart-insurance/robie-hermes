"""Contract tests for ensure-gcloud-ssh-key.sh and Cloud Shell proof."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENSURE = ROOT / "scripts" / "ensure-gcloud-ssh-key.sh"
CLOUD = ROOT / "scripts" / "cloud-shell-restart-ssh-proof.sh"


class EnsureGcloudSshKeyContractTests(unittest.TestCase):
    def test_ensure_rotates_passphrase_keys(self):
        text = ENSURE.read_text(encoding="utf-8")
        self.assertIn("passphrase-backup", text)
        self.assertIn("google_compute_engine", text)
        self.assertIn("os-login ssh-keys add", text)

    def test_cloud_shell_proof_is_non_interactive(self):
        text = CLOUD.read_text(encoding="utf-8")
        self.assertIn("ensure-gcloud-ssh-key.sh", text)
        self.assertIn("BatchMode=yes", text)
        self.assertIn("ROBIE_CLOUD_SHELL_SSH_PROOF_BEGIN", text)
