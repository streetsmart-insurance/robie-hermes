from __future__ import annotations

import unittest
from pathlib import Path

from robie_job_engine.submission_audit_runner import _normalize_option_label


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
        self.assertIn('options.first.wait_for(state="visible", timeout=5_000)', source)
        self.assertIn('for selector in (".cdk-overlay-container mat-checkbox", "mat-checkbox")', source)

    def test_picker_labels_ignore_presentation_whitespace_only(self):
        self.assertEqual(
            _normalize_option_label("  Streetsmart\n  Insurance  "),
            _normalize_option_label("Streetsmart Insurance"),
        )
        self.assertNotEqual(
            _normalize_option_label("Streetsmart Insurance Team"),
            _normalize_option_label("Streetsmart Insurance"),
        )


if __name__ == "__main__":
    unittest.main()
