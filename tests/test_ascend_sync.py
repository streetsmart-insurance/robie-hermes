"""Unit tests for Ascend to EZLynx account event synchronization."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from robie_job_engine.ascend_sync import (
    AscendCancellationEvent,
    AscendEZLynxSyncManager,
    AscendPastDueEvent,
    AscendSyncStore,
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


class TestAscendEZLynxSyncManager(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_sync.db"
        self.store = AscendSyncStore(self.db_path)

        self.mock_api = MagicMock()
        self.mock_matcher = MagicMock()
        self.mock_poster = MagicMock()

        self.manager = AscendEZLynxSyncManager(
            api_client=self.mock_api,
            store=self.store,
            matcher=self.mock_matcher,
            poster=self.mock_poster,
        )

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_sync_cancellation_and_past_due(self) -> None:
        # Mock cancelation returns
        self.mock_api.fetch_cancelation_returns.return_value = [
            {
                "id": "cancel-001",
                "unearned_premium_cents": 179865,
                "unearned_commission_cents": 0,
                "unearned_surplus_lines_tax_cents": 0,
                "billable": {
                    "id": "bill-001",
                    "cancelation_effective_date": "2026-07-30",
                },
                "cancelation_docs": [
                    {
                        "id": "doc-001",
                        "title": "Cancellation Notice NPP6269395",
                        "url": "https://api.cloudinary.com/doc001.pdf",
                    }
                ],
            }
        ]
        self.mock_api.fetch_billable.return_value = {
            "policy_number": "NPP6269395",
            "program_id": "prog-001",
            "carrier": {"title": "Western World Insurance Company"},
            "wholesaler": {"title": "Johnson & Johnson, Inc"},
            "coverage_type": {"title": "General Liability"},
        }
        self.mock_api.fetch_program.return_value = {
            "insured": {"business_name": "Garden State Drywall Contractors Inc"},
            "producer": {"first_name": "Carlo", "last_name": "Ferrara"},
            "account_manager": {"first_name": "Hello", "last_name": "Inbox"},
        }

        # Mock invoices
        self.mock_api.fetch_invoices.return_value = [
            {
                "id": "inv-001",
                "invoice_number": "IXQPHVKEO9",
                "payer_name": "Pianka Construction LLC",
                "total_amount_cents": 141561,
                "due_date": "2026-09-04",
                "status": "past_due",
                "memo": "33470152 Excess Umbrella",
                "invoice_url": "https://api.cloudinary.com/inv001.pdf",
            }
        ]

        # Mock matcher
        self.mock_matcher.match_account.side_effect = [
            ("app-garden-state", "Zeus Quezada"),  # for cancellation
            ("app-pianka", "Ricardo Aguilar"),      # for past due
        ]

        res = self.manager.sync_once()

        self.assertEqual(res["cancellations_found"], 1)
        self.assertEqual(res["cancellations_synced"], 1)
        self.assertEqual(res["past_due_found"], 1)
        self.assertEqual(res["past_due_synced"], 1)
        self.assertEqual(len(res["errors"]), 0)

        # Verify discussion note posted for cancellation
        self.mock_poster.post_custom_note.assert_any_call(
            applicant_id="app-garden-state",
            title="🚨 Cancellation Notice - Western World Insurance Company - Policy #NPP6269395",
            note_text=unittest.mock.ANY,
            policy_number="NPP6269395",
            line_of_business="General Liability",
            carrier_name="Western World Insurance Company",
        )

        # Verify NO label applied (task assigned directly to CSR)
        self.mock_poster.apply_account_label.assert_not_called()

        # Verify task created for cancellation assigned to Zeus Quezada (CSR/Lead)
        self.mock_poster.create_task.assert_called_once_with(
            applicant_id="app-garden-state",
            title="🚨 CSR ACTION REQUIRED: Cancellation Notice - NPP6269395 - Western World Insurance Company - Due: 2026-07-30",
            description=unittest.mock.ANY,
            assigned_user="Zeus Quezada",
            due_days_out=0,
        )

        # Verify note posted for past due invoice (NO label applied)
        self.mock_poster.post_custom_note.assert_any_call(
            applicant_id="app-pianka",
            title="⚠️ Past Due Payment Alert - Ascend Invoice #IXQPHVKEO9",
            note_text=unittest.mock.ANY,
            policy_number="33470152",
        )

        # Second run should skip already processed items (Idempotency)
        res_second = self.manager.sync_once()
        self.assertEqual(res_second["cancellations_synced"], 0)
        self.assertEqual(res_second["past_due_synced"], 0)

    @patch("robie_job_engine.ascend_sync.send_google_chat_alert")
    def test_sync_signed_agreements_ready_to_bind(self, mock_chat) -> None:
        mock_api = MagicMock()
        mock_api.fetch_cancelation_returns.return_value = []
        mock_api.fetch_invoices.return_value = []
        mock_api.fetch_payouts.return_value = []
        mock_api.fetch_programs.return_value = [
            {
                "id": "prog_signed_123",
                "status": "checked_out",
                "selected_payment_option_type": "monthly_financed",
                "downpayment_amount_cents": 150000,
                "total_payable_amount_cents": 600000,
                "checkedout_at": "2026-09-07T10:00:00Z",
                "program_url": "https://checkout.useascend.com/streetsmart/overview?program_id=prog_signed_123",
                "insured": {"business_name": "Apex Builders LLC"},
                "producer": {"first_name": "Matthew", "last_name": "Mancina"},
                "billables": [
                    {
                        "policy_number": "POL-APEX-777",
                        "carrier": {"title": "Travelers"},
                        "wholesaler": {"title": "Amwins"},
                        "coverage_type": {"title": "Commercial General Liability"},
                    }
                ],
            }
        ]

        mock_matcher = MagicMock()
        mock_matcher.match_account.return_value = ("app-apex", "Matthew Mancina")

        mock_poster = MagicMock()
        mock_poster.post_custom_note.return_value = {"status": "success"}
        mock_poster.create_task.return_value = {"status": "success"}

        manager = AscendEZLynxSyncManager(
            api_client=mock_api,
            store=self.store,
            matcher=mock_matcher,
            poster=mock_poster,
        )

        res = manager.sync_once()
        self.assertEqual(res["signed_agreements_found"], 1)
        self.assertEqual(res["signed_agreements_synced"], 1)

        # Verify ready to bind note posted
        mock_poster.post_custom_note.assert_called_once_with(
            applicant_id="app-apex",
            title="🎉 Agreement Signed & Checked Out - Travelers - POL-APEX-777",
            note_text=unittest.mock.ANY,
            policy_number="POL-APEX-777",
            line_of_business="Commercial General Liability",
            carrier_name="Travelers",
        )

        # Verify ready to bind task assigned to Matthew Mancina
        mock_poster.create_task.assert_called_once_with(
            applicant_id="app-apex",
            title="🚨 READY TO BIND: POL-APEX-777 - Travelers - Apex Builders LLC (Agreement Signed)",
            description=unittest.mock.ANY,
            assigned_user="Matthew Mancina",
            due_days_out=0,
        )

        # Verify Google Chat alert called
        mock_chat.assert_called_once()

    @patch("robie_job_engine.ascend_sync.send_google_chat_alert")
    def test_sync_reinstatement_paid(self, mock_chat) -> None:
        mock_api = MagicMock()
        mock_api.fetch_cancelation_returns.return_value = []
        mock_api.fetch_programs.return_value = []
        mock_api.fetch_payouts.return_value = []
        mock_api.fetch_invoices.return_value = [
            {
                "id": "inv_reinst_999",
                "invoice_number": "INV-REINST-1",
                "status": "paid",
                "is_reinstatement": True,
                "payer_name": "Global Hauling Inc",
                "carrier_name": "Canal Insurance",
                "memo": "CANAL-8899 Commercial Auto Reinstatement",
                "total_amount_cents": 285000,
                "paid_at": "2026-09-07T11:00:00Z",
                "invoice_url": "https://api.cloudinary.com/reinstatement_receipt.pdf",
            }
        ]

        mock_matcher = MagicMock()
        mock_matcher.match_account.return_value = ("app-global-hauling", "Stef Reyes")

        mock_poster = MagicMock()
        mock_poster.post_custom_note.return_value = {"status": "success"}
        mock_poster.create_task.return_value = {"status": "success"}

        manager = AscendEZLynxSyncManager(
            api_client=mock_api,
            store=self.store,
            matcher=mock_matcher,
            poster=mock_poster,
        )

        res = manager.sync_once()
        self.assertEqual(res["reinstatements_found"], 1)
        self.assertEqual(res["reinstatements_synced"], 1)

        # Verify discussion note posted with plain text amount
        mock_poster.post_custom_note.assert_called_once_with(
            applicant_id="app-global-hauling",
            title="✅ Reinstatement Paid - CANAL-8899 - $2,850.00",
            note_text=unittest.mock.ANY,
            policy_number="CANAL-8899",
        )

        # Verify high priority reinstatement task created
        mock_poster.create_task.assert_called_once_with(
            applicant_id="app-global-hauling",
            title="🚨 REINSTATEMENT PAID: Request Carrier Reinstatement - CANAL-8899 - Global Hauling Inc",
            description=unittest.mock.ANY,
            assigned_user="Stef Reyes",
            due_days_out=0,
        )

        # Verify Google Chat alert called
        mock_chat.assert_called_once()

    @patch("robie_job_engine.ascend_sync.send_google_chat_alert")
    def test_sync_accounting_unpaid_supplier_payout(self, mock_chat) -> None:
        mock_api = MagicMock()
        mock_api.fetch_cancelation_returns.return_value = []
        mock_api.fetch_programs.return_value = []
        mock_api.fetch_invoices.return_value = []
        mock_api.fetch_payouts.return_value = [
            {
                "id": "payout_supp_failed_1",
                "payout_type": "supplier",
                "status": "failed",
                "net_payout_amount_cents": 420000,
                "program_id": "prog_xpt_1",
                "paying_at": "2026-09-05",
                "payable_account": {
                    "owner_name": "XPT Specialty",
                },
            }
        ]

        mock_poster = MagicMock()
        mock_poster.create_task.return_value = {"status": "success"}

        manager = AscendEZLynxSyncManager(
            api_client=mock_api,
            store=self.store,
            matcher=MagicMock(),
            poster=mock_poster,
        )

        res = manager.sync_once()
        self.assertEqual(res["payouts_found"], 1)
        self.assertEqual(res["payouts_synced"], 1)

        # Verify task created for accounting
        mock_poster.create_task.assert_called_once_with(
            applicant_id="0",
            title="⚠️ ACCOUNTING AUDIT: FAILED Supplier Payout to XPT Specialty ($4,200.00)",
            description=unittest.mock.ANY,
            assigned_user="Markley1",
            due_days_out=1,
        )

        # Verify Google Chat alert is NOT dispatched for accounting issues per directive
        mock_chat.assert_not_called()

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
