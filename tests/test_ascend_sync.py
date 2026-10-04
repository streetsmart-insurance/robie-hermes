"""Unit tests for Ascend to EZLynx account event synchronization."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from robie_job_engine import ascend_sync
from robie_job_engine.ascend_sync import (
    AscendApiClient,
    AscendCancellationEvent,
    AscendEZLynxSyncManager,
    AscendPastDueEvent,
    AscendSyncStore,
    loan_downpayment_cents,
    program_total_cents,
)
from robie_job_engine.ezlynx_note_poster import (
    ROBIE_SIGNATURE,
    format_cancellation_notice_note,
    format_past_due_notice_note,
)


class TestAscendNoteFormatting(unittest.TestCase):
    def test_cancellation_notice_note_formatting(self) -> None:
        note = format_cancellation_notice_note(
            insured_name="Garden State Drywall Contractors Inc",
            policy_number="NPP6269395",
            carrier_name="Western World Insurance Company",
            wholesaler_name="Johnson & Johnson, Inc",
            coverage_title="General Liability",
            cancellation_effective_date="2026-07-30",
            due_date_text="2026-07-30",
            amount_due_or_return_text="$1,798.65",
            document_url="https://api.cloudinary.com/sample_cancel_notice.pdf",
            assigned_rep="Zeus Quezada",
            unearned_premium_text="$1,798.65",
            unearned_commission_text="$0.00",
            unearned_tax_text="$0.00",
        )
        self.assertIn("🚨 CANCELLATION NOTICE - ASCEND ACCOUNT SYNC", note)
        self.assertIn("• Insured: Garden State Drywall Contractors Inc", note)
        self.assertIn("• Policy Number: NPP6269395", note)
        self.assertIn("• Carrier: Western World Insurance Company (via Johnson & Johnson, Inc)", note)
        self.assertIn("• Plain Text Due Date / Effective Date: 2026-07-30", note)
        self.assertIn("• Plain Text Amount Due / Return Amount: $1,798.65", note)
        self.assertIn("• Unearned Premium: $1,798.65", note)
        self.assertIn("Embedded Cancellation Document:", note)
        self.assertIn("https://api.cloudinary.com/sample_cancel_notice.pdf", note)
        self.assertIn("Assigned Representative: Zeus Quezada", note)
        self.assertNotIn("Label", note)
        self.assertTrue(note.endswith(ROBIE_SIGNATURE))

    def test_past_due_notice_formatting(self) -> None:
        note = format_past_due_notice_note(
            insured_name="Pianka Construction LLC",
            policy_number="33470152",
            invoice_number="IXQPHVKEO9",
            memo="Excess Umbrella Downpayment",
            amount_due_text="$1,415.61",
            due_date_text="2026-09-04",
            payment_status="past_due",
            invoice_url="https://api.cloudinary.com/sample_invoice.pdf",
        )
        self.assertIn("⚠️ ASCEND PAYMENT PAST DUE NOTICE", note)
        self.assertIn("• Status: PAST DUE", note)
        self.assertIn("• Insured: Pianka Construction LLC", note)
        self.assertIn("• Policy Number: 33470152", note)
        self.assertIn("• Invoice Number: IXQPHVKEO9", note)
        self.assertIn("• Plain Text Amount Due: $1,415.61", note)
        self.assertIn("• Plain Text Due Date: 2026-09-04", note)
        self.assertIn("Invoice & Payment Link:", note)
        self.assertIn("https://api.cloudinary.com/sample_invoice.pdf", note)
        self.assertNotIn("Label Applied", note)  # User: No label needed for past due
        self.assertTrue(note.endswith(ROBIE_SIGNATURE))


class TestAscendSyncStore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_ascend_sync.db"
        self.store = AscendSyncStore(self.db_path)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_idempotent_event_recording(self) -> None:
        event_id = "test-event-123"
        self.assertFalse(self.store.is_event_processed(event_id))

        self.store.record_synced_event(
            event_id=event_id,
            event_type="cancellation",
            policy_number="NPP6269395",
            applicant_id="app-456",
            amount_cents=179865,
            status="SUCCESS",
        )

        self.assertTrue(self.store.is_event_processed(event_id))

        # Checkpoints update
        self.store.update_checkpoint("test_feed", synced_count=1)
        # Record again does not raise error
        self.store.record_synced_event(
            event_id=event_id,
            event_type="cancellation",
            status="SUCCESS",
        )
        self.assertTrue(self.store.is_event_processed(event_id))


class TestSignedAgreementMoneyMapping(unittest.TestCase):
    def test_program_total_uses_sub_total_not_invented_keys(self) -> None:
        self.assertEqual(
            program_total_cents(
                {
                    "sub_total_cents": 1261465,
                    "premium_cents": 1200000,
                    "downpayment_amount_cents": 1,
                    "total_payable_amount_cents": 2,
                }
            ),
            1261465,
        )

    def test_program_total_falls_back_to_premium_cents(self) -> None:
        self.assertEqual(program_total_cents({"premium_cents": 125000}), 125000)
        self.assertEqual(program_total_cents({}), 0)
        self.assertEqual(
            program_total_cents(
                {"downpayment_amount_cents": 150000, "total_payable_amount_cents": 600000}
            ),
            0,
        )

    def test_loan_downpayment_for_monthly_financed(self) -> None:
        self.assertEqual(
            loan_downpayment_cents([{"downpayment_cents": 334116}], default_cents=1261465),
            334116,
        )

    def test_annual_pay_in_full_down_equals_total_when_no_loan(self) -> None:
        self.assertEqual(loan_downpayment_cents([], default_cents=1261465), 1261465)
        self.assertEqual(loan_downpayment_cents(None, default_cents=50000), 50000)


class TestAscendProgramReadUrls(unittest.TestCase):
    def test_fetch_program_billables_uses_query_not_nested_path(self) -> None:
        client = AscendApiClient(
            api_key="test-key",
            origin="https://api.useascend.com",
            secret_accessor=MagicMock(),
        )
        client.get = MagicMock(return_value={"data": [{"policy_number": "ISCA-1"}]})
        items = client.fetch_program_billables("prog-isca")
        client.get.assert_called_once_with("/v1/billables", {"program_id": "prog-isca"})
        self.assertEqual(items[0]["policy_number"], "ISCA-1")

    def test_fetch_program_loans_uses_program_id_query(self) -> None:
        client = AscendApiClient(
            api_key="test-key",
            origin="https://api.useascend.com",
            secret_accessor=MagicMock(),
        )
        client.get = MagicMock(return_value={"data": [{"downpayment_cents": 334116}]})
        items = client.fetch_program_loans("prog-isca")
        client.get.assert_called_once_with("/v1/loans", {"program_id": "prog-isca"})
        self.assertEqual(items[0]["downpayment_cents"], 334116)


class TestAscendEZLynxSyncManager(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_sync.db"
        self.store = AscendSyncStore(self.db_path)

        self.mock_api = MagicMock()
        self.mock_matcher = MagicMock()
        self.mock_poster = MagicMock()
        self.mock_poster.create_task.return_value = {"status": "success"}
        self.mock_poster.post_custom_note.return_value = {"status": "success"}

        self.manager = AscendEZLynxSyncManager(
            api_client=self.mock_api,
            store=self.store,
            matcher=self.mock_matcher,
            poster=self.mock_poster,
        )

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_sync_cancellation_and_past_due(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "ASCEND_SYNC_DISABLED"):
            self.manager.sync_once()
        self.mock_api.fetch_cancelation_returns.assert_not_called()
        self.mock_poster.post_note.assert_not_called()
        self.mock_poster.create_task.assert_not_called()

    def test_sync_signed_agreements_ready_to_bind(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "ASCEND_SYNC_DISABLED"):
            self.manager.sync_once()
        self.mock_api.fetch_cancelation_returns.assert_not_called()
        self.mock_poster.post_note.assert_not_called()
        self.mock_poster.create_task.assert_not_called()

    def test_sync_signed_agreements_monthly_financed_lists_billables_and_loan(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "ASCEND_SYNC_DISABLED"):
            self.manager.sync_once()
        self.mock_api.fetch_cancelation_returns.assert_not_called()
        self.mock_poster.post_note.assert_not_called()
        self.mock_poster.create_task.assert_not_called()

    def test_sync_signed_agreements_annual_pay_in_full_down_equals_total(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "ASCEND_SYNC_DISABLED"):
            self.manager.sync_once()
        self.mock_api.fetch_cancelation_returns.assert_not_called()
        self.mock_poster.post_note.assert_not_called()
        self.mock_poster.create_task.assert_not_called()

    def test_sync_reinstatement_paid(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "ASCEND_SYNC_DISABLED"):
            self.manager.sync_once()
        self.mock_api.fetch_cancelation_returns.assert_not_called()
        self.mock_poster.post_note.assert_not_called()
        self.mock_poster.create_task.assert_not_called()

    def test_sync_accounting_unpaid_supplier_payout(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "ASCEND_SYNC_DISABLED"):
            self.manager.sync_once()
        self.mock_api.fetch_cancelation_returns.assert_not_called()
        self.mock_poster.post_note.assert_not_called()
        self.mock_poster.create_task.assert_not_called()

    def test_endorsement_extractor(self) -> None:
        from robie_job_engine.quote_extractor import EndorsementExtractor
        extractor = EndorsementExtractor()

        sample_endorsement = """
        POLICY ENDORSEMENT REQUEST
        Policy Number: GL-2026-99124
        Named Insured: FastTrack Logistics LLC
        Carrier: Liberty Mutual
        Effective Date: 10/15/2026
        Description of Change: Adding Blanket Additional Insured and Waiver of Subrogation
        Additional Premium: $650.00
        Surplus Lines Tax: $32.50
        Commission: 15.0%
        """

        extracted = extractor.extract_from_text(sample_endorsement)
        self.assertEqual(extracted.policy_number, "GL-2026-99124")
        self.assertEqual(extracted.insured_name, "FastTrack Logistics LLC")
        self.assertEqual(extracted.additional_premium_cents, 65000)
        self.assertEqual(extracted.taxes_and_fees_cents, 3250)
        self.assertEqual(extracted.total_cents, 68250)
        self.assertEqual(extracted.effective_date, "2026-10-15")
        self.assertIn("Blanket Additional Insured", extracted.description)


if __name__ == "__main__":
    unittest.main()


class TestSendGoogleChatAlertWebhook(unittest.TestCase):
    """M4: the ascend_sync webhook is a documented exception to the Chat
    single-identity rule — explicit target only, no identity fallback."""

    def test_no_webhook_url_configured_posts_nothing(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ROBIE_GOOGLE_CHAT_WEBHOOK_URL", None)
            with patch(
                "robie_job_engine.ascend_sync.request.urlopen"
            ) as mock_urlopen:
                self.assertFalse(ascend_sync.send_google_chat_alert("hello"))
        mock_urlopen.assert_not_called()

    def test_configured_webhook_posts_to_that_url_only(self) -> None:
        url = "https://chat.googleapis.com/v1/spaces/AAA/webhooks/secret"
        with patch.dict(os.environ, {"ROBIE_GOOGLE_CHAT_WEBHOOK_URL": url}):
            with patch(
                "robie_job_engine.ascend_sync.request.urlopen"
            ) as mock_urlopen:
                resp = MagicMock()
                resp.status = 200
                mock_urlopen.return_value.__enter__.return_value = resp
                self.assertTrue(ascend_sync.send_google_chat_alert("hello"))
        (req,), _kwargs = mock_urlopen.call_args
        self.assertEqual(req.full_url, url)
        payload = json.loads(req.data.decode("utf-8"))
        self.assertEqual(payload, {"text": "hello"})
