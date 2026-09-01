"""Contract tests for IAM review artifacts."""

from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED = ROOT / "deploy" / "iam" / "expected-minimum-access.json"
AUDIT = ROOT / "scripts" / "audit-hermes-iam.sh"
HARDEN = ROOT / "scripts" / "harden-hermes-iam.sh"
PROVE = ROOT / "scripts" / "prove-test-production-secret-denial.sh"


class IamReviewContractTests(unittest.TestCase):
    def test_expected_minimum_access_lists_hermes_identities(self):
        body = json.loads(EXPECTED.read_text(encoding="utf-8"))
        emails = {sa["email"] for sa in body["service_accounts"]}
        self.assertIn("hermes-poc@streetsmart-hermes-poc.iam.gserviceaccount.com", emails)
        self.assertIn(
            "robie-test-drive-reader@streetsmart-hermes-poc.iam.gserviceaccount.com",
            emails,
        )
        test_sa = next(
            sa for sa in body["service_accounts"]
            if sa["email"].startswith("robie-test-drive-reader@")
        )
        self.assertIn("ezlynx-username", test_sa["forbidden_secret_accessor"])

    def test_scripts_reference_iam_audit_and_hardening(self):
        audit = AUDIT.read_text(encoding="utf-8")
        harden = HARDEN.read_text(encoding="utf-8")
        prove = PROVE.read_text(encoding="utf-8")
        self.assertIn("get-iam-policy", audit)
        self.assertIn("account-role-summary", audit)
        self.assertIn("default-allow-ssh", harden)
        self.assertIn("ezlynx-username", prove)
        self.assertIn("mktemp", prove)
        self.assertNotIn("/tmp/robie-deny-test.err", prove)
        self.assertIn("PASS: Production secrets denied", prove)
