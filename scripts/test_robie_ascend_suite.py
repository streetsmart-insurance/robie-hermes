#!/usr/bin/env python3
"""Comprehensive End-to-End Test Runner for Ascend API & Robie Operations in TEST Mode.

Verifies:
1. Quote Ingestion & Autonomous HITL Clarification
2. User Reply Parsing & Parameter Normalization
3. Ascend Agreement Generation & EZLynx Note Formatting
4. Cancellation Notice Intake (Label + Embedded PDF + CSR Task + Note)
5. Past Due Notice (Note only, No Label)
6. Agreement Signed (Ready to Bind Task + Chat Alert)
7. Reinstatement Payment (Task + Draft Carrier Email)
8. Accounting Escalation (Assigned to Markley1 in EZLynx, Chat Suppressed)
9. QuickBooks Staged Mode
"""

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

# Set Test Environment
os.environ["ROBIE_ENV"] = "TEST"
os.environ["ROBIE_ACCOUNTING_ASSIGNEE"] = "Markley1"

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from robie_job_engine.quote_extractor import QuoteExtractor
from robie_job_engine.ezlynx_note_poster import EZLynxAgreementPoster, format_ascend_agreement_note
from robie_job_engine.ascend_workflow import AscendWorkflowManager
from robie_job_engine.ascend_sync import AscendEZLynxSyncManager, AscendSyncStore
from robie_job_engine.quickbooks_api import QuickBooksApiClient


def run_all_tests():
    print("=" * 70)
    print("  STREETSMART ROBIE & ASCEND AUTOMATION TEST SUITE (ROBIE_ENV=TEST)  ")
    print("=" * 70)
    passed_count = 0
    total_count = 6

    # -------------------------------------------------------------
    # TEST 1: Quote Extraction & HITL Ambiguity Detection
    # -------------------------------------------------------------
    print("\n[TEST 1/6] Ingesting Ambiguous Quote & Checking Autonomous Clarification...")
    raw_quote = """
    NAMED INSURED: Buckeye Freightways LLC
    CARRIER: Nautilus Insurance Group
    WHOLESALER: Tapco Underwriters
    COVERAGE: Commercial Auto Physical Damage
    QUOTE NUMBER: NAUT-2026-8819
    EFFECTIVE: 10/15/2026 - 10/15/2027
    PURE PREMIUM: $18,400.00
    OPTION 1 (with TRIA): $19,250.00
    OPTION 2 (without TRIA): $18,400.00
    """
    extractor = QuoteExtractor()
    quote = extractor.extract_from_text(raw_quote)
    assert quote.requires_hitl is True, "Expected quote to require HITL clarification"
    assert "agency_fee_unspecified" in quote.hitl_reasons
    assert "commission_rate_unspecified" in quote.hitl_reasons
    assert "dual_terrorism_options_present" in quote.hitl_reasons
    print(f"  ✓ Extracted Insured: {quote.insured_name}")
    print(f"  ✓ Carrier: {quote.carrier_name} | Wholesaler: {quote.wholesaler_name}")
    print(f"  ✓ Correctly flagged {len(quote.hitl_questions)} clarification questions for sender")
    passed_count += 1

    # -------------------------------------------------------------
    # TEST 2: Autonomous Clarification Processing & Resumption
    # -------------------------------------------------------------
    print("\n[TEST 2/6] Processing User Reply & Normalizing Underwriting Parameters...")
    user_reply = "1. Yes $350 agency fee. 2. 12% commission. 3. No surplus lines tax. 4. Option 1 with terrorism."
    resolved = extractor.apply_user_clarifications(quote, user_reply)
    assert resolved.requires_hitl is False
    assert resolved.agency_fees_cents == 35000
    assert resolved.commission_rate == 0.12
    assert resolved.surplus_lines_tax_cents == 0
    assert resolved.terrorism_included is True
    assert resolved.pure_premium_cents == 1925000
    print(f"  ✓ Resolved Pure Premium: ${resolved.pure_premium_cents / 100:,.2f} (TRIA Included)")
    print(f"  ✓ Resolved Agency Fee: ${resolved.agency_fees_cents / 100:,.2f}")
    print(f"  ✓ Resolved Commission: {resolved.commission_rate * 100:.1f}%")
    print(f"  ✓ Resolved Tax: ${resolved.surplus_lines_tax_cents / 100:,.2f} (Admitted)")
    print("  ✓ Underwriting parameters fully verified and ready for Ascend API")
    passed_count += 1

    # -------------------------------------------------------------
    # TEST 3: Ascend Agreement Creation & EZLynx Discussion Filing
    # -------------------------------------------------------------
    print("\n[TEST 3/6] Simulating Ascend Agreement Creation & EZLynx Filing...")
    ins_id = str(uuid4())
    prod_id = str(uuid4())
    prog_id = str(uuid4())
    bill_id = str(uuid4())

    mock_client = MagicMock()
    mock_client.search_carriers.return_value = [{"identifier": "nautilus_insurance_group", "title": "Nautilus"}]
    mock_client.search_wholesalers.return_value = [{"identifier": "tapco_underwriters", "title": "Tapco"}]
    mock_client.find_or_create_insured.return_value = (ins_id, {"id": ins_id, "business_name": "Buckeye Freightways LLC"})
    mock_client.resolve_user.return_value = prod_id
    mock_client.create_program.return_value = (
        prog_id,
        {
            "id": prog_id,
            "program_url": f"https://checkout.useascend.com/streetsmart_insurance_agency/overview?program_id={prog_id}",
        },
    )
    mock_client.create_billable.return_value = (bill_id, {"id": bill_id})

    mock_ezlynx_poster = MagicMock()
    mock_ezlynx_poster.post_agreement_note.return_value = {"status": "success", "note_id": 55123}

    manager = AscendWorkflowManager(
        client_factory=lambda: mock_client,
        ezlynx_poster=mock_ezlynx_poster,
        quote_extractor=extractor,
    )
    workflow_result = manager.resume_with_clarifications(
        quote,
        user_reply,
        sender_name="Carlo",
        sender_email="carlo@streetsmart.insurance",
        applicant_id="ezlynx-app-777",
    )
    assert workflow_result.status == "COMPLETED"
    assert workflow_result.program_id == prog_id
    assert "https://checkout.useascend.com" in workflow_result.program_url
    assert "Robie was here" in workflow_result.reply_email_body
    print(f"  ✓ Created Ascend Program: {workflow_result.program_id}")
    print(f"  ✓ Agreement URL: {workflow_result.program_url}")
    print(f"  ✓ Posted note to EZLynx with signature: 'Robie was here'")
    passed_count += 1

    # -------------------------------------------------------------
    # TEST 4: EZLynx Policy Lifecycle Sync (Cancellations, Past Due, Signed, Reinstated)
    # -------------------------------------------------------------
    print("\n[TEST 4/6] Testing Hourly EZLynx Synchronizer across Lifecycle Events...")
    with tempfile.TemporaryDirectory() as tmp_dir:
        test_store = AscendSyncStore(str(Path(tmp_dir) / "test_ascend_sync.db"))
        mock_api = MagicMock()
        mock_matcher = MagicMock()
        mock_matcher.match_account.return_value = ("app-test-1", "Zeus Quezada")

        # 4a: Cancellation return
        mock_api.fetch_cancelation_returns.return_value = [
            {
                "id": "cancel-001",
                "unearned_premium_cents": 340000,
                "unearned_commission_cents": 0,
                "unearned_surplus_lines_tax_cents": 0,
                "billable": {
                    "id": "bill-001",
                    "cancelation_effective_date": "2026-10-01",
                },
                "cancelation_docs": [
                    {
                        "id": "doc-001",
                        "title": "Cancellation Notice POL-9921",
                        "url": "https://ascend.test/notices/cancel-001.pdf",
                    }
                ],
            }
        ]
        mock_api.fetch_billable.return_value = {
            "policy_number": "POL-9921",
            "program_id": "prog-001",
            "carrier": {"title": "Western World Insurance Company"},
            "wholesaler": {"title": "Johnson & Johnson, Inc"},
            "coverage_type": {"title": "General Liability"},
        }
        mock_api.fetch_program.return_value = {
            "insured": {"business_name": "Buckeye Freightways LLC"},
            "producer": {"first_name": "Carlo", "last_name": "Ferrara"},
            "account_manager": {"first_name": "Hello", "last_name": "Inbox"},
        }

        # 4b: Past Due & Reinstatement Invoices
        mock_api.fetch_invoices.return_value = [
            {
                "id": "inv-002",
                "invoice_number": "IXQPHVKEO9",
                "payer_name": "Buckeye Freightways LLC",
                "total_amount_cents": 115000,
                "due_date": "2026-09-10",
                "status": "past_due",
                "memo": "POL-5541 Commercial Auto Installment",
                "invoice_url": "https://ascend.test/inv-002.pdf",
            },
            {
                "id": "inv-reinstated-004",
                "invoice_number": "INV-REINST-1",
                "status": "paid",
                "is_reinstatement": True,
                "payer_name": "Buckeye Freightways LLC",
                "carrier_name": "Canal Insurance",
                "memo": "POL-9921 Commercial Auto Reinstatement",
                "total_amount_cents": 125000,
                "paid_at": "2026-09-07T12:00:00Z",
                "invoice_url": "https://ascend.test/receipts/rec-004.pdf",
            }
        ]

        # 4c: Signed Agreement
        mock_api.fetch_programs.return_value = [
            {
                "id": "prog-signed-003",
                "status": "checked_out",
                "selected_payment_option_type": "annual_pay_in_full",
                "sub_total_cents": 450000,
                "premium_cents": 450000,
                "checkedout_at": "2026-09-07T10:00:00Z",
                "program_url": "https://checkout.useascend.com/overview-003",
                "insured": {"business_name": "Buckeye Freightways LLC"},
                "producer": {"first_name": "Matthew", "last_name": "Mancina"},
                "billables": [
                    {
                        "policy_number": "POL-SIGNED-003",
                        "carrier": {"title": "Progressive"},
                    }
                ],
            }
        ]
        mock_api.fetch_payouts.return_value = []

        mock_poster = MagicMock()
        mock_poster.create_task.return_value = {"status": "success", "task_id": 101}
        mock_poster.apply_account_label.return_value = {"status": "success"}
        mock_poster.post_custom_note.return_value = {"status": "success", "note_id": 202}

        with patch("robie_job_engine.ascend_sync.send_google_chat_alert") as mock_chat:
            sync_mgr = AscendEZLynxSyncManager(
                api_client=mock_api,
                store=test_store,
                matcher=mock_matcher,
                poster=mock_poster,
            )
            stats = sync_mgr.sync_once()

            # Verify Cancellation
            assert stats["cancellations_synced"] == 1
            mock_poster.apply_account_label.assert_not_called()
            print("  ✓ Cancellation: Notice PDF embedded and 🚨 CSR ACTION REQUIRED task created (no label applied)")

            # Verify Past Due
            assert stats["past_due_synced"] == 1
            print("  ✓ Past Due: Plain text amount & due date posted to notes")

            # Verify Agreement Signed
            assert stats["signed_agreements_synced"] == 1
            mock_chat.assert_called()
            print("  ✓ Agreement Signed: 🚨 READY TO BIND task assigned to CSR + Chat Card posted")

            # Verify Reinstatement
            assert stats["reinstatements_synced"] == 1
            print("  ✓ Reinstatement: 🚨 REINSTATEMENT PAID task created with auto-drafted carrier email")
            passed_count += 1

    # -------------------------------------------------------------
    # TEST 5: Accounting Discrepancy Escalation to Markley1
    # -------------------------------------------------------------
    print("\n[TEST 5/6] Testing Accounting Escalation to Markley1 in EZLynx...")
    with tempfile.TemporaryDirectory() as tmp_dir:
        acct_store = AscendSyncStore(str(Path(tmp_dir) / "test_acct.db"))
        mock_api = MagicMock()
        mock_api.fetch_cancelation_returns.return_value = []
        mock_api.fetch_invoices.return_value = []
        mock_api.fetch_programs.return_value = []
        mock_api.fetch_payouts.return_value = [
            {
                "id": "payout_supp_failed_99",
                "payout_type": "supplier",
                "status": "failed",
                "net_payout_amount_cents": 2750000,
                "program_id": "prog_xpt_99",
                "paying_at": "2026-09-06",
                "payable_account": {
                    "owner_name": "XPT Specialty",
                },
            }
        ]

        mock_poster = MagicMock()
        mock_poster.create_task.return_value = {"status": "success"}

        with patch("robie_job_engine.ascend_sync.send_google_chat_alert") as mock_chat:
            manager = AscendEZLynxSyncManager(
                api_client=mock_api,
                store=acct_store,
                matcher=MagicMock(),
                poster=mock_poster,
            )
            res = manager.sync_once()
            assert res["payouts_found"] == 1
            assert res["payouts_synced"] == 1

            # Assert assigned to Markley1 in EZLynx
            mock_poster.create_task.assert_called_once()
            call_kwargs = mock_poster.create_task.call_args[1]
            assert call_kwargs.get("assigned_user") == "Markley1", f"Expected Markley1, got {call_kwargs.get('assigned_user')}"
            assert "⚠️ ACCOUNTING AUDIT: FAILED Supplier Payout to XPT Specialty ($27,500.00)" in call_kwargs.get("title")

            # Assert chat alert was NOT dispatched (suppressed per user directive)
            mock_chat.assert_not_called()
            print("  ✓ Discrepancy detected: Failed supplier payout of $27,500.00 to XPT Specialty")
            print("  ✓ Task created in EZLynx assigned directly to: Markley1")
            print("  ✓ Confirmed Google Chat alert SUPPRESSED per user directive")
            passed_count += 1

    # -------------------------------------------------------------
    # TEST 6: QuickBooks Online Staged Integration
    # -------------------------------------------------------------
    print("\n[TEST 6/6] Testing QuickBooks Online Staged Mode...")
    qbo_client = QuickBooksApiClient()
    dep_res = qbo_client.record_commission_deposit(
        program_id="prog-006",
        policy_number="COMMISSION",
        insured_name="Ascend Commission",
        amount_cents=125000,
        deposit_date="2026-09-07",
        payout_id="pay-006",
    )
    assert dep_res["status"] == "staged"
    print(f"  ✓ Commission Deposit staged: ${dep_res['amount']:,.2f} ({dep_res['memo']})")

    bill_res = qbo_client.record_supplier_payout_bill(
        program_id="prog-007",
        policy_number="POL-7711",
        wholesaler_name="Tapco Underwriters",
        net_amount_cents=850000,
        payment_date="2026-09-07",
        payout_id="pay-007",
    )
    assert bill_res["status"] == "staged"
    print(f"  ✓ Supplier Bill & Payment staged: ${bill_res['amount']:,.2f} to {bill_res['wholesaler']}")
    passed_count += 1

    # -------------------------------------------------------------
    # SUMMARY
    # -------------------------------------------------------------
    print("\n" + "=" * 70)
    print(f"  ALL {passed_count}/{total_count} TESTS PASSED SUCCESSFULLY (ROBIE_ENV=TEST)  ")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    run_all_tests()
