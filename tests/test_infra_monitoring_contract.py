"""Contract tests for infra monitoring alert policy JSON."""

from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MONITORING = ROOT / "deploy" / "monitoring"
CONFIGURE = ROOT / "scripts" / "configure-hermes-infra-alerts.sh"
DRILL = ROOT / "scripts" / "fire-hermes-infra-alert-drill.sh"


class InfraMonitoringContractTests(unittest.TestCase):
    def test_policy_files_target_hermes_instances(self):
        files = sorted(MONITORING.glob("alert-*.json"))
        self.assertGreaterEqual(len(files), 4)
        bodies = [json.loads(path.read_text(encoding="utf-8")) for path in files]
        joined = json.dumps(bodies)
        self.assertIn("5649534881067121807", joined)  # hermes-poc-01
        self.assertIn("6971056864475829887", joined)  # hermes-test-01
        for body in bodies:
            self.assertTrue(body.get("displayName", "").startswith("INFRA"))
            self.assertIn("conditions", body)

    def test_scripts_reference_monitoring_api(self):
        configure = CONFIGURE.read_text(encoding="utf-8")
        drill = DRILL.read_text(encoding="utf-8")
        self.assertIn("monitoring.googleapis.com", configure)
        self.assertIn("alertPolicies", configure)
        self.assertIn("google-cloud-ops-agent", configure)
        self.assertIn("INFRA ALERT DRILL", drill)
        self.assertIn("notificationChannels", drill)
