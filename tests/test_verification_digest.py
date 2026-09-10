"""Unit tests for the daily verification digest worker. Fakes only."""

import hashlib
import sys
import types
import unittest
from datetime import datetime, timezone

from _sibling_fakes import install_fake, teardown_fakes

_INSTALLED: dict = {}  # module name -> (original sys.modules entry, installed fake)

def _track_fake(name, fake):
    """Install a fake sibling module (see _sibling_fakes for the rules)."""
    install_fake(_INSTALLED, name, fake)

def teardown_module(module):
    """Restore faked sys.modules entries (see _sibling_fakes)."""
    teardown_fakes(_INSTALLED)

def _install_fake_verification_common(outcomes_by_type):
    fake = types.ModuleType("robie_job_engine.verification_common")
    calls = {"authorized": {}}

    def is_action_authorized(job, action):
        return bool(calls["authorized"].get(action, False))

    def read_outcomes(store, job_type, *, since_hours=24):
        assert since_hours == 24
        return [dict(item) for item in outcomes_by_type.get(job_type, [])]

    fake.is_action_authorized = is_action_authorized
    fake.read_outcomes = read_outcomes
    fake.calls = calls
    install_fake(_INSTALLED, "robie_job_engine.verification_common", fake)
    return fake

def _outcomes():
    return {
        "manual_renewal_verification": [
            {
                "policy_number": "WC PI 2695561-001",
                "insured_name": "Sun Volt Energy LLC",
                "department": "Commercial Lines",
                "carrier": "Pie Insurance",
                "status": "done",
                "reason": "pass: renewal terms retrieved and filed",
                "actions_taken": ["portal_check"],
                "waiting_on": None,
                "next_action": "none",
                "follow_up_date": None,
                "evidence": {"ezlynx_note_id": "1123945626"},
            },
            {
                "policy_number": "WC PI 2695561-002",
                "insured_name": "Volt Solar Inc",
                "department": "Commercial Lines",
                "carrier": "Pie Insurance",
                "status": "pending",
                "reason": "waiting_for_carrier: underwriter reply pending",
                "actions_taken": ["email_sent"],
                "waiting_on": "carrier",
                "next_action": "follow up with underwriter",
                "follow_up_date": "2026-09-17",
                "evidence": {},
            },
        ],
        "audit_verification": [
            {
                "policy_number": "AUD-7",
                "insured_name": "Main St Bakery",
                "department": "Personal Lines",
                "carrier": "Guard",
                "status": "not_done",
                "reason": "portal login failed",
                "actions_taken": ["portal_check"],
                "waiting_on": "csr",
                "next_action": "CSR to obtain papers manually",
                "follow_up_date": "2026-09-11",
                "evidence": {},
            },
        ],
        "mortgagee_verification": [],
        "policy_change_verification": [
            {
                "policy_number": "PC-3",
                "request_id": "REQ-3",
                "insured_name": "Trucking LLC",
                "department": "Trucking and Transportation",
                "carrier": "Progressive",
                "status": "pending",
                "reason": "waiting_for_carrier: endorsement not yet issued",
                "actions_taken": ["request_reconstructed"],
                "waiting_on": "carrier",
                "next_action": "chase carrier for issued endorsement",
                "follow_up_date": "2026-09-18",
                "evidence": {"match_state": "waiting_for_carrier"},
            },
        ],
    }

_common = _install_fake_verification_common(_outcomes())

from robie_job_engine import verification_digest_worker as digest  # noqa: E402
from robie_job_engine.models import JobStatus  # noqa: E402

class DigestAggregationTests(unittest.TestCase):
    def test_groups_by_department_with_status_sections(self):
        report = digest.build_digest_report(
            _outcomes(), run_at=datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
        )
        self.assertIn(digest.DIGEST_TITLE, report)
        self.assertIn("## Commercial Lines", report)
        self.assertIn("### Done (1)", report)
        self.assertIn("### Pending (1)", report)
        self.assertIn("## Personal Lines", report)
        self.assertIn("### Not done (1)", report)
        self.assertIn("## Trucking and Transportation", report)
        self.assertTrue(report.rstrip().endswith("ROBIE was here"))

    def test_every_row_carries_queue_status_reason_owner_next_action(self):
        report = digest.build_digest_report(_outcomes())
        self.assertIn("[queue 4247]", report)  # manual renewals row
        self.assertIn("[queue 4246]", report)  # audit row
        self.assertIn("[queue 4359]", report)  # policy change row
        self.assertIn("Owner: carrier", report)
        self.assertIn("Owner: csr", report)
        self.assertIn("Next: follow up with underwriter", report)
        self.assertIn("Evidence: ezlynx_note_id 1123945626", report)

    def test_empty_queue_shows_explicit_no_items_line(self):
        report = digest.build_digest_report(_outcomes())
        self.assertIn(
            "Mortgagee verifications (4372) (report 4372): 0 — no items in this queue in the last 24h",
            report,
        )

    def test_no_policy_silently_omitted(self):
        report = digest.build_digest_report(_outcomes())
        for policy in ("WC PI 2695561-001", "WC PI 2695561-002", "AUD-7", "PC-3"):
            self.assertIn(policy, report)

class DigestWorkerTests(unittest.TestCase):
    def setUp(self):
        _common.calls["authorized"].clear()
        self._orig_deliver = digest.accountability_delivery.deliver_report
        self.sent = []

        def fake_deliver(report_path, *, mode, delivery, environment):
            self.sent.append(
                {"path": str(report_path), "mode": mode, "delivery": delivery}
            )
            return [
                {
                    "kind": "gmail",
                    "destination": delivery["email_recipients"][mode],
                    "message_id": "fake-digest-1",
                    "sender": delivery["email_sender"],
                }
            ]

        digest.accountability_delivery.deliver_report = fake_deliver

    def tearDown(self):
        digest.accountability_delivery.deliver_report = self._orig_deliver

    def _job(self, tmp_path):
        return {
            "id": "digest-1",
            "action_type": "daily_verification_digest",
            "payload": {
                "output_dir": str(tmp_path),
                "email_sender": "robie@streetsmart.insurance",
            },
        }

    def test_authorized_send_delivers_to_carlo(self):
        import tempfile

        _common.calls["authorized"]["send_digest_email"] = True
        with tempfile.TemporaryDirectory() as tmp:
            worker = digest.VerificationDigestWorker(store=object())
            result = worker.perform(self._job(tmp), idempotency_key="d1")
        self.assertTrue(result.succeeded)
        self.assertEqual(len(self.sent), 1)
        call = self.sent[0]
        self.assertEqual(call["mode"], "verification")
        self.assertEqual(
            call["delivery"]["email_recipients"]["verification"],
            ["carlo@streetsmart.insurance"],
        )
        self.assertEqual(
            call["delivery"]["email_sender"], "robie@streetsmart.insurance"
        )

    def test_unauthorized_send_never_executes(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            worker = digest.VerificationDigestWorker(store=object())
            result = worker.perform(self._job(tmp), idempotency_key="d2")
        self.assertFalse(result.succeeded)
        self.assertEqual(result.hold_status, JobStatus.NEEDS_CLARIFICATION)
        self.assertEqual(self.sent, [])

    def test_missing_output_dir_holds(self):
        worker = digest.VerificationDigestWorker(store=object())
        result = worker.perform(
            {"action_type": "daily_verification_digest", "payload": {}},
            idempotency_key="d3",
        )
        self.assertFalse(result.succeeded)
        self.assertEqual(result.hold_status, JobStatus.NEEDS_CLARIFICATION)

class DigestVerifierTests(unittest.TestCase):
    def setUp(self):
        self._orig_deliver = digest.accountability_delivery.deliver_report
        self._orig_verify = digest.accountability_delivery.verify_delivery_receipts
        digest.accountability_delivery.deliver_report = (
            lambda report_path, **kwargs: [
                {
                    "kind": "gmail",
                    "destination": ["carlo@streetsmart.insurance"],
                    "message_id": "fake-digest-9",
                    "sender": "robie@streetsmart.insurance",
                }
            ]
        )
        self.readback_ok = True

        def fake_verify(receipts):
            return self.readback_ok, [{"id": r.get("message_id"), "exists": self.readback_ok} for r in receipts]

        digest.accountability_delivery.verify_delivery_receipts = fake_verify
        _common.calls["authorized"]["send_digest_email"] = True

    def tearDown(self):
        digest.accountability_delivery.deliver_report = self._orig_deliver
        digest.accountability_delivery.verify_delivery_receipts = self._orig_verify
        _common.calls["authorized"].clear()

    def _run_worker(self, tmp):
        worker = digest.VerificationDigestWorker(store=object())
        job = {
            "id": "digest-9",
            "action_type": "daily_verification_digest",
            "payload": {
                "output_dir": str(tmp),
                "email_sender": "robie@streetsmart.insurance",
            },
        }
        result = worker.perform(job, idempotency_key="d9")
        self.assertTrue(result.succeeded)
        return job, {"destination": result.destination, "detail": result.detail}

    def test_fresh_readback_verifies(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            job, action = self._run_worker(tmp)
            result = digest.VerificationDigestVerifier().verify(job, action)
        self.assertTrue(result.verified)
        self.assertTrue(result.evidence.authoritative)

    def test_tampered_artifact_fails(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            job, action = self._run_worker(tmp)
            path = action["destination"]["artifact_path"]
            with open(path, "a", encoding="utf-8") as handle:
                handle.write("tampered\n")
            result = digest.VerificationDigestVerifier().verify(job, action)
        self.assertFalse(result.verified)

    def test_failed_sent_mailbox_readback_fails(self):
        import tempfile

        self.readback_ok = False
        with tempfile.TemporaryDirectory() as tmp:
            job, action = self._run_worker(tmp)
            result = digest.VerificationDigestVerifier().verify(job, action)
        self.assertFalse(result.verified)

if __name__ == "__main__":
    unittest.main()
