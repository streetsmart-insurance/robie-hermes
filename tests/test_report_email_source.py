"""Unit tests for the morning-email CSV report source. No network, no browser."""

from __future__ import annotations

import base64
import csv
import io
import unittest
from datetime import date, datetime, timedelta, timezone

from robie_job_engine import gmail_report_ingestion as ing
from robie_job_engine import report_email_source as src


DAY = date(2026, 9, 20)
NOW = datetime(2026, 9, 20, 14, 30, tzinfo=timezone.utc)


def _csv_bytes(report_id: str, rows: list[dict]) -> bytes:
    headers = ing.expected_headers(report_id)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({header: row.get(header, "") for header in headers})
    return buffer.getvalue().encode("utf-8")


def _row4247(policy="P-4247", account="Acme LLC", expiration="10/30/2026"):
    return {
        "Account Name": account,
        "Applicant ID": "A1",
        "Policy Number": policy,
        "Policy Effective Date": "10/30/2025",
        "Policy Expiration Date": expiration,
        "Master Company": "Coterie",
        "Line Of Business": "Commercial",
        "Premium - Annualized": "1200",
        "Assigned Producer": "Carlo",
        "Department": "Commercial Lines",
        "CSR": "Pat",
        "Policy Source": "Manual",
    }


def _nineteen_col_daily_csv() -> bytes:
    """The foreign robie@ CSV that #517 4372 jobs fingerprinted and failed on."""
    headers = (
        ["Account Name"]
        + [f"Col{i}" for i in range(2, 19)]
        + ["Total Written Premium"]
    )
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(headers)
    writer.writerow(["Acme"] + ["x"] * 17 + ["1000"])
    return buffer.getvalue().encode("utf-8")


def _row4372(policy="P-4372", account="Mortgagee Co"):
    return {
        "Applicant ID": "A-4372",
        "Account Name": account,
        "Task Assigned To": "CSR",
        "Branch": "Personal",
        "Activity Type": "Task",
        "Note Created by": "Carlo",
        "Task Status": "Open",
        "Assigned Producer": "Carlo",
        "CSR": "Pat",
        "Task Created By": "Carlo",
        "Created Date": "09/19/2026",
        "Task Due Date": "10/30/2026",
        "Task Last Modified Date": "09/19/2026",
        "Task Last Modified By": "Carlo",
        "Note": "verify mortgagee",
        "Comment": "",
        "Task Closed By": "",
        "Policy Master ID": "PM-1",
        "Task Priority": "Normal",
        "Sticky": "No",
        "Task Created By ID": "1",
        "Task Created Date": "09/19/2026",
        "Task ID": "T-4372",
        "Task Closed Date": "",
        "Policy Number": policy,
        "Producer Code": "",
        "Producer Code Override": "",
        "Activity Labels": "",
        "Lead Source": "",
        "Discussion ID": "D-1",
        "Department": "Personal Lines",
        "Service Team": "Personal",
    }


def _row4360(policy, account, effective, status="Active", txn="PTX-1"):
    return {
        "Applicant ID": "220250093",
        "Branch": "Commercial Lines",
        "Account Name": account,
        "Account Type": "Commercial",
        "Assigned Producer": "P",
        "CSR": "C",
        "Policy Number": policy,
        "Policy ID": "PID-1",
        "Policy Transaction ID": txn,
        "Transaction Type": "Renewal",
        "Transaction Date": "08/15/2026",
        "Line of Business": "Workers Comp",
        "Master Company": "Test Carrier",
        "Download Date": "09/19/2026",
        "Effective Date": effective,
        "Expiration Date": "08/15/2027",
        "Current Policy Status": status,
        "Policy Term": f"{effective} - 08/15/2027",
        "Policy Type": "Workers Comp",
        "Transaction Deleted": "No",
        "Service Team": "Commercial",
        "Total Written Premium": "1000",
        "Total Customers": "1",
        "Total Transactions": "1",
    }


class _Exec:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class FakeScheduledReportGmail:
    """Minimal Gmail surface for ingest_daily_reports. No network."""

    def __init__(
        self,
        *,
        csv_bytes: bytes,
        filename: str,
        received_at: datetime,
        sender=None,
        subject="ROBIE daily CSV",
    ):
        self.csv_bytes = csv_bytes
        self.filename = filename
        encoded = base64.urlsafe_b64encode(csv_bytes).decode("ascii")
        sender = sender or "Applied Reporting <DoNotReply@appliedsystems.com>"
        self.message = {
            "id": "msg-daily-1",
            "internalDate": str(int(received_at.timestamp() * 1000)),
            "payload": {
                "headers": [
                    {"name": "From", "value": sender},
                    {"name": "Subject", "value": subject},
                ],
                "parts": [
                    {
                        "filename": filename,
                        "body": {"attachmentId": "att-1"},
                    }
                ],
            },
        }
        self.attachment = {"data": encoded}

    def users(self):
        return self

    def messages(self):
        return self

    def attachments(self):
        return self

    def list(self, **kwargs):
        return _Exec({"messages": [{"id": self.message["id"]}]})

    def get(self, userId=None, id=None, format=None, messageId=None):
        if messageId or id == "att-1":
            return _Exec(self.attachment)
        if id == self.message["id"]:
            return _Exec(self.message)
        return _Exec({})


class FakeMailboxGmail:
    """Multi-message Gmail surface. list() returns every id; subject post-filter applies."""

    def __init__(self, messages: list[dict]):
        self._messages = {item["id"]: item["message"] for item in messages}
        self._attachments = {item["att_id"]: item["attachment"] for item in messages}

    def users(self):
        return self

    def messages(self):
        return self

    def attachments(self):
        return self

    def list(self, **kwargs):
        return _Exec({"messages": [{"id": message_id} for message_id in self._messages]})

    def get(self, userId=None, id=None, format=None, messageId=None):
        if messageId in self._attachments:
            return _Exec(self._attachments[messageId])
        if id in self._attachments:
            return _Exec(self._attachments[id])
        if id in self._messages:
            return _Exec(self._messages[id])
        return _Exec({})


def _mailbox_item(
    *,
    message_id: str,
    att_id: str,
    csv_bytes: bytes,
    filename: str,
    received_at: datetime,
    subject: str,
    sender="Applied Reporting <DoNotReply@appliedsystems.com>",
):
    return {
        "id": message_id,
        "att_id": att_id,
        "message": {
            "id": message_id,
            "internalDate": str(int(received_at.timestamp() * 1000)),
            "payload": {
                "headers": [
                    {"name": "From", "value": sender},
                    {"name": "Subject", "value": subject},
                ],
                "parts": [
                    {
                        "filename": filename,
                        "body": {"attachmentId": att_id},
                    }
                ],
            },
        },
        "attachment": {"data": base64.urlsafe_b64encode(csv_bytes).decode("ascii")},
    }


class EmailSourceGateTests(unittest.TestCase):
    def test_email_first_ids_include_verification_queues(self):
        self.assertTrue(src.uses_email_source("4247"))
        self.assertTrue(src.uses_email_source("4246"))
        self.assertTrue(src.uses_email_source("4372"))
        self.assertTrue(src.uses_email_source("4359"))
        self.assertFalse(src.uses_email_source("9999"))


class Project4359Tests(unittest.TestCase):
    def _row(self, policy="POL-4359-001", created="09/01/2026"):
        row = {header: "" for header in ing.expected_headers("4359")}
        row.update(
            {
                "Account Name": "Example Trucking LLC",
                "Applicant ID": "A-100",
                "Policy Number": policy,
                "Line Of Business": "Commercial Auto",
                "Effective Date": "09/15/2026",
                "Master Company": "Example Carrier",
                "Request Status": "Open",
                "Created By": "CSR Example",
                "Written Premium": "1500.00",
                "Premium - Annualized": "2400.00",
                "Branch": "Commercial Lines",
                "Department": "Commercial Lines",
                "Service Team": "Service A",
                "Assigned Producer": "Producer Example",
                "CSR": "CSR Example",
                "Preferred Language": "English",
                "Applicant Labels": "VIP",
                "Policy Labels": "Audit",
                "Change Request Created Date": created,
            }
        )
        return row

    def test_aliases_cover_the_19_csv_headers(self):
        self.assertEqual(
            list(src.COLUMN_ALIASES["4359"]), ing.expected_headers("4359")
        )
        self.assertNotIn("request_id", src.COLUMN_ALIASES["4359"])

    def test_projects_real_columns(self):
        rows = src.rows_from_csv_bytes("4359", _csv_bytes("4359", [self._row()]))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["policy_number"], "POL-4359-001")
        self.assertEqual(row["Policy Number"], "POL-4359-001")
        self.assertEqual(row["change_request_created_date"], "09/01/2026")
        self.assertEqual(row["annualized_premium"], "2400.00")
        self.assertEqual(row["written_premium"], "1500.00")
        self.assertEqual(row["account_name"], "Example Trucking LLC")
        self.assertEqual(row["insured_name"], "Example Trucking LLC")
        self.assertEqual(row["master_company"], "Example Carrier")
        self.assertEqual(row["carrier"], "Example Carrier")
        self.assertEqual(row["_fetch_source"], "gmail_email_csv")
        self.assertNotIn("request_id", row)

    def test_two_requests_on_one_policy_both_kept(self):
        rows = src.rows_from_csv_bytes(
            "4359",
            _csv_bytes(
                "4359",
                [
                    self._row(created="09/01/2026"),
                    self._row(created="09/15/2026"),
                ],
            ),
        )
        self.assertEqual(
            [row["change_request_created_date"] for row in rows],
            ["09/01/2026", "09/15/2026"],
        )


class Project4247Tests(unittest.TestCase):
    def test_maps_worker_aliases_and_keeps_display_headers(self):
        rows = src.rows_from_csv_bytes("4247", _csv_bytes("4247", [_row4247()]))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["policy_number"], "P-4247")
        self.assertEqual(row["Policy Number"], "P-4247")
        self.assertEqual(row["insured_name"], "Acme LLC")
        self.assertEqual(row["expiration_date"], "10/30/2026")
        self.assertEqual(row["carrier_name"], "Coterie")
        self.assertEqual(row["line_of_business"], "Commercial")
        self.assertEqual(row["_fetch_source"], "gmail_email_csv")

    def test_wrong_schema_fails_closed(self):
        bad = b"policy_name,carrier\nP1,Coterie\n"
        with self.assertRaises(ing.GmailReportIngestionError) as ctx:
            src.rows_from_csv_bytes("4247", bad)
        self.assertIn("header mismatch", str(ctx.exception))


class Project4246Tests(unittest.TestCase):
    def test_4360_active_row_maps_audit_aliases(self):
        rows = src.rows_from_csv_bytes(
            "4246", _csv_bytes("4246", [_row4360("WC999", "Test Co", "08/15/2026")])
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["policy_number"], "WC999")
        self.assertEqual(row["audit_id"], "PTX-1")
        self.assertEqual(row["renewal_effective_date"], "08/15/2026")
        self.assertEqual(row["effective_date"], "08/15/2026")
        self.assertEqual(row["insured_name"], "Test Co")
        self.assertEqual(row["carrier"], "Test Carrier")
        self.assertEqual(row["department"], "Commercial Lines")
        self.assertEqual(row["policy_status"], "Active")

    def test_4360_skips_non_active_and_keeps_active(self):
        data = _csv_bytes(
            "4246",
            [
                _row4360("WC-DEAD", "Cancelled Co", "08/15/2026", status="Cancelled"),
                _row4360("WC-LIVE", "Active Co", "08/16/2026", status="Active", txn="PTX-2"),
            ],
        )
        rows = src.rows_from_csv_bytes("4246", data)
        self.assertEqual([row["policy_number"] for row in rows], ["WC-LIVE"])

    def test_4360_all_non_active_fails_closed(self):
        data = _csv_bytes(
            "4246",
            [_row4360("WC-DEAD", "Cancelled Co", "08/15/2026", status="Cancelled")],
        )
        with self.assertRaises(ing.GmailReportIngestionError) as ctx:
            src.rows_from_csv_bytes("4246", data)
        self.assertIn("no Active rows", str(ctx.exception))

    def test_old_19col_4246_fails_closed(self):
        old = (
            "Account Name,Applicant ID,Policy Number\n"
            "X,1,P1\n"
        ).encode("utf-8")
        with self.assertRaises(ing.GmailReportIngestionError) as ctx:
            src.rows_from_csv_bytes("4246", old)
        self.assertIn("header mismatch", str(ctx.exception))

    def test_dedupes_on_policy_number(self):
        data = _csv_bytes(
            "4246",
            [
                _row4360("WC-SAME", "Co", "08/15/2026", txn="PTX-1"),
                _row4360("WC-SAME", "Co", "08/15/2026", txn="PTX-2"),
            ],
        )
        rows = src.rows_from_csv_bytes("4246", data)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["audit_id"], "PTX-1")


class FreshnessTests(unittest.TestCase):
    def test_stale_received_at_fails_closed(self):
        rows, skipped = ing.parse_and_validate_csv(
            "4247", _csv_bytes("4247", [_row4247()]), source_label="unittest"
        )
        ingested = ing.IngestedReport(
            report_id="4247",
            display_name="Manual Renewal Queue - ROBIE",
            received_at=(NOW - timedelta(hours=48)).isoformat(),
            message_id_sha256="x",
            filename_sha256="y",
            attachment_sha256="z",
            row_count=len(rows),
            skipped_rows=skipped,
            rows=rows,
        )
        with self.assertRaises(src.EmailReportStaleError) as ctx:
            src.rows_from_ingested(ingested, day=DAY, now=NOW)
        self.assertIn("stale", str(ctx.exception))

    def test_wrong_filename_day_fails_closed(self):
        rows, skipped = ing.parse_and_validate_csv(
            "4247", _csv_bytes("4247", [_row4247()]), source_label="unittest"
        )
        ingested = ing.IngestedReport(
            report_id="4247",
            display_name="Manual Renewal Queue - ROBIE",
            received_at=NOW.isoformat(),
            message_id_sha256="x",
            filename_sha256="y",
            attachment_sha256="z",
            row_count=len(rows),
            skipped_rows=skipped,
            rows=rows,
        )
        with self.assertRaises(src.EmailReportStaleError) as ctx:
            src.rows_from_ingested(
                ingested,
                day=DAY,
                now=NOW,
                filename="ROBIE_daily_CSV_2026-09-19T0605.csv",
            )
        self.assertIn("2026-09-19", str(ctx.exception))

    def test_same_day_filename_and_received_at_pass(self):
        rows, skipped = ing.parse_and_validate_csv(
            "4247", _csv_bytes("4247", [_row4247()]), source_label="unittest"
        )
        ingested = ing.IngestedReport(
            report_id="4247",
            display_name="Manual Renewal Queue - ROBIE",
            received_at=datetime(2026, 9, 20, 10, 5, tzinfo=timezone.utc).isoformat(),
            message_id_sha256="x",
            filename_sha256="y",
            attachment_sha256="z",
            row_count=len(rows),
            skipped_rows=skipped,
            rows=rows,
        )
        projected = src.rows_from_ingested(
            ingested,
            day=DAY,
            now=NOW,
            filename="ROBIE_daily_CSV_2026-09-20T0605.csv",
        )
        self.assertEqual(projected[0]["policy_number"], "P-4247")


class GmailWiringTests(unittest.TestCase):
    def test_gmail_service_returns_projected_rows(self):
        received = datetime(2026, 9, 20, 10, 5, tzinfo=timezone.utc)
        service = FakeScheduledReportGmail(
            csv_bytes=_csv_bytes("4247", [_row4247()]),
            filename="ROBIE_daily_CSV_2026-09-20T0605.csv",
            received_at=received,
        )
        rows = src.fetch_email_report_rows(
            report_id="4247",
            gmail_service=service,
            day=DAY,
            now=NOW,
            filename="ROBIE_daily_CSV_2026-09-20T0605.csv",
        )
        self.assertEqual(rows[0]["policy_number"], "P-4247")

    def test_empty_mailbox_is_missing_not_schema(self):
        class _Empty:
            def users(self):
                return self

            def messages(self):
                return self

            def list(self, **kwargs):
                return _Exec({"messages": []})

        with self.assertRaises(ing.GmailReportMissingError) as ctx:
            src.fetch_email_report_rows(
                report_id="4247", gmail_service=_Empty(), day=DAY, now=NOW
            )
        self.assertIn("no scheduled report email found for 4247", str(ctx.exception))


class Mortgagee4372SubjectPickerTests(unittest.TestCase):
    """4372 must pick Mortgagee Verification Queue - ROBIE, not any robie@ CSV."""

    received = datetime(2026, 9, 20, 10, 5, tzinfo=timezone.utc)

    def test_correct_mortgagee_subject_and_schema_are_accepted(self):
        service = FakeScheduledReportGmail(
            csv_bytes=_csv_bytes("4372", [_row4372()]),
            filename="Mortgagee_Verification_Queue_ROBIE_2026-09-20T0605.csv",
            received_at=self.received,
            subject=ing.MORTGAGEE_4372_SUBJECT,
        )
        rows = src.fetch_email_report_rows(
            report_id="4372", gmail_service=service, day=DAY, now=NOW
        )
        self.assertEqual(rows[0]["policy_number"], "P-4372")
        self.assertEqual(rows[0]["task_id"], "T-4372")
        self.assertEqual(rows[0]["_fetch_source"], "gmail_email_csv")

    def test_wrong_subject_19col_daily_csv_is_not_4372(self):
        service = FakeScheduledReportGmail(
            csv_bytes=_nineteen_col_daily_csv(),
            filename="ROBIE_daily_CSV_2026-09-20T0605.csv",
            received_at=self.received,
            subject="ROBIE daily CSV",
        )
        with self.assertRaises(ing.GmailReportMissingError) as ctx:
            src.fetch_email_report_rows(
                report_id="4372", gmail_service=service, day=DAY, now=NOW
            )
        self.assertIn("no scheduled report email found for 4372", str(ctx.exception))
        self.assertNotIn("no known ROBIE fingerprint", str(ctx.exception))

    def test_4372_schema_under_generic_daily_subject_is_rejected(self):
        service = FakeScheduledReportGmail(
            csv_bytes=_csv_bytes("4372", [_row4372()]),
            filename="ROBIE_daily_CSV_2026-09-20T0605.csv",
            received_at=self.received,
            subject="ROBIE daily CSV",
        )
        with self.assertRaises(ing.GmailReportMissingError) as ctx:
            src.fetch_email_report_rows(
                report_id="4372", gmail_service=service, day=DAY, now=NOW
            )
        self.assertIn("no scheduled report email found for 4372", str(ctx.exception))

    def test_mortgagee_subject_with_wrong_schema_fails_closed(self):
        service = FakeScheduledReportGmail(
            csv_bytes=_nineteen_col_daily_csv(),
            filename="Mortgagee_Verification_Queue_ROBIE.csv",
            received_at=self.received,
            subject=ing.MORTGAGEE_4372_SUBJECT,
        )
        with self.assertRaises(ing.GmailReportIngestionError) as ctx:
            src.fetch_email_report_rows(
                report_id="4372", gmail_service=service, day=DAY, now=NOW
            )
        self.assertNotIsInstance(ctx.exception, ing.GmailReportMissingError)
        self.assertIn("19-col CSV Account Name…Total Written Premium", str(ctx.exception))
        self.assertIn("no known ROBIE fingerprint", str(ctx.exception))

    def test_mixed_mailbox_4372_picks_mortgagee_not_other_daily_csv(self):
        service = FakeMailboxGmail(
            [
                _mailbox_item(
                    message_id="msg-wrong",
                    att_id="att-wrong",
                    csv_bytes=_nineteen_col_daily_csv(),
                    filename="ROBIE_daily_CSV_2026-09-20T0605.csv",
                    received_at=self.received,
                    subject="ROBIE daily CSV",
                ),
                _mailbox_item(
                    message_id="msg-mortgagee",
                    att_id="att-mortgagee",
                    csv_bytes=_csv_bytes("4372", [_row4372()]),
                    filename="Mortgagee_Verification_Queue_ROBIE.csv",
                    received_at=self.received,
                    subject=ing.MORTGAGEE_4372_SUBJECT,
                ),
            ]
        )
        rows = src.fetch_email_report_rows(
            report_id="4372", gmail_service=service, day=DAY, now=NOW
        )
        self.assertEqual(rows[0]["policy_number"], "P-4372")

    def test_4247_still_uses_generic_daily_csv_when_mortgagee_mail_is_present(self):
        service = FakeMailboxGmail(
            [
                _mailbox_item(
                    message_id="msg-4247",
                    att_id="att-4247",
                    csv_bytes=_csv_bytes("4247", [_row4247()]),
                    filename="ROBIE_daily_CSV_2026-09-20T0605.csv",
                    received_at=self.received,
                    subject="ROBIE daily CSV",
                ),
                _mailbox_item(
                    message_id="msg-mortgagee",
                    att_id="att-mortgagee",
                    csv_bytes=_csv_bytes("4372", [_row4372()]),
                    filename="Mortgagee_Verification_Queue_ROBIE.csv",
                    received_at=self.received,
                    subject=ing.MORTGAGEE_4372_SUBJECT,
                ),
            ]
        )
        rows = src.fetch_email_report_rows(
            report_id="4247",
            gmail_service=service,
            day=DAY,
            now=NOW,
            filename="ROBIE_daily_CSV_2026-09-20T0605.csv",
        )
        self.assertEqual(rows[0]["policy_number"], "P-4247")

    def test_4246_still_uses_4360_fingerprint_not_mortgagee_subject(self):
        # 4246's real daily mail is the separate 4360 transaction-feed
        # delivery ("Workers Comp Renewal Audit Queue - ROBIE"), proven
        # 2026-09-25 — NOT the generic "ROBIE daily CSV" envelope. The
        # 4360 24-col schema is still what validates the attachment, and
        # the mortgagee decoy must not be picked up.
        service = FakeMailboxGmail(
            [
                _mailbox_item(
                    message_id="msg-4246",
                    att_id="att-4246",
                    csv_bytes=_csv_bytes(
                        "4246", [_row4360("WC999", "Test Co", "08/15/2026")]
                    ),
                    filename="ROBIE_daily_CSV_2026-09-20T0605.csv",
                    received_at=self.received,
                    subject=ing.AUDIT_4246_SUBJECT,
                ),
                _mailbox_item(
                    message_id="msg-mortgagee",
                    att_id="att-mortgagee",
                    csv_bytes=_csv_bytes("4372", [_row4372()]),
                    filename="Mortgagee_Verification_Queue_ROBIE.csv",
                    received_at=self.received,
                    subject=ing.MORTGAGEE_4372_SUBJECT,
                ),
            ]
        )
        rows = src.fetch_email_report_rows(
            report_id="4246",
            gmail_service=service,
            day=DAY,
            now=NOW,
            filename="ROBIE_daily_CSV_2026-09-20T0605.csv",
        )
        self.assertEqual(rows[0]["policy_number"], "WC999")
        self.assertEqual(rows[0]["audit_id"], "PTX-1")


if __name__ == "__main__":
    unittest.main()
