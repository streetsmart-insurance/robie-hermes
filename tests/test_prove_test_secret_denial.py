"""Contract tests for prove-test-production-secret-denial.sh."""

from __future__ import annotations

import unittest
from pathlib import Path


PROVE = Path(__file__).resolve().parents[1] / "scripts" / "prove-test-production-secret-denial.sh"


class ProveTestSecretDenialContractTests(unittest.TestCase):
    def test_uses_mktemp_and_captures_denied_exit_in_else(self):
        text = PROVE.read_text(encoding="utf-8")
        self.assertIn("mktemp", text)
        self.assertNotIn("/tmp/robie-deny-test.err", text)
        self.assertIn("else", text)
        self.assertIn("code=$?", text)
        self.assertIn("DENIED ${secret_id}: exit=${code}", text)
        self.assertIn("PASS: Production secrets denied", text)
