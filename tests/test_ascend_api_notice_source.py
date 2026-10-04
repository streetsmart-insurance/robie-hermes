"""API-primary Ascend notice source. Fixtures are synthetic. No network."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

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
    assert past_due.event_key == source.make_event_key(_pid(1), triage.LATE_PAYMENT, _iid(11))
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
    assert source.email_covered_by_api(
        program_id=_pid(1),
        notice_type=triage.LATE_PAYMENT,
        subject=f"Payment failed for {INSURED}",
        body="We couldn't process your payment of $412.10.\n" + body,
        internal_date=email_at,
        store=store,
    ) == ""
    for notice_type in (triage.UNDERWRITING, triage.RETURN_PREMIUM, triage.REFUND):
        assert source.email_covered_by_api(
            program_id=_pid(1),
            notice_type=notice_type,
            subject="ignored",
            body=body,
            internal_date=email_at,
            store=store,
        ) == ""
    late_email = "2026-09-30T12:01:00Z"
    assert source.email_covered_by_api(
        program_id=_pid(1),
        notice_type=triage.LATE_PAYMENT,
        subject=f"Past due payment for {INSURED}",
        body=body,
        internal_date=late_email,
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
    assert failed_result.reason != "api_already_filed"
    assert failed_result.detail.get("notice_type") == triage.LATE_PAYMENT


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
    assert "Environment=ROBIE_EZLYNX_WRITE_SCOPE=all" in service
    assert "Environment=ROBIE_PLAYGROUND=1" in service
    assert "Environment=ROBIE_EZLYNX_WRITE_SCOPE=all" in drop_in
    text = (ROOT / "robie_job_engine/ascend_api_notice_source.py").read_text(encoding="utf-8")
    assert "HTTPServer" not in text
    assert "smtplib" not in text
    assert "def events_from_webhook" in text


def test_main_dry_run_prints_the_would_file_line(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(source.LIVE_ENV, raising=False)
    monkeypatch.setenv(source.DB_ENV, str(tmp_path / "events.db"))
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
