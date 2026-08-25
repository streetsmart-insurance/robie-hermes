from __future__ import annotations

import ast
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
        self.assertIn('options.first.wait_for(state="visible", timeout=5_000)', source)
        self.assertIn('for selector in (".cdk-overlay-container mat-checkbox", "mat-checkbox")', source)

    def test_picker_labels_ignore_presentation_whitespace_only(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "robie_job_engine"
            / "submission_audit_runner.py"
        ).read_text()
        tree = ast.parse(source)
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_normalize_option_label"
        )
        namespace = {}
        exec(
            compile(ast.Module(body=[function], type_ignores=[]), "<normalizer>", "exec"),
            namespace,
        )
        normalize = namespace["_normalize_option_label"]
        self.assertEqual(
            normalize("  Streetsmart\n  Insurance  "),
            normalize("Streetsmart Insurance"),
        )
        self.assertNotEqual(
            normalize("Streetsmart Insurance Team"),
            normalize("Streetsmart Insurance"),
        )

    def test_page_size_uses_accessible_keyboard_activation(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "robie_job_engine"
            / "submission_audit_runner.py"
        ).read_text()
        page_size_block = source.split("def _set_page_size", 1)[1].split(
            "def _headers", 1
        )[0]
        self.assertIn('selector.first.press("Enter")', page_size_block)
        self.assertNotIn("selector.first.click()", page_size_block)


if __name__ == "__main__":
    unittest.main()
