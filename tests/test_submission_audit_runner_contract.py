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
        self.assertIn(".mat-mdc-select-value-text", page_size_block)

    def test_page_size_is_followed_by_scope_refresh_and_reread(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "robie_job_engine"
            / "submission_audit_runner.py"
        ).read_text()
        audit_block = source.split("def audit", 1)[1]
        first_scope = audit_block.index("_set_agency_scope(page)")
        page_size = audit_block.index("_set_page_size(page)")
        second_scope = audit_block.index("_set_agency_scope(page)", first_scope + 1)
        reread = audit_block.index("_verify_page_size_result(page)")
        self.assertLess(first_scope, page_size)
        self.assertLess(page_size, second_scope)
        self.assertLess(second_scope, reread)
        verification_block = source.split(
            "def _verify_page_size_result", 1
        )[1].split("def _headers", 1)[0]
        self.assertIn("document.querySelectorAll('mat-row').length === expected", verification_block)
        self.assertIn("paginator did not confirm the selected page size", verification_block)

    def test_status_wait_uses_current_playwright_keyword_argument(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "robie_job_engine"
            / "submission_audit_runner.py"
        ).read_text()
        status_block = source.split("def _normalize_status_sort", 1)[1].split(
            "def _pager_total", 1
        )[0]
        self.assertIn("arg=before", status_block)
        self.assertIn("for _ in range(5)", status_block)
        self.assertIn("Cycle away and back", status_block)
        self.assertIn("refreshed_ascending", status_block)
        self.assertIn("_set_agency_scope(page)", status_block)
        self.assertIn("_verify_page_size_result(page)", status_block)
        self.assertLess(
            status_block.index("header.click()"),
            status_block.rindex("ascending sort showed a closed first row"),
        )


if __name__ == "__main__":
    unittest.main()
