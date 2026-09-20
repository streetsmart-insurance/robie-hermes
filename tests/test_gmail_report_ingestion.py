"""Unittest coverage for Gmail scheduled-report CSV ingestion.

The module self-test used to live only under ``if __name__ == "__main__"``,
so the ROBIE verification-gate battery never collected it. These cases pin
the 2026-09-19 header fingerprints, identity rules, and the open 4359 gate.
"""
from __future__ import annotations

import csv
import io
import unittest

from robie_job_engine import gmail_report_ingestion as ing


class HeaderFingerprintTests(unittest.TestCase):
    def test_expected_header_counts(self):
        self.assertEqual(len(ing.expected_headers("4247")), 22)
        self.assertEqual(len(ing.expected_headers("4246")), 24)
        self.assertEqual(len(ing.expected_headers("4372")), 32)
        self.assertEqual(len(ing.expected_headers("4359")), 19)

    def test_fingerprint_routes_all_four_reports(self):
        for report_id in ("4247", "4246", "4372", "4359"):
            routed = ing.fingerprint_report_id(ing.expected_headers(report_id))
            self.assertEqual(routed, report_id)

    def test_4246_fingerprint_is_4360_24col_not_old_19col(self):
        headers = ing.expected_headers("4246")
        self.assertIn("Effective Date", headers)
        self.assertIn("Policy Transaction ID", headers)
        self.assertNotIn("Department", headers)

    def test_unknown_headers_are_rejected(self):
        with self.assertRaises(ing.GmailReportIngestionError) as ctx:
            ing.fingerprint_report_id(
                ["Account Name"] + [f"Col{i}" for i in range(2, 19)] + ["Total Written Premium"]
            )
        self.assertIn("19-col CSV Account Name…Total Written Premium", str(ctx.exception))
        self.assertIn("no known ROBIE fingerprint", str(ctx.exception))

    def test_missing_email_error_is_distinct_from_schema(self):
        self.assertTrue(issubclass(ing.GmailReportMissingError, ing.GmailReportIngestionError))


class SubjectFingerprintTests(unittest.TestCase):
    def test_4372_requires_mortgagee_queue_subject(self):
        self.assertTrue(ing.subject_matches_report(ing.MORTGAGEE_4372_SUBJECT, "4372"))
        self.assertTrue(
            ing.subject_matches_report("Fwd: Mortgagee Verification Queue – ROBIE", "4372")
        )
        self.assertTrue(
            ing.subject_matches_report(
                "Mortgagee Verification Queue - ROBIE 2026-09-20", "4372"
            )
        )
        self.assertFalse(ing.subject_matches_report("ROBIE daily CSV", "4372"))
        self.assertFalse(ing.subject_matches_report("Manual Renewal Queue - ROBIE", "4372"))
        self.assertFalse(ing.subject_matches_report("Mortgagee Verification Queue", "4372"))
        self.assertFalse(ing.subject_matches_report("", "4372"))

    def test_4246_4247_keep_generic_daily_csv_subject(self):
        for report_id in ("4246", "4247", "4359"):
            self.assertTrue(ing.subject_matches_report("ROBIE daily CSV", report_id))
            self.assertFalse(
                ing.subject_matches_report(ing.MORTGAGEE_4372_SUBJECT, report_id)
            )

    def test_4372_gmail_query_is_mortgagee_subject_not_daily_csv(self):
        self.assertEqual(ing.gmail_subject_queries(["4372"]), [ing.MORTGAGEE_4372_SUBJECT])
        self.assertEqual(
            ing.gmail_subject_queries(["4247", "4246"]), [ing.DEFAULT_SUBJECT_CONTAINS]
        )
        self.assertEqual(
            ing.gmail_subject_queries(["4247", "4372"]),
            [ing.DEFAULT_SUBJECT_CONTAINS, ing.MORTGAGEE_4372_SUBJECT],
        )
        self.assertEqual(
            ing.gmail_subject_queries(["4372"], subject_contains="ROBIE daily CSV"),
            ["ROBIE daily CSV"],
        )


class SchemaGateTests(unittest.TestCase):
    def test_4359_gate_is_open_after_2026_09_19_verification(self):
        ing.check_report_gate("4359")
        self.assertTrue(ing.SCHEMA_VERIFIED["4359"])

    def test_verified_reports_pass_the_gate(self):
        for report_id in ("4247", "4246", "4372", "4359"):
            ing.check_report_gate(report_id)

    def test_unknown_report_id_is_rejected(self):
        with self.assertRaises(ing.GmailReportIngestionError):
            ing.check_report_gate("9999")


class IdentityTests(unittest.TestCase):
    def test_4247_4246_4372_identity_is_policy_number_alone(self):
        for report_id in ("4247", "4246", "4372"):
            rows, _ = ing.parse_and_validate_csv(
                report_id, ing._synthetic_csv(report_id, n_rows=1), source_label="unittest"
            )
            self.assertEqual(ing.identity_value(report_id, rows[0]), f"ID-{report_id}-1")

    def test_4359_identity_is_per_request(self):
        rows, _ = ing.parse_and_validate_csv(
            "4359", ing._synthetic_csv("4359", n_rows=2), source_label="unittest"
        )
        key1 = ing.identity_value("4359", rows[0])
        key2 = ing.identity_value("4359", rows[1])
        self.assertNotEqual(key1, key2)
        self.assertTrue(key1.startswith("ID-4359-1 | "))

    def test_4359_blank_created_date_is_rejected(self):
        content = ing._synthetic_csv("4359", n_rows=1).decode("utf-8").splitlines()
        headers = ing.expected_headers("4359")
        idx = headers.index(ing.IDENTITY_4359_CREATED_COLUMN)
        cells = next(csv.reader([content[1]]))
        cells[idx] = "   "
        content[1] = ",".join(f'"{cell}"' for cell in cells)
        with self.assertRaises(ing.GmailReportIngestionError):
            ing.parse_and_validate_csv(
                "4359", "\n".join(content).encode("utf-8"), source_label="unittest"
            )


class ParseValidateTests(unittest.TestCase):
    def test_header_only_csv_is_rejected(self):
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(ing.expected_headers("4246"))
        with self.assertRaises(ing.GmailReportIngestionError):
            ing.parse_and_validate_csv(
                "4246", buffer.getvalue().encode("utf-8"), source_label="unittest"
            )

    def test_self_test_passes(self):
        # Fail-closed: a stale self-test assertion is a CI failure, not a
        # silently skipped Ralph script.
        ing._self_test()


if __name__ == "__main__":
    unittest.main()
