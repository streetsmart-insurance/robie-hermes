"""Unit tests for the policy-change verification worker. Fakes only."""

import os
import sys
import tempfile
import types
import unittest
from dataclasses import dataclass, field

# Fake data only — never real credentials, customer artifacts, or secrets.
CLIENT_EMAIL = "insured-client@example.test"
CARRIER_EMAIL = "underwriter@examplecarrier.test"
CSR_EMAIL = "csr@example.test"

from _sibling_fakes import install_fake, teardown_fakes

_INSTALLED: dict = {}  # module name -> (original sys.modules entry, installed fake)

def _track_fake(name, fake):
    """Install a fake sibling module (see _sibling_fakes for the rules)."""
    install_fake(_INSTALLED, name, fake)

def teardown_module(module):
    """Restore faked sys.modules entries (see _sibling_fakes)."""
    teardown_fakes(_INSTALLED)

def _install_fake_verification_common():
    fake = types.ModuleType("robie_job_engine.verification_common")
    calls = {
        "record_outcomes": [],
        "record_login_gap": [],
        "authorized": {},
    }

    @dataclass
    class FakePolicyOutcome:
        policy_number: str = ""
        status: str = ""
        reason: str = ""
        extra: dict = field(default_factory=dict)

        def __init__(self, data=None, **kwargs):
            if isinstance(data, dict):
                kwargs = {**data, **kwargs}
            self.policy_number = kwargs.get("policy_number", "")
            self.status = kwargs.get("status", "")
            self.reason = kwargs.get("reason", "")
            self.extra = kwargs

    def is_action_authorized(job, action):
        return bool(calls["authorized"].get(action, False))

    def record_outcomes(store, job_id, job_type, outcomes):
        calls["record_outcomes"].append(
            {"store": store, "job_id": job_id, "job_type": job_type, "outcomes": list(outcomes)}
        )

    def record_login_gap(store, job_id, job_type, portal_name, step, whats_missing):
        calls["record_login_gap"].append(
            {
                "store": store,
                "job_id": job_id,
                "job_type": job_type,
                "portal_name": portal_name,
                "step": step,
                "whats_missing": whats_missing,
            }
        )

    fake.PolicyOutcome = FakePolicyOutcome
    fake.is_action_authorized = is_action_authorized
    fake.record_outcomes = record_outcomes
    fake.record_login_gap = record_login_gap
    fake.calls = calls
    install_fake(_INSTALLED, "robie_job_engine.verification_common", fake)
    return fake

def _install_fake_verification_mailer():
    fake = types.ModuleType("robie_job_engine.verification_mailer")
    sent = []

    def send_verification_email(*, to, cc, subject, text_body, html_body=None):
        sent.append({"to": list(to), "cc": list(cc), "subject": subject, "text_body": text_body})
        return {"message_id": "fake-msg-1"}

    fake.send_verification_email = send_verification_email
    fake.sent = sent
    install_fake(_INSTALLED, "robie_job_engine.verification_mailer", fake)
    return fake

def _install_fake_report_email_source():
    fake = types.ModuleType("robie_job_engine.report_email_source")
    state = {"rows": [], "error": None, "calls": []}

    def fetch_email_report_rows(*, report_id, fields=None, **kwargs):
        state["calls"].append({"report_id": report_id, "fields": fields})
        if state["error"] is not None:
            raise state["error"]
        return [dict(r) for r in state["rows"]]

    fake.fetch_email_report_rows = fetch_email_report_rows
    fake.state = state
    install_fake(_INSTALLED, "robie_job_engine.report_email_source", fake)
    return fake

_common = _install_fake_verification_common()
_mailer = _install_fake_verification_mailer()
_email_source = _install_fake_report_email_source()

from robie_job_engine import policy_change_worker as pcw  # noqa: E402
from robie_job_engine.models import JobStatus  # noqa: E402

def _request(**overrides):
    base = {
        "request_id": "REQ-1",
        "policy_number": "WC PI 2695561-001",
        "insured_name": "Sun Volt Energy LLC",
        "effective_date": "2026-09-15",
        "change_action": "add",
        "affected_item": "2021 Ford Transit",
        "requested_values": {"vehicle": "2021 Ford Transit", "coverage": "hired/non-owned"},
        "premium_expectation": "150.00",
        "ambiguous": False,
        "ambiguity_reasons": [],
        "coverage_review_flag": False,
    }
    base.update(overrides)
    return base

def _carrier(**fields):
    merged = {
        "effective_date": "2026-09-15",
        "vehicle": "2021 Ford Transit",
        "coverage": "hired/non-owned",
        "premium": "150.00",
    }
    merged.update(fields)
    return {"fields": merged, "document_ref": "END-123", "carrier": "Pie Insurance"}

def _ezlynx(**fields):
    merged = {
        "effective_date": "2026-09-15",
        "vehicle": "2021 Ford Transit",
        "coverage": "hired/non-owned",
        "premium": "150.00",
    }
    merged.update(fields)
    return merged

class GateFlipTests(unittest.TestCase):
    """The 4359 pilot gate flip (Carlo's 2026-09-22 ratification).

    Each test discriminates against the pre-pilot branch, where perform()
    held at the kill switch BEFORE any row fetch: retryable=False, error
    "report 4359 schema unverified", and no outcomes were ever recorded.
    """

    def setUp(self):
        _email_source.state["rows"] = []
        _email_source.state["error"] = None
        _email_source.state["calls"] = []
        self._tmpdir = tempfile.mkdtemp(prefix="pcw-gate-test-")

    def _clear_row(self):
        return {
            "Policy Number": "P-100",
            "Change Request Created Date": "2026-09-19",
            "policy_number": "P-100",
            "insured_name": "Sun Volt Energy LLC",
            "department": "Commercial",
            "carrier": "Pie Insurance",
            "effective_date": "2026-10-01",
            "change_action": "add",
            "affected_item": "2022 Honda Civic",
            "requested_values": {"vehicle": "2022 Honda Civic"},
            "premium_expectation": "200.00",
            "_identity_key": "P-100 | 2026-09-19",
        }

    def _ambiguous_row(self):
        return {
            "Policy Number": "P-200",
            "Change Request Created Date": "2026-09-19",
            "policy_number": "P-200",
            "_identity_key": "P-200 | 2026-09-19",
        }

    def _perform(self):
        db_path = os.path.join(self._tmpdir, "registry.db")
        worker = pcw.PolicyChangeWorker()
        return worker.perform(
            {
                "id": "job-1",
                "action_type": "policy_change_verification",
                "payload": {"db_path": db_path},
            },
            idempotency_key="k1",
        )

    def test_missing_registry_db_path_holds(self):
        worker = pcw.PolicyChangeWorker()
        result = worker.perform(
            {"id": "job-1", "action_type": "policy_change_verification", "payload": {}},
            idempotency_key="k1",
        )
        # Fail closed: without a registry db the schema gate cannot run.
        self.assertFalse(result.succeeded)
        self.assertFalse(result.retryable)
        self.assertIn("registry db path unavailable", result.error)
        self.assertEqual(_email_source.state["calls"], [])

    def test_enabled_flag_is_set(self):
        self.assertIs(pcw.POLICY_CHANGE_ENABLED, True)

    def test_perform_processes_open_requests(self):
        _email_source.state["rows"] = [self._clear_row(), self._ambiguous_row()]
        result = self._perform()
        self.assertTrue(result.succeeded)
        self.assertIsNone(result.error)
        # The kill switch held BEFORE any fetch; a fetch call proves the flip.
        self.assertEqual(
            [c["report_id"] for c in _email_source.state["calls"]], ["4359"]
        )
        outcomes = result.detail["outcomes"]
        self.assertEqual(len(outcomes), 2)
        clear, ambiguous = outcomes
        # No evidence retrieval is wired yet: a clear request waits on the
        # carrier; an ambiguous one routes to the CSR. Nothing auto-passes.
        self.assertEqual(clear["status"], "pending")
        self.assertEqual(clear["waiting_on"], "carrier")
        self.assertEqual(clear["policy_number"], "P-100")
        # The composite per-request identity becomes the tracked request id.
        self.assertEqual(clear["evidence"]["request_id"], "P-100 | 2026-09-19")
        self.assertEqual(ambiguous["status"], "not_done")
        self.assertEqual(ambiguous["waiting_on"], "csr")
        # The worker never closes the underlying request.
        self.assertFalse(result.detail["request_closed"])

    def test_fetch_failure_holds_retryable(self):
        _email_source.state["error"] = RuntimeError("no email today")
        result = self._perform()
        # Past the old kill switch: the fetch was attempted and its failure
        # surfaces as a retryable hold, never a crash or guessed rows.
        self.assertTrue(_email_source.state["calls"])
        self.assertFalse(result.succeeded)
        self.assertTrue(result.retryable)
        self.assertEqual(result.hold_status, JobStatus.NEEDS_CLARIFICATION)
        self.assertIn("row fetch failed", result.error)

    def test_untrackable_row_refused(self):
        row = self._clear_row()
        del row["_identity_key"]
        _email_source.state["rows"] = [row]
        result = self._perform()
        self.assertFalse(result.succeeded)
        self.assertIn("untrackable", result.error)

class ReconstructRequestTests(unittest.TestCase):
    def test_reconstructs_clear_request(self):
        row = {
            "request_id": "REQ-9",
            "policy_number": "P-1",
            "effective_date": "2026-10-01",
            "change_action": "add",
            "affected_item": "2022 Honda Civic",
            "requested_values": {"vehicle": "2022 Honda Civic"},
            "premium_expectation": "200.00",
        }
        request = pcw.reconstruct_request(row, "adding a vehicle per client email")
        self.assertFalse(request["ambiguous"])
        self.assertEqual(request["effective_date"], "2026-10-01")
        self.assertEqual(request["change_action"], "add")
        self.assertEqual(request["affected_item"], "2022 Honda Civic")

    def test_ambiguous_request_flagged(self):
        request = pcw.reconstruct_request({"request_id": "REQ-X"}, "some discussion text")
        self.assertTrue(request["ambiguous"])
        self.assertTrue(request["ambiguity_reasons"])

class ThreeWayMatchTests(unittest.TestCase):
    def test_pass_when_all_three_match(self):
        result = pcw.three_way_match(_request(), _carrier(), _ezlynx())
        self.assertEqual(result["state"], "pass")
        self.assertFalse(result["exceptions"])

    def test_request_unclear(self):
        request = _request(ambiguous=True, ambiguity_reasons=["missing effective date"])
        result = pcw.three_way_match(request, _carrier(), _ezlynx())
        self.assertEqual(result["state"], "request_unclear")

    def test_waiting_for_carrier_without_evidence(self):
        result = pcw.three_way_match(_request(), None, _ezlynx())
        self.assertEqual(result["state"], "waiting_for_carrier")

    def test_carrier_correction_required(self):
        result = pcw.three_way_match(
            _request(), _carrier(vehicle="2020 Ford Transit"), _ezlynx()
        )
        self.assertEqual(result["state"], "carrier_correction_required")
        self.assertEqual(result["exceptions"][0]["exception_side"], "carrier")

    def test_ezlynx_correction_required(self):
        result = pcw.three_way_match(
            _request(), _carrier(), _ezlynx(coverage="owned only")
        )
        self.assertEqual(result["state"], "ezlynx_correction_required")
        self.assertEqual(result["exceptions"][0]["exception_side"], "ezlynx")

    def test_carrier_mismatch_takes_precedence_over_ezlynx(self):
        result = pcw.three_way_match(
            _request(),
            _carrier(vehicle="2020 Ford Transit"),
            _ezlynx(coverage="owned only"),
        )
        self.assertEqual(result["state"], "carrier_correction_required")

    def test_coverage_review_required(self):
        result = pcw.three_way_match(
            _request(coverage_review_flag=True), _carrier(), _ezlynx()
        )
        self.assertEqual(result["state"], "coverage_review_required")

    def test_all_six_states_known(self):
        self.assertEqual(
            set(pcw.RESULT_STATES),
            {
                "pass",
                "request_unclear",
                "waiting_for_carrier",
                "carrier_correction_required",
                "ezlynx_correction_required",
                "coverage_review_required",
            },
        )

class ClientRecipientGuardTests(unittest.TestCase):
    def test_blocks_client_in_to(self):
        with self.assertRaises(ValueError) as ctx:
            pcw.assert_no_client_recipient(
                [CARRIER_EMAIL, CLIENT_EMAIL], [CSR_EMAIL], client_addresses=[CLIENT_EMAIL]
            )
        self.assertIn("client address present", str(ctx.exception))

    def test_blocks_client_in_cc_case_insensitive(self):
        with self.assertRaises(ValueError):
            pcw.assert_no_client_recipient(
                [CARRIER_EMAIL], [CLIENT_EMAIL.upper()], client_addresses=[CLIENT_EMAIL]
            )

    def test_carrier_only_passes(self):
        pcw.assert_no_client_recipient(
            [CARRIER_EMAIL], [CSR_EMAIL], client_addresses=[CLIENT_EMAIL]
        )

class CarrierEmailGateTests(unittest.TestCase):
    def setUp(self):
        _mailer.sent.clear()
        _common.calls["authorized"].clear()

    def _job(self):
        return {
            "id": "job-1",
            "action_type": "policy_change_verification",
            "payload": {"client_emails": [CLIENT_EMAIL]},
        }

    def test_unauthorized_email_never_sent(self):
        worker = pcw.PolicyChangeWorker()
        outcome = worker._maybe_send_carrier_email(
            self._job(),
            to=[CARRIER_EMAIL],
            cc=[CSR_EMAIL],
            subject="Policy Change Request REQ-1",
            body="hello carrier",
        )
        self.assertFalse(outcome["sent"])
        self.assertEqual(_mailer.sent, [])

    def test_client_recipient_raises_before_send(self):
        _common.calls["authorized"]["send_carrier_email"] = True
        worker = pcw.PolicyChangeWorker()
        with self.assertRaises(ValueError):
            worker._maybe_send_carrier_email(
                self._job(),
                to=[CARRIER_EMAIL],
                cc=[CLIENT_EMAIL],
                subject="Policy Change Request REQ-1",
                body="hello carrier",
            )
        self.assertEqual(_mailer.sent, [])

    def test_authorized_carrier_email_sent_via_mailer(self):
        _common.calls["authorized"]["send_carrier_email"] = True
        worker = pcw.PolicyChangeWorker()
        outcome = worker._maybe_send_carrier_email(
            self._job(),
            to=[CARRIER_EMAIL],
            cc=[CSR_EMAIL],
            subject="Policy Change Request REQ-1",
            body="hello carrier",
        )
        self.assertTrue(outcome["sent"])
        self.assertEqual(len(_mailer.sent), 1)
        self.assertNotIn(CLIENT_EMAIL, _mailer.sent[0]["to"] + _mailer.sent[0]["cc"])

class LoginGapTests(unittest.TestCase):
    def setUp(self):
        _common.calls["record_login_gap"].clear()

    def test_portal_login_stall_records_gap_and_stays_pending(self):
        store = object()
        worker = pcw.PolicyChangeWorker(store=store)
        job = {"id": "job-9", "action_type": "policy_change_verification", "payload": {}}
        fragment = worker._record_portal_login_gap(
            job,
            portal_name="ExampleCarrier Portal",
            step="mfa",
            whats_missing="OTP delivery step not covered by SOP",
        )
        self.assertEqual(fragment["status"], "pending")
        self.assertEqual(fragment["waiting_on"], "carrier")
        recorded = _common.calls["record_login_gap"]
        self.assertEqual(len(recorded), 1)
        self.assertEqual(
            (
                recorded[0]["store"],
                recorded[0]["job_id"],
                recorded[0]["job_type"],
                recorded[0]["portal_name"],
                recorded[0]["step"],
                recorded[0]["whats_missing"],
            ),
            (
                store,
                "job-9",
                "policy_change_verification",
                "ExampleCarrier Portal",
                "mfa",
                "OTP delivery step not covered by SOP",
            ),
        )

class ConfirmationReportTests(unittest.TestCase):
    def test_report_has_fixed_sections_and_signoff(self):
        request = _request()
        match_result = pcw.three_way_match(request, _carrier(), _ezlynx())
        report = pcw.build_confirmation_report(
            request=request,
            match_result=match_result,
            carrier_evidence=_carrier(),
            ezlynx_data=_ezlynx(),
            document_refs=["END-123.pdf"],
            next_action="confirmation posted; request resolved",
            follow_up_date="2026-09-17",
        )
        for section in (
            "### Requested",
            "### Carrier issued",
            "### EZLynx recorded",
            "### Matches",
            "### Exceptions",
            "### Documents",
            "### Result",
            "### Next action",
        ):
            self.assertIn(section, report)
        self.assertTrue(report.rstrip().endswith("ROBIE was here"))

class PolicyChangeVerifierTests(unittest.TestCase):
    def _action(self, outcomes, request_closed=False):
        return {
            "destination": {"report_id": "4359"},
            "detail": {"outcomes": outcomes, "request_closed": request_closed},
        }

    def _job(self):
        return {
            "action_type": "policy_change_verification",
            "payload": {"client_emails": [CLIENT_EMAIL]},
        }

    def test_valid_outcomes_verify(self):
        outcomes = [
            {
                "policy_number": "P-1",
                "status": "done",
                "reason": "pass: request, carrier evidence, and EZLynx data match field by field",
                "actions_taken": ["request_reconstructed"],
                "evidence": {"document_refs": ["END-123.pdf"], "match_state": "pass"},
            }
        ]
        result = pcw.PolicyChangeVerifier().verify(self._job(), self._action(outcomes))
        self.assertTrue(result.verified)

    def test_client_email_in_evidence_fails(self):
        outcomes = [
            {
                "policy_number": "P-1",
                "status": "done",
                "reason": "pass: match",
                "actions_taken": ["request_reconstructed"],
                "evidence": {"notes": f"sent update to {CLIENT_EMAIL}", "document_refs": ["END-123.pdf"]},
            }
        ]
        result = pcw.PolicyChangeVerifier().verify(self._job(), self._action(outcomes))
        self.assertFalse(result.verified)
        self.assertIn("carrier-only", result.error)

    def test_client_email_in_actions_taken_fails(self):
        outcomes = [
            {
                "policy_number": "P-1",
                "status": "pending",
                "reason": "waiting_for_carrier: chasing",
                "actions_taken": [f"emailed {CLIENT_EMAIL}"],
                "evidence": {},
            }
        ]
        result = pcw.PolicyChangeVerifier().verify(self._job(), self._action(outcomes))
        self.assertFalse(result.verified)

    def test_done_without_evidence_refs_fails(self):
        outcomes = [
            {
                "policy_number": "P-1",
                "status": "done",
                "reason": "pass: match",
                "actions_taken": ["request_reconstructed"],
                "evidence": {},
            }
        ]
        result = pcw.PolicyChangeVerifier().verify(self._job(), self._action(outcomes))
        self.assertFalse(result.verified)
        self.assertIn("no evidence references", result.error)

    def test_worker_closing_request_fails(self):
        outcomes = [
            {
                "policy_number": "P-1",
                "status": "done",
                "reason": "pass: match",
                "actions_taken": [],
                "evidence": {"document_refs": ["END-123.pdf"]},
            }
        ]
        result = pcw.PolicyChangeVerifier().verify(
            self._job(), self._action(outcomes, request_closed=True)
        )
        self.assertFalse(result.verified)
        self.assertIn("Quality Controller", result.error)

if __name__ == "__main__":
    unittest.main()
