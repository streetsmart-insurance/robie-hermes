"""Unit tests for robie_job_engine.cancellation_gate. Fakes only."""

from __future__ import annotations

import sys
import unittest

sys.modules.pop("robie_job_engine.verification_common", None)

from robie_job_engine import cancellation_gate as cg


class _FakeClient:
    def __init__(self, policies=None, error=None):
        self._policies = policies
        self._error = error
        self.calls = []

    def get_policies(self, applicant_id):
        self.calls.append(applicant_id)
        if self._error is not None:
            raise self._error
        return self._policies


class CancellationGateTests(unittest.TestCase):
    def test_no_client_fails_closed(self):
        active, reason = cg.check_policy_active(
            applicant_id="1", policy_number="P1", ezlynx_client=None
        )
        self.assertFalse(active)
        self.assertEqual(reason, "cancellation check unavailable")

    def test_no_applicant_id_fails_closed(self):
        client = _FakeClient(policies=[])
        active, reason = cg.check_policy_active(
            policy_number="P1", ezlynx_client=client
        )
        self.assertFalse(active)
        self.assertEqual(reason, "cancellation check unavailable")
        self.assertEqual(client.calls, [])

    def test_client_error_fails_closed(self):
        client = _FakeClient(error=RuntimeError("transport down"))
        active, reason = cg.check_policy_active(
            applicant_id="164706131", policy_number="P1", ezlynx_client=client
        )
        self.assertFalse(active)
        self.assertEqual(reason, "cancellation check unavailable")

    def test_active_policy(self):
        client = _FakeClient(
            policies=[
                {
                    "policyNumber": "WC PI 2695561-001",
                    "policyStatusViewModelID": 1,
                    "cancellationDate": None,
                }
            ]
        )
        active, reason = cg.check_policy_active(
            applicant_id="164706131",
            policy_number="wc pi 2695561-001",  # case-insensitive match
            ezlynx_client=client,
        )
        self.assertTrue(active)
        self.assertEqual(reason, "active")
        self.assertEqual(client.calls, ["164706131"])

    def test_cancelled_status_blocks(self):
        client = _FakeClient(
            policies=[
                {
                    "policyNumber": "P1",
                    "policyStatusViewModelID": 2,
                    "cancellationDate": None,
                }
            ]
        )
        active, reason = cg.check_policy_active(
            applicant_id="1", policy_number="P1", ezlynx_client=client
        )
        self.assertFalse(active)
        self.assertIn("policyStatusViewModelID=2", reason)

    def test_cancellation_date_blocks(self):
        client = _FakeClient(
            policies=[
                {
                    "policyNumber": "P1",
                    "policyStatusViewModelID": 1,
                    "cancellationDate": "2026-08-01T00:00:00",
                }
            ]
        )
        active, reason = cg.check_policy_active(
            applicant_id="1", policy_number="P1", ezlynx_client=client
        )
        self.assertFalse(active)
        self.assertIn("cancellationDate=2026-08-01", reason)

    def test_string_status_and_blank_date_treated_as_active(self):
        client = _FakeClient(
            policies=[
                {"policyNumber": "P1", "policyStatusViewModelID": "1", "cancellationDate": ""}
            ]
        )
        active, _ = cg.check_policy_active(
            applicant_id="1", policy_number="P1", ezlynx_client=client
        )
        self.assertTrue(active)

    def test_policy_not_found_fails_closed(self):
        client = _FakeClient(
            policies=[
                {"policyNumber": "OTHER", "policyStatusViewModelID": 1, "cancellationDate": None}
            ]
        )
        active, reason = cg.check_policy_active(
            applicant_id="1", policy_number="P1", ezlynx_client=client
        )
        self.assertFalse(active)
        self.assertIn("not found", reason)

    def test_ambiguous_without_policy_number_fails_closed(self):
        client = _FakeClient(
            policies=[
                {"policyNumber": "P1", "policyStatusViewModelID": 1, "cancellationDate": None},
                {"policyNumber": "P2", "policyStatusViewModelID": 1, "cancellationDate": None},
            ]
        )
        active, reason = cg.check_policy_active(applicant_id="1", ezlynx_client=client)
        self.assertFalse(active)
        self.assertIn("ambiguous", reason)

    def test_single_policy_without_number_ok(self):
        client = _FakeClient(
            policies=[
                {"policyNumber": "P1", "policyStatusViewModelID": 1, "cancellationDate": None}
            ]
        )
        active, _ = cg.check_policy_active(applicant_id="1", ezlynx_client=client)
        self.assertTrue(active)


if __name__ == "__main__":
    unittest.main()
