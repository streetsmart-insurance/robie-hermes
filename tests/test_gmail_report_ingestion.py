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

    def test_4247_keeps_generic_daily_csv_subject_4246_has_dedicated(self):
        # 4246's daily email is the 4360 transaction feed, not the generic envelope.
        self.assertTrue(
            ing.subject_matches_report(
                "Workers Comp Renewal Audit Queue - ROBIE", "4246"
            )
        )
        self.assertFalse(ing.subject_matches_report("ROBIE daily CSV", "4246"))
        for report_id in ("4247", "4359"):
            self.assertTrue(ing.subject_matches_report("ROBIE daily CSV", report_id))
            self.assertFalse(
                ing.subject_matches_report(ing.MORTGAGEE_4372_SUBJECT, report_id)
            )

    def test_4372_gmail_query_is_mortgagee_subject_not_daily_csv(self):
        self.assertEqual(ing.gmail_subject_queries(["4372"]), [ing.MORTGAGEE_4372_SUBJECT])
        self.assertEqual(ing.gmail_subject_queries(["4246"]), [ing.AUDIT_4246_SUBJECT])
        self.assertEqual(
            ing.gmail_subject_queries(["4247", "4246"]),
            [ing.DEFAULT_SUBJECT_CONTAINS, ing.AUDIT_4246_SUBJECT],
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


class MondayTimerRegressionTests(unittest.TestCase):
    """Regression tests for the 2026-09-25 Monday-timer failures.

    4247/4246 timers crashed on an unrecognized 19-col CSV in the shared
    envelope; 4246's Gmail query could never find its 4360 email; 4372
    failed on a closed test task with an empty Policy Number.
    """

    def test_4246_gmail_query_is_audit_queue_subject(self):
        self.assertEqual(
            ing.gmail_subject_queries(["4246"]), [ing.AUDIT_4246_SUBJECT]
        )
        self.assertEqual(
            ing.gmail_subject_queries(["4247", "4246"]),
            [ing.DEFAULT_SUBJECT_CONTAINS, ing.AUDIT_4246_SUBJECT],
        )

    def test_4246_subject_matching_uses_dedicated_subject(self):
        self.assertTrue(
            ing.subject_matches_report(
                "Workers Comp Renewal Audit Queue - ROBIE", "4246"
            )
        )
        # The generic daily-CSV envelope is NOT 4246's mail.
        self.assertFalse(ing.subject_matches_report("ROBIE daily CSV", "4246"))
        # 4247 still uses the generic envelope.
        self.assertTrue(ing.subject_matches_report("ROBIE daily CSV", "4247"))
        self.assertFalse(
            ing.subject_matches_report(
                "Workers Comp Renewal Audit Queue - ROBIE", "4247"
            )
        )

    def test_empty_identity_row_is_skipped_not_raised(self):
        headers = ing.expected_headers("4372")
        id_col = ing.identity_column("4372")
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(headers)
        good = [""] * len(headers)
        good[headers.index("Policy Number")] = "POL123"
        good[headers.index("Account Name")] = "Good Account"
        writer.writerow(good)
        # Closed test task: empty Policy Number, data in other columns.
        bad = [""] * len(headers)
        bad[headers.index("Account Name")] = "ROBIE Test LLC"
        bad[headers.index("Task ID")] = "63010391"
        writer.writerow(bad)
        rows, skipped = ing.parse_and_validate_csv(
            "4372", buffer.getvalue().encode("utf-8"), source_label="unittest"
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Policy Number"], "POL123")
        self.assertEqual(skipped, 1)

    def test_4372_test_ho_empty_policy_recovered_other_report_skipped(self):
        """4372 TEST-HO note fallback keeps the row; another report skips it.

        (a) A 4372 row with an empty Policy Number and a TEST-HO number in
        the Note is recovered and is not logged as a skip.
        (b) The same empty Policy Number on another report is skipped with
        a warning, and a sibling row with a policy number is kept.
        """
        headers_4372 = ing.expected_headers("4372")
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(headers_4372)
        canary = [""] * len(headers_4372)
        canary[headers_4372.index("Account Name")] = "ROBIE Test LLC"
        canary[headers_4372.index("Note")] = (
            "Task note with TEST-HO-08312026-01 for mortgagee verification"
        )
        writer.writerow(canary)
        with self.assertNoLogs(
            "robie_job_engine.gmail_report_ingestion", level="WARNING"
        ):
            rows, skipped = ing.parse_and_validate_csv(
                "4372", buffer.getvalue().encode("utf-8"), source_label="unittest"
            )
        self.assertEqual(skipped, 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Policy Number"], "")
        self.assertEqual(ing.identity_value("4372", rows[0]), "TEST-HO-08312026-01")

        headers_4247 = ing.expected_headers("4247")
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(headers_4247)
        kept = [""] * len(headers_4247)
        kept[headers_4247.index("Policy Number")] = "POL-KEEP"
        kept[headers_4247.index("Account Name")] = "Kept Account"
        writer.writerow(kept)
        empty = [""] * len(headers_4247)
        empty[headers_4247.index("Account Name")] = "Empty Policy"
        writer.writerow(empty)
        with self.assertLogs(
            "robie_job_engine.gmail_report_ingestion", level="WARNING"
        ) as logs:
            rows, skipped = ing.parse_and_validate_csv(
                "4247", buffer.getvalue().encode("utf-8"), source_label="unittest"
            )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Policy Number"], "POL-KEEP")
        self.assertEqual(skipped, 1)
        self.assertTrue(
            any("skipping row with empty identity" in message and "4247" in message
                for message in logs.output)
        )

    def test_all_empty_identity_rows_still_fails_closed(self):
        headers = ing.expected_headers("4372")
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(headers)
        bad = [""] * len(headers)
        bad[headers.index("Account Name")] = "ROBIE Test LLC"
        writer.writerow(bad)
        with self.assertRaises(ing.GmailReportIngestionError) as ctx:
            ing.parse_and_validate_csv(
                "4372", buffer.getvalue().encode("utf-8"), source_label="unittest"
            )
        self.assertIn("no data rows", str(ctx.exception))

    def test_unknown_fingerprint_csv_does_not_kill_ingest(self):
        """An unrecognized CSV in the envelope is skipped, not fatal."""
        import base64
        from datetime import date

        def b64_csv(headers, rows):
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(headers)
            w.writerows(rows)
            return base64.urlsafe_b64encode(
                buf.getvalue().encode("utf-8")
            ).decode()

        good_headers = ing.expected_headers("4247")
        good_row = ["x"] * len(good_headers)
        good_row[good_headers.index("Policy Number")] = "POL999"
        # 19-col foreign CSV: matches no known fingerprint.
        foreign_headers = (
            ["Account Name"] + [f"Col{i}" for i in range(2, 19)] + ["Total Written Premium"]
        )
        foreign_row = ["y"] * len(foreign_headers)

        messages = {
            "msg-good": (
                "ROBIE daily CSV",
                b64_csv(good_headers, [good_row]),
            ),
            "msg-foreign": (
                "ROBIE daily CSV",
                b64_csv(foreign_headers, [foreign_row]),
            ),
        }

        class _Exec:
            def __init__(self, v):
                self.v = v

            def execute(self):
                return self.v

        class _Messages:
            def list(self, **kw):
                return _Exec({"messages": [{"id": mid} for mid in messages]})

            def get(self, userId=None, id=None, format=None):
                subject, data = messages[id]
                return _Exec({
                    "id": id,
                    "internalDate": "1758790891000",
                    "payload": {
                        "headers": [
                            {"name": "Subject", "value": subject},
                            {"name": "From", "value": "donotreply@appliedsystems.com"},
                        ],
                        "parts": [{
                            "filename": "r.csv",
                            "mimeType": "text/csv",
                            "body": {"data": data},
                        }],
                    },
                })

        class _Users:
            def messages(self):
                return _Messages()

        class FakeService:
            def users(self):
                return _Users()

        got = ing.ingest_daily_reports(
            FakeService(), day=date(2026, 9, 25), report_ids=["4247"]
        )
        self.assertIn("4247", got)
        self.assertEqual(got["4247"].row_count, 1)


class Mortgagee4744Tests(unittest.TestCase):
    """The 4744 mortgagee report (policy-expiration, 8 cols) replaced the
    retired 4372 task export. Fingerprinted 2026-09-28 against the real
    05:01 delivery (Gmail 1a0e73f8f39ced03): 189 data rows, no totals row."""

    def test_4744_header_count_is_8(self):
        self.assertEqual(len(ing.expected_headers("4744")), 8)

    def test_4744_fingerprint_routes(self):
        headers = [
            "Account Name",
            "Policy Number",
            "Master Company",
            "Line Of Business",
            "Premium - Annualized",
            "Assigned Producer",
            "CSR",
            "Policy Expiration Date",
        ]
        self.assertEqual(ing.fingerprint_report_id(headers), "4744")
        self.assertEqual(ing.expected_headers("4744"), headers)

    def test_4744_identity_is_policy_number(self):
        rows, _ = ing.parse_and_validate_csv(
            "4744", ing._synthetic_csv("4744", n_rows=1), source_label="unittest"
        )
        self.assertEqual(ing.identity_value("4744", rows[0]), "ID-4744-1")

    def test_4744_schema_is_verified(self):
        self.assertTrue(ing.SCHEMA_VERIFIED["4744"])

    def test_4744_display_name(self):
        self.assertEqual(
            ing.REPORT_DISPLAY_NAMES["4744"], "Mortgagee Verification Queue - ROBIE"
        )

    def test_4744_selected_by_fingerprint_not_subject(self):
        # The 4744 email shares its subject line with a stale schedule
        # export, so subject alone must not identify it.
        self.assertFalse(ing.subject_matches_report("Mortgagee Verification Queue - ROBIE", "4744"))


if __name__ == "__main__":
    unittest.main()
