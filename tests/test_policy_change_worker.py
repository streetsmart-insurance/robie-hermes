"""Unit tests for the policy-change verification worker. Fakes only."""

import types
import unittest
from dataclasses import dataclass, field
from unittest import mock

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

_common = _install_fake_verification_common()
_mailer = _install_fake_verification_mailer()

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

class KillSwitchTests(unittest.TestCase):
    def test_kill_switch_holds_at_needs_clarification(self):
        self.assertFalse(pcw.POLICY_CHANGE_ENABLED)
        worker = pcw.PolicyChangeWorker()
        result = worker.perform(
            {"action_type": "policy_change_verification", "payload": {}},
            idempotency_key="k1",
        )
        self.assertFalse(result.succeeded)
        self.assertEqual(result.hold_status, JobStatus.NEEDS_CLARIFICATION)
        self.assertIn("report 4359 schema unverified", result.error)

    def test_kill_switch_never_flipped_by_accident(self):
        self.assertIs(pcw.POLICY_CHANGE_ENABLED, False)

    def test_schema_gate_holds_even_if_kill_switch_is_patched(self):
        worker = pcw.PolicyChangeWorker()
        with mock.patch.object(pcw, "POLICY_CHANGE_ENABLED", True), mock.patch.object(
            pcw, "_load_fetch_report_rows", side_effect=AssertionError("fetch touched")
        ):
            result = worker.perform(
                {"action_type": "policy_change_verification", "payload": {}},
                idempotency_key="k-enabled",
            )
        self.assertFalse(result.succeeded)
        self.assertEqual(result.hold_status, JobStatus.NEEDS_CLARIFICATION)
        self.assertIn("disabled", result.error)


class OpenRequestFetchTests(unittest.TestCase):
    def test_iter_open_requests_returns_fetched_rows(self):
        fetched = [_sample_looker_row()]

        def fake_fetch(**kwargs):
            self.assertEqual(kwargs["report_id"], "4359")
            self.assertIsNone(kwargs["fields"])
            self.assertIsNone(kwargs["filters"])
            self.assertEqual(kwargs["db_path"], "/tmp/policy-change-test.db")
            return list(fetched)

        worker = pcw.PolicyChangeWorker()
        job = {
            "action_type": "policy_change_verification",
            "payload": {"db_path": "/tmp/policy-change-test.db"},
        }
        with mock.patch.object(pcw, "_load_fetch_report_rows", return_value=fake_fetch):
            rows = list(worker._iter_open_requests(job))
        self.assertEqual(rows, fetched)
        parsed = pcw.parse_policy_change_row(rows[0])
        self.assertEqual(parsed["policy_number"], "POL-4359-001")
        self.assertEqual(parsed["change_request_created_date"], "09/01/2026")

    def test_process_open_request_keys_real_identity(self):
        worker = pcw.PolicyChangeWorker(store=object())
        job = {
            "id": "job-1",
            "action_type": "policy_change_verification",
            "payload": {},
        }
        with mock.patch.object(
            worker, "_iter_open_requests", return_value=[_sample_csv_row()]
        ):
            result = worker._process_open_requests(job, idempotency_key="k-row")
        self.assertTrue(result.succeeded)
        self.assertFalse(result.detail["request_closed"])
        outcome = result.detail["outcomes"][0]
        self.assertEqual(outcome["policy_number"], "POL-4359-001")
        self.assertEqual(outcome["evidence"]["change_request_created_date"], "09/01/2026")
        self.assertEqual(outcome["evidence"]["look_id"], "4602")
        self.assertNotIn("request_id", outcome["evidence"])
        self.assertIn("waiting_for_carrier", outcome["reason"])
        self.assertIn("POL-4359-001", result.destination["confirmation_reports"][0]["report"])

def _sample_csv_row(**overrides):
    """One synthetic 19-column Gmail CSV row. No customer data."""
    row = {
        "Account Name": "Example Trucking LLC",
        "Applicant ID": "A-100",
        "Policy Number": "POL-4359-001",
        "Line Of Business": "Commercial Auto",
        "Effective Date": "09/15/2026",
        "Master Company": "Example Carrier",
        "Request Status": "Open",
        "Created By": "CSR Example",
        "Written Premium": "1500.00",
        "Premium - Annualized": "2400.00",
        "Branch": "Commercial Lines",
        "Department": "Commercial Lines",
        "Service Team": "Service A",
        "Assigned Producer": "Producer Example",
        "CSR": "CSR Example",
        "Preferred Language": "English",
        "Applicant Labels": "VIP",
        "Policy Labels": "Audit",
        "Change Request Created Date": "09/01/2026",
    }
    row.update(overrides)
    return row


def _sample_looker_row():
    """The same request keyed by the verified Look 4602 headers."""
    csv_row = _sample_csv_row()
    return {
        looker: csv_row[csv_name]
        for looker, csv_name in (
            ("Applicant Data Account Name", "Account Name"),
            ("Policy Transaction Data Applicant ID", "Applicant ID"),
            ("Policy Change Request Detail Policy Number", "Policy Number"),
            ("Policy Transaction Data Line Of Business", "Line Of Business"),
            ("Policy Change Request Detail Policy Effective Date", "Effective Date"),
            ("Policy Transaction Data Master Company", "Master Company"),
            ("Policy Change Request Detail Request Status", "Request Status"),
            ("Policy Change Request Detail Created By", "Created By"),
            ("Policy Transaction Data Written Premium", "Written Premium"),
            ("Policy Transaction Data Annualized Premium", "Premium - Annualized"),
            ("Applicant Data Branch", "Branch"),
            ("Policy Transaction Data Department", "Department"),
            ("Serviceteampolicychangerequest Service Team", "Service Team"),
            ("Applicant Data Assigned Producer", "Assigned Producer"),
            ("Applicant Data CSR", "CSR"),
            ("Applicant Data Preferred Language", "Preferred Language"),
            ("Applicant Labels Applicant Labels", "Applicant Labels"),
            ("Policy Labels Policy Labels", "Policy Labels"),
            (
                "Policy Change Request Detail Change Request Created Date",
                "Change Request Created Date",
            ),
        )
    }


class RegistryIdentityTests(unittest.TestCase):
    def test_4359_identity_look_and_schema_gate(self):
        from robie_job_engine.job_schema import get_bounded_job_schema
        from robie_job_engine.report_registry import LOOK_ID_BY_REPORT, get_report_spec

        spec = get_report_spec("4359")
        self.assertEqual(
            spec.identity_fields, ("policy_number", "change_request_created_date")
        )
        self.assertEqual(spec.look_id, "4602")
        self.assertFalse(spec.schema_verified)
        self.assertEqual(LOOK_ID_BY_REPORT["4359"], "4602")
        self.assertEqual(spec.filter_name, "Policy Change Request Confirmation Queue - ROBIE")
        schema = get_bounded_job_schema("policy_change_verification")
        self.assertFalse(schema["schema_verified"])
        self.assertEqual(
            schema["identity"], ("policy_number", "change_request_created_date")
        )
        self.assertIs(pcw.POLICY_CHANGE_ENABLED, False)


class ParsePolicyChangeRowTests(unittest.TestCase):
    def test_csv_and_looker_headers_parse_to_the_same_19_fields(self):
        csv_parsed = pcw.parse_policy_change_row(_sample_csv_row())
        looker_parsed = pcw.parse_policy_change_row(_sample_looker_row())
        self.assertEqual(csv_parsed, looker_parsed)
        self.assertEqual(tuple(csv_parsed), pcw.POLICY_CHANGE_FIELDS)
        self.assertEqual(len(csv_parsed), 19)
        self.assertEqual(csv_parsed["policy_number"], "POL-4359-001")
        self.assertEqual(csv_parsed["change_request_created_date"], "09/01/2026")
        self.assertEqual(csv_parsed["account_name"], "Example Trucking LLC")
        self.assertEqual(csv_parsed["annualized_premium"], "2400.00")
        self.assertEqual(csv_parsed["written_premium"], "1500.00")
        self.assertEqual(csv_parsed["master_company"], "Example Carrier")
        self.assertNotIn("request_id", csv_parsed)

    def test_phantom_columns_are_dropped(self):
        row = _sample_csv_row(
            request_id="REQ-SHOULD-DROP",
            change_action="add",
            change_description="add a vehicle",
            affected_item="2022 Honda Civic",
        )
        parsed = pcw.parse_policy_change_row(row)
        self.assertNotIn("request_id", parsed)
        self.assertEqual(parsed["policy_number"], "POL-4359-001")
        request = pcw.reconstruct_request(row, "please add a vehicle")
        self.assertFalse(request["ambiguous"])
        self.assertIsNone(request["change_action"])
        self.assertEqual(request["requested_values"], {})
        self.assertEqual(request["affected_item"], None)


class ReconstructRequestTests(unittest.TestCase):
    def test_reconstructs_clear_request(self):
        request = pcw.reconstruct_request(
            _sample_csv_row(), "adding a vehicle per client email"
        )
        self.assertFalse(request["ambiguous"])
        self.assertEqual(request["policy_number"], "POL-4359-001")
        self.assertEqual(request["change_request_created_date"], "09/01/2026")
        self.assertEqual(request["effective_date"], "09/15/2026")
        self.assertEqual(request["insured_name"], "Example Trucking LLC")
        self.assertEqual(request["premium_expectation"], "1500.00")
        self.assertIsNone(request["change_action"])

    def test_ambiguous_request_flagged(self):
        request = pcw.reconstruct_request(
            {"request_id": "REQ-X", "change_description": "add a vehicle"},
            "some discussion text",
        )
        self.assertTrue(request["ambiguous"])
        self.assertIn("missing policy number", request["ambiguity_reasons"])
        self.assertIn("missing change request created date", request["ambiguity_reasons"])
        self.assertEqual(request["policy_number"], "")

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
