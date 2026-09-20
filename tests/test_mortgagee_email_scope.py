"""Real 32-column ingestion -> job worker regressions. No live services."""
import csv
import io
import unittest
from unittest.mock import Mock, patch

from robie_job_engine import gmail_report_ingestion as ing
from robie_job_engine import mortgagee_verification_worker as worker
from robie_job_engine import report_email_source as source
from robie_job_engine.mortgagee_policy_scope import (
    PolicyScopeUnavailable, normalize_lob, resolve_policy_metadata,
)


def csv_rows(*overrides):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=ing.expected_headers("4372"))
    writer.writeheader()
    for override in overrides:
        writer.writerow({"Policy Number": "TEST-HOME-1", "Applicant ID": "TEST-A",
                         "Policy Master ID": "TEST-M", "Task Status": "Open",
                         "Task Due Date": "01/01/2025", "Task ID": "TEST-T", **override})
    return source.rows_from_csv_bytes("4372", buf.getvalue().encode())


def metadata(**overrides):
    return {"policyNumber": "TEST-HOME-1", "applicantId": "TEST-A",
            "policyMasterId": "TEST-M", "lineOfBusiness": "Home",
            "expirationDate": "2026-10-30", **overrides}


def run_worker(rows, lookup=None):
    with patch.object(worker, "fetch_report_rows", return_value=rows) as fetch, \
         patch.object(worker, "_ledger_for_job", return_value=None), \
         patch.object(worker, "record_outcomes", side_effect=lambda job, outcomes, **kw: outcomes):
        result = worker.MortgageeVerificationWorker(policy_lookup=lookup).perform(
            {"id": "scope-test", "action_type": "mortgagee_verification",
             "payload": {"as_of": "2026-09-20", "voice_enabled": False}},
            idempotency_key="scope-test")
        assert fetch.call_args.kwargs["source"] == "email"
    return result


class MortgageeEmailScopeTests(unittest.TestCase):
    def test_wrong_report_id_refused_before_fetch(self):
        with patch.object(worker, "fetch_report_rows") as fetch:
            result = worker.MortgageeVerificationWorker().perform(
                {"payload": {"report_id": "4247"}}, idempotency_key="wrong-report")
        self.assertFalse(result.succeeded)
        self.assertFalse(result.retryable)
        fetch.assert_not_called()

    def test_four_closed_tasks_are_excluded_before_lookup(self):
        rows = csv_rows(*[{"Policy Number": f"TEST-{i}", "Task Status": " Closed "}
                          for i in range(4)])
        lookup = Mock()
        result = run_worker(rows, lookup)
        self.assertEqual(result.destination["skipped_closed"], 4)
        self.assertEqual(result.destination["skipped_lob"], 0)
        self.assertEqual(result.destination["in_scope_count"], 0)
        self.assertEqual(result.detail["policy_outcomes"], [])
        self.assertEqual(len(result.detail["excluded_tasks"]), 4)
        lookup.search_policy_by_number.assert_not_called()

    def test_open_task_survives_closed_first_duplicate_policy(self):
        rows = csv_rows({"Task Status": "Closed"}, {"Task ID": "OPEN-T"}, {"Task ID": "OPEN-T2"})
        self.assertEqual(len(rows), 3)
        lookup = Mock()
        lookup.search_policy_by_number.return_value = {"status": "success", "data": [metadata()]}
        result = run_worker(rows, lookup)
        self.assertEqual(result.destination["skipped_closed"], 1)
        self.assertEqual(result.destination["in_scope_count"], 1)
        self.assertEqual(len(result.detail["policy_outcomes"]), 1)
        lookup.search_policy_by_number.assert_called_once_with("TEST-HOME-1")

    def test_property_aliases_reach_planning_with_real_policy_expiration(self):
        for lob in ("Home", "Home NJ", "HO", "homeowners", "Flood", "FLD", "Dwelling Fire", "Condo"):
            with self.subTest(lob=lob):
                rows = csv_rows({})
                self.assertNotIn("expiration_date", rows[0])
                self.assertEqual(rows[0]["due_date"], "01/01/2025")
                lookup = Mock()
                lookup.search_policy_by_number.return_value = {"data": [metadata(lineOfBusiness=lob)]}
                result = run_worker(rows, lookup)
                self.assertEqual(result.destination["in_scope_count"], 1)
                outcome, = result.detail["policy_outcomes"]
                self.assertIn("retrieval_intent_recorded", outcome["actions_taken"])
                self.assertNotIn("overdue", outcome["reason"])
                self.assertEqual(outcome["evidence"]["policy_scope"]["expiration_date"], "2026-10-30")

    def test_known_nonproperty_is_the_only_lob_skip(self):
        for lob in ("Auto", "WC", "GL"):
            lookup = Mock()
            lookup.search_policy_by_number.return_value = {"data": [metadata(lineOfBusiness=lob)]}
            result = run_worker(csv_rows({}), lookup)
            self.assertEqual(result.destination["skipped_lob"], 1)
            self.assertEqual(result.destination["unresolved_scope"], 0)

    def test_unknown_or_missing_lob_is_recorded_not_skipped(self):
        for lob in ("", "unrecognized"):
            lookup = Mock()
            lookup.search_policy_by_number.return_value = {"data": [metadata(lineOfBusiness=lob)]}
            result = run_worker(csv_rows({}), lookup)
            self.assertEqual(result.destination["skipped_lob"], 0)
            self.assertEqual(result.destination["unresolved_scope"], 1)
            outcome, = result.detail["policy_outcomes"]
            self.assertEqual(outcome["status"], "not_done")
            self.assertEqual(outcome["actions_taken"], ["policy_scope_blocked"])

    def test_blank_or_unknown_task_status_blocks_without_lookup(self):
        for status in ("", "Completed", "unknown"):
            lookup = Mock()
            result = run_worker(csv_rows({"Task Status": status}), lookup)
            self.assertEqual(result.destination["unresolved_scope"], 1)
            lookup.search_policy_by_number.assert_not_called()

    def test_conflicting_duplicate_policy_identity_blocks(self):
        lookup = Mock()
        result = run_worker(csv_rows({}, {"Applicant ID": "WRONG"}), lookup)
        self.assertEqual(result.destination["unresolved_scope"], 1)
        lookup.search_policy_by_number.assert_not_called()

    def test_missing_expiration_does_not_use_task_due_date(self):
        lookup = Mock()
        lookup.search_policy_by_number.return_value = {"data": [metadata(expirationDate="")]}
        result = run_worker(csv_rows({}), lookup)
        outcome, = result.detail["policy_outcomes"]
        self.assertEqual(outcome["status"], "not_done")
        self.assertIn("expiration date", outcome["reason"])

    def test_lookup_failure_is_redacted_and_blocks(self):
        lookup = Mock()
        lookup.search_policy_by_number.side_effect = RuntimeError("secret-test-sentinel")
        result = run_worker(csv_rows({}), lookup)
        self.assertEqual(result.destination["unresolved_scope"], 1)
        self.assertNotIn("secret-test-sentinel", str(result))

    def test_default_lookup_bound_lazily_once(self):
        lookup = Mock()
        lookup.search_policy_by_number.return_value = {"data": [metadata()]}
        with patch.object(worker, "default_policy_lookup", return_value=lookup) as factory:
            result = run_worker(csv_rows({}))
        factory.assert_called_once_with()
        self.assertEqual(result.destination["in_scope_count"], 1)

    def test_default_lookup_configuration_failure_stays_unresolved(self):
        with patch.object(worker, "default_policy_lookup", side_effect=RuntimeError("secret-test-sentinel")):
            result = run_worker(csv_rows({}))
        self.assertEqual(result.destination["unresolved_scope"], 1)
        self.assertNotIn("secret-test-sentinel", str(result))

    def test_verifier_refuses_empty_and_unresolved_scope(self):
        for result in (run_worker(csv_rows({"Task Status": "Closed"})),
                       run_worker(csv_rows({}), Mock(search_policy_by_number=Mock(return_value={"data": []})))):
            checkpoint = {"destination": result.destination, "detail": result.detail}
            with patch.object(worker, "_fresh_checkpoint", return_value=checkpoint), \
                 patch.object(worker, "_durable_policy_states", return_value={}):
                checked = worker.MortgageeVerificationVerifier().verify({}, checkpoint)
            self.assertFalse(checked.verified)


class PolicyMetadataTests(unittest.TestCase):
    def test_failed_api_envelope_cannot_supply_policy_facts(self):
        lookup = Mock()
        lookup.search_policy_by_number.return_value = {"status": "error", "data": [metadata()]}
        with self.assertRaises(PolicyScopeUnavailable):
            resolve_policy_metadata(csv_rows({})[0], lookup)

    def test_iso_timestamp_expiration_normalizes_without_using_task_due(self):
        result = self.resolve([metadata(expirationDate="2026-10-30T00:00:00Z")])
        self.assertEqual(result["expiration_date"], "2026-10-30")

    def test_missing_master_id_is_not_silently_ignored(self):
        record = metadata()
        del record["policyMasterId"]
        with self.assertRaises(PolicyScopeUnavailable):
            self.resolve([record])

    def test_row_lob_conflict_stays_blocked(self):
        lookup = Mock()
        lookup.search_policy_by_number.return_value = {"data": [metadata()]}
        row = {**csv_rows({})[0], "lob": "Flood"}
        with self.assertRaises(PolicyScopeUnavailable):
            resolve_policy_metadata(row, lookup)

    def resolve(self, records):
        lookup = Mock()
        lookup.search_policy_by_number.return_value = {"data": records}
        return resolve_policy_metadata(csv_rows({})[0], lookup)

    def test_wrong_policy_applicant_or_master_refused(self):
        for change in ({"policyNumber": "OTHER"}, {"applicantId": "OTHER"},
                       {"policyMasterId": "OTHER"}, {"applicantId": ""}):
            with self.subTest(change=change), self.assertRaises(PolicyScopeUnavailable):
                self.resolve([metadata(**change)])

    def test_multiple_terms_not_arbitrarily_selected(self):
        with self.assertRaises(PolicyScopeUnavailable):
            self.resolve([metadata(), metadata(expirationDate="2027-10-30")])

    def test_conflicting_lob_keys_refused(self):
        with self.assertRaises(PolicyScopeUnavailable):
            self.resolve([metadata(lob="Auto")])

    def test_lookup_cannot_supply_lender_or_authorization(self):
        result = self.resolve([metadata(producer_review_complete=True, loan_number="DO-NOT-COPY",
                                        portal_lender_lookup={"servicer": "DO-NOT-COPY"})])
        self.assertNotIn("producer_review_complete", result)
        self.assertNotIn("loan_number", result)
        self.assertNotIn("portal_lender_lookup", result)

    def test_prefix_is_never_a_lob(self):
        self.assertIsNone(normalize_lob("SAHO581361"))
        self.assertIsNone(normalize_lob("FLD272903"))


if __name__ == "__main__":
    unittest.main()
