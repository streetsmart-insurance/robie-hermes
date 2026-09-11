"""Unit tests for the manual_renewal_verification worker.

Fakes only: no live browser, network, credentials, or voice. The sibling
modules (report_fetcher, cancellation_gate, verification_mailer,
carrier_directory.route_carrier) are not delivered yet, so the worker's
fail-closed fallbacks apply; tests rebind the module attributes directly
with fakes (restored after each test) instead of touching sys.modules, so
other test modules are unaffected. Durable state uses a temporary sqlite
file per test.
"""

from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from robie_job_engine import manual_renewal_worker as mrnw

TODAY = date(2026, 9, 10)  # a Thursday


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

FAKE_ROWS: list[dict] = []
SENT_EMAILS: list[dict] = []
GATE_ACTIVE: bool = True
GATE_REASON: str = "active"
GATE_RAISE: bool = False


def fake_fetch_report_rows(*, report_id, fields=None, filters=None, db_path=None, session=None):
    assert report_id == "4247", report_id
    assert fields  # the worker must request explicit fields
    return [dict(r) for r in FAKE_ROWS]


def fake_check_policy_active(*args, **kwargs):
    # Sibling contract: returns (active, reason). Raising simulates the gate
    # being unavailable -> the worker must fail closed (never email).
    if GATE_RAISE:
        raise RuntimeError("simulated gate outage")
    return GATE_ACTIVE, GATE_REASON


def fake_route_carrier(carrier_name):
    return {
        "channel": "EMAIL",
        "underwriter_email": "uw.renewals@fake-carrier.example",
        "portal_url": None,
        "source": "test_fake",
    }


def fake_send_verification_email(**kwargs):
    SENT_EMAILS.append(kwargs)
    return {"id": f"fake-msg-{len(SENT_EMAILS)}", "ok": True}


def fake_is_action_authorized(job, action_name):
    # Mirror the real gate: the job payload's authorized_actions decide.
    payload = (job or {}).get("payload") or {}
    return str(action_name) in (payload.get("authorized_actions") or [])


class FakeNotePoster:
    def __init__(self):
        self.notes: list[dict] = []

    def __call__(self, **kwargs):
        self.notes.append(kwargs)
        note_text = kwargs.get("note_text", "")
        assert "ROBIE was here" in note_text, "note must end with the ROBIE signoff"
        assert note_text.rstrip().endswith("ROBIE was here")
        return {"ezlynx_note_id": f"fake-note-{len(self.notes)}", "posted": True}


class FakeVoiceDispatcher:
    def __init__(self):
        self.calls: list[dict] = []

    def dispatch_carrier_call(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "success": True,
            "mode": "LIVE_FAKE",
            "call_id": "fake-call-1",
            "call_placed": True,
            "phone": kwargs.get("phone_e164"),
            "queue_status": "queued",
            "status": "DISPATCHED",
            "policy_number": kwargs.get("policy_number"),
            "carrier": kwargs.get("carrier_name"),
        }


class FakeStore:
    """Minimal job-DB stand-in exposing get_checkpoint(job_id, name)."""

    def __init__(self, action):
        self._action = action

    def get_checkpoint(self, job_id, name):
        assert name == "action"
        return self._action


def make_durable_reader(db_path):
    def _get(namespace, key):
        conn = sqlite3.connect(db_path)
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT outcome FROM durable_work_items WHERE namespace=? AND work_item_key=?",
                (namespace, key),
            ).fetchone()
            if row and row["outcome"]:
                return {"outcome": row["outcome"]}
            return None
        finally:
            conn.close()

    return _get


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def make_row(**overrides):
    row = {
        "policy_number": "WC-100",
        "insured_name": "Acme Trucking LLC",
        "applicant_id": "164706131",
        "carrier_name": "Acme Carriers",
        "line_of_business": "Commercial Auto",
        "expiration_date": (TODAY + timedelta(days=35)).isoformat(),
        "source": "Manual",
        "expiring_premium": 12500.00,
        "underwriter_name": "Pat Underwriter",
        "underwriter_email": "pat.underwriter@fake-carrier.example",
        "assigned_agent": "Maria Bara",
        "department": "Commercial Lines",
    }
    row.update(overrides)
    return row


def make_job(db_path, **payload_overrides):
    payload = {
        "db_path": db_path,
        "reference_date": TODAY.isoformat(),
        "authorized_actions": [
            "read_report_rows",
            "check_cancellation",
            "send_underwriter_email",
            "send_followup_email",
            "post_ezlynx_note",
            "create_ezlynx_task",
            "place_carrier_voice_call",
        ],
    }
    payload.update(payload_overrides)
    return {
        "id": "job-manual-1",
        "action_type": "manual_renewal_verification",
        "payload": payload,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class ManualRenewalWorkerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "jobs.db")
        # Rebind module seams with fakes; restore afterwards.
        self._saved = {}
        for name, fake in [
            ("fetch_report_rows", fake_fetch_report_rows),
            ("check_policy_active", fake_check_policy_active),
            ("route_carrier", fake_route_carrier),
            ("send_verification_email", fake_send_verification_email),
            ("is_action_authorized", fake_is_action_authorized),
        ]:
            self._saved[name] = getattr(mrnw, name)
            setattr(mrnw, name, fake)
        global FAKE_ROWS, SENT_EMAILS
        FAKE_ROWS = []
        SENT_EMAILS = []
        global GATE_ACTIVE, GATE_REASON, GATE_RAISE
        GATE_ACTIVE, GATE_REASON, GATE_RAISE = True, "active", False
        self.notes = FakeNotePoster()
        self.voice = FakeVoiceDispatcher()

    def tearDown(self):
        for name, original in self._saved.items():
            setattr(mrnw, name, original)
        self._tmp.cleanup()

    def _worker(self):
        return mrnw.ManualRenewalWorker(
            note_poster=self.notes, voice_dispatcher=self.voice
        )

    def _run(self, job):
        return self._worker().perform(job, idempotency_key="test-key-1")

    # 1. Inactive cancellation gate blocks all outreach.
    def test_inactive_policy_blocks_all_outreach(self):
        global FAKE_ROWS, GATE_RAISE, GATE_ACTIVE, GATE_REASON
        FAKE_ROWS = [make_row()]
        GATE_RAISE = True  # gate cannot run at all -> fail closed
        result = self._run(make_job(self.db_path))
        outcome = result.detail["outcomes"][0]
        self.assertEqual(outcome["status"], "not_done")
        self.assertIn("CANCELLATION_CHECK_UNAVAILABLE", outcome["reason"])
        self.assertEqual(SENT_EMAILS, [])

        GATE_RAISE = False
        GATE_ACTIVE, GATE_REASON = False, "policy cancelled 2026-01-01"
        result = self._run(make_job(self.db_path))
        outcome = result.detail["outcomes"][0]
        self.assertEqual(outcome["status"], "not_done")
        self.assertIn("EXCLUDED_INACTIVE_ACCOUNT", outcome["reason"])
        self.assertEqual(SENT_EMAILS, [])
        self.assertEqual(self.notes.notes, [])

    # 2. Unauthorized email is recorded pending and never sent.
    def test_unauthorized_email_recorded_pending_not_sent(self):
        global FAKE_ROWS
        FAKE_ROWS = [make_row()]
        job = make_job(self.db_path, authorized_actions=["read_report_rows"])
        result = self._run(job)
        outcome = result.detail["outcomes"][0]
        self.assertEqual(outcome["status"], "pending")
        self.assertIn("EMAIL_NOT_AUTHORIZED", outcome["reason"])
        self.assertEqual(outcome["waiting_on"], "carrier")
        self.assertEqual(SENT_EMAILS, [])
        intent = outcome["evidence"]["email_intent"]
        self.assertEqual(intent["to"], "pat.underwriter@fake-carrier.example")
        self.assertIn("jake@streetsmart.insurance", intent["cc"])
        self.assertTrue(intent["subject"].startswith("[RENEWAL-REQ-"))

    # 3. Follow-up cadence increments correctly across runs.
    def test_followup_cadence_increments(self):
        global FAKE_ROWS
        FAKE_ROWS = [make_row()]
        job = make_job(self.db_path)
        run1 = self._run(job)
        first = run1.detail["outcomes"][0]
        self.assertEqual(first["status"], "pending")
        self.assertIn("initial outreach", first["reason"])
        self.assertEqual(len(SENT_EMAILS), 1)
        self.assertFalse(SENT_EMAILS[0]["subject"].startswith("Re:"))
        self.assertEqual(first["next_followup_due"], "2026-09-15")  # Thu + 5d, no weekend shift
        self.assertEqual(first["evidence"]["email"]["email_message_id"], "fake-msg-1")

        # Second run on the due date sends follow-up #1 and re-arms the cadence.
        job2 = make_job(self.db_path, reference_date="2026-09-15")
        run2 = self._run(job2)
        second = run2.detail["outcomes"][0]
        self.assertEqual(second["followup_count"], 1)
        self.assertEqual(len(SENT_EMAILS), 2)
        self.assertTrue(SENT_EMAILS[1]["subject"].startswith("Re: [RENEWAL-REQ-"))
        # 2026-09-15 + 5d = Sunday 2026-09-20 -> pushed to Monday 2026-09-21.
        self.assertEqual(second["next_followup_due"], "2026-09-21")

    # 4. Voice once-only guard prevents a second dispatch.
    def test_voice_guard_prevents_second_dispatch(self):
        global FAKE_ROWS
        FAKE_ROWS = [make_row(carrier_name="Acme Carriers")]
        job = make_job(
            self.db_path,
            carrier_phone_directory={"Acme Carriers": "+15551234567"},
        )
        # Seed: initial + 2 quiet follow-ups already done, voice not attempted.
        mrnw._mirror_outcomes_local(
            [
                {
                    "policy_number": "WC-100",
                    "status": "pending",
                    "reason": "follow-up #2 email sent - awaiting underwriter reply",
                    "followup_count": 2,
                    "initial_sent": True,
                    "next_followup_due": TODAY.isoformat(),
                    "carrier_voice_attempted": False,
                    "updated_at": mrnw._utcnow(),
                }
            ],
            db_path=self.db_path,
            namespace=mrnw.NAMESPACE,
        )
        run1 = self._run(job)
        outcome = run1.detail["outcomes"][0]
        self.assertEqual(outcome["evidence"]["voice_branch"]["outcome"], "dispatched")
        self.assertEqual(len(self.voice.calls), 1)
        call = self.voice.calls[0]
        self.assertEqual(call["phone_e164"], "+15551234567")
        self.assertTrue(outcome["carrier_voice_attempted"])
        self.assertTrue(outcome["evidence"]["voice_call"]["phone_from_directory"])

        # Second run must not dispatch again.
        run2 = self._run(job)
        outcome2 = run2.detail["outcomes"][0]
        self.assertEqual(outcome2["evidence"]["voice_branch"]["outcome"], "already_attempted")
        self.assertEqual(len(self.voice.calls), 1)

    # 4b. Voice never dials a client number, even if it matches the directory.
    def test_voice_never_dials_client_number(self):
        phone, refusal = mrnw.resolve_carrier_voice_target(
            "Acme Carriers",
            {"insured_phone": "+15551234567"},
            {"Acme Carriers": "+15551234567"},
        )
        self.assertIsNone(phone)
        self.assertIn("refused", refusal)

        phone, refusal = mrnw.resolve_carrier_voice_target(
            "Unknown Carrier XYZ", {}, {}
        )
        self.assertIsNone(phone)
        self.assertIn("never invented", refusal)

    # 5. Alias matching accepts prior/renewal-term policy numbers.
    def test_alias_matching_accepts_renewal_term_numbers(self):
        global FAKE_ROWS
        FAKE_ROWS = [
            make_row(policy_number="WC-100-R"),
            make_row(policy_number="UNRELATED-1"),
        ]
        job = make_job(
            self.db_path,
            policies=[{"policy_number": "WC-100", "policy_aliases": ["WC-100", "WC-100-R"]}],
        )
        result = self._run(job)
        self.assertEqual(len(result.detail["outcomes"]), 1)
        self.assertEqual(result.detail["outcomes"][0]["policy_number"], "WC-100-R")
        # Folding is exact: dashes/spaces/case ignored, but a renewal-term
        # suffix is a different number unless listed as an explicit alias.
        self.assertTrue(mrnw.numbers_equivalent("WC-100", "wc 100"))
        self.assertTrue(mrnw.numbers_equivalent("WC-100-R", "WC-100-R"))
        self.assertFalse(mrnw.numbers_equivalent("WC-100", "WC-100-R"))
        self.assertFalse(mrnw.numbers_equivalent("WC-100", "WC-200"))

    # 6. Verifier rejects malformed outcomes with specific failures.
    def test_verifier_rejects_malformed_outcomes(self):
        action = {
            "detail": {
                "report_id": "4247",
                "outcomes": [
                    {
                        "policy_number": "P1",
                        "status": "bogus",
                        "reason": "",
                        "updated_at": "not-a-date",
                        "evidence": {},
                    },
                    {
                        "policy_number": "P2",
                        "status": "done",
                        "reason": "terms filed",
                        "updated_at": mrnw._utcnow(),
                        "evidence": {},
                    },
                    {
                        "status": "pending",
                        "reason": "waiting",
                        "updated_at": mrnw._utcnow(),
                        "evidence": {},
                    },
                ],
            }
        }
        job = {"id": "job-manual-1", "action_type": "manual_renewal_verification"}
        verifier = mrnw.ManualRenewalVerifier(
            store=FakeStore(action), durable=make_durable_reader(self.db_path)
        )
        result = verifier.verify(job, {})
        self.assertFalse(result.verified)
        error = result.error or ""
        self.assertIn("invalid status 'bogus'", error)
        self.assertIn("missing reason", error)
        self.assertIn("invalid updated_at", error)
        self.assertIn("done but carries no evidence ref", error)
        self.assertIn("missing policy_number", error)
        self.assertIn("missing durable_work_items mirror", error)
        self.assertEqual(result.evidence.locator, "4247/manual_renewal_verification")
        self.assertEqual(result.evidence.expected["report_id"], "4247")

    # 7. Verifier accepts a well-formed worker run (positive control).
    def test_verifier_accepts_well_formed_run(self):
        global FAKE_ROWS
        FAKE_ROWS = [make_row()]
        job = make_job(self.db_path)
        result = self._run(job)
        self.assertTrue(result.succeeded)
        action = {"detail": result.detail}
        verifier = mrnw.ManualRenewalVerifier(
            store=FakeStore(action), durable=make_durable_reader(self.db_path)
        )
        verification = verifier.verify(job, {})
        self.assertTrue(verification.verified, verification.error)
        self.assertTrue(verification.evidence.authoritative)
        self.assertEqual(verification.evidence.method, "FRESH_CHECKPOINT_AND_DURABLE_READBACK")

    # 8. Escalation band: <=25 days with no terms -> ESCALATED_MANUAL.
    def test_escalation_band_pending_csr(self):
        global FAKE_ROWS
        FAKE_ROWS = [make_row(expiration_date=(TODAY + timedelta(days=20)).isoformat())]
        result = self._run(make_job(self.db_path))
        outcome = result.detail["outcomes"][0]
        self.assertEqual(outcome["status"], "pending")
        self.assertIn("ESCALATED_MANUAL", outcome["reason"])
        self.assertEqual(outcome["waiting_on"], "csr")
        self.assertEqual(SENT_EMAILS, [])
        task = outcome["evidence"]["urgent_csr_task"]
        self.assertTrue(task["task_intent_recorded"])

    # 9. PORTAL channel records a bounded stub attempt + login gap, never a live pull.
    def test_portal_channel_is_bounded_stub(self):
        global FAKE_ROWS
        FAKE_ROWS = [make_row(carrier_name="Coterie")]
        real_route = mrnw.route_carrier
        try:
            mrnw.route_carrier = None  # force the builtin fallback table: Coterie -> PORTAL
            result = self._run(make_job(self.db_path))
        finally:
            mrnw.route_carrier = real_route
        outcome = result.detail["outcomes"][0]
        self.assertEqual(outcome["status"], "pending")
        self.assertEqual(outcome["waiting_on"], "carrier")
        self.assertIn("portal_attempted", outcome["actions_taken"])
        attempt = outcome["evidence"]["portal_attempt"]
        self.assertTrue(attempt["portal_attempt_stub"])
        self.assertIn("login_gap", attempt)

    # 10. Dry-run is a library-level stop across every outward-action branch.
    def test_dry_run_blocks_every_external_action_branch(self):
        global FAKE_ROWS

        # Email intent: even a fully authorized job cannot send.
        FAKE_ROWS = [make_row()]
        email_result = self._run(make_job(self.db_path, dry_run=True))
        self.assertEqual(SENT_EMAILS, [])
        self.assertEqual(self.notes.notes, [])
        self.assertIn("DRY_RUN", email_result.detail["outcomes"][0]["reason"])

        # Escalation: neither its note nor task may be created.
        FAKE_ROWS = [make_row(expiration_date=(TODAY + timedelta(days=20)).isoformat())]
        escalation = self._run(make_job(self.db_path, dry_run=True))
        escalated = escalation.detail["outcomes"][0]
        self.assertEqual(self.notes.notes, [])
        self.assertFalse(escalated["evidence"]["escalation_note"]["posted"])
        self.assertIn("dry_run", escalated["evidence"]["escalation_note"]["reason"])
        self.assertFalse(escalated["evidence"]["urgent_csr_task"]["created"])
        self.assertIn("dry_run", escalated["evidence"]["urgent_csr_task"]["reason"])

        # Portal stub may record internal job evidence, but no EZLynx note.
        FAKE_ROWS = [make_row(carrier_name="Coterie")]
        real_route = mrnw.route_carrier
        try:
            mrnw.route_carrier = None
            portal = self._run(make_job(self.db_path, dry_run=True))
        finally:
            mrnw.route_carrier = real_route
        self.assertEqual(self.notes.notes, [])
        self.assertFalse(portal.detail["outcomes"][0]["evidence"]["portal_note"]["posted"])

        # Voice dispatcher is never invoked, even if an injected port ignores dry_run.
        mrnw._mirror_outcomes_local(
            [{
                "policy_number": "WC-100",
                "status": "pending",
                "reason": "follow-up #2 email sent - awaiting underwriter reply",
                "followup_count": 2,
                "initial_sent": True,
                "next_followup_due": TODAY.isoformat(),
                "carrier_voice_attempted": False,
                "updated_at": mrnw._utcnow(),
            }],
            db_path=self.db_path,
            namespace=mrnw.NAMESPACE,
        )
        FAKE_ROWS = [make_row(carrier_name="Acme Carriers")]
        voice = self._run(
            make_job(
                self.db_path,
                dry_run=True,
                voice_enabled=True,
                carrier_phone_directory={"Acme Carriers": "+15551234567"},
            )
        )
        branch = voice.detail["outcomes"][0]["evidence"]["voice_branch"]
        self.assertEqual(branch["outcome"], "deferred")
        self.assertIn("dry_run", branch["reason"])
        self.assertEqual(self.voice.calls, [])

        # The real authorization gate also fails closed for external actions.
        real_gate = self._saved["is_action_authorized"]
        dry_job = make_job(self.db_path, dry_run=True)
        for action in mrnw.MANUAL_RENEWAL_EXTERNAL_ACTIONS:
            self.assertFalse(real_gate(dry_job, action), action)


if __name__ == "__main__":
    unittest.main()
