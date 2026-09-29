from __future__ import annotations

import ast
from datetime import date, datetime
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
        self.assertIn('activate_mdc_checkbox(mine, "My Submissions checkbox")', source)
        self.assertIn('activate_mdc_checkbox(agency, "Streetsmart Insurance checkbox")', source)
        self.assertGreaterEqual(source.count('_option_selected'), 4)

    def test_all_interactive_controls_use_visible_target_activation(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "robie_job_engine"
            / "submission_audit_runner.py"
        ).read_text()
        self.assertIn("def _first_visible", source)
        self.assertIn("def _activate", source)
        self.assertIn("target.press(key, timeout=5_000)", source)
        self.assertIn("target.click(timeout=5_000)", source)
        self.assertNotIn(".first.click()", source)
        self.assertNotIn(".last.click()", source)
        self.assertIn("activate_mdc_checkbox", source)
        self.assertIn("activate_mdc_combobox", source)
        self.assertIn("activate_sort_header", source)
        self.assertIn('_activate(picker_button, "agency picker")', source)
        self.assertIn('_activate(next_button, "next Submission Center page")', source)

    def test_live_picker_waits_for_async_checkbox_render(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "robie_job_engine"
            / "submission_audit_runner.py"
        ).read_text()
        self.assertIn('for _ in range(10)', source)
        self.assertIn('for selector in (".cdk-overlay-container mat-checkbox", "mat-checkbox")', source)
        self.assertIn('_exact_visible_option(options, "My Submissions")', source)
        self.assertIn('page.wait_for_timeout(500)', source)

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

    def test_page_size_force_clicks_mdc_combobox_and_option_100(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "robie_job_engine"
            / "submission_audit_runner.py"
        ).read_text()
        page_size_block = source.split("def _set_page_size", 1)[1].split(
            "def _headers", 1
        )[0]
        self.assertIn('activate_mdc_combobox(selector.first, "page-size control")', page_size_block)
        self.assertIn('activate_mdc_combobox(option, "100 page-size option")', page_size_block)
        self.assertIn("page.wait_for_timeout(500)", page_size_block)
        self.assertIn('pick_exact_labeled_option(', page_size_block)
        self.assertNotIn('selector.first.press("Enter")', page_size_block)
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
            status_block.index('activate_sort_header(header, "Status sort header")'),
            status_block.rindex("ascending sort showed a closed first row"),
        )

    def test_day_31_date_rule_is_explicit_and_date_parser_is_bounded(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "robie_job_engine"
            / "submission_audit_runner.py"
        ).read_text()
        tree = ast.parse(source)
        parser = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_parse_visible_date"
        )
        namespace = {
            "datetime": datetime,
            "date": date,
            "DATE_FORMATS": ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"),
        }
        exec(compile(ast.Module(body=[parser], type_ignores=[]), "<date-parser>", "exec"), namespace)
        parse = namespace["_parse_visible_date"]
        self.assertEqual(parse("07/31/2026").isoformat(), "2026-07-31")
        self.assertIsNone(parse("GC0"))
        self.assertIn("age_days > 30", source)
        self.assertIn("day_31_qualifies", source)

    def test_live_red_state_and_cross_page_boundary_are_required(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "robie_job_engine"
            / "submission_audit_runner.py"
        ).read_text()
        self.assertIn('RED_OVERDUE_COLOR = "rgb(211, 47, 47)"', source)
        self.assertIn("getComputedStyle(element).color", source)
        self.assertIn('"overdue_class": overdue_class', source)
        self.assertIn("def _advance_page", source)
        audit_block = source.split("def audit", 1)[1]
        self.assertIn("while first_closed_page is None", audit_block)
        self.assertIn("_advance_page(page, start)", audit_block)
        self.assertIn('"qualifying_records": qualifying', audit_block)
        self.assertIn('"email_delivery_enabled": False', audit_block)

    def test_pagination_reports_perform_progress_to_job_engine(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "robie_job_engine"
            / "submission_audit_runner.py"
        ).read_text()
        self.assertIn("def _report_pagination_progress", source)
        self.assertIn("report_current_job_perform_progress", source)
        audit_block = source.split("def audit", 1)[1]
        self.assertIn("_report_pagination_progress(", audit_block)
        self.assertIn("pages_reviewed=pages_reviewed", audit_block)
        self.assertIn("rows_inspected=rows_inspected", audit_block)


if __name__ == "__main__":
    unittest.main()
