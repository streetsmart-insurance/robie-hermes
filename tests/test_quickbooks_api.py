"""Unit tests for QuickBooks Online REST API client and accounting workflows."""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from robie_job_engine.quickbooks_api import (
    QuickBooksApiClient,
    QuickBooksConfig,
)


class TestQuickBooksApi(unittest.TestCase):
    def test_unconfigured_client_graceful_staging(self) -> None:
        client = QuickBooksApiClient(config=None)
        self.assertFalse(client.is_configured)

        # Connection check returns unconfigured
        conn = client.test_connection()
        self.assertEqual(conn["status"], "unconfigured")

        # Commission deposit stages gracefully
        res = client.record_commission_deposit(
            program_id="prog_test_123",
            policy_number="POL123456",
            insured_name="Acme Roofing Corp",
            amount_cents=50000,
            payout_id="pay_999",
        )
        self.assertEqual(res["status"], "staged")
        self.assertEqual(res["amount"], 500.0)
        self.assertEqual(res["policy_number"], "POL123456")

        # Supplier payout stages gracefully
        res_supp = client.record_supplier_payout_bill(
            program_id="prog_test_123",
            policy_number="POL123456",
            wholesaler_name="XPT Specialty",
            net_amount_cents=350000,
            payout_id="pay_supplier_1",
        )
        self.assertEqual(res_supp["status"], "staged")
        self.assertEqual(res_supp["amount"], 3500.0)
        self.assertEqual(res_supp["wholesaler"], "XPT Specialty")

    @patch("robie_job_engine.quickbooks_api.request.urlopen")
    def test_configured_client_token_refresh_and_deposit(self, mock_urlopen) -> None:
        cfg = QuickBooksConfig(
            client_id="dummy_cid",
            client_secret="dummy_csec",
            refresh_token="dummy_rtok",
            realm_id="123456789",
            is_production=False,
        )
        client = QuickBooksApiClient(config=cfg)
        self.assertTrue(client.is_configured)

        # Mock token refresh response
        mock_token_resp = MagicMock()
        mock_token_resp.read.return_value = json.dumps({
            "access_token": "fresh_access_token_abc",
            "expires_in": 3600,
            "refresh_token": "new_refresh_token_xyz",
        }).encode("utf-8")

        # Mock deposit creation response
        mock_deposit_resp = MagicMock()
        mock_deposit_resp.read.return_value = json.dumps({
            "Deposit": {
                "Id": "9876",
                "TotalAmt": 450.0,
                "TxnDate": "2026-09-07",
            }
        }).encode("utf-8")

        mock_urlopen.side_effect = [
            MagicMock(__enter__=MagicMock(return_value=mock_token_resp)),
            MagicMock(__enter__=MagicMock(return_value=mock_deposit_resp)),
        ]

        res = client.record_commission_deposit(
            program_id="prog_live_456",
            policy_number="POL999",
            insured_name="Peak Performance LLC",
            amount_cents=45000,
        )

        self.assertEqual(res["status"], "success")
        self.assertEqual(res["deposit"]["Id"], "9876")
        self.assertEqual(client.config.refresh_token, "new_refresh_token_xyz")
