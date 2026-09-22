"""Integration regression tests: verification workers vs the REAL shared modules.

Pins the integration contract that broke when the workers were built against
guessed sibling signatures:

- ``record_outcomes(store, job_id, job_type, outcomes)`` — positional store,
  job id, job type (NOT ``(job, outcomes)`` or keyword variants).
- ``send_verification_email(to, cc, subject, text_body)`` — keyword-only with
  ``text_body`` (NOT ``body`` / ``job=`` / ``policy_number=``).
- Worker outcome dicts must fit the real ``PolicyOutcome`` schema; worker-only
  keys such as ``audit_id`` travel inside ``evidence`` so the shared identity
  key derivation keeps working.
- The policy-change worker runs live on report 4359 (schema verified against
  the 2026-09-19 delivery, re-verified 2026-09-22, pilot ratified by Carlo),
  and the bounded schema gate admits it; Production promotion stays
  separately gated by job_type_gate.

Isolation: every worker scenario runs in a FRESH SUBPROCESS
(``_verification_integration_child.py``) that imports the real worker with
the real shared modules; only ``report_fetcher`` is faked (in-subprocess
shim, no live browser). This test module never imports the worker modules
in-process, so it cannot poison — or be poisoned by — the fake-based worker
test files that share this pytest process.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from robie_job_engine.job_schema import bounded_schema_hold_reason
from robie_job_engine.request_routing import BOUNDED_ENGINE_ACTIONS, WORKER_FOR_ACTION

REPO_ROOT = Path(__file__).resolve().parents[1]
CHILD = REPO_ROOT / "tests" / "_verification_integration_child.py"
TODAY = date(2026, 9, 10)


def _make_db_dir() -> Path:
    # DurableWorkLedger refuses /tmp paths; mirror the mortgagee fixture base.
    base = Path.home() / ".cache" / "robie-integration-tests"
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="robie-integration-", dir=str(base)))


def _run_child(spec: dict) -> dict:
    workdir = _make_db_dir()
    spec = dict(spec)
    spec["db_path"] = str(workdir / "jobs.db")
    input_path = workdir / "input.json"
    input_path.write_text(json.dumps(spec), encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, str(CHILD), str(input_path)],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=str(REPO_ROOT),
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"integration child failed (rc={proc.returncode}):\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr[-4000:]}"
        )
    result = json.loads(proc.stdout)
    assert result.get("ok"), f"child reported not-ok: {result}"
    return result


def _audit_row(**overrides):
    row = {
        "audit_id": "AUD-1",
        "policy_number": "WC PI 2695561-001",
        "insured_name": "Sun Volt Energy LLC",
        "carrier": "The Hartford",
        "line_of_business": "Workers Compensation",
        "renewal_effective_date": (TODAY - timedelta(days=40)).isoformat(),
        "audit_status": "open",
    }
    row.update(overrides)
    return row


def _audit_spec(rows, **payload_overrides):
    payload = {"as_of_date": TODAY.isoformat(), "authorized_actions": []}
    payload.update(payload_overrides)
    return {
        "scenario": "audit",
        "rows": rows,
        "idempotency_key": "itest-idem",
        "job": {"action_type": "audit_verification", "payload": payload},
    }


class AuditWorkerRealSharedModulesTest(unittest.TestCase):
    """The audit worker records through the REAL verification_common."""

    def test_record_outcomes_uses_real_signature_and_schema(self):
        result = _run_child(_audit_spec([_audit_row()]))
        self.assertTrue(result["succeeded"], result["error"])
        outcomes = result["outcomes"]
        self.assertEqual(len(outcomes), 1)
        outcome = outcomes[0]
        self.assertEqual(outcome["policy_number"], "WC PI 2695561-001")
        self.assertEqual(outcome["status"], "pending")
        # audit_id survives inside evidence so the shared identity key keeps it.
        self.assertEqual(outcome["evidence"]["audit_id"], "AUD-1")

    def test_carrier_email_uses_real_mailer_signature(self):
        spec = _audit_spec(
            [_audit_row(carrier_email="audits@example.com")],
            authorized_actions=["send_carrier_email"],
        )
        spec["capture_email"] = True
        result = _run_child(spec)
        self.assertTrue(result["succeeded"], result["error"])
        sent = result["sent_emails"]
        self.assertEqual(len(sent), 1)
        email = sent[0]
        self.assertEqual(email["to"], ["audits@example.com"])
        self.assertIn("[AUDIT-REQ-AUD-1]", email["subject"])
        self.assertTrue(email["text_body"].strip())

    def test_no_carrier_email_stays_pending_without_sending(self):
        spec = _audit_spec(
            [_audit_row()],  # no carrier_email on the row
            authorized_actions=["send_carrier_email"],
        )
        spec["capture_email"] = True
        result = _run_child(spec)
        self.assertTrue(result["succeeded"], result["error"])
        self.assertEqual(result["sent_emails"], [])
        outcomes = result["outcomes"]
        self.assertEqual(outcomes[0]["status"], "not_done")
        self.assertIn("no carrier/underwriter email", outcomes[0]["reason"])


class PolicyChangeGateTest(unittest.TestCase):
    """Post-flip behavior (Carlo's 2026-09-22 ratification). On the pre-pilot
    branch both tests fail: the child held with "disabled" and the bounded
    gate held with "unregistered or unverified schema"."""

    def test_worker_processes_open_requests_after_gate_flip(self):
        rows = [
            {
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
            },
            {
                "Policy Number": "P-200",
                "Change Request Created Date": "2026-09-19",
                "policy_number": "P-200",
            },
        ]
        result = _run_child({"scenario": "policy_change", "rows": rows})
        self.assertTrue(result["policy_change_enabled"])
        self.assertTrue(result["succeeded"])
        self.assertIsNone(result["error"])
        outcomes = result["outcomes"]
        self.assertEqual(len(outcomes), 2)
        # No carrier evidence is retrieved in the pilot: a clear request
        # waits on the carrier, an ambiguous one routes to the CSR. The
        # composite identity from real ingestion is the tracked request id.
        self.assertEqual(outcomes[0]["status"], "pending")
        self.assertEqual(outcomes[0]["waiting_on"], "carrier")
        self.assertEqual(outcomes[0]["evidence"]["request_id"], "P-100 | 2026-09-19")
        self.assertEqual(outcomes[1]["status"], "not_done")
        self.assertEqual(outcomes[1]["waiting_on"], "csr")

    def test_bounded_schema_gate_allows_policy_change(self):
        reason = bounded_schema_hold_reason(
            "policy_change_verification", {"report_id": "4359"}
        )
        self.assertIsNone(reason)

    def test_bounded_schema_gate_allows_manual_renewal(self):
        reason = bounded_schema_hold_reason(
            "manual_renewal_verification", {"report_id": "4247"}
        )
        self.assertIsNone(reason)

    def test_bounded_schema_gate_requires_digest_fields(self):
        reason = bounded_schema_hold_reason("daily_verification_digest", {})
        self.assertIsNotNone(reason)
        self.assertIn("output_dir", reason)


class VerificationRegistrationTest(unittest.TestCase):
    def test_routing_maps_all_five_action_types(self):
        expected = {
            "manual_renewal_verification": "manual-renewal",
            "audit_verification": "audit-verification",
            "mortgagee_verification": "mortgagee-verification",
            "policy_change_verification": "policy-change-verification",
            "daily_verification_digest": "verification-digest",
        }
        for action_type, worker_name in expected.items():
            self.assertEqual(WORKER_FOR_ACTION.get(action_type), worker_name)
            self.assertIn(action_type, BOUNDED_ENGINE_ACTIONS)


if __name__ == "__main__":
    unittest.main()


def _load_installer():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "install_verification_schedules",
        REPO_ROOT / "scripts" / "install_verification_schedules.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class VerificationScheduleInstallerTest(unittest.TestCase):
    """Digest lands inside Carlo's 9-10 AM ET window with delivery config."""

    def test_worker_and_digest_schedules(self):
        installer = _load_installer()
        workdir = _make_db_dir()
        db_path = str(workdir / "schedules.db")
        installed = installer.install_verification_schedules(
            db_path,
            digest_output_dir="/var/lib/robie/verification-digest",
            digest_email_sender="robie@streetsmart.insurance",
        )
        by_action = {item["action_type"]: item for item in installed}
        self.assertEqual(len(installed), 5)

        for action in (
            "manual_renewal_verification",
            "audit_verification",
            "mortgagee_verification",
            "policy_change_verification",
        ):
            item = by_action[action]
            self.assertEqual(item["cron_spec"], "0 6 * * *")
            self.assertEqual(item["timezone"], "America/New_York")
            self.assertTrue(item["enabled"])

        digest = by_action["daily_verification_digest"]
        # Carlo's delivery window is 9-10 AM Eastern; the digest run itself
        # must start inside that window.
        self.assertEqual(digest["cron_spec"], "0 9 * * *")
        self.assertEqual(digest["timezone"], "America/New_York")
        params = digest["parameters"]
        self.assertEqual(params["output_dir"], "/var/lib/robie/verification-digest")
        self.assertEqual(params["email_sender"], "robie@streetsmart.insurance")
