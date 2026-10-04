"""API-primary Ascend notice source. Fixtures are synthetic. No network."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

from robie_job_engine import ascend_api_notice_source as source
from robie_job_engine import ascend_notice_driver as driver
from robie_job_engine import ascend_notice_triage as triage
from robie_job_engine import ezlynx_discussions as discussions

ROOT = Path(__file__).resolve().parents[1]
APPLICANT = "220250093"
POLICY = "HO-998877"
INSURED = "Fixture Hauling LLC"
PRODUCER = {
    "first_name": "Karla",
    "last_name": "Brown",
    "email": "karla@streetsmart.insurance",
}


def _pid(n: int) -> str:
    return f"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaa{n:02d}"


def _iid(n: int) -> str:
    return f"bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbb{n:02d}"


def _program(n: int, status: str, updated: str, **extra) -> dict:
    row = {
        "id": _pid(n),
        "status": status,
        "updated_at": updated,
        "insured": {"business_name": INSURED},
        "producer": dict(PRODUCER),
        "account_manager": dict(PRODUCER),
        "policy_number": POLICY,
    }
    row.update(extra)
    return row


def _loan(n: int, program_n: int, status: str, updated: str) -> dict:
    return {
        "id": _iid(n),
        "program_id": _pid(program_n),
        "status": status,
        "updated_at": updated,
        "policy_number": POLICY,
    }


def _types(notices: list[source.ApiNotice]) -> dict[str, source.ApiNotice]:
    return {item.event_type: item for item in notices}


class ProgramClient:
    def __init__(self, program: dict):
        self.program = program

    def get_program(self, program_id: str):
        return dict(self.program)

    def find_program_by_policy(self, policy_number: str):
        return None


class Ezlynx:
    def search_policy_by_number(self, policy_number: str):
        return {
            "status": "success",
            "data": [{"PolicyNumber": policy_number, "ApplicantId": APPLICANT}],
        }


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


class FakeUrlopen:
    def __init__(self, routes):
        self.routes = list(routes)
        self.calls = []

    def __call__(self, url, *, data=None, headers=None, timeout=None):
        self.calls.append({"url": url, "data": data, "headers": dict(headers or {})})
        for needle, payload in self.routes:
            if needle in url:
                return FakeResponse(payload)
        raise AssertionError(f"unexpected Discussion API URL: {url}")

    def posts_to(self, needle):
        return [call for call in self.calls if needle in call["url"] and call["data"]]


def discussion_client(rows):
    routes = [
        ("connect/token", {"access_token": "tok123", "expires_in": 3600}),
        ("by-applicant", rows),
        ("/notes", {"noteId": "n7"}),
        ("v8/discussions/", {"discussionId": "d1", "notes": [{"noteId": "n7", "body": "filed"}]}),
    ]
    config = discussions.DiscussionApiConfig(
        discussion_base_url="https://app.uatezlynx.com/DiscussionApi/",
        token_endpoint="https://identity.example.com/connect/token",
        client_id="street_smart_api",
        client_secret="secret",
        username="SSRobie",
        integration_group_id="159",
    )
    client = discussions.DiscussionApiClient(config, urlopen=FakeUrlopen(routes))
    return client


def driver_ctx(rows=None):
    return driver.DriverContext(
        ascend_client=ProgramClient({"id": _pid(1), "producer": PRODUCER}),
        ezlynx_client=Ezlynx(),
        discussion_client=discussion_client(rows if rows is not None else []),
        source=driver.NullNoticeSource(),
        dry_run=True,
        today=datetime(2026, 10, 4, tzinfo=timezone.utc).date(),
    )


class FeedClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, path, query=None):
        self.calls.append((path, dict(query or {})))
        payload = self.pages.get(path)
        if payload is None:
            return {"data": [], "meta": {"next": None}}
        return payload


def _snapshot():
    programs = [
        _program(1, "payment_overdue", "2026-09-28T00:01:00Z", due_date="2026-09-27"),
        _program(2, "pending_cancellation", "2026-09-24T12:35:00Z", cancel_at="2026-10-01"),
        _program(3, "cancelled", "2026-09-24T12:36:00Z", balance_cents=41210),
        _program(4, "active", "2026-09-22T12:44:00Z"),
        _program(5, "active", "2026-10-02T12:41:00Z"),
        _program(6, "active", "2026-09-28T12:32:00Z"),
        _program(7, "active", "2026-09-26T23:17:00Z"),
    ]
    loans = [
        _loan(2, 2, "pending_cancel", "2026-09-24T12:35:00Z"),
        _loan(3, 3, "canceled", "2026-09-24T12:36:00Z"),
        _loan(4, 4, "completed", "2026-09-22T12:44:00Z"),
    ]
    invoices = [
        {
            "id": _iid(11),
            "program_id": _pid(1),
            "status": "overdue",
            "due_date": "2026-09-27",
            "total_amount_cents": 41210,
            "updated_at": "2026-09-28T00:01:00Z",
            "invoice_number": "INV-3003",
            "policy_number": POLICY,
            "payer_name": INSURED,
        },
        {
            "id": _iid(15),
            "program_id": _pid(5),
            "status": "paid",
            "is_reinstatement": True,
            "paid_at": "2026-10-02T12:41:00Z",
            "updated_at": "2026-10-02T12:41:00Z",
            "policy_number": POLICY,
            "payer_name": INSURED,
        },
        {
            "id": _iid(16),
            "program_id": _pid(6),
            "status": "paid",
            "paid_at": "2026-09-28T12:32:00Z",
            "updated_at": "2026-09-28T12:32:00Z",
            "total_amount_cents": 25000,
            "invoice_number": "INV-2002",
            "policy_number": POLICY,
            "payer_name": INSURED,
        },
        {
            "id": _iid(17),
            "program_id": _pid(7),
            "status": "awaiting_payment",
            "memo": "Settlement for Invoice No. INV-1001",
            "invoice_items": [
                {"title": "Disputed amount", "amount_cents": 16527},
                {"title": "Dispute fee", "amount_cents": 1500},
            ],
            "total_amount_cents": 18027,
            "updated_at": "2026-09-26T23:17:00Z",
            "policy_number": POLICY,
            "payer_name": INSURED,
        },
    ]
    payouts = [
        {
            "id": "payout-1001",
            "payout_type": "commission",
            "paying_at": "2026-09-28T23:01:00Z",
            "updated_at": "2026-09-28T23:01:00Z",
            "net_payout_amount_cents": 74274,
            "status": "paid",
        }
    ]
    return programs, loans, invoices, payouts


def test_each_mapper_emits_the_study_event_and_classifies():
    programs, loans, invoices, payouts = _snapshot()
    notices = source.notices_from_snapshot(
        programs=programs, loans=loans, invoices=invoices, payouts=payouts
    )
    by_type = _types(notices)
    assert set(by_type) == {
        triage.LATE_PAYMENT,
        triage.INTENT_TO_CANCEL,
        triage.CANCELLATION,
        triage.PAID_OFF,
        triage.REINSTATEMENT,
        triage.PAYMENT_CONFIRMATION,
        triage.DISPUTED_CHARGE,
        triage.AGENCY_REMITTANCE,
    }
    past_due = by_type[triage.LATE_PAYMENT]
    assert past_due.invoice_id == _iid(11)
    assert past_due.event_key == source.make_event_key(
        _pid(1), triage.LATE_PAYMENT, "INV-3003"
    )
    assert source.make_event_key(_pid(1), triage.LATE_PAYMENT, _iid(11)) in past_due.alias_keys
    assert any(source.make_event_key(_pid(1), triage.LATE_PAYMENT, "2026-09-28T00:01:00Z") in key
               or key.endswith("2026-09-28T00:01:00Z")
               for key in past_due.alias_keys)
    for notice in notices:
        if notice.remittance:
            continue
        assert triage.classify_notice(notice.subject, notice.body) == notice.event_type
        assert triage.extract_program_uuid(notice.body) == notice.program_id
        assert POLICY in triage.extract_policy_numbers(notice.body)


def test_dispute_requires_settlement_title_and_a_dispute_line():
    program = _program(7, "active", "2026-09-26T23:17:00Z")
    fee_only = {
        "id": _iid(17),
        "program_id": _pid(7),
        "memo": "Settlement for Invoice No. INV-9",
        "invoice_items": [{"title": "Dispute fee", "amount_cents": 1500}],
        "total_amount_cents": 1500,
        "updated_at": "2026-09-26T23:17:00Z",
    }
    assert source._invoice_notice(fee_only, program).event_type == triage.DISPUTED_CHARGE
    plain = dict(fee_only, memo="Installment", invoice_items=[{"title": "Installment"}])
    assert source._invoice_notice(plain, program) is None
    settlement_without_lines = dict(fee_only, invoice_items=[{"title": "Installment"}])
    assert source._invoice_notice(settlement_without_lines, program) is None


def test_reinstatement_needs_a_prior_cancel_or_the_invoice_flag():
    program = _program(5, "active", "2026-10-02T12:41:00Z")
    loan = _loan(5, 5, "bound", "2026-10-02T12:41:00Z")
    quiet = source.notices_from_snapshot(
        programs=[program], loans=[loan], invoices=[], payouts=[]
    )
    assert triage.REINSTATEMENT not in _types(quiet)
    moved = source.notices_from_snapshot(
        programs=[program],
        loans=[loan],
        invoices=[],
        payouts=[],
        prior_loan_status={loan["id"]: "pending_cancel"},
    )
    assert _types(moved)[triage.REINSTATEMENT].event_key == source.make_event_key(
        _pid(5), triage.REINSTATEMENT, "2026-10-02T12:41:00Z"
    )


def test_a_later_poll_sees_pending_cancel_become_reinstatement(tmp_path, monkeypatch):
    monkeypatch.delenv(source.LIVE_ENV, raising=False)
    program = _program(5, "pending_cancellation", "2026-10-01T12:40:00Z")
    pending = _loan(5, 5, "pending_cancel", "2026-10-01T12:40:00Z")
    bound = _loan(5, 5, "bound", "2026-10-02T12:41:00Z")
    store = source.EventKeyStore(tmp_path / "events.db")

    def pages(loan):
        return {
            source.FEED_PROGRAMS: {"data": [program], "meta": {"next": None}},
            source.FEED_LOANS: {"data": [loan], "meta": {"next": None}},
            source.FEED_INVOICES: {"data": [], "meta": {"next": None}},
            source.FEED_PAYOUTS: {"data": [], "meta": {"next": None}},
        }

    first = source.run_once(
        client=FeedClient(pages(pending)),
        store=store,
        driver_ctx=driver_ctx(),
        since=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    assert triage.REINSTATEMENT not in {row["event_type"] for row in first["results"]}
    second = source.run_once(
        client=FeedClient(pages(bound)),
        store=store,
        driver_ctx=driver_ctx(),
        since=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    kinds = [row["event_type"] for row in second["results"] if row["status"] == "dry_run"]
    assert triage.REINSTATEMENT in kinds


def test_paid_off_and_nonpay_keys_use_program_type_and_timestamp():
    program = _program(4, "active", "2026-09-22T12:44:00Z")
    loan = _loan(4, 4, "completed", "2026-09-22T12:44:00Z")
    notices = source.notices_from_snapshot(
        programs=[program], loans=[loan], invoices=[], payouts=[]
    )
    paid = _types(notices)[triage.PAID_OFF]
    assert paid.event_key == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaa04|paid_off|2026-09-22T12:44:00Z"
    cancelled = _program(3, "cancelled", "2026-09-24T12:36:00Z", balance_cents=1000)
    canceled = _loan(3, 3, "canceled", "2026-09-24T12:36:00Z")
    cancel_notices = source.notices_from_snapshot(
        programs=[cancelled], loans=[canceled], invoices=[], payouts=[]
    )
    assert _types(cancel_notices)[triage.CANCELLATION].event_type == triage.CANCELLATION
    assert "non-payment" in _types(cancel_notices)[triage.CANCELLATION].subject


def test_episode_anchor_stays_put_until_the_status_changes(tmp_path):
    store = source.EventKeyStore(tmp_path / "events.db")
    episode = f"{_pid(1)}|{triage.LATE_PAYMENT}"
    first = store.episode_anchor(episode, "payment_overdue", "2026-09-28T00:01:00Z", persist=True)
    second = store.episode_anchor(episode, "payment_overdue", "2026-09-29T00:01:00Z", persist=True)
    assert first == second == "2026-09-28T00:01:00Z"
    cleared = store.episode_anchor(episode, "active", "2026-09-30T00:01:00Z", persist=True)
    assert cleared == "2026-09-30T00:01:00Z"
    again = store.episode_anchor(episode, "payment_overdue", "2026-10-02T00:01:00Z", persist=True)
    assert again == "2026-10-02T00:01:00Z"


def test_email_skips_api_filed_events_and_keeps_the_gaps(tmp_path, monkeypatch):
    store = source.EventKeyStore(tmp_path / "events.db")
    monkeypatch.setenv(source.DB_ENV, str(store.path))
    past = source.notices_from_snapshot(
        programs=[_program(1, "payment_overdue", "2026-09-28T00:01:00Z", due_date="2026-09-27")],
        loans=[],
        invoices=[],
        payouts=[],
    )[0]
    store.record_filed(past)
    email_at = "2026-09-28T12:01:00Z"
    body = (
        f"This policy has a past-due payment of $412.10 which was due on 09/27/2026.\n"
        f"Policy ID {POLICY}\n"
        f"Customer {INSURED}\n"
        f"https://dashboard.useascend.com/programs/{_pid(1)}\n"
    )
    assert source.email_covered_by_api(
        program_id=_pid(1),
        notice_type=triage.LATE_PAYMENT,
        subject=f"Past due payment for {INSURED}",
        body=body,
        internal_date=email_at,
        store=store,
    ) == past.event_key
    # The filed row is the program overdue episode and has no invoice id.
    # Payment failed for that program and policy is the same episode.
    assert source.email_covered_by_api(
        program_id=_pid(1),
        notice_type=triage.LATE_PAYMENT,
        subject=f"Payment failed for {INSURED}",
        body="We couldn't process your payment of $412.10.\n" + body,
        internal_date=email_at,
        store=store,
    ) == past.event_key
    for notice_type in (triage.UNDERWRITING, triage.RETURN_PREMIUM, triage.REFUND):
        assert source.email_covered_by_api(
            program_id=_pid(1),
            notice_type=notice_type,
            subject="ignored",
            body=body,
            internal_date=email_at,
            store=store,
        ) == ""
    # 60h after the flip is inside the 96h past-due window. The body has no
    # invoice number; the subject and program URL are enough.
    late_email = "2026-09-30T12:01:00Z"
    assert source.email_covered_by_api(
        program_id=_pid(1),
        notice_type=triage.LATE_PAYMENT,
        subject=f"Past due payment for {INSURED}",
        body=body,
        internal_date=late_email,
        store=store,
    ) == past.event_key
    too_late = "2026-10-02T04:02:00Z"
    assert source.email_covered_by_api(
        program_id=_pid(1),
        notice_type=triage.LATE_PAYMENT,
        subject=f"Past due payment for {INSURED}",
        body=body,
        internal_date=too_late,
        store=store,
    ) == ""
    assert source.email_covered_by_api(
        program_id=_pid(2),
        notice_type=triage.LATE_PAYMENT,
        subject=f"Past due payment for {INSURED}",
        body=body.replace(_pid(1), _pid(2)),
        internal_date=email_at,
        store=store,
    ) == ""

    ctx = driver_ctx()
    ctx.ascend_client = ProgramClient(past.program)
    covered = driver.EmailNotice(
        message_id="mail-1",
        subject=f"Past due payment for {INSURED}",
        body=body,
        internal_date=email_at,
    )
    result = driver.process_notice(covered, ctx)
    assert result.status == "skipped"
    assert result.reason == "api_already_filed"
    assert result.detail["event_key"] == past.event_key
    assert ctx.discussion_client._urlopen.posts_to("/notes") == []

    failed = driver.EmailNotice(
        message_id="mail-2",
        subject=f"Payment failed for {INSURED}",
        body="We couldn't process your payment of $412.10.\n" + body,
        internal_date=email_at,
    )
    failed_result = driver.process_notice(failed, ctx)
    assert failed_result.reason == "api_already_filed"
    assert failed_result.detail["event_key"] == past.event_key
    assert failed_result.detail.get("notice_type") == triage.LATE_PAYMENT


FAILED_POLICY = "AYMGH-D"


def _overdue_invoice(invoice_id: str, *, policy: str, amount_cents: int, due_date: str, number: str) -> dict:
    return {
        "id": invoice_id,
        "program_id": _pid(1),
        "status": "overdue",
        "due_date": due_date,
        "invoice_number": number,
        "policy_number": policy,
        "total_amount_cents": amount_cents,
        "updated_at": "2026-10-04T18:16:00Z",
    }


def _file_overdue(store, invoice: dict):
    program = _program(
        1,
        "payment_overdue",
        "2026-10-04T18:16:00Z",
        policy_number=invoice["policy_number"],
        due_date=invoice["due_date"],
    )
    notice = source._invoice_notice(invoice, program)
    assert notice is not None
    store.record_filed(notice)
    return notice


def _failed_mail(
    *,
    policy: str,
    invoice_id: str = "",
    amount: str = "412.10",
    due: str = "10/03/2026",
    message_id: str = "mail-failed",
) -> driver.EmailNotice:
    invoice_line = ""
    if invoice_id:
        invoice_line = (
            "Make a payment ( https://checkout.useascend.com/agency/invoices/"
            f"{invoice_id}?s_token=FIXTURE )\n"
        )
    due_line = f"The payment was due on {due}.\n" if due else ""
    body = (
        "We're writing to let you know that we couldn't process your payment of "
        f"${amount} for StreetSmart Insurance Agency because of insufficient funds.\n"
        f"{due_line}"
        f"Policy ID {policy}\n"
        f"https://dashboard.useascend.com/programs/{_pid(1)}\n"
        f"{invoice_line}"
    )
    return driver.EmailNotice(
        message_id=message_id,
        subject=f"Payment failed for {INSURED}",
        body=body,
        internal_date="2026-10-04T22:16:00Z",
    )


def test_payment_failed_skips_same_invoice_files_a_different_one_and_files_when_unmatched(
    tmp_path, monkeypatch
):
    store = source.EventKeyStore(tmp_path / "events.db")
    monkeypatch.setenv(source.DB_ENV, str(store.path))
    filed = _file_overdue(
        store,
        _overdue_invoice(
            _iid(21),
            policy=FAILED_POLICY,
            amount_cents=41210,
            due_date="2026-10-03",
            number="INV-AYM",
        ),
    )
    same = _failed_mail(policy=FAILED_POLICY, invoice_id=_iid(21))
    covered = source.email_covered_by_api(
        program_id=_pid(1),
        notice_type=triage.LATE_PAYMENT,
        subject=same.subject,
        body=same.body,
        internal_date=same.internal_date,
        store=store,
    )
    assert covered == filed.event_key
    ctx = driver_ctx()
    skipped = driver.process_notice(same, ctx)
    assert skipped.status == "skipped"
    assert skipped.reason == "api_already_filed"
    assert skipped.detail["event_key"] == filed.event_key
    assert ctx.discussion_client._urlopen.posts_to("/notes") == []

    # Same due date and amount, different invoice. Bill matching must not
    # swallow an email that names another invoice.
    other = _failed_mail(
        policy=FAILED_POLICY,
        invoice_id=_iid(22),
        message_id="mail-other-invoice",
    )
    other_ctx = driver_ctx()
    other_result = driver.process_notice(other, other_ctx)
    assert other_result.reason != "api_already_filed"
    assert other_result.status == "dry_run"
    assert other_ctx.discussion_client._urlopen.posts_to("/notes") == []

    empty = source.EventKeyStore(tmp_path / "empty.db")
    monkeypatch.setenv(source.DB_ENV, str(empty.path))
    unmatched = _failed_mail(policy=FAILED_POLICY, invoice_id=_iid(21), message_id="mail-none")
    none_ctx = driver_ctx()
    none_result = driver.process_notice(unmatched, none_ctx)
    assert none_result.reason != "api_already_filed"
    assert none_result.status == "dry_run"
    assert none_result.reason != "api_store_unreadable"
    assert none_ctx.discussion_client._urlopen.posts_to("/notes") == []


def test_payment_failed_same_bill_without_invoice_id_is_skipped(tmp_path, monkeypatch):
    store = source.EventKeyStore(tmp_path / "events.db")
    monkeypatch.setenv(source.DB_ENV, str(store.path))
    filed = _file_overdue(
        store,
        _overdue_invoice(
            _iid(21),
            policy=FAILED_POLICY,
            amount_cents=41210,
            due_date="2026-10-03",
            number="INV-AYM",
        ),
    )
    mail = _failed_mail(policy=FAILED_POLICY, invoice_id="", amount="412.10", due="10/03/2026")
    assert source.email_covered_by_api(
        program_id="",
        notice_type=triage.LATE_PAYMENT,
        subject=mail.subject,
        body=mail.body,
        store=store,
    ) == filed.event_key
    result = driver.process_notice(mail, driver_ctx())
    assert result.status == "skipped"
    assert result.reason == "api_already_filed"


def test_payment_failed_matches_policy_from_program_cache_on_an_old_store(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE filed_events (
            event_key TEXT PRIMARY KEY,
            program_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            anchor TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            invoice_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            insured_name TEXT NOT NULL DEFAULT '',
            loan_id TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE program_policies (
            program_id TEXT PRIMARY KEY,
            policy_numbers TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    event_key = f"{_pid(1)}|late_payment|{_iid(21)}"
    conn.execute(
        """
        INSERT INTO filed_events (
            event_key, program_id, event_type, anchor, occurred_at,
            invoice_id, status, recorded_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'filed', ?)
        """,
        (
            event_key,
            _pid(1),
            triage.LATE_PAYMENT,
            "INV-AYM",
            "2026-10-04T18:16:00Z",
            _iid(21),
            "2026-10-04T18:16:00Z",
        ),
    )
    conn.execute(
        "INSERT INTO program_policies (program_id, policy_numbers, updated_at) VALUES (?, ?, ?)",
        (_pid(1), json.dumps([FAILED_POLICY]), "2026-10-04T18:16:00Z"),
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv(source.DB_ENV, str(path))
    mail = _failed_mail(policy=FAILED_POLICY, invoice_id=_iid(21))
    assert source.email_covered_by_api(
        program_id=_pid(1),
        notice_type=triage.LATE_PAYMENT,
        subject=mail.subject,
        body=mail.body,
    ) == event_key
    result = driver.process_notice(mail, driver_ctx())
    assert result.reason == "api_already_filed"


def test_fixture_04_is_covered_by_the_program_overdue_episode(tmp_path, monkeypatch):
    fixture = json.loads(
        (ROOT / "tests/fixtures/ascend_notices/payment_failed_04.json").read_text(
            encoding="utf-8"
        )
    )
    program_id = "a2786f7c-d081-4b19-a374-72b6fd7ec635"
    flip = "2026-10-04T15:50:00Z"
    program = {
        "id": program_id,
        "status": "payment_overdue",
        "updated_at": flip,
        "insured": {"business_name": "Fixture Insured A LLC"},
        "policy_number": "CBL58682451P-85",
        "overdue_amount_cents": 27395,
    }
    store = source.EventKeyStore(tmp_path / "events.db")
    notices = source._program_notices(program, [], store=store, persist=True)
    assert len(notices) == 1
    filed = notices[0]
    assert filed.invoice_id == ""
    assert filed.event_type == triage.LATE_PAYMENT
    store.record_filed(filed)
    body = fixture["text_plain"]
    assert "$290.87" in body or "290.87" in body
    assert "due on" not in body.casefold()
    monkeypatch.setenv(source.DB_ENV, str(store.path))
    assert source.email_covered_by_api(
        program_id=program_id,
        notice_type=triage.LATE_PAYMENT,
        subject=fixture["subject"],
        body=body,
        store=store,
    ) == filed.event_key
    assert source.email_covered_by_api(
        program_id=program_id,
        notice_type=triage.LATE_PAYMENT,
        subject=fixture["subject"],
        body=body,
    ) == filed.event_key
    result = driver.process_notice(
        driver.EmailNotice(
            message_id="mail-fixture-04",
            subject=fixture["subject"],
            body=body,
            internal_date="2026-10-04T22:16:00Z",
        ),
        driver_ctx(),
    )
    assert result.reason == "api_already_filed"
    assert result.detail["event_key"] == filed.event_key

    store.episode_anchor(
        f"{program_id}|{triage.LATE_PAYMENT}",
        "active",
        "2026-10-05T12:00:00Z",
        persist=True,
    )
    assert source.email_covered_by_api(
        program_id=program_id,
        notice_type=triage.LATE_PAYMENT,
        subject=fixture["subject"],
        body=body,
        store=store,
    ) == ""


def test_invoice_uuid_covers_payment_failed_without_a_policy(tmp_path):
    store = source.EventKeyStore(tmp_path / "events.db")
    notice = source._notice(
        event_type=triage.LATE_PAYMENT,
        program_id=_pid(1),
        anchor="INV-BARE",
        occurred_at="2026-10-04T18:16:00Z",
        subject="Past due",
        body="past due",
        invoice_id=_iid(21),
        policy_numbers=[],
    )
    assert notice is not None
    store.record_filed(notice)
    mail = _failed_mail(policy="OTHER-POLICY-9", invoice_id=_iid(21))
    assert source.email_covered_by_api(
        program_id=_pid(1),
        notice_type=triage.LATE_PAYMENT,
        subject=mail.subject,
        body=mail.body,
        store=store,
    ) == notice.event_key


def test_due_and_amount_fallback_still_requires_the_same_policy(tmp_path):
    store = source.EventKeyStore(tmp_path / "events.db")
    _file_overdue(
        store,
        _overdue_invoice(
            _iid(21),
            policy=FAILED_POLICY,
            amount_cents=41210,
            due_date="2026-10-03",
            number="INV-AYM",
        ),
    )
    mail = _failed_mail(
        policy="OTHER-POLICY-9",
        invoice_id="",
        amount="412.10",
        due="10/03/2026",
    )
    assert source.email_covered_by_api(
        program_id=_pid(1),
        notice_type=triage.LATE_PAYMENT,
        subject=mail.subject,
        body=mail.body,
        store=store,
    ) == ""


def test_payment_confirmation_does_not_suppress_payment_failed(tmp_path):
    store = source.EventKeyStore(tmp_path / "events.db")
    program = _program(1, "active", "2026-10-04T18:16:00Z", policy_number=FAILED_POLICY)
    notice = source._invoice_notice(
        {
            "id": _iid(21),
            "program_id": _pid(1),
            "status": "paid",
            "paid_at": "2026-10-04T18:16:00Z",
            "invoice_number": "INV-AYM",
            "policy_number": FAILED_POLICY,
            "total_amount_cents": 41210,
        },
        program,
    )
    assert notice is not None
    assert notice.event_type == triage.PAYMENT_CONFIRMATION
    store.record_filed(notice)
    mail = _failed_mail(policy=FAILED_POLICY, invoice_id=_iid(21))
    assert source.email_covered_by_api(
        program_id=_pid(1),
        notice_type=triage.LATE_PAYMENT,
        subject=mail.subject,
        body=mail.body,
        store=store,
    ) == ""


def test_invoice_labels_ignore_date_from_and_amount():
    body = (
        "Invoice date 10/03/2026\n"
        "Invoice from Ascend\n"
        "Invoice amount $412.10\n"
        f"Invoice No. INV-AYM\n"
        f"Invoice # INV-HASH\n"
        f"Invoice {_iid(21)}\n"
    )
    found = source._payment_failed_invoice_ids(body, _pid(1))
    assert "date" not in found
    assert "from" not in found
    assert "amount" not in found
    assert "inv-aym" in found
    assert "inv-hash" in found
    assert _iid(21) in found


def test_payment_failed_holds_when_the_store_cannot_be_read(tmp_path, monkeypatch):
    garbage = tmp_path / "garbage.db"
    garbage.write_text("not a database", encoding="utf-8")
    monkeypatch.setenv(source.DB_ENV, str(garbage))
    mail = _failed_mail(policy=FAILED_POLICY, invoice_id=_iid(21))
    ctx = driver_ctx()
    result = driver.process_notice(mail, ctx)
    assert result.status == "skipped"
    assert result.reason == "api_store_unreadable"
    assert ctx.discussion_client._urlopen.posts_to("/notes") == []
    assert garbage.read_text(encoding="utf-8") == "not a database"

    missing = tmp_path / "missing.db"
    monkeypatch.setenv(source.DB_ENV, str(missing))
    filed = driver.process_notice(
        _failed_mail(policy=FAILED_POLICY, invoice_id=_iid(21), message_id="mail-missing"),
        driver_ctx(),
    )
    assert filed.reason != "api_store_unreadable"
    assert filed.status == "dry_run"
    assert not missing.exists()


def test_invoice_id_dedupes_even_when_the_email_is_the_same_minute(tmp_path):
    store = source.EventKeyStore(tmp_path / "events.db")
    invoice = {
        "id": _iid(16),
        "program_id": _pid(6),
        "status": "paid",
        "paid_at": "2026-09-28T12:32:00Z",
        "total_amount_cents": 25000,
        "invoice_number": "INV-2002",
        "policy_number": POLICY,
    }
    notice = source._invoice_notice(invoice, _program(6, "active", "2026-09-28T12:32:00Z"))
    store.record_filed(notice)
    body = f"Payment of $250.00 was received.\nInvoice No. {_iid(16)}\n"
    assert source.email_covered_by_api(
        program_id=_pid(6).upper(),
        notice_type=triage.PAYMENT_CONFIRMATION,
        subject="Payment confirmation",
        body=body,
        internal_date="2026-09-28T12:33:00Z",
        store=store,
    ) == notice.event_key


def test_poll_is_get_only_and_uses_updated_since():
    class Pager:
        def __init__(self):
            self.calls = []

        def get(self, path, query=None):
            self.calls.append((path, dict(query or {})))
            page = int((query or {}).get("page") or 1)
            if page == 1:
                return {
                    "data": [
                        {"id": "old", "updated_at": "2026-08-01T00:00:00Z"},
                        {"id": "new", "updated_at": "2026-10-01T00:00:00Z"},
                    ],
                    "meta": {"next": 2},
                }
            return {
                "data": [{"id": "newer", "updated_at": "2026-10-02T00:00:00Z"}],
                "meta": {"next": None},
            }

    pager = Pager()
    wrapped = source.GetOnlyClient(pager)
    rows = source.list_updated_since(wrapped, "/v1/invoices", "2026-09-18T00:00:00Z")
    assert [row["id"] for row in rows] == ["new", "newer"]
    assert pager.calls[0][1]["updated_at[gte]"] == "2026-09-18T00:00:00Z"
    assert pager.calls[0][1]["page_size"] == 25
    assert all(call[0] == "GET" for call in wrapped.calls)
    with pytest.raises(RuntimeError, match="GET only"):
        wrapped.post("/v1/invoices")


def test_webhook_seam_maps_invoice_and_payout_and_leaves_the_gaps():
    paid = source.events_from_webhook(
        "invoice.paid",
        {
            "data": {
                "id": _iid(16),
                "program_id": _pid(6),
                "paid_at": "2026-09-28T12:32:00Z",
                "total_amount_cents": 25000,
                "payer_name": INSURED,
                "policy_number": POLICY,
            }
        },
    )
    assert [item.event_type for item in paid] == [triage.PAYMENT_CONFIRMATION]
    overdue = source.events_from_webhook(
        "invoice.marked_overdue",
        {"data": {"id": _iid(11), "program_id": _pid(1), "updated_at": "2026-09-28T00:01:00Z"}},
    )
    assert [item.event_type for item in overdue] == [triage.LATE_PAYMENT]
    dispute = source.events_from_webhook(
        "invoice.created",
        {
            "data": {
                "id": _iid(17),
                "program_id": _pid(7),
                "memo": "Settlement for Invoice No. INV-1001",
                "items": [{"title": "Disputed amount"}],
                "updated_at": "2026-09-26T23:17:00Z",
            }
        },
    )
    assert [item.event_type for item in dispute] == [triage.DISPUTED_CHARGE]
    assert source.events_from_webhook(
        "invoice.created",
        {"data": {"id": _iid(1), "program_id": _pid(1), "memo": "Installment"}},
    ) == []
    assert source.events_from_webhook("invoice.processing_payment", {"data": {}}) == []
    assert source.events_from_webhook("invoice.voided", {"data": {}}) == []
    assert source.events_from_webhook("refund.paid", {"data": {}}) == []
    payout = source.events_from_webhook(
        "payout.paid",
        {"data": {"id": "payout-1001", "type": "full_premium", "paying_at": "2026-09-28T23:01:00Z"}},
    )
    assert [item.event_type for item in payout] == [triage.AGENCY_REMITTANCE]
    assert source.events_from_webhook("program.updated", {"data": {}}) == []


def test_dry_run_prints_would_file_lines_and_does_not_write(tmp_path, monkeypatch):
    fired = []
    monkeypatch.setattr(
        driver.zapier_tasks,
        "fire_task",
        lambda payload, *, dry_run=False: fired.append(payload) or {"ok": True},
    )
    monkeypatch.delenv(source.LIVE_ENV, raising=False)
    monkeypatch.delenv(source.REMITTANCE_APPLICANT_ENV, raising=False)
    programs, loans, invoices, payouts = _snapshot()
    pages = {
        source.FEED_PROGRAMS: {"data": programs, "meta": {"next": None}},
        source.FEED_LOANS: {"data": loans, "meta": {"next": None}},
        source.FEED_INVOICES: {"data": invoices, "meta": {"next": None}},
        source.FEED_PAYOUTS: {"data": payouts, "meta": {"next": None}},
    }
    client = FeedClient(pages)
    store = source.EventKeyStore(tmp_path / "events.db")
    ctx = driver_ctx()
    summary = source.run_once(
        client=client,
        store=store,
        driver_ctx=ctx,
        now=datetime(2026, 10, 4, 16, 0, tzinfo=timezone.utc),
        since=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    assert summary["dry_run"] is True
    assert summary["live"] is False
    assert summary["http_methods"] == ["GET"]
    assert summary["errors"] == []
    assert ctx.discussion_client._urlopen.posts_to("/notes") == []
    assert ctx.discussion_client._urlopen.posts_to("with-note") == []
    assert fired == []
    assert store.cursor() is None
    assert summary["would_file_count"] == 7
    kinds = {line.split()[2] for line in summary["would_file_lines"]}
    assert kinds == {
        "type=late_payment",
        "type=intent_to_cancel",
        "type=cancellation",
        "type=paid_off",
        "type=reinstatement",
        "type=payment_confirmation",
        "type=disputed_charge",
    }
    assert all(line.startswith("would-file ") for line in summary["would_file_lines"])
    assert any("task=cancellation" in line for line in summary["would_file_lines"])
    assert any("task=disputed_charge" in line for line in summary["would_file_lines"])
    remittance = next(row for row in summary["results"] if row["event_type"] == triage.AGENCY_REMITTANCE)
    assert remittance["reason"] == "remittance_target_unconfigured"
    by_type = {row["event_type"]: row for row in summary["results"] if row["status"] == "dry_run"}
    assert by_type[triage.DISPUTED_CHARGE]["detail"]["task"]["assigned_user_id"] == 263046
    assert by_type[triage.CANCELLATION]["detail"]["task"]["assignee"] == "KarlaSS"
    assert "task" not in by_type[triage.INTENT_TO_CANCEL]["detail"]
    assert {call[0] for call in client.calls} <= set(source.FEEDS)


def test_live_flag_is_required_and_records_the_key(tmp_path, monkeypatch):
    fired = []
    monkeypatch.setattr(
        driver.zapier_tasks,
        "fire_task",
        lambda payload, *, dry_run=False: fired.append(payload) or {"ok": True, "dry_run": dry_run},
    )
    programs = [_program(3, "cancelled", "2026-09-24T12:36:00Z", balance_cents=41210)]
    loans = [_loan(3, 3, "canceled", "2026-09-24T12:36:00Z")]
    pages = {
        source.FEED_PROGRAMS: {"data": programs, "meta": {"next": None}},
        source.FEED_LOANS: {"data": loans, "meta": {"next": None}},
        source.FEED_INVOICES: {"data": [], "meta": {"next": None}},
        source.FEED_PAYOUTS: {"data": [], "meta": {"next": None}},
    }
    store = source.EventKeyStore(tmp_path / "events.db")
    rows = [{"discussionId": "d1", "title": "Ascend - Cancellation Notices"}]
    monkeypatch.delenv(source.LIVE_ENV, raising=False)
    dry = source.run_once(
        client=FeedClient(pages),
        store=store,
        driver_ctx=driver_ctx(rows),
        since=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    assert dry["dry_run"] is True
    assert store.filed_for(_pid(3), triage.CANCELLATION) == []
    assert fired == []

    monkeypatch.setenv(source.LIVE_ENV, "1")
    ctx = driver_ctx(rows)
    live = source.run_once(
        client=FeedClient(pages),
        store=store,
        driver_ctx=ctx,
        now=datetime(2026, 10, 4, 16, 0, tzinfo=timezone.utc),
        since=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    assert live["live"] is True
    assert live["results"][0]["status"] == "done"
    assert store.is_filed(live["results"][0]["event_key"])
    assert len(ctx.discussion_client._urlopen.posts_to("/notes")) == 1
    assert len(fired) == 1
    assert store.cursor() == datetime(2026, 10, 4, 16, 0, tzinfo=timezone.utc)

    again = source.run_once(
        client=FeedClient(pages),
        store=store,
        driver_ctx=driver_ctx(rows),
        since=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    assert again["results"][0]["reason"] == "api_already_filed"
    assert len(fired) == 1


def test_stall_probe_alerts_on_the_fourth_failure(tmp_path, monkeypatch):
    monkeypatch.delenv(source.LIVE_ENV, raising=False)
    alerts = []

    class Boom:
        def get(self, path, query=None):
            raise RuntimeError("Ascend API GET /v1/programs returned HTTP 503")

    store = source.EventKeyStore(tmp_path / "events.db")
    ctx = driver_ctx()
    for _ in range(3):
        summary = source.run_once(
            client=Boom(), store=store, driver_ctx=ctx, alerter=alerts.append
        )
        assert summary["stall_alerted"] is False
        assert summary["errors"]
    fourth = source.run_once(
        client=Boom(), store=store, driver_ctx=ctx, alerter=alerts.append
    )
    assert fourth["stall_streak"] >= source.STALL_RUNS
    assert fourth["stall_alerted"] is True
    assert len(alerts) == 1
    assert "4 times" in alerts[0]

    source.run_once(
        client=FeedClient({}),
        store=store,
        driver_ctx=ctx,
        alerter=alerts.append,
    )
    quiet = source.run_once(
        client=Boom(), store=store, driver_ctx=ctx, alerter=alerts.append
    )
    assert quiet["stall_streak"] == 1
    assert quiet["stall_alerted"] is False


def test_remittance_files_only_when_a_target_applicant_is_configured(tmp_path, monkeypatch):
    monkeypatch.delenv(source.LIVE_ENV, raising=False)
    payouts = [
        {
            "id": "payout-1001",
            "payout_type": "overpayment",
            "paying_at": "2026-09-28T23:01:00Z",
            "updated_at": "2026-09-28T23:01:00Z",
            "net_payout_amount_cents": 800,
        }
    ]
    pages = {
        source.FEED_PROGRAMS: {"data": [], "meta": {"next": None}},
        source.FEED_LOANS: {"data": [], "meta": {"next": None}},
        source.FEED_INVOICES: {"data": [], "meta": {"next": None}},
        source.FEED_PAYOUTS: {"data": payouts, "meta": {"next": None}},
    }
    store = source.EventKeyStore(tmp_path / "events.db")
    skipped = source.run_once(
        client=FeedClient(pages),
        store=store,
        driver_ctx=driver_ctx(),
        since=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    assert skipped["would_file_lines"] == []
    assert skipped["results"][0]["reason"] == "remittance_target_unconfigured"

    monkeypatch.setenv(source.REMITTANCE_APPLICANT_ENV, APPLICANT)
    rows = [{"discussionId": "d-pay", "title": "Ascend - Payments"}]
    ctx = driver_ctx(rows)
    filed = source.run_once(
        client=FeedClient(pages),
        store=store,
        driver_ctx=ctx,
        since=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    assert filed["would_file_count"] == 1
    assert "agency_remittance" in filed["would_file_lines"][0]
    assert ctx.discussion_client._urlopen.posts_to("/notes") == []
    assert "Ascend - Payments" in filed["would_file_lines"][0]


def test_timer_polls_every_fifteen_minutes_and_stays_dry_run():
    service = (ROOT / "deploy/systemd/robie-ascend-api-notice.service").read_text(encoding="utf-8")
    drop_in = (
        ROOT / "deploy/systemd/robie-ascend-api-notice.service.d/10-write-scope.conf"
    ).read_text(encoding="utf-8")
    timer = (ROOT / "deploy/systemd/robie-ascend-api-notice.timer").read_text(encoding="utf-8")
    assert "OnCalendar=*:0/15" in timer
    assert "robie-ascend-api-notice.service" in timer
    assert "Environment=ASCEND_API_SOURCE_LIVE" not in service
    assert "secrets/ascend-prod-api-key/versions/latest" in service
    assert "-m robie_job_engine.ascend_api_notice_source" in service
    assert "ExecStart=/opt/streetsmart-hermes/venv/bin/python " in service
    assert ".hermes/hermes-agent/venv" not in service
    assert "User=streetsmart-hermes" in service
    assert "Environment=ROBIE_EZLYNX_WRITE_SCOPE=all" in service
    assert "Environment=ROBIE_PLAYGROUND=1" in service
    assert "Environment=ROBIE_EZLYNX_WRITE_SCOPE=all" in drop_in
    assert "Environment=ROBIE_PLAYGROUND=1" in drop_in
    assert "Environment=ROBIE_ASCEND_API_ENABLED=1" in service
    assert "Environment=ROBIE_ASCEND_API_BASE_URL=https://api.useascend.com" in service
    assert "Environment=ROBIE_ASCEND_API_PRODUCTION_ENABLED=1" in service
    assert "Environment=ASCEND_API_SOURCE_LIVE=1" not in service
    assert "UMask=0022" in service
    assert "UMask=0077" not in service
    assert "data/ascend-api/ascend_api_notice_events.db" in service
    assert "data/ascend-api/ascend_api_notice_events.dry-run.db" in service
    assert "runs as carlo" in service
    assert "email driver runs as streetsmart-hermes" not in service.casefold()
    assert "same User= as the email driver" not in service
    drop_store = (
        ROOT / "deploy/systemd/robie-ascend-notice-driver.service.d/20-api-notice-store.conf"
    ).read_text(encoding="utf-8")
    assert "data/ascend-api/ascend_api_notice_events.db" in drop_store
    assert "ROBIE_EZLYNX_WRITE_SCOPE=all" not in drop_store
    text = (ROOT / "robie_job_engine/ascend_api_notice_source.py").read_text(encoding="utf-8")
    assert "cd /" in text
    assert "/opt/streetsmart-hermes/venv/bin/python" in text
    assert ".hermes/hermes-agent/venv" not in text
    assert "HTTPServer" not in text
    assert "smtplib" not in text
    assert "def events_from_webhook" in text


def test_main_dry_run_prints_the_would_file_line(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(source.LIVE_ENV, raising=False)
    monkeypatch.setenv(source.DRY_RUN_DB_ENV, str(tmp_path / "dry-run.db"))
    monkeypatch.setenv(source.LOOKBACK_ENV, str(60 * 24 * 40))
    programs, loans, invoices, payouts = _snapshot()
    pages = {
        source.FEED_PROGRAMS: {"data": [programs[0]], "meta": {"next": None}},
        source.FEED_LOANS: {"data": [], "meta": {"next": None}},
        source.FEED_INVOICES: {"data": [invoices[0]], "meta": {"next": None}},
        source.FEED_PAYOUTS: {"data": [], "meta": {"next": None}},
    }

    def fake_build():
        return FeedClient(pages)

    monkeypatch.setattr(source, "build_client", fake_build)
    monkeypatch.setattr(source, "build_driver_context", lambda *, dry_run: driver_ctx())
    code = source.main([])
    captured = capsys.readouterr()
    assert code == 0
    assert "would-file " in captured.out
    assert "late_payment" in captured.out
    assert "ASCEND_API_SOURCE_LIVE" not in captured.out


def test_billables_supply_policy_numbers_and_are_cached(tmp_path):
    program = _program(1, "payment_overdue", "2026-09-28T00:01:00Z", due_date="2026-09-27")
    program.pop("policy_number")
    invoice = {
        "id": _iid(11),
        "program_id": _pid(1),
        "status": "overdue",
        "due_date": "2026-09-27",
        "total_amount_cents": 41210,
        "updated_at": "2026-09-28T00:01:00Z",
        "invoice_number": "INV-3003",
        "payer_name": INSURED,
    }
    pages = {
        source.FEED_PROGRAMS: {"data": [program], "meta": {"next": None}},
        source.FEED_LOANS: {"data": [], "meta": {"next": None}},
        source.FEED_INVOICES: {"data": [invoice], "meta": {"next": None}},
        source.FEED_PAYOUTS: {"data": [], "meta": {"next": None}},
        source.FEED_BILLABLES: {
            "data": [
                {"id": "bill-1", "program_id": _pid(1), "policy_number": POLICY},
                {"id": "bill-2", "billable_identifier": "QUOTE-NOT-A-POLICY"},
            ],
            "meta": {"next": None},
        },
    }
    client = FeedClient(pages)
    store = source.EventKeyStore(tmp_path / "events.db")
    first = source.run_once(
        client=client,
        store=store,
        driver_ctx=driver_ctx(),
        since=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    billable_calls = [call for call in client.calls if call[0] == source.FEED_BILLABLES]
    assert len(billable_calls) == 1
    assert billable_calls[0][1]["program_id"] == _pid(1)
    assert store.cached_policy_numbers(_pid(1)) == [POLICY]
    filed = next(row for row in first["results"] if row["status"] == "dry_run")
    assert filed["detail"]["policy_number"] == POLICY
    assert "Policy ID " + POLICY in filed["detail"].get("note_text", "") or POLICY in str(
        filed["detail"]
    )
    source.run_once(
        client=client,
        store=store,
        driver_ctx=driver_ctx(),
        since=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    assert len([call for call in client.calls if call[0] == source.FEED_BILLABLES]) == 1


def test_past_due_key_uses_the_human_invoice_number(tmp_path):
    invoice = {
        "id": _iid(11),
        "program_id": _pid(1),
        "status": "overdue",
        "updated_at": "2026-09-28T00:01:00Z",
        "invoice_number": "INV-3003",
        "policy_number": POLICY,
    }
    notice = source._invoice_notice(invoice, _program(1, "payment_overdue", "2026-09-28T00:01:00Z"))
    assert notice is not None
    assert notice.event_key.endswith("|late_payment|INV-3003")
    assert notice.invoice_id == _iid(11)
    assert any(key.endswith(_iid(11)) for key in notice.alias_keys)
    store = source.EventKeyStore(tmp_path / "events.db")
    store.record_filed(notice)
    body = "This policy has a past-due payment of $412.10.\nInvoice No. INV-3003\n"
    assert source.email_covered_by_api(
        program_id=_pid(1),
        notice_type=triage.LATE_PAYMENT,
        subject=f"Past due payment for {INSURED}",
        body=body,
        internal_date="2026-09-28T12:00:00Z",
        store=store,
    ) == notice.event_key


def test_lookback_drops_an_old_loan_event_and_keeps_a_recent_one():
    program = _program(4, "cancelled", "2026-09-24T12:36:00Z")
    old = _loan(4, 4, "completed", "2024-12-28T00:00:00Z")
    recent = _loan(8, 4, "completed", "2026-09-24T12:36:00Z")
    recent["id"] = _iid(8)
    notices = source.notices_from_snapshot(
        programs=[program],
        loans=[old, recent],
        invoices=[],
        payouts=[],
        since=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    paid = [item for item in notices if item.event_type == triage.PAID_OFF]
    assert len(paid) == 1
    assert "2024-12-28" not in paid[0].event_key
    assert "2026-09-24" in paid[0].occurred_at


def test_dry_run_database_is_separate_and_stall_alert_stays(tmp_path, monkeypatch):
    live = tmp_path / "live.db"
    dry = tmp_path / "dry.db"
    monkeypatch.setenv(source.DB_ENV, str(live))
    monkeypatch.setenv(source.DRY_RUN_DB_ENV, str(dry))
    monkeypatch.delenv(source.LIVE_ENV, raising=False)
    assert source.db_path() == dry
    assert source.live_db_path() == live
    monkeypatch.setenv(source.LIVE_ENV, "1")
    assert source.db_path() == live
    monkeypatch.delenv(source.LIVE_ENV, raising=False)
    alerts = []
    store = source.EventKeyStore(dry)

    class Boom:
        def get(self, path, query=None):
            raise RuntimeError("feed down")

    for _ in range(source.STALL_RUNS):
        source.run_once(
            client=Boom(),
            store=store,
            driver_ctx=driver_ctx(),
            since=datetime(2026, 9, 1, tzinfo=timezone.utc),
            alerter=lambda message: alerts.append(message) or True,
        )
    assert alerts
    assert not live.exists()
    mode = dry.stat().st_mode & 0o777
    assert mode == 0o644


def _past_due_email() -> driver.EmailNotice:
    body = (
        f"This policy has a past-due payment of $412.10 which was due on 09/27/2026.\n"
        f"Policy ID {POLICY}\n"
        f"Customer {INSURED}\n"
        f"Invoice No. INV-3003\n"
        f"https://dashboard.useascend.com/programs/{_pid(1)}\n"
    )
    return driver.EmailNotice(
        message_id="mail-late",
        subject=f"Past due payment for {INSURED}",
        body=body,
        internal_date="2026-09-28T12:00:00Z",
    )


def test_missing_store_files_the_email_and_a_live_row_skips_it(tmp_path, monkeypatch):
    missing = tmp_path / "ascend-api" / "missing.db"
    monkeypatch.setenv(source.DB_ENV, str(missing))
    monkeypatch.setenv(source.DRY_RUN_DB_ENV, str(tmp_path / "dry-run.db"))
    ctx = driver_ctx()
    result = driver.process_notice(_past_due_email(), ctx)
    assert result.status == "dry_run"
    assert result.reason != "api_store_unavailable"
    assert not missing.exists()
    assert not missing.parent.exists()
    assert ctx.discussion_client._urlopen.posts_to("/notes") == []

    dry = source.EventKeyStore(tmp_path / "dry-run.db")
    notices = source.notices_from_snapshot(
        programs=[_program(1, "payment_overdue", "2026-09-28T00:01:00Z", due_date="2026-09-27")],
        loans=[],
        invoices=[
            {
                "id": _iid(11),
                "program_id": _pid(1),
                "status": "overdue",
                "updated_at": "2026-09-28T00:01:00Z",
                "invoice_number": "INV-3003",
                "policy_number": POLICY,
            }
        ],
        payouts=[],
    )
    invoice_notice = next(item for item in notices if item.invoice_id == _iid(11))
    dry.record_filed(invoice_notice)
    still = driver.process_notice(_past_due_email(), driver_ctx())
    assert still.status == "dry_run"
    assert still.reason != "api_already_filed"

    live = source.EventKeyStore(tmp_path / "live.db")
    live.record_filed(invoice_notice)
    monkeypatch.setenv(source.DB_ENV, str(live.path))
    skipped = driver.process_notice(_past_due_email(), driver_ctx())
    assert skipped.status == "skipped"
    assert skipped.reason == "api_already_filed"
    garbage = tmp_path / "garbage.db"
    garbage.write_text("not a database", encoding="utf-8")
    monkeypatch.setenv(source.DB_ENV, str(garbage))
    filed = driver.process_notice(_past_due_email(), driver_ctx())
    assert filed.status == "dry_run"
    assert garbage.read_text(encoding="utf-8") == "not a database"


def test_driver_gate_runs_before_a_filed_store_match(tmp_path, monkeypatch):
    live = source.EventKeyStore(tmp_path / "live.db")
    notice = source._invoice_notice(
        {
            "id": _iid(11),
            "program_id": _pid(1),
            "status": "overdue",
            "updated_at": "2026-09-28T00:01:00Z",
            "invoice_number": "INV-3003",
            "policy_number": POLICY,
        },
        _program(1, "payment_overdue", "2026-09-28T00:01:00Z"),
    )
    assert notice is not None
    live.record_filed(notice)
    monkeypatch.setenv(source.DB_ENV, str(live.path))
    ctx = driver_ctx()
    ctx.dry_run = False

    def _refused() -> None:
        from robie_job_engine.ezlynx_driver_gate import EzlynxDriverGateRefused

        raise EzlynxDriverGateRefused("EZLYNX_DRIVER_NOT_IN: driver belongs to TEST")

    with mock.patch("robie_job_engine.safety_seal.driver_gate_for_write", _refused):
        result = driver.process_notice(_past_due_email(), ctx)
    assert "driver_gate_refused" in result.reason
    assert result.reason != "api_already_filed"
    assert ctx.discussion_client._urlopen.posts_to("/notes") == []


def test_writer_does_not_chmod_the_shared_data_directory(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    os.chmod(data, 0o770)
    nested = data / "ascend-api" / "events.db"
    source.EventKeyStore(nested)
    assert (data.stat().st_mode & 0o777) == 0o770
    assert (nested.parent.stat().st_mode & 0o777) == 0o755
    assert (nested.stat().st_mode & 0o777) == 0o644
    direct = data / "events.db"
    source.EventKeyStore(direct)
    assert (data.stat().st_mode & 0o777) == 0o770


def test_past_due_without_updated_at_survives_once(tmp_path, monkeypatch):
    fired: list[dict] = []
    monkeypatch.setattr(
        driver.zapier_tasks,
        "fire_task",
        lambda payload, *, dry_run=False: fired.append(payload) or {"ok": True},
    )
    invoice = {
        "id": _iid(11),
        "program_id": _pid(1),
        "status": "overdue",
        "invoice_number": "INV-3003",
        "policy_number": POLICY,
        "total_amount_cents": 41210,
    }
    program = _program(1, "active", "2026-10-03T00:00:00Z")
    store = source.EventKeyStore(tmp_path / "events.db")
    now = datetime(2026, 10, 4, 16, 0, tzinfo=timezone.utc)
    since = datetime(2026, 10, 1, tzinfo=timezone.utc)
    seen = source.notices_from_snapshot(
        programs=[program],
        loans=[],
        invoices=[invoice],
        payouts=[],
        store=store,
        since=since,
        now=now,
    )
    late = [item for item in seen if item.event_type == triage.LATE_PAYMENT]
    assert len(late) == 1
    assert late[0].occurred_at == "2026-10-04T16:00:00Z"
    later = source.notices_from_snapshot(
        programs=[program],
        loans=[],
        invoices=[invoice],
        payouts=[],
        store=store,
        since=since,
        now=now + timedelta(hours=2),
    )
    assert [item.event_key for item in later if item.event_type == triage.LATE_PAYMENT] == [
        late[0].event_key
    ]
    dated = dict(invoice, due_date="2026-10-02", id=_iid(12), invoice_number="INV-3004")
    kept = source.notices_from_snapshot(
        programs=[program],
        loans=[],
        invoices=[dated],
        payouts=[],
        store=store,
        since=since,
        now=now,
    )
    dated_late = [item for item in kept if item.invoice_id == _iid(12)]
    assert dated_late and dated_late[0].occurred_at == "2026-10-04T16:00:00Z"
    assert dated_late[0].occurred_at != "2026-10-02"
    old_program = _program(1, "payment_overdue", "2024-12-28T00:00:00Z")
    stale = dict(invoice, due_date="2026-10-03", id=_iid(13), invoice_number="INV-3005")
    dropped = source.notices_from_snapshot(
        programs=[old_program],
        loans=[],
        invoices=[stale],
        payouts=[],
        store=store,
        since=since,
        now=now,
    )
    assert all(item.invoice_id != _iid(13) for item in dropped)
    assert not any(item.event_type == triage.LATE_PAYMENT for item in dropped)

    pages = {
        source.FEED_PROGRAMS: {"data": [program], "meta": {"next": None}},
        source.FEED_LOANS: {"data": [], "meta": {"next": None}},
        source.FEED_INVOICES: {"data": [invoice], "meta": {"next": None}},
        source.FEED_PAYOUTS: {"data": [], "meta": {"next": None}},
    }
    monkeypatch.setenv(source.LIVE_ENV, "1")
    rows = [{"discussionId": "d-pay", "title": "Ascend - Payments"}]
    ctx = driver_ctx(rows)
    first = source.run_once(
        client=FeedClient(pages),
        store=store,
        driver_ctx=ctx,
        now=now,
        since=since,
    )
    done = [row for row in first["results"] if row["status"] == "done"]
    assert [row["event_type"] for row in done] == [triage.LATE_PAYMENT]
    second = source.run_once(
        client=FeedClient(pages),
        store=store,
        driver_ctx=driver_ctx(rows),
        now=now + timedelta(minutes=15),
        since=since,
    )
    assert second["would_file_count"] == 0
    assert any(row["reason"] == "api_already_filed" for row in second["results"])
    assert len(ctx.discussion_client._urlopen.posts_to("/notes")) == 1
    assert fired == []


def test_fifteen_minute_past_due_files_once_on_the_flip_not_the_due_date(tmp_path, monkeypatch):
    fired: list[dict] = []
    monkeypatch.setattr(
        driver.zapier_tasks,
        "fire_task",
        lambda payload, *, dry_run=False: fired.append(payload) or {"ok": True},
    )
    now = datetime(2026, 10, 4, 16, 0, tzinfo=timezone.utc)
    since = now - timedelta(minutes=15)
    flip = "2026-10-04T15:50:00Z"
    program = _program(1, "payment_overdue", flip)
    invoice = {
        "id": _iid(11),
        "program_id": _pid(1),
        "status": "overdue",
        "due_date": "2026-10-03",
        "invoice_number": "INV-3003",
        "policy_number": POLICY,
        "total_amount_cents": 41210,
    }
    store = source.EventKeyStore(tmp_path / "events.db")
    seen = source.notices_from_snapshot(
        programs=[program],
        loans=[],
        invoices=[invoice],
        payouts=[],
        store=store,
        since=since,
        now=now,
    )
    late = [item for item in seen if item.event_type == triage.LATE_PAYMENT]
    assert len(late) == 1
    assert late[0].occurred_at == flip
    assert late[0].invoice_id == _iid(11)
    assert source.make_event_key(_pid(1), triage.LATE_PAYMENT, flip) in late[0].alias_keys

    pages = {
        source.FEED_PROGRAMS: {"data": [program], "meta": {"next": None}},
        source.FEED_LOANS: {"data": [], "meta": {"next": None}},
        source.FEED_INVOICES: {"data": [invoice], "meta": {"next": None}},
        source.FEED_PAYOUTS: {"data": [], "meta": {"next": None}},
    }
    monkeypatch.setenv(source.LIVE_ENV, "1")
    rows = [{"discussionId": "d-pay", "title": "Ascend - Payments"}]
    ctx = driver_ctx(rows)
    first = source.run_once(
        client=FeedClient(pages),
        store=store,
        driver_ctx=ctx,
        now=now,
        since=since,
    )
    done = [row for row in first["results"] if row["status"] == "done"]
    assert [row["event_type"] for row in done] == [triage.LATE_PAYMENT]
    assert first["would_file_count"] == 1
    second = source.run_once(
        client=FeedClient(pages),
        store=store,
        driver_ctx=driver_ctx(rows),
        now=now + timedelta(minutes=15),
        since=since,
    )
    assert second["would_file_count"] == 0
    assert any(row["reason"] == "api_already_filed" for row in second["results"])
    assert len(ctx.discussion_client._urlopen.posts_to("/notes")) == 1
    assert fired == []


def test_collapse_keeps_the_in_window_flip_when_the_invoice_time_is_outside():
    since = datetime(2026, 10, 4, 15, 45, tzinfo=timezone.utc)
    flip = "2026-10-04T15:50:00Z"
    program_notice = source._program_notices(
        _program(1, "payment_overdue", flip),
        [],
        store=None,
        persist=False,
    )[0]
    invoice_notice = source._invoice_notice(
        {
            "id": _iid(11),
            "program_id": _pid(1),
            "status": "overdue",
            "due_date": "2026-10-03",
            "invoice_number": "INV-3003",
        },
        _program(1, "payment_overdue", flip),
    )
    assert invoice_notice is not None
    invoice_notice.occurred_at = "2026-10-03"
    merged = source._collapse([program_notice, invoice_notice], since=since)
    assert len(merged) == 1
    assert merged[0].invoice_id == _iid(11)
    assert merged[0].occurred_at == flip


def test_past_due_email_dedupes_on_program_and_insured_without_invoice_number(tmp_path):
    store = source.EventKeyStore(tmp_path / "events.db")
    flip = "2026-10-04T15:50:00Z"
    program = _program(1, "payment_overdue", flip, loan_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    notice = source.notices_from_snapshot(
        programs=[program],
        loans=[],
        invoices=[
            {
                "id": _iid(11),
                "program_id": _pid(1),
                "status": "overdue",
                "due_date": "2026-10-03",
                "invoice_number": "INV-3003",
            }
        ],
        payouts=[],
        store=store,
    )[0]
    assert notice.occurred_at == flip
    store.record_filed(notice)
    body = (
        f"This policy has a past-due payment of $412.10 which was due on 10/03/2026.\n"
        f"Customer {INSURED}\n"
        f"https://dashboard.useascend.com/programs/{_pid(1)}\n"
    )
    assert "Invoice No." not in body
    inside = "2026-10-08T15:50:00Z"
    assert source.email_covered_by_api(
        program_id=_pid(1),
        notice_type=triage.LATE_PAYMENT,
        subject=f"Past due payment for {INSURED}",
        body=body,
        internal_date=inside,
        store=store,
    ) == notice.event_key
    outside = "2026-10-08T16:00:00Z"
    assert source.email_covered_by_api(
        program_id=_pid(1),
        notice_type=triage.LATE_PAYMENT,
        subject=f"Past due payment for {INSURED}",
        body=body,
        internal_date=outside,
        store=store,
    ) == ""
    assert source.email_covered_by_api(
        program_id=_pid(2),
        notice_type=triage.LATE_PAYMENT,
        subject=f"Past due payment for {INSURED}",
        body=body,
        internal_date=inside,
        store=store,
    ) == ""
    assert source.email_covered_by_api(
        program_id=_pid(1),
        notice_type=triage.LATE_PAYMENT,
        subject=f"Payment failed for {INSURED}",
        body="We couldn't process your payment of $412.10.\n" + body,
        internal_date=inside,
        store=store,
    ) == ""
    loan_body = (
        f"Customer Other Hauling LLC\n"
        f"https://dashboard.useascend.com/loans/cccccccc-cccc-4ccc-8ccc-cccccccccccc\n"
    )
    assert source.email_covered_by_api(
        program_id="",
        notice_type=triage.LATE_PAYMENT,
        subject="Past due payment for Other Hauling LLC",
        body=loan_body,
        internal_date=inside,
        store=store,
    ) == notice.event_key
    assert source.email_covered_by_api(
        program_id="",
        notice_type=triage.LATE_PAYMENT,
        subject=f"Past due payment for {INSURED}",
        body=f"Customer {INSURED}\n",
        internal_date=inside,
        store=store,
    ) == notice.event_key


def test_unmatched_applicant_is_one_ask_list_and_is_not_filed(tmp_path, monkeypatch):
    monkeypatch.delenv(source.LIVE_ENV, raising=False)

    class NoMatch:
        def search_policy_by_number(self, policy_number):
            return {"data": [], "totalSize": 0}

        def search_applicants_by_name(self, name):
            return {"data": [], "totalSize": 0}

        def search_applicants_by_email(self, email):
            return {"data": [], "totalSize": 0}

        def search_applicants_by_phone(self, phone):
            return {"data": [], "totalSize": 0}

    now = datetime(2026, 10, 4, 16, 0, tzinfo=timezone.utc)
    program = _program(1, "payment_overdue", "2026-10-04T15:50:00Z")
    invoice = {
        "id": _iid(11),
        "program_id": _pid(1),
        "status": "overdue",
        "due_date": "2026-10-03",
        "invoice_number": "INV-3003",
        "policy_number": POLICY,
    }
    pages = {
        source.FEED_PROGRAMS: {"data": [program], "meta": {"next": None}},
        source.FEED_LOANS: {"data": [], "meta": {"next": None}},
        source.FEED_INVOICES: {"data": [invoice], "meta": {"next": None}},
        source.FEED_PAYOUTS: {"data": [], "meta": {"next": None}},
    }
    ctx = driver_ctx()
    ctx.ezlynx_client = NoMatch()
    summary = source.run_once(
        client=FeedClient(pages),
        store=source.EventKeyStore(tmp_path / "events.db"),
        driver_ctx=ctx,
        now=now,
        since=now - timedelta(minutes=15),
    )
    assert summary["would_file_count"] == 0
    assert summary["errors"] == []
    assert len(summary["ask"]) == 1
    assert summary["ask"][0]["event_type"] == triage.LATE_PAYMENT
    assert summary["ask"][0]["insured_name"] == INSURED
    assert summary["ask"][0]["reason"].startswith("applicant_unresolved")
    assert ctx.discussion_client._urlopen.posts_to("/notes") == []


def test_live_unreadable_note_bodies_do_not_post(tmp_path, monkeypatch):
    """POST /notes returns no note id; the duplicate check reads note bodies.
    When that read fails, the live path holds and the key stays unfiled."""
    fired = []
    monkeypatch.setattr(
        driver.zapier_tasks,
        "fire_task",
        lambda payload, *, dry_run=False: fired.append(payload) or {"ok": True},
    )
    programs = [_program(3, "cancelled", "2026-09-24T12:36:00Z", balance_cents=41210)]
    loans = [_loan(3, 3, "canceled", "2026-09-24T12:36:00Z")]
    pages = {
        source.FEED_PROGRAMS: {"data": programs, "meta": {"next": None}},
        source.FEED_LOANS: {"data": loans, "meta": {"next": None}},
        source.FEED_INVOICES: {"data": [], "meta": {"next": None}},
        source.FEED_PAYOUTS: {"data": [], "meta": {"next": None}},
    }
    store = source.EventKeyStore(tmp_path / "events.db")
    rows = [{"discussionId": "d1", "title": "Ascend - Cancellation Notices"}]
    monkeypatch.setenv(source.LIVE_ENV, "1")
    ctx = driver_ctx(rows)

    def _fail(_discussion_id):
        raise discussions.DiscussionApiError(503, "with-notes unavailable")

    ctx.discussion_client.get_discussion_with_notes = _fail
    live = source.run_once(
        client=FeedClient(pages),
        store=store,
        driver_ctx=ctx,
        now=datetime(2026, 10, 4, 16, 0, tzinfo=timezone.utc),
        since=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    result = live["results"][0]
    assert result["status"] == "skipped"
    assert result["reason"].startswith("existing_note_unreadable:")
    assert ctx.discussion_client._urlopen.posts_to("/notes") == []
    assert not store.is_filed(result["event_key"])
    assert fired == []
