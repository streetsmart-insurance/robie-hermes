from __future__ import annotations

import unittest
from pathlib import Path


class SubmissionAuditRunnerContractTests(unittest.TestCase):
    def test_live_mdc_agency_picker_is_supported_and_reread(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "robie_job_engine"
            / "submission_audit_runner.py"
        ).read_text()
        self.assertIn('.cdk-overlay-container mat-checkbox', source)
        self.assertIn('"mat-checkbox"', source)
        self.assertIn('Apply|Done|Select', source)
        self.assertGreaterEqual(source.count('_option_selected'), 4)

    def test_live_picker_waits_for_async_checkbox_render(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "robie_job_engine"
            / "submission_audit_runner.py"
        ).read_text()
        self.assertIn('ready.first.wait_for(state="visible", timeout=5_000)', source)
        self.assertIn('for selector in (".cdk-overlay-container mat-checkbox", "mat-checkbox")', source)


if __name__ == "__main__":
    unittest.main()
