"""Acceptance tests for the read-only policy-change confirmation pilot.

Synthetic case only. No live EZLynx, carrier, or Production call.
"""

from __future__ import annotations

import copy
import os
import unittest
from pathlib import Path
from unittest import mock

from _durable_tmp import durable_tmpdir
from robie_job_engine.job_schema import get_bounded_job_schema
from robie_job_engine.job_type_gate import production_hold_reason
from robie_job_engine.models import JobStatus
from robie_job_engine.policy_change_confirmation import (
    JOB_TYPE,
    NOTE_SIGNATURE,
    ROLE_DECISION,
    ConfirmationLedger,
    DisabledWrites,
    PolicyChangeConfirmationWorker,
    progressive_access_proof,
    run_confirmation,
)
from robie_job_engine.policy_change_worker import JOB_TYPE as OLD_JOB_TYPE
from robie_job_engine.policy_change_worker import POLICY_CHANGE_ENABLED
from robie_job_engine.store import JobStore

_FORBIDDEN_NOTE_TOKENS = (
    "policy_number",
    "named_insured",
    "change_request_id",
    "exact_match",
    "ready_for_human_review",
    "document_id",
    "source_id",
    "None found",
)


def _cited(raw, reference):
    return {"raw": raw, "reference": reference}


def _fields(**items):
    return {key: _cited(value, reference) for key, (value, reference) in items.items()}


def _address(unit="A"):
    return {"street": "10 Main St", "unit": unit, "city": "Newark", "state": "NJ", "postal": "07102"}


def clean_packet():
    request_fields = _fields(
        policy_number=("CA-10001", "change request"),
        term=("2026-01-01 to 2027-01-01", "change request"),
        named_insured=("Acme Trucking LLC", "policy named insured"),
        carrier=("Progressive", "change request"),
        product=("Commercial Auto", "change request"),
        change_action=("Add a vehicle", "change request"),
        effective_date=("03/01/2026", "change request"),
        vin=("1HGCM82633A004352", "vehicle"),
        garaging_address=(_address(), "garaging address"),
        limit=("$1,000,000", "liability limit"),
        premium_amount=("250.00", "transaction premium"),
        premium_basis=("transaction premium", "premium basis"),
    )
    carrier_fields = _fields(
        policy_number=("CA-10001", "endorsement page 1"),
        term=("2026-01-01 to 2027-01-01", "endorsement page 1"),
        named_insured=("acme trucking llc", "policy named insured"),
        carrier=("Progressive", "endorsement page 1"),
        product=("commercial auto", "endorsement page 1"),
        change_action=("add a vehicle", "endorsement page 1"),
        effective_date=("2026-03-01", "endorsement page 1"),
        vin=("1HGCM82633A004352", "vehicle"),
        garaging_address=(_address("a"), "garaging address"),
        limit=("1000000", "liability limit"),
        premium_amount=("$250.00", "transaction premium"),
        premium_basis=("transaction", "premium basis"),
    )
    ezlynx_fields = _fields(
        policy_number=("CA-10001", "policy record"),
        term=("2026-01-01 to 2027-01-01", "policy record"),
        named_insured=("Acme Trucking LLC", "policy named insured"),
        carrier=("Progressive", "policy record"),
        product=("Commercial Auto", "policy record"),
        change_action=("Add a vehicle", "policy record"),
        effective_date=("March 1, 2026", "policy record"),
        vin=("1HGCM82633A004352", "vehicle"),
        garaging_address=(_address(), "garaging address"),
        limit=("$1,000,000.00", "liability limit"),
        premium_amount=("250.00", "transaction premium"),
        premium_basis=("Transaction premium", "premium basis"),
    )
    return {
        "task_id": "task-100",
        "assignment_event_id": "assign-100",
        "source_version": "src-1",
        "case": {
            "agency": "StreetSmart",
            "applicant_id": "applicant-100",
            "policy_id": "policy-100",
            "policy_number": "CA-10001",
            "carrier": "Progressive",
            "product": "Commercial Auto",
            "term": "2026-01-01 to 2027-01-01",
            "change_request_id": "change-100",
            "task_id": "task-100",
            "discussion_id": "discussion-100",
            "assignment_event_id": "assign-100",
            "assignment_timestamp": "2026-10-01T15:00:00+00:00",
            "original_assigner_id": "csr-maria",
            "request_date": "2026-02-20",
            "requested_effective_date": "2026-03-01",
            "due_date": "2026-03-05",
            "submission_evidence": "Submitted on the carrier site on February 20.",
        },
        "task": {
            "id": "task-100",
            "current_owner_id": "SSRobie",
            "created_by": "someone-else",
            "assigned_producer_id": "producer-jake",
        },
        "assignment_events": [
            {
                "id": "assign-100",
                "timestamp": "2026-10-01T15:00:00+00:00",
                "previous_owner_id": "csr-maria",
                "previous_owner_name": "Maria Bara",
                "current_owner_id": "SSRobie",
            }
        ],
        "discussions": [
            {"id": "discussion-100", "body": "Please add the truck."}
        ],
        "reread": {
            "current_owner_id": "SSRobie",
            "assignment_event_id": "assign-100",
            "source_version": "src-1",
        },
        "request": {
            "source_id": "request-100",
            "summary": "Add one truck for Acme Trucking LLC, effective March 1, 2026.",
            "fields": request_fields,
        },
        "carrier_document": {
            "source_id": "endorsement-100",
            "kind": "endorsement",
            "issued": True,
            "pages_missing": False,
            "ocr_quality": "clear",
            "summary": "Progressive issued an endorsement adding the truck.",
            "fields": carrier_fields,
            "unrequested": [],
        },
        "ezlynx_record": {
            "source_id": "ez-100",
            "summary": "EZLynx shows the truck added on this term.",
            "fields": ezlynx_fields,
            "unrequested": [],
        },
        "linked_applicant": {
            "name": "Acme Trucking LLC",
            "source_id": "sidebar",
            "reference": "linked applicants",
        },
        "filing": {
            "verified": True,
            "document_id": "doc-100",
            "name": "Endorsement.pdf",
            "folder": "Policy changes",
            "label": "Endorsement",
            "policy_number": "CA-10001",
        },
        "readback": {"ok": True, "document_id": "doc-100"},
        "directory_entry": {"document_download_route": "Progressive / Document Download"},
    }


def _assert_plain_note(test, note):
    test.assertTrue(note.strip().endswith(NOTE_SIGNATURE))
    for token in _FORBIDDEN_NOTE_TOKENS:
        test.assertNotIn(token, note)


class PolicyChangeConfirmationAcceptanceTests(unittest.TestCase):
    def test_clean_supported_match(self):
        writes = DisabledWrites()
        result = run_confirmation(clean_packet(), writes=writes)
        self.assertEqual(result["outcome"], "ready_for_human_review")
        self.assertTrue(result["review_ready"])
        self.assertEqual(result["external_writes"], 0)
        self.assertEqual(writes.performed, 0)
        self.assertEqual(writes.refused, [])
        self.assertFalse(result["writes_enabled"])
        self.assertTrue(result["task_open"])
        self.assertTrue(result["change_request_open"])
        self.assertFalse(result["completed"])
        self.assertEqual(result["confirmation_owner"], "producer")
        self.assertEqual(result["result_recipient"]["id"], "csr-maria")
        self.assertNotEqual(result["result_recipient"]["id"], "producer-jake")
        self.assertNotEqual(result["result_recipient"]["id"], "someone-else")
        self.assertEqual(result["role_decision"]["status"], "resolved")
        self.assertEqual(ROLE_DECISION["status"], "resolved")
        self.assertFalse(result["carrier_pilot"]["confirmed"])
        self.assertEqual(result["live_test"], "UNVERIFIED")
        _assert_plain_note(self, result["note"])
        self.assertIn("No exceptions.", result["note"])
        self.assertIn("Maria Bara", result["note"])
        self.assertIn("producer", result["note"].casefold())
        named = [row for row in result["comparison"] if row["field"] == "named_insured"]
        self.assertEqual(len(named), 1)
        self.assertIn(named[0]["verdict"], {"exact_match", "normalized_match"})
        self.assertEqual(named[0]["carrier"]["reference"], "policy named insured")

    def test_wrong_identity_term_date_or_vin_blocks_clean_result(self):
        cases = {
            "policy number": ("policy_number", "CA-99999"),
            "leading zeros": ("policy_number", "00123"),
            "term": ("term", "2025-01-01 to 2026-01-01"),
            "effective date": ("effective_date", "2026-04-01"),
            "vin": ("vin", "1HGCM82633A009999"),
        }
        for label, (field, wrong) in cases.items():
            with self.subTest(label=label):
                packet = copy.deepcopy(clean_packet())
                packet["carrier_document"]["fields"][field]["raw"] = wrong
                result = run_confirmation(packet)
                self.assertFalse(result["review_ready"])
                self.assertNotEqual(result["outcome"], "ready_for_human_review")
                self.assertEqual(result["external_writes"], 0)
                row = next(item for item in result["comparison"] if item["field"] == field)
                self.assertEqual(row["verdict"], "mismatch")
                self.assertEqual(row["carrier"]["raw"], wrong)
                self.assertNotEqual(row["carrier"]["raw"], row["request"]["raw"])
                self.assertIn(str(wrong), row["explanation"])

    def test_linked_applicant_difference_is_not_a_named_insured_mismatch(self):
        packet = copy.deepcopy(clean_packet())
        packet["linked_applicant"] = {
            "name": "Other Person LLC",
            "source_id": "sidebar",
            "reference": "linked applicants",
        }
        result = run_confirmation(packet)
        named = next(row for row in result["comparison"] if row["field"] == "named_insured")
        self.assertIn(named["verdict"], {"exact_match", "normalized_match"})
        self.assertEqual(named["carrier"]["reference"], "policy named insured")
        linked = next(row for row in result["comparison"] if row["field"] == "linked_applicant")
        self.assertEqual(linked["verdict"], "not_applicable")
        self.assertIn("policy", linked["explanation"].casefold())
        self.assertEqual(result["outcome"], "ready_for_human_review")
        self.assertNotIn("Other Person LLC", named["explanation"])

    def test_missing_page_weak_ocr_quote_or_acknowledgement_holds(self):
        variants = {
            "missing page": {"pages_missing": True},
            "weak ocr": {"ocr_quality": "weak"},
            "quote": {"kind": "quote", "issued": False},
            "acknowledgement": {"kind": "acknowledgement", "issued": True},
            "carrier processed": {"kind": "carrier_processed", "issued": True},
        }
        for label, changes in variants.items():
            with self.subTest(label=label):
                packet = copy.deepcopy(clean_packet())
                packet["carrier_document"].update(changes)
                packet["carrier_document"]["fields"]["vin"]["raw"] = ""
                result = run_confirmation(packet)
                self.assertFalse(result["review_ready"])
                self.assertIn(result["outcome"], {"waiting_for_carrier", "evidence_invalid"})
                self.assertEqual(result["comparison"], [])
                self.assertFalse(result["comparison_complete"])
                self.assertNotIn("1HGCM82633A004352", result["note"])
                self.assertNotIn("None found", result["note"])
                self.assertNotIn("No exceptions.", result["note"])
                self.assertTrue(result["note"].strip().endswith(NOTE_SIGNATURE))
                self.assertEqual(result["external_writes"], 0)

    def test_extra_change_or_wrong_premium_basis(self):
        extra = copy.deepcopy(clean_packet())
        extra["carrier_document"]["unrequested"] = [
            {
                "label": "newly added exclusion",
                "raw": "Nuclear exclusion",
                "reference": "endorsement page 2",
                "source_id": "endorsement-100",
                "coverage": True,
            }
        ]
        extra_result = run_confirmation(extra)
        self.assertFalse(extra_result["review_ready"])
        self.assertIn("coverage_review_required", extra_result["outcomes"])
        flagged = next(row for row in extra_result["comparison"] if row["verdict"] == "unrequested_change")
        self.assertEqual(flagged["carrier"]["raw"], "Nuclear exclusion")
        self.assertIn("Nuclear exclusion", extra_result["note"])

        basis = copy.deepcopy(clean_packet())
        basis["ezlynx_record"]["fields"]["premium_basis"]["raw"] = "full-term premium"
        basis["ezlynx_record"]["fields"]["premium_amount"]["raw"] = "1200.00"
        basis_result = run_confirmation(basis)
        self.assertFalse(basis_result["review_ready"])
        self.assertIn("ezlynx_correction_required", basis_result["outcomes"])
        amount = next(row for row in basis_result["comparison"] if row["field"] == "premium_amount")
        kind = next(row for row in basis_result["comparison"] if row["field"] == "premium_basis")
        self.assertEqual(amount["verdict"], "mismatch")
        self.assertEqual(kind["verdict"], "mismatch")
        self.assertIn("1200.00", kind["explanation"] + amount["explanation"])
        self.assertIn("full-term premium", kind["explanation"])

    def test_duplicate_discussion_unknown_assigner_or_reassignment(self):
        duplicate = copy.deepcopy(clean_packet())
        duplicate["discussions"] = [
            {"id": "discussion-100", "timestamp": "2026-02-01T00:00:00+00:00"},
            {"id": "discussion-newer", "timestamp": "2026-10-01T00:00:00+00:00"},
        ]
        duplicate_result = run_confirmation(duplicate)
        self.assertEqual(duplicate_result["outcome"], "destination_unverified")
        self.assertFalse(duplicate_result["review_ready"])
        self.assertIsNone(duplicate_result["result_recipient"])
        self.assertNotIn("discussion-newer", duplicate_result["note"])

        unknown = copy.deepcopy(clean_packet())
        unknown["assignment_events"][0]["previous_owner_id"] = ""
        unknown["assignment_events"][0]["previous_owner_name"] = ""
        unknown["case"]["original_assigner_id"] = ""
        unknown_result = run_confirmation(unknown)
        self.assertEqual(unknown_result["outcome"], "destination_unverified")
        self.assertIsNone(unknown_result["result_recipient"])
        self.assertNotIn("someone-else", str(unknown_result["result_recipient"]))
        self.assertNotIn("producer-jake", unknown_result["note"])

        reassigned = copy.deepcopy(clean_packet())
        reassigned["reread"]["current_owner_id"] = "csr-maria"
        reassigned_result = run_confirmation(reassigned)
        self.assertEqual(reassigned_result["outcome"], "stale_context")
        self.assertFalse(reassigned_result["review_ready"])
        self.assertFalse(reassigned_result["completed"])
        self.assertEqual(reassigned_result["external_writes"], 0)

    def test_replay_uncertain_write_or_readback_failure(self):
        ledger = ConfirmationLedger()
        writes = DisabledWrites()
        first = run_confirmation(clean_packet(), ledger=ledger, writes=writes)
        second = run_confirmation(clean_packet(), ledger=ledger, writes=writes)
        self.assertTrue(first["review_ready"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["output_id"], second["output_id"])
        self.assertEqual(len(ledger), 1)
        self.assertEqual(second["external_writes"], 0)
        self.assertFalse(second["completed"])
        self.assertEqual(writes.performed, 0)

        changed = copy.deepcopy(clean_packet())
        changed["source_version"] = "src-2"
        changed["reread"]["source_version"] = "src-2"
        changed["request"]["fields"]["vin"]["raw"] = "1HGCM82633A000000"
        drifted = run_confirmation(changed, ledger=ledger, writes=writes)
        self.assertEqual(drifted["outcome"], "stale_context")
        self.assertFalse(drifted["review_ready"])
        self.assertIsNone(drifted["output_id"])
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger.get(first["case_key"])["output_id"], first["output_id"])

        claimed = copy.deepcopy(clean_packet())
        claimed["write_claim"] = "unknown"
        claim_writes = DisabledWrites()
        claim_ledger = ConfirmationLedger()
        claimed_result = run_confirmation(claimed, ledger=claim_ledger, writes=claim_writes)
        claimed_again = run_confirmation(claimed, ledger=claim_ledger, writes=claim_writes)
        self.assertEqual(claimed_result["outcome"], "writeback_unverified")
        self.assertEqual(claimed_again["outcome"], "writeback_unverified")
        self.assertFalse(claimed_result["completed"])
        self.assertFalse(claimed_result["review_ready"])
        self.assertEqual(claim_writes.performed, 0)
        self.assertEqual(len(claim_ledger), 0)
        self.assertIn("write_claim", claim_writes.refused)

        unread = copy.deepcopy(clean_packet())
        unread["readback"] = {"ok": False, "uncertain": True, "document_id": ""}
        unread_result = run_confirmation(unread)
        self.assertEqual(unread_result["outcome"], "writeback_unverified")
        self.assertEqual(unread_result["blocked_target"], "doc-100")
        self.assertFalse(unread_result["completed"])
        self.assertFalse(unread_result["review_ready"])
        self.assertEqual(unread_result["external_writes"], 0)

    def test_progressive_memo_does_not_confirm_the_carrier(self):
        memo = progressive_access_proof({"source": "fao_memo", "memo_only": True})
        self.assertFalse(memo["confirmed"])
        self.assertEqual(memo["status"], "provisional")
        self.assertIn("memo_retrieval_is_not_an_endorsement", memo["missing"])
        empty = progressive_access_proof(None)
        self.assertFalse(empty["confirmed"])
        switched = copy.deepcopy(clean_packet())
        switched["case"]["carrier"] = "Geico"
        refused = run_confirmation(switched)
        self.assertEqual(refused["outcome"], "retrieval_blocked")
        self.assertFalse(refused["review_ready"])
        self.assertIn("will not switch", refused["reason"])

    def test_old_checker_and_verification_worker_stay_untouched(self):
        self.assertFalse(POLICY_CHANGE_ENABLED)
        self.assertEqual(OLD_JOB_TYPE, "policy_change_verification")
        self.assertNotEqual(OLD_JOB_TYPE, JOB_TYPE)
        old_schema = get_bounded_job_schema("policy_change_verification")
        self.assertFalse(old_schema["schema_verified"])
        new_schema = get_bounded_job_schema(JOB_TYPE)
        self.assertTrue(new_schema["schema_verified"])
        self.assertIsNotNone(production_hold_reason(JOB_TYPE, env="PRODUCTION"))
        self.assertIsNone(production_hold_reason(JOB_TYPE, env="TEST"))

    def test_opening_a_change_request_form_is_refused(self):
        packet = copy.deepcopy(clean_packet())
        packet["open_change_request_form"] = True
        writes = DisabledWrites()
        result = run_confirmation(packet, writes=writes)
        self.assertEqual(result["outcome"], "retrieval_blocked")
        self.assertEqual(writes.performed, 0)
        self.assertIn("open_change_request_form", writes.refused)
        self.assertFalse(result["completed"])

    def test_worker_holds_for_the_producer_without_a_second_run(self):
        packet = clean_packet()
        with mock.patch.dict(os.environ, {"ROBIE_ENV": "TEST"}):
            from robie_job_engine.test_runtime import build_test_engine

            root = durable_tmpdir(self)
            store = JobStore(str(root / "jobs.db"))
            job = store.create_job(JOB_TYPE, packet, idempotency_key="policy-change-confirmation:task-100|assign-100|policy-100")
            engine = build_test_engine(store)
            finished = engine.run(job["id"])
            self.assertEqual(finished["status"], JobStatus.AWAITING_HUMAN_INPUT.value)
            self.assertNotEqual(finished["status"], JobStatus.COMPLETE.value)
            again = engine.run(job["id"])
            self.assertEqual(again["status"], JobStatus.AWAITING_HUMAN_INPUT.value)
            attempts = store.list_attempts(job["id"])
            self.assertEqual(len(attempts), 1)
            worker = PolicyChangeConfirmationWorker()
            held = worker.perform({"action_type": JOB_TYPE, "payload": packet}, idempotency_key="once")
            self.assertFalse(held.succeeded)
            self.assertEqual(held.hold_status, JobStatus.AWAITING_HUMAN_INPUT)
            self.assertEqual(held.detail["external_writes"], 0)
            self.assertEqual(held.destination["confirmation_owner"], "producer")

    def test_pilot_module_does_not_call_ezlynx_write_functions(self):
        source = Path("robie_job_engine/policy_change_confirmation.py").read_text(encoding="utf-8")
        for banned in (
            "add_note_to_discussion",
            "upload_applicant_document",
            "file_note_to_existing_discussion",
        ):
            self.assertNotIn(banned, source)


if __name__ == "__main__":
    unittest.main()
