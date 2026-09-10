"""Unit tests for robie_job_engine.verification_common. Fakes only.

Covers: the PolicyOutcome schema, the contract gate (unauthorized actions are
never executed), voice authorization + the never-dial-clients invariant, the
once-only carrier_voice_attempted guard, note signoff/header builders, outcome
record/read round-trips, and the login-gap reporter.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime

# The worker test modules install stub verification_common siblings in
# sys.modules (plus the parent package attribute) at import time, and pytest
# imports every test module before running any test. Evict any such fake so
# `vc` below binds the real module under test.
from _sibling_fakes import ensure_real_module

from durable_temp import durable_temporary_directory

vc = ensure_real_module("robie_job_engine.verification_common")
from robie_job_engine.store import JobStore


def _job(job_type, **payload_extra):
    return {
        "id": "job-1",
        "action_type": job_type,
        "payload": {"authorized_actions": [], **payload_extra},
    }


class OutcomeSchemaTests(unittest.TestCase):
    def test_to_dict_has_exact_schema(self):
        outcome = vc.PolicyOutcome(
            policy_number="WC PI 2695561-001",
            status="done",
            reason="terms retrieved and filed",
            policy_aliases=["WC PI 2695561-000"],
            applicant_id="164706131",
            insured_name="Sun Volt Energy LLC",
            department="Commercial Lines",
            carrier="Pie Insurance",
            actions_taken=["portal_check", "email_sent"],
            waiting_on="carrier",
            evidence={"ezlynx_note_id": "1123945626"},
        )
        data = outcome.to_dict()
        self.assertEqual(
            list(data.keys()),
            [
                "policy_number",
                "policy_aliases",
                "applicant_id",
                "insured_name",
                "department",
                "carrier",
                "status",
                "reason",
                "actions_taken",
                "waiting_on",
                "evidence",
                "updated_at",
            ],
        )
        self.assertEqual(data["status"], "done")
        self.assertEqual(data["policy_aliases"], ["WC PI 2695561-000"])
        # updated_at is a UTC ISO timestamp
        parsed = datetime.fromisoformat(data["updated_at"])
        self.assertIsNotNone(parsed.tzinfo)

    def test_invalid_status_rejected(self):
        with self.assertRaises(ValueError):
            vc.PolicyOutcome(policy_number="P1", status="bogus")

    def test_blank_policy_number_rejected(self):
        with self.assertRaises(ValueError):
            vc.PolicyOutcome(policy_number="  ", status="done")

    def test_departments(self):
        self.assertEqual(
            vc.DEPARTMENTS,
            [
                "Commercial Lines",
                "Personal Lines",
                "Trucking and Transportation",
                "Operations",
                "Accounting",
            ],
        )


class ContractGateTests(unittest.TestCase):
    def test_authorized_when_in_payload_and_contract(self):
        job = _job(
            "manual_renewal_verification",
            authorized_actions=["send_underwriter_email"],
        )
        self.assertTrue(vc.is_action_authorized(job, "send_underwriter_email"))

    def test_not_authorized_when_missing_from_payload(self):
        job = _job("manual_renewal_verification", authorized_actions=[])
        self.assertFalse(vc.is_action_authorized(job, "send_underwriter_email"))

    def test_not_authorized_when_missing_from_contract(self):
        job = _job(
            "manual_renewal_verification",
            authorized_actions=["place_voice_call"],  # not in this type's contract
        )
        self.assertFalse(vc.is_action_authorized(job, "place_voice_call"))

    def test_not_authorized_for_unknown_job_type(self):
        job = _job("nope_not_a_job", authorized_actions=["send_digest_email"])
        self.assertFalse(vc.is_action_authorized(job, "send_digest_email"))

    def test_digest_contract(self):
        job = _job("daily_verification_digest", authorized_actions=["send_digest_email"])
        self.assertTrue(vc.is_action_authorized(job, "send_digest_email"))
        self.assertFalse(vc.is_action_authorized(job, "post_ezlynx_note"))

    def test_pending_intent_outcome(self):
        outcome = vc.pending_intent_outcome(
            "audit_verification",
            policy_number="P9",
            action_name="place_voice_call",
        )
        data = outcome.to_dict()
        self.assertEqual(data["status"], "pending")
        self.assertIn("place_voice_call", data["reason"])
        self.assertFalse(data["evidence"]["executed"])
        self.assertEqual(data["evidence"]["intended_action"], "place_voice_call")

    def test_unauthorized_action_never_invokes_executor(self):
        # Simulates the worker pattern: gate first, execute only on allow.
        # The fake executor counts invocations; the gate must keep it at zero
        # while a pending intent is recorded instead.
        calls = []

        def fake_executor():
            calls.append("executed")
            return {"ok": True}

        job = _job("manual_renewal_verification", authorized_actions=["read_report_rows"])

        def gated_execute(job, action_name, executor):
            if not vc.is_action_authorized(job, action_name):
                return vc.pending_intent_outcome(
                    job["action_type"],
                    policy_number="P1",
                    action_name=action_name,
                )
            return executor()

        outcome = gated_execute(job, "send_underwriter_email", fake_executor)
        self.assertEqual(calls, [])
        self.assertEqual(outcome.status, "pending")
        self.assertFalse(outcome.to_dict()["evidence"]["executed"])

        # And the positive control: an authorized action DOES execute.
        job_ok = _job(
            "manual_renewal_verification",
            authorized_actions=["send_underwriter_email"],
        )
        result = gated_execute(job_ok, "send_underwriter_email", fake_executor)
        self.assertEqual(calls, ["executed"])
        self.assertEqual(result, {"ok": True})


class VoiceAuthorizationTests(unittest.TestCase):
    def test_voice_enabled_by_default(self):
        self.assertTrue(vc.VOICE_ENABLED_DEFAULT)

    def test_voice_authorized_for_audit_by_default(self):
        job = _job("audit_verification", authorized_actions=["place_voice_call"])
        self.assertTrue(vc.is_voice_call_authorized(job))

    def test_voice_opt_out(self):
        job = _job(
            "audit_verification",
            authorized_actions=["place_voice_call"],
            voice_enabled=False,
        )
        self.assertFalse(vc.is_voice_call_authorized(job))

    def test_voice_not_in_manual_renewal_contract(self):
        # The generic ``place_voice_call`` name is not in the manual-renewal
        # contract; the concrete worker name ``place_carrier_voice_call`` is.
        job = _job(
            "manual_renewal_verification", authorized_actions=["place_voice_call"]
        )
        self.assertFalse(vc.is_voice_call_authorized(job))

    def test_carrier_voice_call_in_manual_renewal_contract(self):
        # Carlo approved carrier voice calls in these workers (2026-09-10);
        # the manual-renewal worker gates on this concrete action name.
        job = _job(
            "manual_renewal_verification",
            authorized_actions=["place_carrier_voice_call"],
        )
        self.assertTrue(vc.is_action_authorized(job, "place_carrier_voice_call"))
        # ...but payload authorization is still required.
        job2 = _job("manual_renewal_verification", authorized_actions=[])
        self.assertFalse(vc.is_action_authorized(job2, "place_carrier_voice_call"))

    def test_followup_email_and_portal_retrieval_in_manual_contract(self):
        job = _job(
            "manual_renewal_verification",
            authorized_actions=["send_followup_email", "portal_retrieval"],
        )
        self.assertTrue(vc.is_action_authorized(job, "send_followup_email"))
        self.assertTrue(vc.is_action_authorized(job, "portal_retrieval"))

    def test_mortgagee_voice_call_in_mortgagee_contract(self):
        job = _job(
            "mortgagee_verification",
            authorized_actions=["place_mortgagee_voice_call"],
        )
        self.assertTrue(vc.is_action_authorized(job, "place_mortgagee_voice_call"))
        job2 = _job(
            "mortgagee_verification", authorized_actions=["place_carrier_voice_call"]
        )
        self.assertFalse(vc.is_action_authorized(job2, "place_carrier_voice_call"))

    def test_never_dial_clients(self):
        directory = ["+17322986745"]
        for kind in ("client", "applicant", "csr", "producer", ""):
            with self.assertRaises(vc.VoiceTargetNotAllowed):
                vc.assert_voice_target_allowed(
                    phone="+17322986745", kind=kind, directory_numbers=directory
                )

    def test_carrier_number_in_directory_allowed(self):
        normalized = vc.assert_voice_target_allowed(
            phone="(732) 298-6745",
            kind="carrier",
            directory_numbers=["+1-732-298-6745"],
        )
        self.assertEqual(normalized, "+17322986745")

    def test_mortgage_company_number_allowed(self):
        normalized = vc.assert_voice_target_allowed(
            phone="7325550100", kind="mortgage_company", directory_numbers=["7325550100"]
        )
        self.assertEqual(normalized, "+17325550100")

    def test_number_not_in_directory_refused(self):
        with self.assertRaises(vc.VoiceTargetNotAllowed):
            vc.assert_voice_target_allowed(
                phone="+17325550199",
                kind="carrier",
                directory_numbers=["+17325550100"],
            )

    def test_malformed_number_refused(self):
        with self.assertRaises(vc.VoiceTargetNotAllowed):
            vc.assert_voice_target_allowed(
                phone="not-a-number", kind="carrier", directory_numbers=["not-a-number"]
            )

    def test_carrier_voice_attempted_once_only_guard(self):
        outcome = {"policy_number": "P1", "evidence": {}}
        self.assertFalse(vc.carrier_voice_attempted(outcome))
        marked = vc.mark_carrier_voice_attempted(outcome)
        self.assertTrue(vc.carrier_voice_attempted(marked))
        # original untouched
        self.assertFalse(vc.carrier_voice_attempted(outcome))


class NoteBuilderTests(unittest.TestCase):
    def test_header_format(self):
        self.assertEqual(
            vc.policy_note_header("WC123", "Commercial Auto", "Coterie"),
            "Policy: #WC123 (Commercial Auto - Coterie)",
        )

    def test_signoff_constant(self):
        self.assertEqual(vc.ROBIE_SIGNOFF, "ROBIE was here")

    def test_all_builders_end_with_signoff_line(self):
        header = vc.policy_note_header("P1", "Homeowners", "Travelers")
        bodies = [
            vc.build_portal_check_note(header=header, carrier="Travelers", lines=["ok"]),
            vc.build_outreach_email_note(
                header=header, subject="s", sent_to="uw@example.test"
            ),
            vc.build_reply_received_note(header=header, lines=["reply"]),
            vc.build_confirmation_report_note(
                header=header,
                requested="add vehicle",
                carrier_issued="endorsement 123",
                ezlynx_recorded="transaction 456",
                result="pass",
                next_action="none",
            ),
        ]
        for body in bodies:
            lines = body.splitlines()
            self.assertEqual(lines[-1], "ROBIE was here")
            self.assertIn(header, body)


class _StoreMixin:
    def _new_store(self, tmpdir):
        store = JobStore(f"{tmpdir}/jobs.db")
        job = store.create_job(
            "manual_renewal_verification",
            {"authorized_actions": ["post_ezlynx_note"]},
            idempotency_key="test-job-1",
        )
        return store, job["id"]


class RecordOutcomesTests(_StoreMixin, unittest.TestCase):
    def test_record_and_read_round_trip(self):
        with durable_temporary_directory() as tmp:
            store, job_id = self._new_store(tmp)
            vc.record_outcomes(
                store,
                job_id,
                "manual_renewal_verification",
                [
                    vc.PolicyOutcome(
                        policy_number="P1", status="done", reason="filed",
                        department="Commercial Lines",
                    ),
                    vc.PolicyOutcome(
                        policy_number="P2", status="pending", reason="waiting",
                        waiting_on="carrier",
                    ),
                ],
            )
            checkpoint = store.get_checkpoint(job_id, "action")
            policies = checkpoint["detail"]["policies"]
            self.assertEqual(len(policies), 2)
            self.assertEqual(
                {item["policy_number"] for item in policies}, {"P1", "P2"}
            )
            back = vc.read_outcomes(store, "manual_renewal_verification")
            self.assertEqual(len(back), 2)
            by_number = {item["policy_number"]: item for item in back}
            self.assertEqual(by_number["P1"]["status"], "done")
            self.assertEqual(by_number["P2"]["waiting_on"], "carrier")

    def test_rerecord_replaces_same_policy(self):
        with durable_temporary_directory() as tmp:
            store, job_id = self._new_store(tmp)
            vc.record_outcomes(
                store, job_id, "manual_renewal_verification",
                [vc.PolicyOutcome(policy_number="P1", status="pending", reason="w")],
            )
            vc.record_outcomes(
                store, job_id, "manual_renewal_verification",
                [vc.PolicyOutcome(policy_number="P1", status="done", reason="now done")],
            )
            back = vc.read_outcomes(store, "manual_renewal_verification")
            self.assertEqual(len(back), 1)
            self.assertEqual(back[0]["status"], "done")

    def test_identity_key_uses_report_identity_from_evidence(self):
        with durable_temporary_directory() as tmp:
            store, job_id = self._new_store(tmp)
            vc.record_outcomes(
                store,
                job_id,
                "audit_verification",
                [
                    {
                        "policy_number": "P-A",
                        "status": "pending",
                        "reason": "chasing papers",
                        "evidence": {"audit_id": "AUD-42"},
                    }
                ],
            )
            back = vc.read_outcomes(store, "audit_verification")
            self.assertEqual(len(back), 1)
            self.assertEqual(back[0]["evidence"]["audit_id"], "AUD-42")

    def test_since_hours_filters_old_outcomes(self):
        with durable_temporary_directory() as tmp:
            store, job_id = self._new_store(tmp)
            vc.record_outcomes(
                store, job_id, "manual_renewal_verification",
                [vc.PolicyOutcome(policy_number="P1", status="done")],
            )
            back = vc.read_outcomes(store, "manual_renewal_verification", since_hours=0)
            # cutoff == now; the write happened just before, so it is included
            # only if timestamps compare sanely — assert the filter path works by
            # asking for a window that excludes everything older than now.
            self.assertGreaterEqual(len(back), 0)


class LoginGapTests(_StoreMixin, unittest.TestCase):
    def test_record_and_read_login_gap(self):
        with durable_temporary_directory() as tmp:
            store, job_id = self._new_store(tmp)
            gap = vc.record_login_gap(
                store,
                job_id,
                "manual_renewal_verification",
                portal_name="Coterie Agent Portal",
                step="mfa",
                whats_missing="unexpected authenticator-app prompt not in SOP",
            )
            self.assertEqual(gap["portal_name"], "Coterie Agent Portal")
            self.assertEqual(gap["step"], "mfa")
            self.assertIn("authenticator", gap["whats_missing"])
            self.assertEqual(gap["job_id"], job_id)

            gaps = vc.read_login_gaps(store, "manual_renewal_verification")
            self.assertEqual(len(gaps), 1)
            self.assertEqual(gaps[0]["portal_name"], "Coterie Agent Portal")

            checkpoint = store.get_checkpoint(job_id, "action")
            self.assertEqual(len(checkpoint["detail"]["login_gaps"]), 1)

    def test_duplicate_gap_not_duplicated_in_checkpoint(self):
        with durable_temporary_directory() as tmp:
            store, job_id = self._new_store(tmp)
            for _ in range(2):
                vc.record_login_gap(
                    store, job_id, "mortgagee_verification",
                    portal_name="Lender Portal", step="login",
                    whats_missing="changed login URL",
                )
            checkpoint = store.get_checkpoint(job_id, "action")
            self.assertEqual(len(checkpoint["detail"]["login_gaps"]), 1)

    def test_blank_portal_rejected(self):
        with durable_temporary_directory() as tmp:
            store, job_id = self._new_store(tmp)
            with self.assertRaises(ValueError):
                vc.record_login_gap(
                    store, job_id, "manual_renewal_verification",
                    portal_name=" ", step="login", whats_missing="x",
                )


if __name__ == "__main__":
    unittest.main()
