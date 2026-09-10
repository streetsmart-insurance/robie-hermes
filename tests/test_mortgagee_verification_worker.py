"""Unit tests for the mortgagee verification worker.

Fakes only: no network, no live EZLynx, no real customer data. The sibling
modules ``report_fetcher`` and ``verification_common`` do not exist yet, so
this suite injects fake modules into ``sys.modules`` BEFORE importing the
worker module under test.
"""

import json
import sqlite3
import sys
import tempfile
import types
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# ---------------------------------------------------------------------------
# Fake sibling modules (must be installed before the worker module imports).
# ---------------------------------------------------------------------------

_FAKE_ROWS: list[dict] = []
_AUTHORIZED: dict[str, bool] = {}

def _fake_fetch_report_rows(*, report_id, fields=None, filters=None, db_path=None, session=None):
    assert report_id == "4372"
    return [dict(r) for r in _FAKE_ROWS]

@dataclass
class FakePolicyOutcome:
    policy_number: str
    policy_aliases: list = field(default_factory=list)
    applicant_id: str | None = None
    insured_name: str | None = None
    department: str | None = None
    carrier: str | None = None
    status: str = "pending"
    reason: str = ""
    actions_taken: list = field(default_factory=list)
    waiting_on: str | None = None
    evidence: dict = field(default_factory=dict)
    updated_at: str = ""

def _fake_record_outcomes(store, job_id, job_type, outcomes):
    from dataclasses import asdict
    return [asdict(o) for o in (outcomes or [])]

def _fake_is_action_authorized(job, action_name):
    return bool(_AUTHORIZED.get(str(action_name), False))

def _fake_record_login_gap(store, job_id, job_type, portal_name, step, whats_missing):
    return {
        "recorded": True,
        "recorded_via": "fake",
        "job_id": job_id,
        "job_type": job_type,
        "portal_name": portal_name,
        "step": step,
        "whats_missing": whats_missing,
    }

from _sibling_fakes import install_fake, teardown_fakes

_INSTALLED: dict = {}  # module name -> (original sys.modules entry, installed fake)

def _track_fake(name, fake):
    """Install a fake sibling module (see _sibling_fakes for the rules)."""
    install_fake(_INSTALLED, name, fake)

def teardown_module(module):
    """Restore faked sys.modules entries (see _sibling_fakes)."""
    teardown_fakes(_INSTALLED)

def _install_fakes():
    fetcher = types.ModuleType("robie_job_engine.report_fetcher")
    fetcher.fetch_report_rows = _fake_fetch_report_rows
    install_fake(_INSTALLED, "robie_job_engine.report_fetcher", fetcher)

    common = types.ModuleType("robie_job_engine.verification_common")
    common.PolicyOutcome = FakePolicyOutcome
    common.record_outcomes = _fake_record_outcomes
    common.record_login_gap = _fake_record_login_gap
    common.is_action_authorized = _fake_is_action_authorized
    install_fake(_INSTALLED, "robie_job_engine.verification_common", common)

_install_fakes()

from robie_job_engine.mortgagee_verification_worker import (  # noqa: E402
    DURABLE_NAMESPACE,
    LENDER_PORTAL_NEEDS_LOGIN_DEFAULT,
    MortgageeVerificationVerifier,
    MortgageeVerificationWorker,
    bucket_window,
    build_discussion_note,
    build_mortgagee_voice_intent,
    days_to_expiration,
    escalation_decision,
    is_policy_stopped,
    next_payment_check_date,
    payment_check_due,
    producer_gate,
    route_payer,
    verify_lender,
    verify_lender_of_record,
)

@pytest.fixture()
def today():
    return date(2026, 9, 10)

@pytest.fixture()
def row(today):
    return {
        "policy_number": "HO 1234567",
        "loan_number": "LN-000111",
        "lob": "Homeowners",
        "carrier": "Test Carrier Co",
        "insured_name": "Test Homeowner",
        "applicant_id": "A-1",
        "expiration_date": (today + timedelta(days=40)).isoformat(),
        "edocs_downloaded": True,
        "producer_review_complete": True,
        "payer": "mortgagee",
        "mortgage_company": "Test Mortgage Co",
        "property_zip": "08527",
        "lender_portal": "https://lender.example.test/agent",
        "portal_lender_lookup": {"servicer": "Test Mortgage Co", "loan_number": "LN-000111"},
    }

@pytest.fixture()
def job():
    return {"id": "job-1", "action_type": "mortgagee_verification",
            "payload": {"report_id": "4372", "worker": "mortgagee-verification"}}

@pytest.fixture()
def durable_db():
    """A durable (non-/tmp) sqlite path: DurableWorkLedger rejects /tmp."""
    base = Path.home() / ".cache" / "robie-mortgagee-tests"
    base.mkdir(parents=True, exist_ok=True)
    tmpdir = Path(tempfile.mkdtemp(dir=str(base)))
    db = tmpdir / "jobs.db"
    yield str(db)
    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)

@pytest.fixture(autouse=True)
def _reset_fakes():
    _FAKE_ROWS.clear()
    _AUTHORIZED.clear()
    yield
    _FAKE_ROWS.clear()
    _AUTHORIZED.clear()

# ---------------------------------------------------------------------------
# Pure functions: window bucketing
# ---------------------------------------------------------------------------

class TestWindowBucketing:
    def test_in_window_30_to_45(self):
        assert bucket_window(30) == "in_window"
        assert bucket_window(45) == "in_window"
        assert bucket_window(38) == "in_window"

    def test_above_45_waits(self):
        assert bucket_window(46) == "not_yet_in_window"
        assert bucket_window(120) == "not_yet_in_window"

    def test_below_30_is_overdue(self):
        assert bucket_window(29) == "overdue"
        assert bucket_window(0) == "overdue"
        assert bucket_window(-3) == "overdue"

    def test_unparseable_is_unknown(self):
        assert bucket_window(None) == "unknown"

    def test_days_to_expiration(self, today):
        assert days_to_expiration((today + timedelta(days=40)).isoformat(), today=today) == 40
        assert days_to_expiration("not-a-date", today=today) is None
        assert days_to_expiration(None, today=today) is None

# ---------------------------------------------------------------------------
# Pure functions: producer gate
# ---------------------------------------------------------------------------

class TestProducerGate:
    def test_clear_when_recorded(self):
        clear, reason = producer_gate({"producer_review_complete": True})
        assert clear is True
        assert reason

    def test_blocks_when_review_pending(self):
        clear, reason = producer_gate({"review_pending": {"coverage": True}})
        assert clear is False
        assert "coverage" in reason

    def test_blocks_when_flag_pending(self):
        clear, _ = producer_gate({"premium_review_pending": True})
        assert clear is False

    def test_fail_closed_when_unknown(self):
        """No review info at all -> blocked, never silently delivered."""
        clear, reason = producer_gate({})
        assert clear is False
        assert "unknown" in reason.lower()

# ---------------------------------------------------------------------------
# Pure functions: lender verification
# ---------------------------------------------------------------------------

class TestVerifyLender:
    def test_ok(self):
        ok, reason = verify_lender("Test Mortgage Co", "LN-000111", "08527")
        assert ok is True
        assert reason

    def test_zip_plus4_ok(self):
        ok, _ = verify_lender("Test Mortgage Co", "LN-000111", "08527-1234")
        assert ok is True

    def test_missing_loan_number_fails_closed(self):
        ok, reason = verify_lender("Test Mortgage Co", "", "08527")
        assert ok is False
        assert "loan_number" in reason

    def test_missing_zip_fails_closed(self):
        ok, reason = verify_lender("Test Mortgage Co", "LN-000111", "")
        assert ok is False
        assert "property_zip" in reason

    def test_missing_company_fails_closed(self):
        ok, reason = verify_lender(None, "LN-000111", "08527")
        assert ok is False
        assert "mortgage_company" in reason

    def test_invalid_zip_fails_closed(self):
        ok, reason = verify_lender("Test Mortgage Co", "LN-000111", "ABCDE")
        assert ok is False
        assert "ZIP" in reason

# ---------------------------------------------------------------------------
# Pure functions: payer routing + escalation timing + payment checks
# ---------------------------------------------------------------------------

class TestPayerRouting:
    def test_mortgagee(self):
        assert route_payer({"payer": "mortgagee"})[0] == "mortgagee"

    def test_insured(self):
        assert route_payer({"payer": "insured"})[0] == "insured"

    def test_unknown(self):
        assert route_payer({})[0] == "unknown"
        assert route_payer({"payer": "alien"})[0] == "unknown"

class TestEscalationTiming:
    def test_escalates_at_20_days_unconfirmed(self):
        escalate, reason = escalation_decision(20, False, False)
        assert escalate is True
        assert "CSR" in reason

    def test_escalates_below_20_days(self):
        assert escalation_decision(5, False, False)[0] is True

    def test_no_escalation_above_threshold(self):
        assert escalation_decision(21, False, False)[0] is False

    def test_no_escalation_when_paid(self):
        assert escalation_decision(5, True, False)[0] is False

    def test_no_double_escalation(self):
        assert escalation_decision(5, False, True)[0] is False

    def test_weekly_check_cadence(self, today):
        assert next_payment_check_date(today, today=today) == today + timedelta(days=7)
        due, nxt = payment_check_due((today - timedelta(days=8)).isoformat(), today=today)
        assert due is True
        assert nxt == today - timedelta(days=1)
        due, _ = payment_check_due(today.isoformat(), today=today)
        assert due is False

class TestStoppedPolicies:
    def test_cancelled_stops_delivery(self):
        stopped, reason = is_policy_stopped({"policy_status": "Cancelled"})
        assert stopped is True
        assert "EXCLUDED_INACTIVE_ACCOUNT" in reason

    def test_nonrenewed_stops_delivery(self):
        assert is_policy_stopped({"nonrenewed": True})[0] is True

    def test_active_not_stopped(self):
        assert is_policy_stopped({"policy_status": "Active"})[0] is False

class TestNoteFormat:
    def test_header_and_signature(self):
        note = build_discussion_note("HO 1", "Homeowners", "Carrier", "body text")
        assert note.startswith("Policy: #HO 1 (Homeowners - Carrier)")
        assert note.rstrip().endswith("ROBIE was here")

# ---------------------------------------------------------------------------
# Worker behaviour
# ---------------------------------------------------------------------------

def _run_worker(rows, job, today):
    _FAKE_ROWS.extend(rows)
    worker = MortgageeVerificationWorker()
    return worker.perform(dict(job), idempotency_key="test-key")

class TestWorkerFlows:
    def test_full_mortgagee_path_with_authorization(self, row, job, today, durable_db):
        _AUTHORIZED["upload_lender_document"] = True
        job["payload"]["jobs_db_path"] = durable_db
        result = _run_worker([row], job, today)
        assert result.succeeded is True
        (outcome,) = result.detail["policy_outcomes"]
        assert outcome["status"] == "pending"
        assert outcome["waiting_on"] == "mortgagee"
        assert "upload_lender_document_intent_recorded" in outcome["actions_taken"]
        # payment check scheduled in durable_work_items
        conn = sqlite3.connect(durable_db)
        try:
            db_row = conn.execute(
                "SELECT outcome FROM durable_work_items WHERE namespace=? AND work_item_key=?",
                (DURABLE_NAMESPACE, f"{DURABLE_NAMESPACE}:LN-000111"),
            ).fetchone()
        finally:
            conn.close()
        state = json.loads(db_row[0])
        assert state["producer_review_complete"] is True
        assert state["next_payment_check"] == (today + timedelta(days=7)).isoformat()

    def test_unauthorized_delivery_becomes_pending_intent(self, row, job, today):
        # no authorization -> intent recorded, nothing executed
        result = _run_worker([row], job, today)
        (outcome,) = result.detail["policy_outcomes"]
        assert outcome["status"] == "pending"
        assert outcome["waiting_on"] == "authorization"
        assert outcome["evidence"]["intended_action"] == "upload_lender_document"
        assert "upload_lender_document" in outcome["reason"]

    def test_producer_gate_blocks_lender_delivery(self, row, job, today):
        row.pop("producer_review_complete")
        result = _run_worker([row], job, today)
        (outcome,) = result.detail["policy_outcomes"]
        assert outcome["status"] == "pending"
        assert outcome["waiting_on"] == "producer"
        assert "producer_review_requested" in outcome["actions_taken"]
        # nothing sent to any lender
        assert not any("upload_lender_document" in a for a in outcome["actions_taken"])
        assert not any("send_lender_email" in a for a in outcome["actions_taken"])

    def test_not_yet_in_window_waits(self, row, job, today):
        row["expiration_date"] = (today + timedelta(days=60)).isoformat()
        result = _run_worker([row], job, today)
        (outcome,) = result.detail["policy_outcomes"]
        assert outcome["status"] == "pending"
        assert outcome["waiting_on"] == "schedule"

    def test_overdue_goes_to_csr(self, row, job, today):
        row["expiration_date"] = (today + timedelta(days=20)).isoformat()
        result = _run_worker([row], job, today)
        (outcome,) = result.detail["policy_outcomes"]
        assert outcome["status"] == "pending"
        assert outcome["waiting_on"] == "csr"
        assert "overdue" in outcome["reason"].lower()

    def test_unparseable_expiration_is_not_done(self, row, job, today):
        row["expiration_date"] = "not-a-date"
        result = _run_worker([row], job, today)
        (outcome,) = result.detail["policy_outcomes"]
        assert outcome["status"] == "not_done"

    def test_client_paid_handoff_to_csr(self, row, job, today):
        row["payer"] = "insured"
        result = _run_worker([row], job, today)
        (outcome,) = result.detail["policy_outcomes"]
        assert outcome["status"] == "pending"
        assert outcome["waiting_on"] == "csr"
        assert "csr_handoff_requested" in outcome["actions_taken"]

    def test_client_paid_documented_handoff_is_done(self, row, job, today):
        row["payer"] = "insured"
        job["payload"]["csr_handoff_evidence"] = {"HO 1234567": {"handoff_note_id": "N-1"}}
        result = _run_worker([row], job, today)
        (outcome,) = result.detail["policy_outcomes"]
        assert outcome["status"] == "done"
        assert outcome["evidence"]["csr_handoff"] == {"handoff_note_id": "N-1"}

    def test_escalation_at_20_days(self, row, job, today, durable_db):
        _AUTHORIZED["upload_lender_document"] = True
        row["expiration_date"] = (today + timedelta(days=20)).isoformat()
        # 20d is <30d -> overdue exception wins before payer routing
        result = _run_worker([row], job, today)
        (outcome,) = result.detail["policy_outcomes"]
        assert outcome["waiting_on"] == "csr"

    def test_lender_verification_failure_goes_to_csr(self, row, job, today):
        row["loan_number"] = ""
        result = _run_worker([row], job, today)
        (outcome,) = result.detail["policy_outcomes"]
        assert outcome["status"] == "pending"
        assert outcome["waiting_on"] == "csr"
        assert "lender verification failed" in outcome["reason"]

    def test_no_duplicate_edocs_retrieval(self, row, job, today):
        """edocs already downloaded -> reuse, never re-fetch."""
        result = _run_worker([row], job, today)
        (outcome,) = result.detail["policy_outcomes"]
        assert "edocs_reused" in outcome["actions_taken"]
        assert outcome["evidence"]["dec_source"] == "carrier_edocs"
        assert outcome["evidence"]["no_duplicate_retrieval"] is True

    def test_missing_edocs_records_retrieval_intent(self, row, job, today):
        row["edocs_downloaded"] = False
        result = _run_worker([row], job, today)
        (outcome,) = result.detail["policy_outcomes"]
        assert outcome["status"] == "pending"
        assert outcome["waiting_on"] == "carrier"
        assert outcome["evidence"]["retrieval_intent"]["documents"] == ["renewal_dec", "invoice"]

    def test_cancelled_policy_not_delivered(self, row, job, today):
        row["policy_status"] = "Cancelled"
        result = _run_worker([row], job, today)
        (outcome,) = result.detail["policy_outcomes"]
        assert outcome["status"] == "not_done"
        assert "EXCLUDED_INACTIVE_ACCOUNT" in outcome["reason"]

    def test_lob_filter_skips_other_lines(self, row, job, today):
        row["lob"] = "Commercial Auto"
        result = _run_worker([row, {**row, "lob": "Flood", "policy_number": "FL 9"}], job, today)
        assert result.destination["in_scope_count"] == 1
        assert result.destination["skipped_lob"] == 1
        (outcome,) = result.detail["policy_outcomes"]
        assert outcome["policy_number"] == "FL 9"

    def test_no_lender_portal_uses_voice_path(self, row, job, today):
        row.pop("lender_portal")
        row["mortgage_company_phone"] = "(732) 555-0100"
        result = _run_worker([row], job, today)
        (outcome,) = result.detail["policy_outcomes"]
        # voice not authorized in this test -> pending intent, never executed
        assert outcome["status"] == "pending"
        assert outcome["waiting_on"] == "authorization"
        assert outcome["evidence"]["intended_action"] == "place_mortgagee_voice_call"
        assert outcome["evidence"]["lender"]["channel"] == "mortgagee_voice_call"

    def test_voice_path_authorized(self, row, job, today, durable_db):
        _AUTHORIZED["place_mortgagee_voice_call"] = True
        row.pop("lender_portal")
        row["mortgage_company_phone"] = "(732) 555-0100"
        job["payload"]["jobs_db_path"] = durable_db
        result = _run_worker([row], job, today)
        (outcome,) = result.detail["policy_outcomes"]
        assert outcome["status"] == "pending"
        assert outcome["waiting_on"] == "mortgagee"
        intent = outcome["evidence"]["voice_intent"]
        assert intent["to"] == "+17325550100"
        assert intent["to_name"] == "Test Mortgage Co"
        assert intent["never_dial_borrower"] is True
        assert "confirming mortgagee payment for renewal term" in outcome["reason"]
        assert "mortgagee_voice_call_intent_recorded" in outcome["actions_taken"]

    def test_fetch_failure_is_retryable(self, job):
        import robie_job_engine.mortgagee_verification_worker as w
        real = w.fetch_report_rows
        w.fetch_report_rows = lambda **kw: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            result = MortgageeVerificationWorker().perform(dict(job), idempotency_key="k")
        finally:
            w.fetch_report_rows = real
        assert result.succeeded is False
        assert result.retryable is True

# ---------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------

def _action_for(outcomes):
    return {
        "action": "mortgagee_verification",
        "destination": {"report_id": "4372"},
        "detail": {"policy_outcomes": outcomes},
    }

def _seed_durable(db_path, key, state):
    from robie_job_engine.mortgagee_verification_worker import _merge_policy_state, _ledger_for_job
    ledger = _ledger_for_job({"payload": {"jobs_db_path": db_path}})
    _merge_policy_state(ledger, key, state)

class TestVerifier:
    def test_verifies_clean_outcomes(self, job, durable_db):
        outcomes = [{
            "policy_number": "HO 1", "status": "pending", "reason": "r",
            "actions_taken": [], "waiting_on": "mortgagee",
            "evidence": {}, "updated_at": "2026-09-10T00:00:00Z",
        }]
        job["payload"]["jobs_db_path"] = durable_db
        result = MortgageeVerificationVerifier().verify(job, _action_for(outcomes))
        assert result.verified is True, result.error

    def test_catches_delivery_without_producer_clearance(self, job, durable_db):
        """Producer gate block: verifier flags a lender delivery with no
        producer_review_complete in durable_work_items."""
        outcomes = [{
            "policy_number": "HO 2", "status": "done", "reason": "delivered",
            "actions_taken": ["upload_lender_document_intent_recorded"],
            "waiting_on": None,
            "evidence": {"upload_intent": {"action": "upload_lender_document"},
                         "lender": {"channel": "lender_portal"},
                         "payment": {"confirmed": False}},
            "updated_at": "2026-09-10T00:00:00Z",
        }]
        job["payload"]["jobs_db_path"] = durable_db
        result = MortgageeVerificationVerifier().verify(job, _action_for(outcomes))
        assert result.verified is False
        assert "producer_review_complete" in (result.error or "")

    def test_allows_delivery_with_recorded_clearance(self, job, durable_db):
        _seed_durable(durable_db, f"{DURABLE_NAMESPACE}:HO 3",
                      {"producer_review_complete": True})
        outcomes = [{
            "policy_number": "HO 3", "status": "done", "reason": "delivered",
            "actions_taken": ["upload_lender_document_intent_recorded"],
            "waiting_on": None,
            "evidence": {"upload_intent": {"action": "upload_lender_document"},
                         "lender": {"channel": "lender_portal"},
                         "payment": {"confirmed": False}},
            "updated_at": "2026-09-10T00:00:00Z",
        }]
        job["payload"]["jobs_db_path"] = durable_db
        result = MortgageeVerificationVerifier().verify(job, _action_for(outcomes))
        assert result.verified is True, result.error

    def test_done_without_evidence_fails(self, job, durable_db):
        outcomes = [{
            "policy_number": "HO 4", "status": "done", "reason": "r",
            "actions_taken": [], "waiting_on": None,
            "evidence": {}, "updated_at": "2026-09-10T00:00:00Z",
        }]
        job["payload"]["jobs_db_path"] = durable_db
        result = MortgageeVerificationVerifier().verify(job, _action_for(outcomes))
        assert result.verified is False
        assert "evidence" in (result.error or "")

    def test_pending_without_waiting_on_fails(self, job, durable_db):
        outcomes = [{
            "policy_number": "HO 5", "status": "pending", "reason": "r",
            "actions_taken": [], "waiting_on": None,
            "evidence": {}, "updated_at": "2026-09-10T00:00:00Z",
        }]
        job["payload"]["jobs_db_path"] = durable_db
        result = MortgageeVerificationVerifier().verify(job, _action_for(outcomes))
        assert result.verified is False

    def test_invalid_status_fails(self, job, durable_db):
        outcomes = [{
            "policy_number": "HO 6", "status": "almost", "reason": "r",
            "actions_taken": [], "waiting_on": None,
            "evidence": {}, "updated_at": "2026-09-10T00:00:00Z",
        }]
        job["payload"]["jobs_db_path"] = durable_db
        result = MortgageeVerificationVerifier().verify(job, _action_for(outcomes))
        assert result.verified is False

# ---------------------------------------------------------------------------
# Carlo refinements: lender of record, no-login portals, voice, login gaps
# ---------------------------------------------------------------------------

class TestVerifyLenderOfRecord:
    def test_match(self):
        ok, reason = verify_lender_of_record(
            "Test Mortgage Co", "LN-000111", "08527",
            {"servicer": "Test Mortgage Co", "loan_number": "LN-000111"})
        assert ok is True
        assert "confirmed" in reason

    def test_match_ignores_suffix_noise(self):
        ok, _ = verify_lender_of_record(
            "Test Mortgage Co", "LN-000111", "08527",
            {"servicer": "Test Mortgage Servicing LLC", "loan_number": "LN-000111"})
        assert ok is True

    def test_mismatch_servicer_fails_closed(self):
        ok, reason = verify_lender_of_record(
            "Test Mortgage Co", "LN-000111", "08527",
            {"servicer": "Different Servicing Corp", "loan_number": "LN-000111"})
        assert ok is False
        assert "mismatch" in reason
        assert "not uploading to wrong lender" in reason

    def test_mismatch_loan_number_fails_closed(self):
        ok, reason = verify_lender_of_record(
            "Test Mortgage Co", "LN-000111", "08527",
            {"servicer": "Test Mortgage Co", "loan_number": "LN-999999"})
        assert ok is False
        assert "not uploading to wrong lender" in reason

    def test_no_lookup_result_fails_closed(self):
        ok, reason = verify_lender_of_record(
            "Test Mortgage Co", "LN-000111", "08527", None)
        assert ok is False
        assert "not yet verified" in reason

    def test_empty_portal_servicer_fails_closed(self):
        ok, _ = verify_lender_of_record(
            "Test Mortgage Co", "LN-000111", "08527", {"servicer": ""})
        assert ok is False

class TestMortgageeVoiceIntent:
    def test_builds_intent(self):
        intent, reason = build_mortgagee_voice_intent({
            "policy_number": "HO 1", "loan_number": "LN-1",
            "mortgage_company": "Test Mortgage Co",
            "mortgage_company_phone": "(732) 555-0100",
            "insured_name": "Test Homeowner", "property_zip": "08527",
            "carrier": "Carrier",
        })
        assert intent is not None
        assert intent["to"] == "+17325550100"
        assert intent["provider"] == "bland"
        assert intent["never_dial_borrower"] is True

    def test_no_phone_blocks_call(self):
        intent, reason = build_mortgagee_voice_intent({"mortgage_company": "Test Mortgage Co"})
        assert intent is None
        assert "never inventing" in reason

    def test_borrower_number_refused(self):
        intent, reason = build_mortgagee_voice_intent({
            "mortgage_company": "Test Mortgage Co",
            "mortgage_company_phone": "732-555-0199",
            "borrower_phone": "(732) 555-0199",
        })
        assert intent is None
        assert "never dial clients" in reason

    def test_invalid_phone_blocked(self):
        intent, reason = build_mortgagee_voice_intent({
            "mortgage_company": "Test Mortgage Co",
            "mortgage_company_phone": "123",
        })
        assert intent is None
        assert "not a valid US number" in reason

class TestWorkerRefinements:
    def test_no_login_default(self):
        assert LENDER_PORTAL_NEEDS_LOGIN_DEFAULT is False

    def test_portal_delivery_evidence_has_no_login(self, row, job, today):
        _AUTHORIZED["upload_lender_document"] = True
        result = _run_worker([row], job, today)
        (outcome,) = result.detail["policy_outcomes"]
        lender = outcome["evidence"]["lender"]
        assert lender["portal_login_required"] is False
        assert lender["no_ssn"] is True
        assert outcome["evidence"]["upload_intent"]["no_ssn"] is True

    def test_lender_of_record_mismatch_blocks_upload(self, row, job, today):
        _AUTHORIZED["upload_lender_document"] = True
        row["portal_lender_lookup"] = {"servicer": "Different Servicing Corp",
                                       "loan_number": "LN-000111"}
        result = _run_worker([row], job, today)
        (outcome,) = result.detail["policy_outcomes"]
        assert outcome["status"] == "pending"
        assert outcome["waiting_on"] == "mortgagee"
        assert "not uploading to wrong lender" in outcome["reason"]
        assert "lender_of_record_mismatch" in outcome["actions_taken"]
        assert "csr_review_flagged" in outcome["actions_taken"]
        assert "upload_intent" not in outcome["evidence"]

    def test_missing_lookup_records_lookup_intent(self, row, job, today):
        row.pop("portal_lender_lookup")
        result = _run_worker([row], job, today)
        (outcome,) = result.detail["policy_outcomes"]
        assert outcome["status"] == "pending"
        assert outcome["waiting_on"] == "mortgagee"
        assert "lender of record" in outcome["reason"]
        lookup = outcome["evidence"]["lender_lookup_intent"]
        assert lookup["portal_login_required"] is False
        assert lookup["no_ssn"] is True
        assert "upload_intent" not in outcome["evidence"]

    def test_portal_demands_login_records_gap(self, row, job, today, durable_db):
        _AUTHORIZED["upload_lender_document"] = True
        row["portal_demands_login"] = True
        job["payload"]["jobs_db_path"] = durable_db
        result = _run_worker([row], job, today)
        (outcome,) = result.detail["policy_outcomes"]
        assert outcome["status"] == "pending"
        assert outcome["waiting_on"] == "mortgagee"
        assert "login gap recorded" in outcome["reason"]
        assert "login_gap_recorded" in outcome["actions_taken"]
        gap = outcome["evidence"]["login_gap"]
        assert gap["job_type"] == "mortgagee_verification"
        assert gap["step"] == "lender_portal_agent_section"
        assert "upload_intent" not in outcome["evidence"]

    def test_voice_never_dials_borrower(self, row, job, today):
        _AUTHORIZED["place_mortgagee_voice_call"] = True
        row.pop("lender_portal")
        row["mortgage_company_phone"] = "732-555-0199"
        row["borrower_phone"] = "(732) 555-0199"
        result = _run_worker([row], job, today)
        (outcome,) = result.detail["policy_outcomes"]
        assert outcome["status"] == "pending"
        assert "voice_intent_blocked" in outcome["actions_taken"]
        assert "voice_intent" not in outcome["evidence"]

    def test_voice_no_phone_on_file(self, row, job, today):
        _AUTHORIZED["place_mortgagee_voice_call"] = True
        row.pop("lender_portal")
        result = _run_worker([row], job, today)
        (outcome,) = result.detail["policy_outcomes"]
        assert "voice_intent_blocked" in outcome["actions_taken"]
        assert "never inventing" in outcome["reason"]

    def test_voice_enabled_defaults_true(self, row, job, today):
        row.pop("lender_portal")
        row["mortgage_company_phone"] = "(732) 555-0100"
        # voice_enabled not set in payload -> defaults True, but unauthorized
        result = _run_worker([row], job, today)
        (outcome,) = result.detail["policy_outcomes"]
        assert "voice_enabled=True" in outcome["reason"]

    def test_payment_purpose_explicit_in_reasons(self, row, job, today, durable_db):
        _AUTHORIZED["upload_lender_document"] = True
        job["payload"]["jobs_db_path"] = durable_db
        result = _run_worker([row], job, today)
        (outcome,) = result.detail["policy_outcomes"]
        assert "confirming mortgagee payment for renewal term" in outcome["reason"]
        assert outcome["evidence"]["payment"]["purpose"] == \
            "confirming mortgagee payment for renewal term"
