"""Acceptance tests for the read-only policy-change confirmation pilot.

Synthetic case only. No live EZLynx, carrier, or Production call.
"""

from __future__ import annotations

import copy
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from _durable_tmp import durable_tmpdir
from robie_job_engine.job_schema import get_bounded_job_schema
from robie_job_engine.job_type_gate import production_hold_reason
from robie_job_engine.models import JobStatus
from robie_job_engine.policy_change_confirmation import (
    EZLYNX_FILED_SOURCE,
    JOB_TYPE,
    NOTE_SIGNATURE,
    REQUEST_SOURCE_UNCLEAR,
    REQUEST_SOURCE_UNCLEAR_OTHER,
    ROLE_DECISION,
    draft_note,
    ConfirmationLedger,
    DisabledWrites,
    PolicyChangeConfirmationWorker,
    progressive_access_proof,
    run_confirmation,
)
from robie_job_engine.policy_change_ezlynx_read import (
    apply_ezlynx_read,
    read_policy_change_context,
)
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


def three_jr_packet():
    """The 2026-10-01 hermes-test-01 packet. Not a synthetic stand-in."""
    path = (
        Path(__file__).resolve().parent
        / "fixtures"
        / "policy_change_confirmation"
        / "3jr-2026-10-01-packet.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


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
        # Import here, not at module load. Discover imports this file before
        # test_policy_change_worker installs its verification fakes, and an
        # earlier import binds that worker to the wrong mailer and login gap.
        from robie_job_engine.policy_change_worker import JOB_TYPE as OLD_JOB_TYPE
        from robie_job_engine.policy_change_worker import POLICY_CHANGE_ENABLED

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
        reader = Path("robie_job_engine/policy_change_ezlynx_read.py").read_text(encoding="utf-8")
        for banned in (
            "add_note_to_discussion",
            "upload_applicant_document",
            "file_note_to_existing_discussion",
            "urlopen",
        ):
            self.assertNotIn(banned, source)
            self.assertNotIn(banned, reader)

    def test_three_jr_replay_flags_effective_date_mismatch(self):
        packet = three_jr_packet()
        writes = DisabledWrites()
        result = run_confirmation(packet, writes=writes)
        self.assertEqual(result["outcome"], "destination_unverified")
        self.assertIn("carrier_correction_required", result["outcomes"])
        self.assertFalse(result["review_ready"])
        self.assertFalse(result["completed"])
        self.assertEqual(result["external_writes"], 0)
        self.assertEqual(writes.performed, 0)
        self.assertFalse(result["writes_enabled"])
        self.assertFalse(result["carrier_pilot"]["confirmed"])
        self.assertEqual(result["carrier_pilot"]["status"], "provisional")
        self.assertIn("directory_route_verified", result["carrier_pilot"]["missing"])
        self.assertEqual(result["carrier_source"]["label"], EZLYNX_FILED_SOURCE)
        self.assertFalse(result["carrier_source"]["proves_directory_route"])
        self.assertFalse(result["carrier_source"]["progressive_confirmed"])
        self.assertFalse(result["carrier_source"]["directory_route_verified"])
        self.assertFalse(result["live_carrier_retrieval"])
        self.assertEqual(result["live_test"], "UNVERIFIED")
        flagged = next(row for row in result["comparison"] if row["field"] == "effective_date")
        self.assertEqual(flagged["verdict"], "mismatch")
        self.assertTrue(flagged["date_mismatch"])
        self.assertEqual(flagged["requested_effective_date"], "2026-06-22")
        self.assertEqual(flagged["issued_effective_date"], "2026-09-16")
        self.assertEqual(flagged["request"]["raw"], "06/22/2026")
        self.assertEqual(flagged["carrier"]["raw"], "September 16, 2026")
        self.assertEqual(flagged["ezlynx"]["state"], "missing")
        self.assertIn("06/22/2026", result["note"])
        self.assertIn("September 16, 2026", result["note"])
        self.assertIn("already filed in EZLynx", result["note"])
        self.assertIn("Progressive is not confirmed", result["note"])
        self.assertTrue(result["note"].strip().endswith(NOTE_SIGNATURE))
        for gap in (
            "task_id",
            "due_date",
            "submission_evidence",
            "ezlynx_vehicle_list",
            "ezlynx_change_effective_date",
        ):
            self.assertIn(gap, result["unread"])
        self.assertNotIn("assignee", result["unread"])
        self.assertEqual(writes.refused, [])

    def test_snapshot_reads_task_vehicles_and_change_date_without_inventing(self):
        note = (
            "pls call progressive and see if they willing to backdate "
            "removing vehicle to the effective date of the policy term 2026 "
            "if not close out task TY"
        )
        context = read_policy_change_context(
            {
                "applicant_id": "182400439",
                "policy_id": "71575837",
                "policy_number": "860521214",
                "notes": [{"id": "1133255175", "body": note}],
                "tasks": [
                    {
                        "TaskId": "task-3jr",
                        "DueDate": "10/07/2026",
                        "AssignedUserId": "eimy-1",
                        "AssignedUserName": "Eimy Ramos",
                        "ApplicantId": "182400439",
                        "PolicyNumber": "860521214",
                    },
                    {
                        "TaskId": "other-task",
                        "DueDate": "10/08/2026",
                        "AssignedUserName": "Someone Else",
                        "PolicyNumber": "999",
                    },
                ],
                "policy": {
                    "transactions": [
                        {"type": "Policy Change", "TransactionDate": "2026-09-25"}
                    ]
                },
            }
        )
        self.assertEqual(context["task_id"], "task-3jr")
        self.assertEqual(context["due_date"], "10/07/2026")
        self.assertEqual(context["assignee_name"], "Eimy Ramos")
        self.assertEqual(context["assignee_id"], "eimy-1")
        self.assertEqual(context["submission_evidence"], "")
        self.assertEqual(context["change_effective_date"], "")
        self.assertEqual(context["transaction_date"], "2026-09-25")
        self.assertFalse(context["transaction_date_used_as_change_effective"])
        self.assertEqual(context["writes"], 0)
        self.assertIn("submission_evidence", context["unread"])
        self.assertIn("ezlynx_change_effective_date", context["unread"])
        self.assertIn("ezlynx_vehicle_list", context["unread"])

        ambiguous = read_policy_change_context(
            {
                "policy_number": "860521214",
                "tasks": [
                    {"TaskId": "a", "PolicyNumber": "860521214", "DueDate": "10/01/2026"},
                    {"TaskId": "b", "PolicyNumber": "860521214", "DueDate": "10/02/2026"},
                ],
            }
        )
        self.assertEqual(ambiguous["task_id"], "")
        self.assertIn("task_id", ambiguous["unread"])

        packet = three_jr_packet()
        packet["ezlynx_snapshot"] = {
            "applicant_id": "182400439",
            "policy_number": "860521214",
            "policy_id": "71575837",
            "tasks": [
                {
                    "TaskId": "task-3jr",
                    "DueDate": "10/07/2026",
                    "AssignedUserName": "Eimy Ramos",
                    "PolicyNumber": "860521214",
                }
            ],
            "policy": {
                "ChangeEffectiveDate": "09/16/2026",
                "transactions": [
                    {"type": "Policy Change", "TransactionDate": "2026-09-25"}
                ],
                "vehicles": [
                    {
                        "Year": "2002",
                        "Make": "Ford",
                        "Model": "Econoline",
                        "VIN": "1FMRE11L12HB45846",
                        "Status": "removed",
                    }
                ],
            },
        }
        filled = apply_ezlynx_read(packet, packet["ezlynx_snapshot"])
        self.assertEqual(filled["case"]["task_id"], "task-3jr")
        self.assertEqual(filled["case"]["due_date"], "10/07/2026")
        self.assertEqual(filled["case"]["submission_evidence"], "")
        self.assertEqual(filled["ezlynx_record"]["transaction_date"], "2026-09-25")
        self.assertEqual(
            filled["ezlynx_record"]["fields"]["effective_date"]["raw"],
            "09/16/2026",
        )
        self.assertEqual(filled["ezlynx_record"]["vehicles"][0]["vin"], "1FMRE11L12HB45846")
        writes = DisabledWrites()
        result = run_confirmation(packet, writes=writes)
        flagged = next(row for row in result["comparison"] if row["field"] == "effective_date")
        self.assertEqual(flagged["verdict"], "mismatch")
        self.assertEqual(flagged["requested_effective_date"], "2026-06-22")
        self.assertEqual(flagged["issued_effective_date"], "2026-09-16")
        self.assertEqual(flagged["ezlynx"]["normalized"], "2026-09-16")
        self.assertEqual(result["flags"][0]["code"], "effective_date_mismatch")
        self.assertNotIn("task_id", result["unread"])
        self.assertNotIn("due_date", result["unread"])
        self.assertNotIn("ezlynx_vehicle_list", result["unread"])
        self.assertNotIn("ezlynx_change_effective_date", result["unread"])
        self.assertIn("submission_evidence", result["unread"])
        self.assertFalse(result["carrier_pilot"]["confirmed"])
        self.assertEqual(result["external_writes"], 0)
        self.assertEqual(writes.performed, 0)

    def test_live_retrieval_without_a_filed_endorsement_still_stops(self):
        packet = copy.deepcopy(clean_packet())
        packet["retrieve_live"] = True
        packet["carrier_proof"] = {
            "worker_identity": "SSRobie",
            "environment": "TEST",
            "host": "hermes-test-01",
            "directory_route_verified": False,
            "genuine_issued_endorsement": True,
            "endorsement_document_id": "doc-100",
            "source": "fao_memo",
            "memo_only": True,
        }
        result = run_confirmation(packet)
        self.assertEqual(result["outcome"], "retrieval_blocked")
        self.assertEqual(result["comparison"], [])
        self.assertFalse(result["carrier_pilot"]["confirmed"])
        self.assertIsNone(result["carrier_source"])

    def test_task_and_client_request_date_disagreement_is_flagged(self):
        packet = copy.deepcopy(clean_packet())
        packet["task"].update(
            {
                "title": "Add the truck",
                "description": "The note below says effective 6/22. That sentence is not a date field.",
                "comments": [{"id": "c1", "text": "Please use 6/22"}],
                "attachments": [{"id": "att-1", "name": "client-request.pdf"}],
                "assignee_name": "Maria Bara",
                "due_date": "2026-03-05",
                "requested_effective_date": "04/01/2026",
            }
        )
        writes = DisabledWrites()
        result = run_confirmation(packet, writes=writes)
        flagged = next(item for item in result["flags"] if item["code"] == "task_request_date_disagreement")
        self.assertEqual(flagged["client_request"], "2026-03-01")
        self.assertEqual(flagged["ezlynx_task"], "2026-04-01")
        self.assertEqual(flagged["client_request_raw"], "03/01/2026")
        self.assertEqual(flagged["ezlynx_task_raw"], "04/01/2026")
        self.assertIn("request_unclear", result["outcomes"])
        self.assertNotEqual(result["outcome"], "ready_for_human_review")
        self.assertFalse(result["review_ready"])
        self.assertFalse(result["completed"])
        self.assertIsNone(result["request_source"])
        self.assertFalse(result["hold_for_human"])
        self.assertFalse(result["carrier_pilot"]["confirmed"])
        self.assertEqual(result["external_writes"], 0)
        self.assertEqual(writes.performed, 0)
        row = next(item for item in result["comparison"] if item["field"] == "effective_date")
        self.assertEqual(row["request"]["raw"], "03/01/2026")
        self.assertIn(row["verdict"], {"exact_match", "normalized_match"})
        self.assertIn("Neither date was chosen.", result["note"])
        self.assertIn("03/01/2026", result["note"])
        self.assertIn("04/01/2026", result["note"])
        self.assertNotIn("6/22", result["note"])
        task = result["request_sources"]["ezlynx_task"]
        self.assertEqual(task["title"], "Add the truck")
        self.assertEqual(task["comments"][0]["text"], "Please use 6/22")
        self.assertEqual(task["attachments"][0]["name"], "client-request.pdf")
        self.assertEqual(task["assignee_name"], "Maria Bara")
        self.assertEqual(task["due_date"], "2026-03-05")
        self.assertEqual(result["request_sources"]["discussion_notes"][0]["body"], "Please add the truck.")
        self.assertEqual(result["request_sources"]["writes"], 0)
        held = PolicyChangeConfirmationWorker().perform(
            {"action_type": JOB_TYPE, "payload": packet},
            idempotency_key="task-date",
        )
        self.assertEqual(held.hold_status, JobStatus.NEEDS_CLARIFICATION)
        self.assertEqual(held.detail["external_writes"], 0)

        due_only = copy.deepcopy(clean_packet())
        due_only["task"].update(
            {
                "title": "Add the truck",
                "description": "effective 6/22",
                "comments": ["use 6/22"],
                "due_date": "2026-04-01",
                "assignee_name": "Maria Bara",
                "attachments": [{"name": "photo.pdf"}],
            }
        )
        due_result = run_confirmation(due_only)
        self.assertFalse(any(item["code"] == "task_request_date_disagreement" for item in due_result["flags"]))
        self.assertTrue(any(item["code"] == "task_request_date_check_skipped" for item in due_result["flags"]))
        self.assertEqual(due_result["outcome"], "ready_for_human_review")
        self.assertEqual(due_result["request_sources"]["ezlynx_task"]["requested_effective_date"], "")
        self.assertEqual(due_result["request_sources"]["ezlynx_task"]["due_date"], "2026-04-01")
        self.assertNotIn("Neither date was chosen.", due_result["note"])
        self.assertIn("task-versus-request date check was skipped", due_result["note"])
        self.assertNotIn("2026-04-01", due_result["note"])
        self.assertIn("No exceptions.", due_result["note"])

        same = copy.deepcopy(clean_packet())
        same["task"]["requested_effective_date"] = "March 1, 2026"
        same_result = run_confirmation(same)
        self.assertFalse(any(item["code"] == "task_request_date_disagreement" for item in same_result["flags"]))
        self.assertEqual(same_result["outcome"], "ready_for_human_review")

        recording = copy.deepcopy(clean_packet())
        recording["request"]["source"] = "call_recording"
        recording["task"]["requested_effective_date"] = "05/01/2026"
        recording["case"]["requested_effective_date"] = "2026-05-01"
        recording_result = run_confirmation(recording)
        self.assertEqual(recording_result["request_source"], REQUEST_SOURCE_UNCLEAR)
        self.assertTrue(recording_result["hold_for_human"])
        self.assertFalse(any(item["code"] == "task_request_date_disagreement" for item in recording_result["flags"]))
        self.assertNotIn("05/01/2026", recording_result["note"])
        self.assertNotIn("2026-05-01", recording_result["note"])
        self.assertFalse(recording_result["carrier_pilot"]["confirmed"])

    def test_unclear_request_source_holds_for_a_person_without_guessing(self):
        recording = copy.deepcopy(clean_packet())
        recording["request"]["source"] = "call_recording"
        recording["request"]["summary"] = "Caller asked to delete a van effective June 1."
        recording["request"]["fields"]["effective_date"] = {
            "raw": "06/01/2026",
            "reference": "recording",
        }
        recording["case"]["requested_effective_date"] = "2026-06-01"
        writes = DisabledWrites()
        result = run_confirmation(recording, writes=writes)
        self.assertEqual(result["outcome"], "request_unclear")
        self.assertEqual(result["request_source"], REQUEST_SOURCE_UNCLEAR)
        self.assertTrue(result["hold_for_human"])
        self.assertFalse(result["review_ready"])
        self.assertFalse(result["completed"])
        self.assertEqual(result["comparison"], [])
        self.assertFalse(result["comparison_complete"])
        self.assertEqual(result["external_writes"], 0)
        self.assertEqual(writes.performed, 0)
        self.assertFalse(result["writes_enabled"])
        self.assertFalse(result["carrier_pilot"]["confirmed"])
        self.assertIn(REQUEST_SOURCE_UNCLEAR, result["note"])
        self.assertIn("No date was guessed.", result["note"])
        self.assertIn("not stated", result["note"])
        self.assertNotIn("06/01/2026", result["note"])
        self.assertNotIn("2026-06-01", result["note"])
        self.assertNotIn("June 1", result["note"])
        self.assertTrue(result["note"].strip().endswith(NOTE_SIGNATURE))
        self.assertIn("A person needs to review", result["note"])
        held = PolicyChangeConfirmationWorker().perform(
            {"action_type": JOB_TYPE, "payload": recording},
            idempotency_key="recording-hold",
        )
        self.assertFalse(held.succeeded)
        self.assertEqual(held.hold_status, JobStatus.NEEDS_CLARIFICATION)
        self.assertTrue(held.detail["hold_for_human"])
        self.assertEqual(held.detail["external_writes"], 0)

        missing = copy.deepcopy(clean_packet())
        missing["request"] = {}
        missing["case"]["requested_effective_date"] = "2026-03-01"
        missing_result = run_confirmation(missing)
        self.assertEqual(missing_result["request_source"], REQUEST_SOURCE_UNCLEAR_OTHER)
        self.assertNotIn("call recording", missing_result["note"].casefold())
        self.assertTrue(missing_result["hold_for_human"])
        self.assertEqual(missing_result["comparison"], [])
        self.assertNotIn("2026-03-01", missing_result["note"])
        self.assertFalse(missing_result["carrier_pilot"]["confirmed"])

        undated = copy.deepcopy(clean_packet())
        del undated["request"]["fields"]["effective_date"]
        undated["case"]["requested_effective_date"] = ""
        undated["carrier_document"]["fields"]["effective_date"]["raw"] = "2026-04-01"
        undated_result = run_confirmation(undated)
        self.assertEqual(undated_result["request_source"], REQUEST_SOURCE_UNCLEAR_OTHER)
        self.assertNotIn("call recording", undated_result["note"].casefold())
        self.assertTrue(undated_result["hold_for_human"])
        self.assertEqual(undated_result["comparison"], [])
        self.assertNotIn("2026-04-01", undated_result["note"])
        self.assertNotIn("March 1, 2026", undated_result["note"])
        self.assertFalse(undated_result["carrier_pilot"]["confirmed"])

        written = copy.deepcopy(clean_packet())
        written["request"]["source"] = "client_center"
        written_result = run_confirmation(written)
        self.assertIsNone(written_result["request_source"])
        self.assertFalse(written_result["hold_for_human"])
        self.assertEqual(written_result["outcome"], "ready_for_human_review")

    def test_filed_endorsement_does_not_verify_the_directory_route(self):
        proof = progressive_access_proof(
            {
                "worker_identity": "SSRobie",
                "environment": "TEST",
                "host": "hermes-test-01",
                "directory_route_verified": True,
                "genuine_issued_endorsement": True,
                "endorsement_document_id": "823968766",
                "source": EZLYNX_FILED_SOURCE,
            }
        )
        self.assertFalse(proof["confirmed"])
        self.assertIn("directory_route_verified", proof["missing"])


class PolicyChangeTaskActionTests(unittest.TestCase):
    """The task goes back to the person who assigned it. No new task, ever."""

    def test_completed_check_reassigns_back_to_original_assigner(self):
        result = run_confirmation(clean_packet(), writes=DisabledWrites())
        action = result["task_action"]
        self.assertEqual(action["action"], "reassign_back")
        self.assertEqual(action["to_assigner_id"], "csr-maria")
        self.assertEqual(action["to_assigner_name"], "Maria Bara")
        self.assertEqual(action["task_id"], "task-100")
        self.assertIs(action["create_task"], False)
        _assert_plain_note(self, action["note"])

    def test_unsure_check_still_reassigns_back(self):
        packet = copy.deepcopy(clean_packet())
        packet["request"]["source"] = "call_recording"
        result = run_confirmation(packet, writes=DisabledWrites())
        self.assertEqual(result["request_source"], REQUEST_SOURCE_UNCLEAR)
        action = result["task_action"]
        self.assertEqual(action["action"], "reassign_back")
        self.assertEqual(action["to_assigner_id"], "csr-maria")
        self.assertIs(action["create_task"], False)
        _assert_plain_note(self, action["note"])

    def test_hold_when_assigner_not_exact(self):
        packet = copy.deepcopy(clean_packet())
        packet["assignment_events"] = []
        packet["case"]["original_assigner_id"] = ""
        packet["task"]["created_by"] = ""
        result = run_confirmation(packet, writes=DisabledWrites())
        action = result["task_action"]
        self.assertEqual(action["action"], "hold")
        self.assertIs(action["create_task"], False)
        self.assertFalse(action["judgment_call"])
        self.assertNotIn("to_assigner_id", action)
        self.assertIn("stays with ROBIE", action["reason"])

    def test_hold_when_assigner_is_robie(self):
        packet = copy.deepcopy(clean_packet())
        packet["assignment_events"][0]["previous_owner_id"] = "SSRobie"
        packet["assignment_events"][0]["previous_owner_name"] = "SSRobie"
        packet["task"]["created_by"] = ""
        result = run_confirmation(packet, writes=DisabledWrites())
        action = result["task_action"]
        self.assertEqual(action["action"], "hold")
        self.assertIs(action["create_task"], False)
        self.assertFalse(action["judgment_call"])
        self.assertIn("stays with ROBIE", action["reason"])

    def test_create_task_is_always_false(self):
        for packet in (clean_packet(), three_jr_packet()):
            result = run_confirmation(packet, writes=DisabledWrites())
            action = result["task_action"]
            self.assertIs(action["create_task"], False)
            self.assertNotIn(action["action"], {"create_task", "create", "new_task"})

    def test_note_always_ends_with_signature(self):
        for packet in (clean_packet(), three_jr_packet()):
            result = run_confirmation(packet, writes=DisabledWrites())
            action = result["task_action"]
            if action["action"] == "reassign_back":
                self.assertTrue(action["note"].strip().endswith(NOTE_SIGNATURE))

    def test_executor_dry_run_touches_nothing(self):
        from robie_job_engine.policy_change_task_executor import execute

        result = run_confirmation(clean_packet(), writes=DisabledWrites())
        outcome = execute(result["task_action"], mode="dry_run")
        self.assertFalse(outcome["executed"])
        self.assertIs(outcome["create_task"], False)
        self.assertTrue(any("csr-maria" in line for line in outcome["plan"]))

    def test_executor_live_refused_until_selectors_confirmed(self):
        from robie_job_engine.policy_change_task_executor import execute

        result = run_confirmation(clean_packet(), writes=DisabledWrites())
        outcome = execute(result["task_action"], mode="live")
        self.assertFalse(outcome["executed"])
        self.assertIn("UNCONFIRMED", outcome["refused"])

    def test_note_channel_is_task_comment(self):
        from robie_job_engine.policy_change_task_action import NOTE_CHANNEL

        self.assertEqual(NOTE_CHANNEL, "task_comment")
        result = run_confirmation(clean_packet(), writes=DisabledWrites())
        self.assertEqual(result["task_action"]["note_channel"], "task_comment")

    def test_validate_note_for_task(self):
        from robie_job_engine.policy_change_task_action import validate_note_for_task

        good = "Plain result.\n" + NOTE_SIGNATURE
        self.assertEqual(validate_note_for_task(good), good)
        with self.assertRaises(ValueError):
            validate_note_for_task("")
        with self.assertRaises(ValueError):
            validate_note_for_task("No signature here.")

    def test_executor_plan_posts_comment_then_reassigns(self):
        from robie_job_engine.policy_change_task_executor import execute

        result = run_confirmation(clean_packet(), writes=DisabledWrites())
        outcome = execute(result["task_action"], mode="dry_run")
        plan_text = "\n".join(outcome["plan"])
        self.assertIn("task comment", plan_text)
        self.assertIn("csr-maria", plan_text)
        self.assertEqual(outcome["note_channel"], "task_comment")

    def test_hold_names_the_person_who_owns_the_task(self):
        from robie_job_engine.policy_change_task_executor import describe_plan

        result = run_confirmation(three_jr_packet(), writes=DisabledWrites())
        action = result["task_action"]
        self.assertEqual(action["action"], "hold")
        self.assertIs(action["create_task"], False)
        self.assertIn("stays with Eimy Ramos", action["reason"])
        self.assertNotIn("stays with ROBIE", action["reason"])
        plan = "\n".join(describe_plan(action))
        self.assertIn("stays with Eimy Ramos", plan)
        self.assertNotIn("stays with ROBIE", plan)
        self.assertEqual(result["external_writes"], 0)

    def test_call_recording_wording_only_for_a_call_recording(self):
        download = copy.deepcopy(clean_packet())
        download["request"]["source"] = "carrier_download"
        download["request"]["written"] = False
        held = run_confirmation(download, writes=DisabledWrites())
        self.assertEqual(held["request_source"], REQUEST_SOURCE_UNCLEAR_OTHER)
        self.assertTrue(held["hold_for_human"])
        self.assertNotIn("call recording", held["note"].casefold())
        self.assertNotIn("call recording", held["task_action"]["reason"].casefold())
        self.assertIn("request source unclear", held["note"])
        self.assertEqual(held["external_writes"], 0)

        blocked = copy.deepcopy(clean_packet())
        blocked["request"]["source"] = "carrier-download"
        blocked["carrier_document"] = {}
        blocked["directory_entry"] = {"document_download_route": "progressive"}
        blocked_result = run_confirmation(blocked, writes=DisabledWrites())
        self.assertEqual(blocked_result["outcome"], "retrieval_blocked")
        self.assertIsNone(blocked_result["request_source"])
        self.assertNotIn("call recording", blocked_result["note"].casefold())

        recording = copy.deepcopy(clean_packet())
        recording["request"]["source"] = "call recording"
        recording_result = run_confirmation(recording, writes=DisabledWrites())
        self.assertEqual(recording_result["request_source"], REQUEST_SOURCE_UNCLEAR)
        self.assertIn("call recording", recording_result["note"])

    def test_blank_requested_date_uses_plain_wording(self):
        packet = copy.deepcopy(clean_packet())
        packet["case"]["requested_effective_date"] = ""
        packet["request"]["fields"]["effective_date"]["raw"] = ""
        note = draft_note(
            packet,
            {
                "request_source": None,
                "comparison_complete": False,
                "comparison": [],
                "flags": [],
                "outcome": "retrieval_blocked",
                "result_sentence": "A person needs to look at this.",
            },
        )
        self.assertNotIn("effective .", note)
        self.assertIn("The requested effective date is not known.", note)
        self.assertTrue(note.strip().endswith(NOTE_SIGNATURE))

    def test_task_date_is_taken_from_task_text_or_the_check_is_skipped(self):
        dated = copy.deepcopy(clean_packet())
        dated["task"]["description"] = "Client asked for the change effective June 22, 2026."
        dated_result = run_confirmation(dated, writes=DisabledWrites())
        flagged = next(
            item for item in dated_result["flags"] if item["code"] == "task_request_date_disagreement"
        )
        self.assertEqual(flagged["client_request"], "2026-03-01")
        self.assertEqual(flagged["ezlynx_task"], "2026-06-22")
        self.assertEqual(flagged["ezlynx_task_raw"], "June 22, 2026")
        self.assertIn("Neither date was chosen.", dated_result["note"])
        self.assertIn("request_unclear", dated_result["outcomes"])
        self.assertEqual(
            dated_result["request_sources"]["ezlynx_task"]["requested_effective_date"],
            "June 22, 2026",
        )
        self.assertEqual(dated_result["external_writes"], 0)

        titled = copy.deepcopy(clean_packet())
        titled["task"]["title"] = "Delete the van effective 06/22/2026"
        titled_result = run_confirmation(titled, writes=DisabledWrites())
        self.assertEqual(
            next(item for item in titled_result["flags"] if item["code"] == "task_request_date_disagreement")[
                "ezlynx_task"
            ],
            "2026-06-22",
        )

        commented = copy.deepcopy(clean_packet())
        commented["task"]["comments"] = [{"id": "c9", "text": "Use September 16, 2026."}]
        commented_result = run_confirmation(commented, writes=DisabledWrites())
        self.assertEqual(
            next(
                item
                for item in commented_result["flags"]
                if item["code"] == "task_request_date_disagreement"
            )["ezlynx_task"],
            "2026-09-16",
        )

        blank = copy.deepcopy(clean_packet())
        blank["task"]["title"] = "Download the carrier endorsement"
        blank["task"]["description"] = "No date in this task."
        blank["task"]["due_date"] = "2026-04-01"
        blank_result = run_confirmation(blank, writes=DisabledWrites())
        self.assertFalse(
            any(item["code"] == "task_request_date_disagreement" for item in blank_result["flags"])
        )
        skipped = next(item for item in blank_result["flags"] if item["code"] == "task_request_date_check_skipped")
        self.assertIn("skipped", skipped["reason"])
        self.assertEqual(blank_result["outcome"], "ready_for_human_review")
        self.assertNotIn("request_unclear", blank_result["outcomes"])
        self.assertIn("No exceptions.", blank_result["note"])
        self.assertIn("task-versus-request date check was skipped", blank_result["note"])
        self.assertNotIn("2026-04-01", blank_result["note"])
        self.assertEqual(blank_result["request_sources"]["ezlynx_task"]["requested_effective_date"], "")

        many = copy.deepcopy(clean_packet())
        many["task"]["description"] = "Could be 06/22/2026 or September 16, 2026."
        many_result = run_confirmation(many, writes=DisabledWrites())
        self.assertFalse(
            any(item["code"] == "task_request_date_disagreement" for item in many_result["flags"])
        )
        self.assertIn("more than one date", many_result["note"])
        self.assertIn("No date was chosen.", many_result["note"])
        self.assertNotIn("Neither date was chosen.", many_result["note"])
        self.assertEqual(many_result["outcome"], "ready_for_human_review")

        quiet = run_confirmation(clean_packet(), writes=DisabledWrites())
        self.assertFalse(any(item["code"] == "task_request_date_check_skipped" for item in quiet["flags"]))
        self.assertNotIn("date check was skipped", quiet["note"])
        self.assertIn("No exceptions.", quiet["note"])

    def test_task_created_already_assigned_to_robie_hands_back_to_creator(self):
        from robie_job_engine.policy_change_task_executor import execute

        packet = copy.deepcopy(clean_packet())
        packet["assignment_events"] = []
        packet["case"]["original_assigner_id"] = ""
        packet["task"]["current_owner_id"] = "Robie"
        packet["task"]["created_by"] = "Carlo"
        packet["task"]["created_by_name"] = "Carlo"
        packet["reread"]["current_owner_id"] = "Robie"
        writes = DisabledWrites()
        result = run_confirmation(packet, writes=writes)
        action = result["task_action"]
        self.assertEqual(action["action"], "reassign_back")
        self.assertEqual(action["to_assigner_name"], "Carlo")
        self.assertTrue(action["judgment_call"])
        self.assertIs(action["create_task"], False)
        self.assertIsNone(result["result_recipient"])
        self.assertIn("judgment call", action["reason"].casefold())
        self.assertNotIn("create", action["action"])
        outcome = execute(action, mode="dry_run")
        plan = "\n".join(outcome["plan"])
        self.assertFalse(outcome["executed"])
        self.assertIs(outcome["create_task"], False)
        self.assertIn("judgment call", plan.casefold())
        self.assertIn("Carlo", plan)
        self.assertIn("A new task is never created.", plan)
        self.assertEqual(writes.performed, 0)
        self.assertEqual(result["external_writes"], 0)

    def test_verifier_rejects_task_creation(self):
        from robie_job_engine.policy_change_confirmation import (
            PolicyChangeConfirmationVerifier,
        )

        verifier = PolicyChangeConfirmationVerifier()
        bad = run_confirmation(clean_packet(), writes=DisabledWrites())
        bad["task_action"] = {"action": "reassign_back", "create_task": True}
        outcome = verifier.verify({}, {"detail": bad})
        self.assertFalse(outcome.verified)
        self.assertIn("never create a task", outcome.error)


if __name__ == "__main__":
    unittest.main()
