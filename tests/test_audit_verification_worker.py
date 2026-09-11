"""Unit tests for the audit_verification worker.

Fakes only: no live browser, network, credentials, or voice. Sibling modules
(report_fetcher, verification_common, verification_mailer) are stubbed in
sys.modules BEFORE the worker module is imported, using the exact import
names the worker uses.
"""

from __future__ import annotations

import json
import re
import sys
import types
import unittest
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

PKG = "robie_job_engine"

# ---------------------------------------------------------------------------
# Fake sibling modules (installed before the worker import)
# ---------------------------------------------------------------------------

FAKE_ROWS: list[dict] = []
SENT_EMAILS: list[dict] = []
RECORDED: list[tuple] = []
LOGIN_GAPS: list[tuple] = []

from _sibling_fakes import install_fake, teardown_fakes

_INSTALLED: dict = {}  # module name -> (original sys.modules entry, installed fake)

def _install_fake_siblings() -> None:
    fetcher = types.ModuleType(f"{PKG}.report_fetcher")

    def fetch_report_rows(*, report_id, fields=None, filters=None, db_path=None, session=None):
        assert report_id == "4246", report_id
        return [dict(r) for r in FAKE_ROWS]

    fetcher.fetch_report_rows = fetch_report_rows

    common = types.ModuleType(f"{PKG}.verification_common")

    @dataclass
    class PolicyOutcome:
        policy_number: str = ""
        audit_id: str = ""
        insured_name: str = ""
        carrier: str = ""
        department: str = ""
        applicant_id: object = None
        policy_aliases: tuple = ()
        status: str = ""
        reason: str = ""
        actions_taken: tuple = ()
        waiting_on: str = ""
        evidence: dict = field(default_factory=dict)
        updated_at: str = ""

    def record_outcomes(store, job_id, job_type, outcomes):
        RECORDED.append((store, job_id, job_type, list(outcomes)))
        return list(outcomes)

    def is_action_authorized(job, action_name):
        payload = job.get("payload") or {}
        return action_name in payload.get("authorized_actions", [])

    def record_login_gap(store, job_id, job_type, portal_name, step, whats_missing):
        LOGIN_GAPS.append((store, job_id, job_type, portal_name, step, whats_missing))
        return {"logged": True}

    common.PolicyOutcome = PolicyOutcome
    common.record_outcomes = record_outcomes
    common.is_action_authorized = is_action_authorized
    common.record_login_gap = record_login_gap

    mailer = types.ModuleType(f"{PKG}.verification_mailer")

    def send_verification_email(*, to, cc=(), subject, text_body, html_body=None):
        SENT_EMAILS.append(
            {"to": list(to), "subject": subject, "body": text_body,
             "cc": list(cc)}
        )
        return {"sent": True, "message_id": f"fake-msg-{len(SENT_EMAILS)}", "to": list(to)}

    mailer.send_verification_email = send_verification_email

    for _name, _fake in (
        (f"{PKG}.report_fetcher", fetcher),
        (f"{PKG}.verification_common", common),
        (f"{PKG}.verification_mailer", mailer),
    ):
        install_fake(_INSTALLED, _name, _fake)

def teardown_module(module):
    """Restore faked sys.modules entries (see _sibling_fakes)."""
    teardown_fakes(_INSTALLED)

_install_fake_siblings()

from robie_job_engine import audit_verification_worker as avw  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TODAY = date(2026, 9, 10)

def make_row(**overrides):
    row = {
        "audit_id": "AUD-1",
        "policy_number": "WC PI 2695561-001",
        "insured_name": "Sun Volt Energy LLC",
        "carrier": "The Hartford",
        "line_of_business": "Workers Compensation",
        "department": "Commercial Lines",
        "applicant_id": "164706131",
        "renewal_effective_date": (TODAY - timedelta(days=35)).isoformat(),
        "audit_status": "open",
        "carrier_email": "audits@example.com",
        "agency_code": "X4688",
        "fein": "12-3456789",
    }
    row.update(overrides)
    return row

def make_job(**payload_overrides):
    payload = {"as_of_date": TODAY.isoformat(), "authorized_actions": ["send_carrier_email"]}
    payload.update(payload_overrides)
    return {"id": "job-1", "action_type": "audit_verification", "payload": payload}

def make_voice_job(**payload_overrides):
    """Job authorizing carrier voice calls (plus email)."""
    return make_job(authorized_actions=["send_carrier_email", "place_carrier_voice_call"],
                    **payload_overrides)

def run_worker(rows, job=None, **worker_kwargs):
    FAKE_ROWS.clear()
    FAKE_ROWS.extend(rows)
    SENT_EMAILS.clear()
    RECORDED.clear()
    LOGIN_GAPS.clear()
    # The worker needs a store for record_outcomes(); the fake record_outcomes
    # ignores it, so a stub suffices here.
    worker_kwargs.setdefault("store", object())
    worker = avw.AuditVerificationWorker(**worker_kwargs)
    result = worker.perform(job or make_job(), idempotency_key="idem-1")
    outcomes = [o for _, _, _, outs in RECORDED for o in outs]
    return worker, result, outcomes

class FakeVoicePort:
    """Stands in for the Bland-backed dispatcher."""

    def __init__(self, queue_status="ringing"):
        self.queue_status = queue_status
        self.requests: list = []

    def dispatch_voice_call(self, request):
        self.requests.append(request)
        return {
            "success": True,
            "call_id": "bland-call-123",
            "call_placed": True,
            "queue_status": self.queue_status,
            "status": "DISPATCHED",
            "retryable": False,
            "error": None,
        }

class FakeNotePoster:
    def __init__(self):
        self.posts: list = []

    def post_note(self, *, policy, note_body):
        self.posts.append({"policy": policy, "note_body": note_body})
        return {"posted": True, "note_id": "note-1"}

# ---------------------------------------------------------------------------
# Timing: post-renewal 30-45 day window
# ---------------------------------------------------------------------------

class TimingTests(unittest.TestCase):
    def test_before_window_pending_schedule(self):
        _, result, outcomes = run_worker([make_row(renewal_effective_date=(TODAY - timedelta(days=10)).isoformat())])
        self.assertTrue(result.succeeded)
        o = outcomes[0]
        self.assertEqual(o.status, "pending")
        self.assertEqual(o.waiting_on, "schedule")
        self.assertIn("10 days", o.reason)
        self.assertIn("audit not yet due", o.reason)
        self.assertEqual(SENT_EMAILS, [])

    def test_window_boundaries(self):
        for days, expected_status, expected_waiting in (
            (29, "pending", "schedule"),
            (30, "pending", "carrier"),
            (45, "pending", "carrier"),
            (46, "pending", "csr"),
        ):
            _, _, outcomes = run_worker(
                [make_row(renewal_effective_date=(TODAY - timedelta(days=days)).isoformat())]
            )
            self.assertEqual(outcomes[0].status, expected_status, f"days={days}")
            self.assertEqual(outcomes[0].waiting_on, expected_waiting, f"days={days}")

    def test_after_window_escalates_to_csr(self):
        _, _, outcomes = run_worker(
            [make_row(renewal_effective_date=(TODAY - timedelta(days=50)).isoformat())]
        )
        o = outcomes[0]
        self.assertEqual(o.status, "pending")
        self.assertEqual(o.waiting_on, "csr")
        self.assertIn("escalated", o.reason)
        self.assertIn("50 days", o.reason)

    def test_missing_renewal_date_not_done(self):
        row = make_row()
        del row["renewal_effective_date"]
        _, _, outcomes = run_worker([row])
        o = outcomes[0]
        self.assertEqual(o.status, "not_done")
        self.assertIn("renewal effective date needed", o.reason)
        self.assertEqual(o.waiting_on, "csr")
        self.assertEqual(SENT_EMAILS, [])

    def test_papers_received_done(self):
        _, _, outcomes = run_worker(
            [make_row(audit_status="complete", document_refs=["EZLYNX-DOC-1", "EZLYNX-DOC-2"])]
        )
        o = outcomes[0]
        self.assertEqual(o.status, "done")
        self.assertEqual(o.evidence["document_refs"], ["EZLYNX-DOC-1", "EZLYNX-DOC-2"])

    def test_done_requires_document_refs(self):
        # Flagged received but nothing on file: pending, never done.
        _, _, outcomes = run_worker([make_row(audit_status="complete")])
        o = outcomes[0]
        self.assertEqual(o.status, "pending")
        self.assertEqual(o.waiting_on, "carrier")
        self.assertIn("no document refs", o.reason)
        self.assertIn("papers_received_unverified", o.actions_taken)

    def test_repeated_no_contact_escalates_to_csr(self):
        _, _, outcomes = run_worker(
            [make_row(no_contact_count=2,
                      renewal_effective_date=(TODAY - timedelta(days=35)).isoformat())]
        )
        o = outcomes[0]
        self.assertEqual(o.status, "pending")
        self.assertEqual(o.waiting_on, "csr")
        self.assertIn("escalated", o.reason)
        self.assertIn("repeated no-contact", o.reason)

    def test_cancelled_policy_excluded(self):
        _, _, outcomes = run_worker([make_row(policy_status="Cancelled")])
        o = outcomes[0]
        self.assertEqual(o.status, "not_done")
        self.assertIn("EXCLUDED_INACTIVE_ACCOUNT", o.reason)
        self.assertEqual(SENT_EMAILS, [])

# ---------------------------------------------------------------------------
# Channels: portal -> email -> voice
# ---------------------------------------------------------------------------

class ChannelTests(unittest.TestCase):
    def test_email_sent_when_authorized(self):
        port = FakeVoicePort()
        _, result, outcomes = run_worker([make_row()], voice_dispatcher=port)
        self.assertTrue(result.succeeded)
        o = outcomes[0]
        self.assertEqual(o.status, "pending")
        self.assertEqual(o.waiting_on, "carrier")
        self.assertEqual(o.actions_taken[:2], ["portal_retrieval_intent", "carrier_email_sent"])
        self.assertEqual(len(SENT_EMAILS), 1)
        email = SENT_EMAILS[0]
        self.assertEqual(email["to"], ["audits@example.com"])
        self.assertIn("[AUDIT-REQ-AUD-1]", email["subject"])
        self.assertIn("WC PI 2695561-001", email["subject"])
        self.assertIn("Robie", email["body"])
        self.assertIn("StreetSmart", email["body"])
        # Fresh row: no prior contact, so the voice branch stays quiet this run.
        self.assertEqual(port.requests, [])

    def test_unauthorized_email_never_sent(self):
        job = make_job(authorized_actions=[])
        _, _, outcomes = run_worker([make_row()], job=job)
        o = outcomes[0]
        self.assertEqual(SENT_EMAILS, [])
        self.assertEqual(o.status, "pending")
        self.assertIn("carrier_email_intended_not_sent", o.actions_taken)
        self.assertIn("send_carrier_email", o.evidence["intended_action"]["action"])

    def test_missing_carrier_email_not_done(self):
        row = make_row()
        del row["carrier_email"]
        _, _, outcomes = run_worker([row])
        o = outcomes[0]
        self.assertEqual(o.status, "not_done")
        self.assertIn("carrier email needed", o.reason)

# ---------------------------------------------------------------------------
# Voice: approved, carriers only, once-only
# ---------------------------------------------------------------------------

class VoiceTests(unittest.TestCase):
    def test_voice_fires_with_directory_number(self):
        port = FakeVoicePort()
        _, _, outcomes = run_worker(
            [make_row(no_contact_count=1)], job=make_voice_job(), voice_dispatcher=port
        )
        o = outcomes[0]
        self.assertEqual(len(port.requests), 1)
        request = port.requests[0]
        # Directory number, never anything else.
        self.assertEqual(request.phone_number, "+18005551234")
        self.assertIn("Robie", request.task)
        self.assertIn("StreetSmart", request.task)
        self.assertIn("WC PI 2695561-001", request.task)
        self.assertIn("X4688", request.task)
        self.assertIn("12-3456789", request.task)
        self.assertIn("robie@streetsmart.insurance", request.task)
        self.assertTrue(o.evidence["voice"]["call_placed"])
        self.assertTrue(o.evidence["voice"]["phone_from_directory"])
        self.assertTrue(o.evidence["carrier_voice_attempted"])
        self.assertIn("carrier_voice_call_placed", o.actions_taken)

    def test_voice_once_only_guard(self):
        port = FakeVoicePort()
        worker = avw.AuditVerificationWorker(voice_dispatcher=port)
        job = make_voice_job()
        FAKE_ROWS.clear(); FAKE_ROWS.append(make_row(no_contact_count=1))
        SENT_EMAILS.clear(); RECORDED.clear()
        worker.perform(job, idempotency_key="idem-1")
        worker.perform(job, idempotency_key="idem-2")
        self.assertEqual(len(port.requests), 1)

    def test_voice_not_authorized_never_dials(self):
        port = FakeVoicePort()
        _, _, outcomes = run_worker(
            [make_row(no_contact_count=1)], voice_dispatcher=port  # no place_carrier_voice_call auth
        )
        o = outcomes[0]
        self.assertEqual(port.requests, [])
        self.assertEqual(o.status, "pending")
        self.assertIn("voice_not_authorized", o.actions_taken)
        self.assertIn("not authorized", o.evidence["voice"]["reason"])
        self.assertEqual(o.evidence["intended_action"]["action"], "place_carrier_voice_call")
        # Not marked attempted: a later authorized run may still call.
        self.assertNotIn("carrier_voice_attempted", o.evidence)

    def test_voice_unknown_number_never_dials(self):
        port = FakeVoicePort()
        _, _, outcomes = run_worker(
            [make_row(carrier="Nonexistent Mutual", no_contact_count=2)],
            voice_dispatcher=port,
        )
        o = outcomes[0]
        self.assertEqual(port.requests, [])
        self.assertEqual(o.status, "not_done")
        self.assertIn("phone number needed", o.reason)
        self.assertIn("never dialed", o.reason)

    def test_voice_never_dials_client_phone(self):
        port = FakeVoicePort()
        directory = {"acme mutual": "+15551234567"}
        row = make_row(
            carrier="Acme Mutual",
            client_phone="+1 (555) 123-4567",
            no_contact_count=1,
        )
        worker = avw.AuditVerificationWorker(directory=directory, voice_dispatcher=port)
        FAKE_ROWS.clear(); FAKE_ROWS.append(row)
        SENT_EMAILS.clear(); RECORDED.clear()
        worker.perform(make_job(), idempotency_key="idem-1")
        # Directory number matches the row's client phone -> refused, never dialed.
        self.assertEqual(port.requests, [])

    def test_voice_disabled_records_deferred(self):
        port = FakeVoicePort()
        job = make_job(voice_enabled=False)
        _, result, outcomes = run_worker(
            [make_row(no_contact_count=2)], job=job, voice_dispatcher=port
        )
        o = outcomes[0]
        self.assertEqual(port.requests, [])
        self.assertTrue(result.detail["voice_enabled"] is False)
        self.assertTrue(o.evidence["voice_deferred"])
        self.assertTrue(o.evidence["carrier_voice_attempted"])
        self.assertIn("voice_deferred", o.actions_taken)
        self.assertEqual(o.evidence["voice"]["reason"],
                         "voice deferred — portal+email only at launch")

    def test_voice_enabled_by_default(self):
        _, result, _ = run_worker([make_row()])
        self.assertTrue(result.detail["voice_enabled"])

    def test_voice_not_wired_places_no_call(self):
        _, _, outcomes = run_worker([make_row(no_contact_count=1)])
        o = outcomes[0]
        self.assertIn("voice_not_wired", o.actions_taken)
        self.assertNotIn("carrier_voice_attempted", o.evidence)
        self.assertIn("no call placed", o.evidence["voice"]["reason"])

    def test_bland_queue_discipline(self):
        # POST acceptance alone is NOT proof.
        classified = avw.classify_voice_result({"status": "success", "call_id": "x"})
        self.assertFalse(classified["call_placed"])
        self.assertEqual(classified["status"], "ACCEPTED_NOT_CONFIRMED")
        # Progress read-back counts.
        classified = avw.classify_voice_result({"queue_status": "ringing"})
        self.assertTrue(classified["call_placed"])
        # Queue errors are retryable; the phone never rang.
        classified = avw.classify_voice_result({"queue_status": "queue_error"})
        self.assertFalse(classified["call_placed"])
        self.assertTrue(classified["retryable"])

    def test_bland_payload_semantics(self):
        request = avw.build_bland_call_request(
            policy_number="WC-1",
            named_insured="Acme LLC",
            carrier_name="The Hartford",
            carrier_phone="+18005551234",
            agency_code="X4688",
            fein="12-3456789",
        )
        payload = avw.bland_post_payload(request)
        self.assertEqual(payload["phone_number"], "+18005551234")
        self.assertEqual(payload["voice"], "nat")
        self.assertEqual(payload["model"], "enhanced")
        self.assertTrue(payload["record"])
        self.assertIn("941", payload["task"])
        self.assertIn("robie@streetsmart.insurance", payload["task"])

    def test_concrete_dispatcher_needs_key(self):
        with self.assertRaises(ValueError):
            avw.BlandVoiceDispatcher("")

    def test_concrete_dispatcher_readback_discipline(self):
        request = avw.build_bland_call_request(
            policy_number="WC-1", named_insured="Acme LLC",
            carrier_name="The Hartford", carrier_phone="+18005551234",
        )
        dispatcher = avw.BlandVoiceDispatcher("fake-key")
        seen = []

        def fake_http(method, path, body=None):
            seen.append((method, path))
            if method == "POST":
                self.assertEqual(path, "/v1/calls")
                self.assertEqual(body["phone_number"], "+18005551234")
                self.assertEqual(body["voice"], "nat")
                return {"status": "success", "call_id": "call-9"}
            return {"queue_status": "ringing", "status": "in-progress"}

        dispatcher._request_json = fake_http
        result = dispatcher.dispatch_voice_call(request)
        self.assertTrue(result["call_placed"])
        self.assertEqual(result["call_id"], "call-9")
        self.assertIn(("GET", "/v1/calls/call-9"), seen)

    def test_concrete_dispatcher_post_without_readback_is_not_proof(self):
        request = avw.build_bland_call_request(
            policy_number="WC-1", named_insured="Acme LLC",
            carrier_name="The Hartford", carrier_phone="+18005551234",
        )
        dispatcher = avw.BlandVoiceDispatcher("fake-key")

        def fake_http(method, path, body=None):
            if method == "POST":
                return {"status": "success"}  # accepted, no call_id
            return {}

        dispatcher._request_json = fake_http
        result = dispatcher.dispatch_voice_call(request)
        self.assertFalse(result["call_placed"])
        self.assertTrue(result["retryable"])

    def test_concrete_dispatcher_queue_error_retryable(self):
        request = avw.build_bland_call_request(
            policy_number="WC-1", named_insured="Acme LLC",
            carrier_name="The Hartford", carrier_phone="+18005551234",
        )
        dispatcher = avw.BlandVoiceDispatcher("fake-key")

        def fake_http(method, path, body=None):
            if method == "POST":
                return {"status": "success", "call_id": "call-9"}
            return {"queue_status": "queue_error"}

        dispatcher._request_json = fake_http
        result = dispatcher.dispatch_voice_call(request)
        self.assertFalse(result["call_placed"])
        self.assertTrue(result["retryable"])

    def test_audit_script_content(self):
        script = avw.build_audit_call_script("WC-1", "Acme LLC", "X4688", "12-3456789")
        for token in ("Robie", "StreetSmart", "X4688", "WC-1", "Acme LLC",
                      "12-3456789", "robie@streetsmart.insurance", "941"):
            self.assertIn(token, script)

# ---------------------------------------------------------------------------
# Login gaps
# ---------------------------------------------------------------------------

class LoginGapTests(unittest.TestCase):
    def test_login_gap_recorded_and_pending(self):
        store = object()
        row = make_row(portal_login_gap={
            "portal_name": "Guard Portal",
            "step": "mfa",
            "whats_missing": "TOTP seed not in vault",
        })
        _, _, outcomes = run_worker([row], voice_dispatcher=FakeVoicePort(), store=store)
        o = outcomes[0]
        self.assertEqual(o.status, "pending")
        self.assertEqual(o.waiting_on, "carrier")
        self.assertIn("Guard Portal", o.reason)
        self.assertIn("TOTP seed not in vault", o.reason)
        self.assertIn("login_gap_logged", o.actions_taken)
        self.assertEqual(len(LOGIN_GAPS), 1)
        logged_store, job_id, job_type, portal, step, missing = LOGIN_GAPS[0]
        self.assertIs(logged_store, store)
        self.assertEqual(job_id, "job-1")
        self.assertEqual(job_type, "audit_verification")
        self.assertEqual(portal, "Guard Portal")
        self.assertEqual(step, "mfa")
        self.assertEqual(missing, "TOTP seed not in vault")

    def test_login_gap_status_row_field(self):
        row = make_row(portal_login_status="mfa_required", portal_gap_detail="Duo push to Carlo")
        _, _, outcomes = run_worker([row])
        o = outcomes[0]
        self.assertEqual(o.status, "pending")
        self.assertEqual(o.waiting_on, "carrier")
        self.assertIn("Duo push to Carlo", o.reason)

# ---------------------------------------------------------------------------
# Call artifacts -> EZLynx note
# ---------------------------------------------------------------------------

class ArtifactTests(unittest.TestCase):
    def test_note_limits_and_signoff(self):
        policy = {"policy_number": "WC-1", "line_of_business": "Workers Compensation",
                  "carrier": "The Hartford"}
        filing = avw.file_call_artifacts(
            policy, "https://example.com/rec.mp3", "x" * 20000, "Summary here."
        )
        note = filing["note_body"]
        self.assertLessEqual(len(note), 6000)
        self.assertTrue(note.endswith("ROBIE was here"))
        self.assertIn("Policy: #WC-1 (Workers Compensation - The Hartford)", note)
        self.assertFalse(filing["posted"])
        self.assertIsNotNone(filing["post_blocked_reason"])

    def test_note_posting_gated_by_authorization(self):
        policy = {"policy_number": "WC-1", "line_of_business": "Workers Compensation",
                  "carrier": "The Hartford"}
        poster = FakeNotePoster()
        # Unauthorized: built but not posted.
        filing = avw.file_call_artifacts(
            policy, "https://example.com/rec.mp3", "transcript", "summary",
            job=make_job(authorized_actions=[]), note_poster=poster,
        )
        self.assertFalse(filing["posted"])
        self.assertEqual(poster.posts, [])
        # Authorized: posted.
        filing = avw.file_call_artifacts(
            policy, "https://example.com/rec.mp3", "transcript", "summary",
            job=make_job(authorized_actions=["post_ezlynx_note"]), note_poster=poster,
        )
        self.assertTrue(filing["posted"])
        self.assertEqual(len(poster.posts), 1)
        self.assertTrue(poster.posts[0]["note_body"].endswith("ROBIE was here"))

    def test_row_artifacts_filed_when_present(self):
        poster = FakeNotePoster()
        job = make_job(authorized_actions=["send_carrier_email", "post_ezlynx_note"])
        _, _, outcomes = run_worker(
            [make_row(call_recording_url="https://example.com/r.mp3",
                      call_transcript="hello", call_summary="done")],
            job=job, note_poster=poster, voice_dispatcher=FakeVoicePort(),
        )
        self.assertIn("call_artifacts_filed", outcomes[0].actions_taken)
        self.assertTrue(outcomes[0].evidence["call_artifact_filing"]["posted"])

# ---------------------------------------------------------------------------
# Directory
# ---------------------------------------------------------------------------

class DirectoryTests(unittest.TestCase):
    def test_lookup_known_carrier(self):
        self.assertEqual(avw.lookup_carrier_phone("The Hartford"), "+18005551234")
        self.assertEqual(avw.lookup_carrier_phone("hartford"), "+18005551234")

    def test_lookup_unknown_returns_none(self):
        self.assertIsNone(avw.lookup_carrier_phone("Nonexistent Mutual"))

    def test_normalize_e164(self):
        self.assertEqual(avw.normalize_phone_e164("(800) 555-1234"), "+18005551234")
        self.assertEqual(avw.normalize_phone_e164("+18005551234"), "+18005551234")
        self.assertIsNone(avw.normalize_phone_e164(None))
        self.assertIsNone(avw.normalize_phone_e164(""))

    def test_seeded_file_exists_with_carrier_phones(self):
        path = Path(avw.__file__).resolve().parent / "data" / "audit_call_directory.json"
        self.assertTrue(path.is_file())
        data = json.loads(path.read_text())
        self.assertIn("the hartford", data["phones"])

# ---------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------

def make_action(outcomes, **detail_overrides):
    detail = {"outcomes": outcomes, "voice_enabled": True, "counts": {}}
    detail.update(detail_overrides)
    return {"action": "audit_verification", "destination": {}, "detail": detail}

def make_verify_job(**payload_overrides):
    payload = {"authorized_actions": ["send_carrier_email", "place_carrier_voice_call"]}
    payload.update(payload_overrides)
    return {"id": "job-1", "action_type": "audit_verification", "payload": payload}

def good_outcome(**overrides):
    outcome = {
        "policy_number": "WC PI 2695561-001",
        "audit_id": "AUD-1",
        "insured_name": "Sun Volt Energy LLC",
        "carrier": "The Hartford",
        "department": "Commercial Lines",
        "status": "pending",
        "reason": "final audit statement requested",
        "actions_taken": ["portal_retrieval_intent", "carrier_email_sent"],
        "waiting_on": "carrier",
        "evidence": {"portal_intent": {"channel": "portal"}},
        "updated_at": "2026-09-10T14:00:00Z",
    }
    outcome.update(overrides)
    return outcome

class FakeStore:
    def __init__(self, action):
        self._action = action
        self.path = None

    def get_checkpoint(self, job_id, kind):
        assert kind == "action"
        return self._action

def durable_for(outcomes):
    def get(namespace, key):
        assert namespace == "audit_verification"
        for o in outcomes:
            if (o.get("audit_id") or o.get("policy_number")) == key:
                return {"outcome": json.dumps(o)}
        return None
    return get

class VerifierTests(unittest.TestCase):
    def test_happy_path_verified(self):
        outcomes = [good_outcome(),
                    good_outcome(audit_id="AUD-2", status="done",
                                 evidence={"document_refs": ["DOC-1"]})]
        action = make_action(outcomes)
        verifier = avw.AuditVerificationVerifier(
            store=FakeStore(action), durable=durable_for(outcomes)
        )
        result = verifier.verify(make_verify_job(), action)
        self.assertTrue(result.verified)
        self.assertTrue(result.evidence.authoritative)
        self.assertIsNone(result.error)

    def test_catches_malformed_outcomes(self):
        outcomes = [
            good_outcome(reason=""),  # missing reason
            good_outcome(audit_id="AUD-2", status="bogus"),
            good_outcome(audit_id="AUD-3", status="done", evidence={}),
            good_outcome(audit_id="AUD-4", waiting_on="nobody"),
            good_outcome(audit_id="", policy_number=""),  # missing identity
        ]
        action = make_action(outcomes)
        verifier = avw.AuditVerificationVerifier(
            store=FakeStore(action), durable=durable_for(outcomes)
        )
        result = verifier.verify(make_verify_job(), action)
        self.assertFalse(result.verified)
        for token in ("missing reason", "invalid status", "no evidence refs",
                      "invalid waiting_on", "missing policy_number/audit identity"):
            self.assertIn(token, result.error)

    def test_catches_unauthorized_voice(self):
        outcomes = [good_outcome(evidence={
            "voice": {"call_placed": True, "queue_status": "completed",
                      "phone_from_directory": True}})]
        action = make_action(outcomes, voice_enabled=False)
        verifier = avw.AuditVerificationVerifier(
            store=FakeStore(action), durable=durable_for(outcomes)
        )
        result = verifier.verify(make_verify_job(authorized_actions=["send_carrier_email"]), action)
        self.assertFalse(result.verified)
        self.assertIn("voice_enabled was false", result.error)
        self.assertIn("place_carrier_voice_call", result.error)

    def test_catches_placed_call_without_directory_proof(self):
        outcomes = [good_outcome(evidence={
            "voice": {"call_placed": True, "queue_status": "completed"}})]
        action = make_action(outcomes, voice_enabled=True)
        verifier = avw.AuditVerificationVerifier(
            store=FakeStore(action), durable=durable_for(outcomes)
        )
        result = verifier.verify(make_verify_job(), action)
        self.assertFalse(result.verified)
        self.assertIn("carrier directory", result.error)

    def test_catches_missing_durable_mirror(self):
        outcomes = [good_outcome()]
        action = make_action(outcomes)
        verifier = avw.AuditVerificationVerifier(
            store=FakeStore(action), durable=lambda ns, key: None
        )
        result = verifier.verify(make_verify_job(), action)
        self.assertFalse(result.verified)
        self.assertIn("durable_work_items mirror", result.error)

    def test_no_store_is_not_authoritative(self):
        outcomes = [good_outcome()]
        action = make_action(outcomes)
        verifier = avw.AuditVerificationVerifier(durable=durable_for(outcomes))
        result = verifier.verify(make_verify_job(), action)
        # Well-formed, but without a store there is no fresh read-back.
        self.assertFalse(result.evidence.authoritative)

# ---------------------------------------------------------------------------
# Safety invariants
# ---------------------------------------------------------------------------

class SafetyTests(unittest.TestCase):
    def test_no_destructive_operations_in_module(self):
        source = Path(avw.__file__).read_text()
        for token in ("os.remove", "shutil.rmtree", ".delete(", "DELETE FROM",
                      "DROP TABLE", "PAWIVA", "221398001"):
            self.assertNotIn(token, source, f"forbidden token {token!r} in worker module")

    def test_no_credentials_in_module_or_data(self):
        # Look for actual credential *assignments*, not prose. The module
        # docstring legitimately discusses credential hygiene.
        assignment = re.compile(
            r"(?im)^\s*(api_key|secret|password|token|client_secret|private_key)\s*=\s*['\"][^'\"]+['\"]"
        )
        for path in (Path(avw.__file__),
                     Path(avw.__file__).resolve().parent / "data" / "audit_call_directory.json"):
            text = path.read_text()
            self.assertIsNone(assignment.search(text), f"{path.name} contains a credential assignment")

    def test_perform_rejects_wrong_action_type(self):
        worker = avw.AuditVerificationWorker()
        result = worker.perform({"action_type": "something_else", "payload": {}}, idempotency_key="x")
        self.assertFalse(result.succeeded)
        self.assertFalse(result.retryable)

    def test_db_path_resolution(self):
        worker = avw.AuditVerificationWorker()
        # 1. payload db_path
        self.assertEqual(worker._db_path({"payload": {"db_path": "/tmp/test1.db"}}), "/tmp/test1.db")
        # 2. payload jobs_db_path
        self.assertEqual(worker._db_path({"payload": {"jobs_db_path": "/tmp/test2.db"}}), "/tmp/test2.db")
        # 3. store path
        import os
        from unittest.mock import Mock, patch
        store_mock = Mock(path="/tmp/store.db")
        worker_with_store = avw.AuditVerificationWorker(store=store_mock)
        self.assertEqual(worker_with_store._db_path({"payload": {}}), "/tmp/store.db")
        # 4. env var
        with patch.dict(os.environ, {"ROBIE_JOB_DB": "/tmp/env.db"}):
            self.assertEqual(worker._db_path({"payload": {}}), "/tmp/env.db")


if __name__ == "__main__":
    unittest.main()
