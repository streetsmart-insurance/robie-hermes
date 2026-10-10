"""Contract tests for HERMES infrastructure runbook."""

from __future__ import annotations

import unittest
from pathlib import Path


RUNBOOK = Path(__file__).resolve().parents[1] / "docs" / "HERMES_INFRA_RUNBOOK.md"


class InfraRunbookContractTests(unittest.TestCase):
    def test_runbook_covers_recovery_rotation_and_deploy(self):
        text = RUNBOOK.read_text(encoding="utf-8")
        self.assertIn("## 3. Deploy process", text)
        self.assertIn("## 4. Secret rotation", text)
        self.assertIn("## 5. Recovery", text)
        self.assertIn("provision-secrets.sh", text)
        self.assertIn("prove-hermes-poc-snapshot-restore.sh", text)
        self.assertIn("deploy-test-release.sh", text)
        self.assertIn("deploy-production-release.sh", text)
        self.assertIn("hermes-test-01", text)
        self.assertIn("hermes-poc-01", text)
