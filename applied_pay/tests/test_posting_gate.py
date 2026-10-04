"""Synthetic fixtures only. No network, no QBO, no EZLynx, no posts ever."""
import unittest
from datetime import datetime, timedelta, timezone

from applied_pay.posting_gate import build_plan, PostingGateError, DRY_RUN, APPROVED

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


def item(amount="250.00", payee="Acme Trucking LLC", policy="POL-123"):
    return {"kind": "qbo_deposit", "amount": amount, "payee": payee,
            "policy": policy}


def token(amount="250.00", payee="Acme Trucking LLC", policy="POL-123"):
    return {"token_id": "TOK-1", "amount": amount, "payee": payee,
            "policy": policy, "approved_by": "Carlo Ferrara",
            "expires_at": (NOW + timedelta(hours=1)).isoformat()}


class PostingGateTest(unittest.TestCase):
    def test_default_is_dry_run(self):
        plan = build_plan([item()], now=NOW)
        self.assertEqual(DRY_RUN, plan["items"][0]["status"])
        self.assertEqual(DRY_RUN, plan["default"])
        self.assertIsNone(plan["items"][0]["approval_token_id"])

    def test_exact_token_approves(self):
        plan = build_plan([item()], approvals=[token()], now=NOW)
        entry = plan["items"][0]
        self.assertEqual(APPROVED, entry["status"])
        self.assertEqual("TOK-1", entry["approval_token_id"])
        self.assertEqual("Carlo Ferrara", entry["approved_by"])

    def test_transfer_never_allowed(self):
        plan = build_plan([item()], approvals=[token()], now=NOW)
        self.assertFalse(plan["transfer_allowed"])
        self.assertEqual(0, plan["posts_made"])
        self.assertEqual(0, plan["qbo_posts"])
        self.assertEqual(0, plan["ezlynx_writes"])

    def test_wrong_amount_refused(self):
        plan = build_plan([item()], approvals=[token(amount="251.00")], now=NOW)
        self.assertEqual(DRY_RUN, plan["items"][0]["status"])

    def test_wrong_payee_refused(self):
        plan = build_plan([item()], approvals=[token(payee="Other LLC")], now=NOW)
        self.assertEqual(DRY_RUN, plan["items"][0]["status"])

    def test_wrong_policy_refused(self):
        plan = build_plan([item()], approvals=[token(policy="POL-999")], now=NOW)
        self.assertEqual(DRY_RUN, plan["items"][0]["status"])

    def test_expired_token_rejected(self):
        bad = token()
        bad["expires_at"] = (NOW - timedelta(minutes=1)).isoformat()
        with self.assertRaises(PostingGateError):
            build_plan([item()], approvals=[bad], now=NOW)

    def test_missing_field_rejected(self):
        bad = token()
        del bad["approved_by"]
        with self.assertRaises(PostingGateError):
            build_plan([item()], approvals=[bad], now=NOW)

    def test_token_single_use(self):
        plan = build_plan([item(), item()], approvals=[token()], now=NOW)
        approved = [i for i in plan["items"] if i["status"] == APPROVED]
        dry = [i for i in plan["items"] if i["status"] == DRY_RUN]
        self.assertEqual(1, len(approved))
        self.assertEqual(1, len(dry))

    def test_each_item_needs_own_token(self):
        toks = [token(), dict(token(), token_id="TOK-2")]
        plan = build_plan([item(), item()], approvals=toks, now=NOW)
        self.assertTrue(all(i["status"] == APPROVED for i in plan["items"]))

    def test_unsupported_kind_fails_closed(self):
        with self.assertRaises(PostingGateError):
            build_plan([dict(item(), kind="wire_transfer")], now=NOW)

    def test_nonpositive_amount_fails_closed(self):
        with self.assertRaises(PostingGateError):
            build_plan([dict(item(), amount="0.00")], now=NOW)

    def test_non_test_environment_refuses(self):
        with self.assertRaises(PostingGateError):
            build_plan([item()], environment="PROD", now=NOW)

    def test_module_has_no_network_imports(self):
        import applied_pay.posting_gate as pg
        import sys
        src = open(pg.__file__, encoding="utf-8").read()
        for banned in ("urllib", "requests", "httpx", "socket"):
            self.assertNotIn("import %s" % banned, src)


if __name__ == "__main__":
    unittest.main()
