"""Contract tests for HERMES billing budget JSON."""

from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUDGET = ROOT / "deploy" / "monitoring" / "budget-hermes-poc.json"
CONFIGURE = ROOT / "scripts" / "configure-hermes-billing-budget.sh"


class BillingBudgetContractTests(unittest.TestCase):
    def test_budget_targets_hermes_project_with_spike_thresholds(self):
        body = json.loads(BUDGET.read_text(encoding="utf-8"))
        self.assertEqual(body["displayName"], "HERMES POC monthly cost spike budget")
        projects = body["budgetFilter"]["projects"]
        self.assertIn("projects/streetsmart-hermes-poc", projects)
        amount = body["amount"]["specifiedAmount"]
        self.assertEqual(amount["currencyCode"], "USD")
        self.assertEqual(int(amount["units"]), 300)
        rules = body["thresholdRules"]
        percents = sorted(r["thresholdPercent"] for r in rules)
        self.assertEqual(percents, [0.5, 0.8, 1.0, 1.0])
        bases = {r["spendBasis"] for r in rules}
        self.assertIn("CURRENT_SPEND", bases)
        self.assertIn("FORECASTED_SPEND", bases)
        self.assertTrue(body["notificationsRule"]["enableIamRecipients"])

    def test_configure_script_uses_billing_budget_api(self):
        script = CONFIGURE.read_text(encoding="utf-8")
        self.assertIn("billingbudgets.googleapis.com", script)
        self.assertIn("01CAD2-76802C-85A6BB", script)
        self.assertIn("monitoringNotificationChannels", script)
        self.assertIn("ROBIE_BUDGET_MONTHLY_USD", script)
