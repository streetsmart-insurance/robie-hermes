from __future__ import annotations

import inspect
import unittest

from robie_job_engine import submission_audit_runner


class SubmissionAuditRunnerContractTests(unittest.TestCase):
    def test_live_mdc_agency_picker_is_supported_and_reread(self):
        source = inspect.getsource(submission_audit_runner._set_agency_scope)
        self.assertIn('.cdk-overlay-container mat-checkbox', source)
        self.assertIn('Apply|Done|Select', source)
        self.assertGreaterEqual(source.count('_option_selected'), 4)


if __name__ == "__main__":
    unittest.main()
