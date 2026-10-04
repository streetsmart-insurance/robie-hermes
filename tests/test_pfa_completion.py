"""Mocked tests for the PFA completion guarantee (Astra Gold never-again).

No real Ascend/Gmail calls: the Ascend client, the Gmail sender, and the
alerter are all fakes.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from robie_job_engine.pfa_completion import (
    KIND_COMPLETION,
    PfaExpected,
    PfaExpectedBillable,
    PfaNotificationOutbox,
    build_expected_from_quote,
    compose_completion_email,
    compose_hitl_stuck_email,
    default_outbox_path,
    drain_outbox,
    enqueue_completion_notification,
    enqueue_hitl_stuck_notification,
    notify_stuck_requests,
    reconcile_pfa_completions,
    stuck_threshold_exceeded,
    verify_program_against_request,
)


PROGRAM_ID = "6f5cec3a-51b7-4ee1-baeb-ca812536cd93"


def make_program(**over):
    prog = {
        "id": PROGRAM_ID,
        "status": "ready_for_checkout",
        "premium_cents": 2480946,
        "program_url": f"https://checkout.useascend.com/x/overview?program_id={PROGRAM_ID}",
        "insured": {
            "business_name": "ASTRAGOLD LLC",
            "mailing_address_street_one": "822 Queen Ave NE",
            "mailing_address_city": "Renton",
            "mailing_address_state": "WA",
            "mailing_address_zip_code": "98056",
            "insured_contacts": [
                {
                    "first_name": "Dmytro",
                    "last_name": "Boiko",
                    "email": "astragold2024@gmail.com",
                    "phone": "+14257545595",
                }
            ],
        },
        "producer": {"email": "jake@streetsmart.insurance"},
    }
    prog.update(over)
    return prog


def make_billable(**over):
    b = {
        "billable_identifier": "Q-1790993394",
        "carrier": {"identifier": "fortegra_specialty_insurance_company_jacksonville_0e48fa"},
        "wholesaler": {"identifier": "diesel_insurance_solutions_inc_san_juan_capistrano_f486e2"},
        "premium_cents": 2480946,
        "taxes_and_fees_cents": 98317,
        "policy_fee_cents": 40000,
        "agency_fees_cents": 55000,
        "seller_commission_rate": 0.1,
        "seller_commission_amount_cents": 248095,
        "effective_date": "2026-10-04",
        "expiration_date": "2027-10-04",
    }
    b.update(over)
    return b


def make_expected(**over):
    exp = PfaExpected(
        insured_business_name="ASTRAGOLD LLC",
        address={
            "mailing_address_street_one": "822 Queen Ave NE",
            "mailing_address_city": "Renton",
            "mailing_address_state": "WA",
            "mailing_address_zip_code": "98056",
        },
        contact={
            "first_name": "Dmytro",
            "last_name": "Boiko",
            "email": "astragold2024@gmail.com",
            "phone": "(425) 754-5595",
        },
        billables=[
            PfaExpectedBillable(
                billable_identifier="Q-1790993394",
                carrier_identifier="fortegra_specialty_insurance_company_jacksonville_0e48fa",
                wholesaler_identifier="diesel_insurance_solutions_inc_san_juan_capistrano_f486e2",
                premium_cents=2480946,
                taxes_and_fees_cents=98317,
                policy_fee_cents=40000,
                agency_fees_cents=55000,
                commission_rate=0.10,
                effective_date="2026-10-04",
                expiration_date="2027-10-04",
            )
        ],
        requester_email="jake@streetsmart.insurance",
        requester_name="Jake",
    )
    for k, v in over.items():
        setattr(exp, k, v)
    return exp


class FakeAscendClient:
    def __init__(self, program=None, billables=None, billables_error=None):
        self._program = program
        self._billables = billables if billables is not None else []
        self._billables_error = billables_error
        self.transport = MagicMock()

        def _request(method, path, query=None):
            if path == "/billables":
                if self._billables_error:
                    raise self._billables_error
                return {"data": self._billables}
            if path == "/programs":
                return {"data": []}
            raise AssertionError(f"unexpected path {path}")

        self.transport.request.side_effect = _request

    def get_program(self, program_id):
        return self._program


class FakeSender:
    """Fake Gmail sender with controllable confirmation."""

    def __init__(self, confirm=True, fail_send=False):
        self.confirm = confirm
        self.fail_send = fail_send
        self.sent = []  # (to, subject, body, message_id)

    def send_email(self, to, subject, body):
        if self.fail_send:
            raise RuntimeError("smtp down")
        mid = f"msg-{len(self.sent)}"
        self.sent.append((to, subject, body, mid))
        return mid

    def confirm_sent(self, message_id):
        return self.confirm

    def find_sent_for_program(self, program_id, since_days=7):
        return any(program_id in body for _, _, body, _ in self.sent)


def make_outbox():
    tmp = tempfile.mkdtemp()
    return PfaNotificationOutbox(str(Path(tmp) / "outbox.db"))


class TestVerifyProgram(unittest.TestCase):
    def test_full_match_ok(self):
        client = FakeAscendClient(make_program(), [make_billable()])
        v = verify_program_against_request(client, PROGRAM_ID, make_expected())
        self.assertTrue(v.ok, v.diffs)
        self.assertEqual(v.diffs, [])
        self.assertIn("ASTRAGOLD LLC", v.summary)
        self.assertIn("$24,809.46", v.summary)

    def test_carrier_mismatch_fails_closed(self):
        client = FakeAscendClient(
            make_program(), [make_billable(carrier={"identifier": "wrong_carrier"})]
        )
        v = verify_program_against_request(client, PROGRAM_ID, make_expected())
        self.assertFalse(v.ok)
        self.assertTrue(any("carrier" in d for d in v.diffs))

    def test_wholesaler_mismatch_fails_closed(self):
        client = FakeAscendClient(
            make_program(),
            [make_billable(wholesaler={"identifier": "someone_else"})],
        )
        v = verify_program_against_request(client, PROGRAM_ID, make_expected())
        self.assertFalse(v.ok)
        self.assertTrue(any("wholesaler" in d for d in v.diffs))

    def test_premium_mismatch_fails_closed(self):
        client = FakeAscendClient(make_program(), [make_billable(premium_cents=1)])
        v = verify_program_against_request(client, PROGRAM_ID, make_expected())
        self.assertFalse(v.ok)
        self.assertTrue(any("premium_cents" in d for d in v.diffs))

    def test_status_not_ready_fails_closed(self):
        client = FakeAscendClient(make_program(status="draft"), [make_billable()])
        v = verify_program_against_request(client, PROGRAM_ID, make_expected())
        self.assertFalse(v.ok)
        self.assertTrue(any("ready_for_checkout" in d for d in v.diffs))

    def test_unreadable_program_fails_closed(self):
        client = FakeAscendClient("not-a-dict", [make_billable()])
        v = verify_program_against_request(client, PROGRAM_ID, make_expected())
        self.assertFalse(v.ok)
        self.assertTrue(any("unreadable" in d for d in v.diffs))

    def test_billables_error_fails_closed(self):
        client = FakeAscendClient(
            make_program(), billables_error=RuntimeError("404")
        )
        v = verify_program_against_request(client, PROGRAM_ID, make_expected())
        self.assertFalse(v.ok)
        self.assertTrue(any("billables" in d for d in v.diffs))

    def test_phone_formats_normalize(self):
        # Expected has (425) 754-5595; Ascend has +14257545595 -> must match.
        client = FakeAscendClient(make_program(), [make_billable()])
        v = verify_program_against_request(client, PROGRAM_ID, make_expected())
        self.assertTrue(v.ok, v.diffs)

    def test_missing_billable_fails_closed(self):
        client = FakeAscendClient(make_program(), [])
        v = verify_program_against_request(client, PROGRAM_ID, make_expected())
        self.assertFalse(v.ok)

    def test_build_expected_from_quote(self):
        quote = MagicMock()
        quote.insured_name = "ASTRAGOLD LLC"
        quote.mailing_address = {"mailing_address_city": "Renton"}
        quote.primary_contact = {"email": "a@b.c"}
        payload_billables = [
            {
                "billable_identifier": "Q-1",
                "carrier_identifier": "c1",
                "premium_cents": 100,
                "taxes_and_fees_cents": 10,
                "agency_fees_cents": 5,
                "organization_commission_rate": 0.1,
                "effective_date": "2026-10-04",
                "expiration_date": "2027-10-04",
            }
        ]
        exp = build_expected_from_quote(
            quote, payload_billables, "j@x.com", "Jake"
        )
        self.assertEqual(exp.insured_business_name, "ASTRAGOLD LLC")
        self.assertEqual(len(exp.billables), 1)
        self.assertEqual(exp.billables[0].carrier_identifier, "c1")
        self.assertIsNone(exp.billables[0].wholesaler_identifier)
        self.assertEqual(exp.billables[0].commission_rate, 0.1)


class TestOutbox(unittest.TestCase):
    def test_enqueue_is_idempotent(self):
        ob = make_outbox()
        i1 = enqueue_completion_notification(
            ob, program_id=PROGRAM_ID, program_url="http://x",
            requester_email="j@x.com", requester_name="Jake",
            subject="s", body="b", verification_summary="v",
        )
        i2 = enqueue_completion_notification(
            ob, program_id=PROGRAM_ID, program_url="http://x",
            requester_email="j@x.com", requester_name="Jake",
            subject="s", body="b", verification_summary="v",
        )
        self.assertEqual(i1, i2)
        self.assertEqual(len(ob.get_pending()), 1)

    def test_drain_sends_and_confirms(self):
        ob = make_outbox()
        sender = FakeSender(confirm=True)
        enqueue_completion_notification(
            ob, program_id=PROGRAM_ID, program_url="http://x",
            requester_email="j@x.com", requester_name="Jake",
            subject="s", body="b", verification_summary="v",
        )
        stats = drain_outbox(ob, sender)
        self.assertEqual(stats["sent"], 1)
        self.assertTrue(ob.is_notified(PROGRAM_ID))
        self.assertEqual(len(sender.sent), 1)

    def test_drain_retries_until_confirmed(self):
        ob = make_outbox()
        sender = FakeSender(confirm=False)  # never confirm
        enqueue_completion_notification(
            ob, program_id=PROGRAM_ID, program_url="http://x",
            requester_email="j@x.com", requester_name="Jake",
            subject="s", body="b", verification_summary="v",
        )
        stats = drain_outbox(ob, sender)
        self.assertEqual(stats["sent"], 0)
        self.assertFalse(ob.is_notified(PROGRAM_ID))
        # Still pending -> a later drain retries (no silent completion).
        self.assertEqual(len(ob.get_pending()), 1)

    def test_drain_marks_failed_after_max_attempts(self):
        ob = make_outbox()
        sender = FakeSender(fail_send=True)
        enqueue_completion_notification(
            ob, program_id=PROGRAM_ID, program_url="http://x",
            requester_email="j@x.com", requester_name="Jake",
            subject="s", body="b", verification_summary="v",
        )
        for _ in range(6):
            drain_outbox(ob, sender, max_attempts=3)
        rec = ob.get_record(KIND_COMPLETION, PROGRAM_ID)
        self.assertEqual(rec["status"], "failed")
        self.assertEqual(len(ob.get_pending()), 0)

    def test_default_outbox_path_respects_env(self):
        import os
        os.environ["PFA_OUTBOX_DB"] = "/tmp/custom-outbox.db"
        try:
            self.assertEqual(default_outbox_path(), "/tmp/custom-outbox.db")
        finally:
            del os.environ["PFA_OUTBOX_DB"]


class TestReconciler(unittest.TestCase):
    def _client_with_programs(self, programs):
        client = FakeAscendClient()
        client.transport.request.side_effect = lambda m, p, query=None: (
            {"data": programs} if p == "/programs" else {"data": []}
        )
        return client

    def test_skips_already_notified(self):
        ob = make_outbox()
        sender = FakeSender()
        alerts = []
        enqueue_completion_notification(
            ob, program_id=PROGRAM_ID, program_url="http://x",
            requester_email="j@x.com", requester_name="Jake",
            subject="s", body="b", verification_summary="v",
        )
        drain_outbox(ob, sender)
        prog = make_program()
        prog["created_at"] = "2026-10-03T02:09:54.374Z"
        stats = reconcile_pfa_completions(
            self._client_with_programs([prog]), ob, sender, alerts.append,
            now=__import__("datetime").datetime(2026, 10, 3, 12, 0, tzinfo=__import__("datetime").timezone.utc),
        )
        self.assertEqual(stats["already_notified"], 1)
        self.assertEqual(alerts, [])
        self.assertEqual(len(sender.sent), 1)  # no duplicate send

    def test_refires_pending_notification_and_alerts(self):
        ob = make_outbox()
        sender = FakeSender()
        alerts = []
        enqueue_completion_notification(
            ob, program_id=PROGRAM_ID, program_url="http://x",
            requester_email="j@x.com", requester_name="Jake",
            subject="s", body="b", verification_summary="v",
        )
        # NOTE: no drain -> pending, simulating the Astra Gold silence.
        prog = make_program()
        prog["created_at"] = "2026-10-03T02:09:54.374Z"
        from datetime import datetime, timezone
        stats = reconcile_pfa_completions(
            self._client_with_programs([prog]), ob, sender, alerts.append,
            now=datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(stats["recovered"], 1)
        self.assertTrue(ob.is_notified(PROGRAM_ID))
        self.assertEqual(len(alerts), 1)
        self.assertIn("recovered", alerts[0].lower())

    def test_manual_program_alerts_without_sending_link(self):
        # Astra Gold case: created outside the workflow, no outbox record.
        ob = make_outbox()
        sender = FakeSender()
        alerts = []
        prog = make_program()
        prog["created_at"] = "2026-10-03T02:09:54.374Z"
        from datetime import datetime, timezone
        stats = reconcile_pfa_completions(
            self._client_with_programs([prog]), ob, sender, alerts.append,
            now=datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(stats["alerted_unverified"], 1)
        self.assertEqual(len(alerts), 1)
        # Fail-closed: no link emailed to the requester for an unverified program.
        self.assertEqual(len(sender.sent), 0)
        self.assertIn("never verified", alerts[0].lower())
        self.assertIn(PROGRAM_ID, alerts[0])

    def test_ignores_old_and_non_ready_programs(self):
        ob = make_outbox()
        sender = FakeSender()
        alerts = []
        old = make_program()
        old["id"] = "old-prog"
        old["created_at"] = "2026-09-01T00:00:00Z"
        draft = make_program(status="draft")
        draft["id"] = "draft-prog"
        draft["created_at"] = "2026-10-03T02:09:54.374Z"
        from datetime import datetime, timezone
        stats = reconcile_pfa_completions(
            self._client_with_programs([old, draft]), ob, sender, alerts.append,
            now=datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc),
            lookback_days=7,
        )
        self.assertEqual(stats["checked"], 0)
        self.assertEqual(alerts, [])


class TestHitlStuck(unittest.TestCase):
    def test_threshold(self):
        self.assertTrue(stuck_threshold_exceeded(
            "2026-10-01T00:00:00+00:00", "2026-10-03T00:00:00+00:00", 24.0))
        self.assertFalse(stuck_threshold_exceeded(
            "2026-10-02T12:00:00+00:00", "2026-10-03T00:00:00+00:00", 24.0))
        self.assertFalse(stuck_threshold_exceeded("bogus", "2026-10-03T00:00:00+00:00"))

    def test_compose_is_plain_english(self):
        subject, body = compose_hitl_stuck_email(
            requester_name="Jake", insured_name="ASTRAGOLD LLC",
            missing_items=["The exact carrier name as it appears in Ascend"],
            waiting_since_display="yesterday",
        )
        self.assertIn("ASTRAGOLD LLC", subject)
        self.assertIn("still waiting", body)
        self.assertIn("The exact carrier name", body)
        self.assertNotIn("HITL", body)
        self.assertNotIn("program_id", body)

    def test_notify_stuck_enqueues_and_sends_once(self):
        ob = make_outbox()
        sender = FakeSender()
        req = {
            "dedupe_key": "thread-1",
            "requester_email": "j@x.com",
            "requester_name": "Jake",
            "insured_name": "ASTRAGOLD LLC",
            "missing_items": ["carrier name"],
            "first_asked_at": "2026-10-01T00:00:00+00:00",
            "waiting_since_display": "Oct 1",
        }
        stats = notify_stuck_requests(
            ob, sender, [req], threshold_hours=24.0,
            now_iso="2026-10-03T00:00:00+00:00",
        )
        self.assertEqual(stats["notified"], 1)
        self.assertEqual(len(sender.sent), 1)
        # Second run: already notified -> no duplicate.
        stats2 = notify_stuck_requests(
            ob, sender, [req], threshold_hours=24.0,
            now_iso="2026-10-04T00:00:00+00:00",
        )
        self.assertEqual(stats2["already_notified"], 1)
        self.assertEqual(len(sender.sent), 1)

    def test_not_yet_due_is_silent(self):
        ob = make_outbox()
        sender = FakeSender()
        req = {
            "dedupe_key": "thread-2",
            "requester_email": "j@x.com",
            "requester_name": "Jake",
            "insured_name": "ACME",
            "missing_items": ["x"],
            "first_asked_at": "2026-10-03T00:00:00+00:00",
            "waiting_since_display": "today",
        }
        stats = notify_stuck_requests(
            ob, sender, [req], threshold_hours=24.0,
            now_iso="2026-10-03T01:00:00+00:00",
        )
        self.assertEqual(stats["not_yet_due"], 1)
        self.assertEqual(len(sender.sent), 0)


class TestComposeCompletion(unittest.TestCase):
    def test_plain_english_with_link(self):
        subject, body = compose_completion_email(
            requester_name="Jake", insured_name="ASTRAGOLD LLC",
            carrier_name="Fortegra Specialty Insurance Company",
            wholesaler_name="Diesel Insurance Solutions Inc.",
            premium_cents=2480946,
            program_url="https://checkout.useascend.com/x?program_id=abc",
            verification_summary="Verified against your request:",
        )
        self.assertIn("ASTRAGOLD LLC", subject)
        self.assertIn("https://checkout.useascend.com/x?program_id=abc", body)
        self.assertIn("$24,809.46", body)
        self.assertIn("saved only", body)
        self.assertNotIn("billable", body.lower())


if __name__ == "__main__":
    unittest.main()
