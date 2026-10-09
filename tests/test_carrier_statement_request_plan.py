import copy
import unittest
from datetime import datetime, timedelta, timezone
from carrier_statements.request_plan import plan_missing_request


class StatementRequestTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
        self.item = {"entity_id": "example-entity", "agency_id": "example-agency", "carrier_id": "example-carrier",
                     "account_reference": "synthetic-account", "printed_period": "2026-09",
                     "sender": "accounting@example.com", "contact": "billing@carrier.example.com",
                     "monthly_obligation": "verified_monthly", "contact_verified": True,
                     "existing_store_checked": True, "portal_checked": True, "mail_search_complete": True,
                     "evidence_checked_at": "2026-10-09T11:00:00+00:00", "evidence_references": ["private-proof"],
                     "statement_received": False, "active_request": False, "send_outcome_unknown": False,
                     "staff_work_in_progress": False, "newer_reply": False}
    def plan(self): return plan_missing_request(self.item, now=self.now)
    def test_complete_evidence_prepares_draft_without_send_authority(self):
        result = self.plan()
        self.assertEqual(result["status"], "DRAFT_READY_REVIEW_ONLY")
        self.assertFalse(result["send_enabled"])
        self.assertEqual(result["draft"]["to"], "billing@carrier.example.com")
    def test_received_chased_unknown_staff_and_reply_each_suppress(self):
        for key in ("statement_received", "active_request", "send_outcome_unknown", "staff_work_in_progress", "newer_reply"):
            with self.subTest(key=key):
                original = self.item[key]
                self.item[key] = True
                self.assertEqual(self.plan()["status"], "SUPPRESSED")
                self.assertIsNone(self.plan()["draft"])
                self.item[key] = original
    def test_each_missing_precondition_holds(self):
        for key in ("statement_received", "active_request", "send_outcome_unknown", "staff_work_in_progress", "newer_reply"):
            value = self.item.pop(key)
            self.assertIn("REQUEST_PRECONDITIONS_UNVERIFIED", self.plan()["holds"])
            self.item[key] = value
    def test_invoice_only_is_not_a_missing_monthly_statement(self):
        self.item["monthly_obligation"] = "not_monthly"
        self.assertEqual(self.plan()["status"], "NOT_APPLICABLE")
        self.item["monthly_obligation"] = "unknown"
        self.assertIn("MONTHLY_OBLIGATION_UNVERIFIED", self.plan()["holds"])
    def test_partial_query_or_unchecked_other_sources_holds(self):
        for key in ("mail_search_complete", "existing_store_checked", "portal_checked", "contact_verified"):
            self.item[key] = False
            self.assertIsNone(self.plan()["draft"])
            self.item[key] = True
    def test_stale_future_missing_and_naive_evidence_holds(self):
        for value in (None, "2026-10-01T00:00:00+00:00", "2026-10-09T13:00:00+00:00", "2026-10-09T11:00:00"):
            self.item["evidence_checked_at"] = value
            self.assertIn("SOURCE_EVIDENCE_STALE_OR_UNVERIFIED", self.plan()["holds"])
    def test_contact_change_cannot_reset_duplicate_key(self):
        key = self.plan()["request_key"]
        self.item["contact"] = "other@carrier.example.com"
        self.assertEqual(self.plan()["request_key"], key)
    def test_entity_and_period_have_distinct_request_keys(self):
        key = self.plan()["request_key"]
        self.item["entity_id"] = "other-entity"
        self.assertNotEqual(self.plan()["request_key"], key)
        self.item["entity_id"] = "example-entity"
        self.item["printed_period"] = "2026-08"
        self.assertNotEqual(self.plan()["request_key"], key)
    def test_headers_current_period_and_unknown_thread_hold(self):
        for changes in ({"contact": "bad@example.com\nBcc: stranger@example.com"},
                        {"printed_period": "2026-10"}, {"printed_period": "2026-13"},
                        {"thread_id": "other-thread"}, {"account_reference": ""},
                        {"evidence_references": []}):
            item = {**self.item, **changes}
            self.assertIsNone(plan_missing_request(item, now=self.now)["draft"])
    def test_verified_thread_is_retained_exactly(self):
        self.item.update(thread_id="verified-thread", thread_verified=True)
        self.assertEqual(self.plan()["draft"]["thread_id"], "verified-thread")
    def test_clock_and_freshness_policy_are_explicit(self):
        with self.assertRaises(ValueError): plan_missing_request(self.item, now=self.now.replace(tzinfo=None))
        with self.assertRaises(ValueError): plan_missing_request(self.item, now=self.now, max_evidence_age=timedelta(0))
    def test_does_not_mutate_source_observation(self):
        prior = copy.deepcopy(self.item)
        self.plan()
        self.assertEqual(self.item, prior)
