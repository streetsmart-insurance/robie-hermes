"""Unmatched Ascend notices: store, near match, and the accounting digest.

Synthetic fixtures only. No network, no EZLynx write, no email send.
"""

from __future__ import annotations

import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from robie_job_engine import ascend_api_notice_source as source
from robie_job_engine import ascend_notice_driver as driver
from robie_job_engine import ascend_notice_triage as triage
from robie_job_engine import ascend_unmatched_digest as digest
from robie_job_engine.ezlynx_api import EzlynxApiClient, EzlynxApiConfig

ROOT = Path(__file__).resolve().parents[1]
EASTERN = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)


def _helpers():
    path = Path(__file__).with_name("test_ascend_api_notice_source.py")
    spec = importlib.util.spec_from_file_location("ascend_notice_source_helpers", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HELPERS = _helpers()


class _Bytes:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload


def _api_config() -> EzlynxApiConfig:
    return EzlynxApiConfig(
        token_endpoint="https://app.uatezlynx.com/auth/connect/token",
        document_base_url="https://app.uatezlynx.com/DocumentApi/",
        client_id="cid",
        client_secret="csecret",
        username="api_user",
        integration_group_id="183",
        scope="PolicyApi openid",
    )


class TrapEzlynx:
    """Exact search misses. Any other call, including a write, is a failure."""

    def __init__(self):
        self.calls: list[str] = []

    def search_policy_by_number(self, policy_number):
        self.calls.append("search_policy_by_number")
        return {"data": [], "totalSize": 0}

    def search_applicants_by_name(self, name):
        self.calls.append("search_applicants_by_name")
        return {"data": [], "totalSize": 0}

    def search_applicants_by_email(self, email):
        self.calls.append("search_applicants_by_email")
        return {"data": [], "totalSize": 0}

    def search_applicants_by_phone(self, phone):
        self.calls.append("search_applicants_by_phone")
        return {"data": [], "totalSize": 0}

    def __getattr__(self, name):
        raise AssertionError(f"near match must not call {name}")


class DiscussionTrap:
    def __getattr__(self, name):
        raise AssertionError(f"near match must not call discussion {name}")


def _index_row(number: str, name: str, applicant: str) -> dict[str, str]:
    return {
        "policy_key": digest.policy_compare_key(number),
        "policy_number": number,
        "applicant_name": name,
        "applicant_id": applicant,
    }


def _save_index(store: source.EventKeyStore, rows: list[dict[str, str]]) -> None:
    store.save_policy_index(
        rows,
        built_at="2026-10-05T12:00:00Z",
        calls=1,
        total_size=len(rows),
        pages=1,
    )


def _unmatched_pages(amount_cents: int | None = 41210, policy: str | None = HELPERS.POLICY):
    program = HELPERS._program(1, "payment_overdue", "2026-10-05T13:50:00Z")
    if policy is None:
        program["policy_number"] = ""
        program.pop("billables", None)
    invoice = {
        "id": HELPERS._iid(11),
        "program_id": HELPERS._pid(1),
        "status": "overdue",
        "due_date": "2026-10-04",
        "invoice_number": "INV-3003",
        "total_amount_cents": amount_cents,
    }
    if policy is not None:
        invoice["policy_number"] = policy
    return {
        source.FEED_PROGRAMS: {"data": [program], "meta": {"next": None}},
        source.FEED_LOANS: {"data": [], "meta": {"next": None}},
        source.FEED_INVOICES: {"data": [invoice], "meta": {"next": None}},
        source.FEED_PAYOUTS: {"data": [], "meta": {"next": None}},
    }


def _run_unmatched(tmp_path, monkeypatch, ezlynx, *, pages=None, now=NOW):
    monkeypatch.delenv(source.LIVE_ENV, raising=False)
    ctx = HELPERS.driver_ctx()
    ctx.ezlynx_client = ezlynx
    ctx.discussion_client = DiscussionTrap()
    store = source.EventKeyStore(tmp_path / "ascend-api" / "events.db")
    summary = source.run_once(
        client=HELPERS.FeedClient(pages or _unmatched_pages()),
        store=store,
        driver_ctx=ctx,
        now=now,
        since=now - timedelta(minutes=15),
    )
    return store, summary


def test_one_character_rules():
    assert digest.one_character_apart("HO998877", "HO998878")
    assert digest.one_character_apart("HO998877", "HO99887")
    assert digest.one_character_apart("HO998877", "HO9988771")
    assert digest.one_character_apart("ABCD", "ACBD")
    assert not digest.one_character_apart("HO998877", "HO998877")
    assert not digest.one_character_apart("HO998877", "HO998899")
    assert digest.policy_compare_key("ho 998-877") == "HO998877"


def test_near_match_requires_one_candidate_and_the_same_name():
    close = _index_row("HO-998878", "Fixture Hauling", "220250093")
    other_name = _index_row("HO-998878", "Other Hauling LLC", "220250094")
    second = _index_row("HO-998879", "Fixture Hauling", "220250095")
    assert digest.suggest_near_match(
        [close], ["HO-998877"], "Fixture Hauling LLC"
    ) == digest.NearMatch("Fixture Hauling", "HO-998878", "220250093")
    assert digest.suggest_near_match([other_name], ["HO-998877"], "Fixture Hauling LLC") is None
    assert digest.suggest_near_match([close, second], ["HO-998877"], "Fixture Hauling LLC") is None
    assert digest.suggest_near_match([close], ["HO-998877"], "Somebody Else LLC") is None


def test_unmatched_row_is_stored_and_a_later_filing_resolves_it(tmp_path, monkeypatch):
    monkeypatch.setattr(
        driver.zapier_tasks,
        "fire_task",
        lambda payload, *, dry_run=False: {"ok": True},
    )
    store, summary = _run_unmatched(tmp_path, monkeypatch, TrapEzlynx())
    assert summary["ask"][0]["unmatched_reason"] == driver.POLICY_OUTCOME_NOT_IN_EZLYNX
    rows = store.list_unmatched()
    assert len(rows) == 1
    assert rows[0]["resolved_at"] in (None, "")
    assert rows[0]["insured_name"] == HELPERS.INSURED
    assert rows[0]["program_id"] == HELPERS._pid(1)
    assert rows[0]["policy_numbers"] == [HELPERS.POLICY]
    assert rows[0]["notice_type"] == triage.LATE_PAYMENT
    assert rows[0]["amount_cents"] == 41210
    assert rows[0]["reason"] == "policy not in EZLynx"
    first_seen = rows[0]["first_seen"]

    again, _summary = _run_unmatched(
        tmp_path, monkeypatch, TrapEzlynx(), now=NOW + timedelta(minutes=15)
    )
    kept = again.list_unmatched()
    assert kept[0]["first_seen"] == first_seen
    assert kept[0]["last_seen"] >= first_seen

    monkeypatch.setenv(source.LIVE_ENV, "1")
    ctx = HELPERS.driver_ctx([{"discussionId": "d-pay", "title": "Ascend - Payments"}])
    filed = source.run_once(
        client=HELPERS.FeedClient(_unmatched_pages()),
        store=again,
        driver_ctx=ctx,
        now=NOW + timedelta(minutes=20),
        since=NOW - timedelta(minutes=15),
    )
    assert any(row["status"] == "done" for row in filed["results"])
    resolved = again.list_unmatched()
    assert resolved[0]["resolved_at"]
    sent: list[dict] = []

    def _mailer(*, to, subject, text_body):
        sent.append({"to": to, "subject": subject, "body": text_body})
        return {"kind": "gmail"}

    result = digest.run_digest(stores=[again], now=NOW, live=True, mailer=_mailer, refresh=False)
    assert result["empty"] is True
    assert sent == []


def test_near_match_is_suggestion_text_and_writes_nothing(tmp_path, monkeypatch):
    ezlynx = TrapEzlynx()
    store = source.EventKeyStore(tmp_path / "ascend-api" / "events.db")
    _save_index(store, [_index_row("HO-998878", "Fixture Hauling", "220250093")])
    monkeypatch.delenv(source.LIVE_ENV, raising=False)
    ctx = HELPERS.driver_ctx()
    ctx.ezlynx_client = ezlynx
    ctx.discussion_client = DiscussionTrap()
    summary = source.run_once(
        client=HELPERS.FeedClient(_unmatched_pages()),
        store=store,
        driver_ctx=ctx,
        now=NOW,
        since=NOW - timedelta(minutes=15),
    )
    assert summary["would_file_count"] == 0
    assert "search_policy_page" not in ezlynx.calls
    row = store.list_unmatched()[0]
    assert row["suggestion_client"] == "Fixture Hauling"
    assert row["suggestion_policy"] == "HO-998878"
    assert row["resolved_at"] in (None, "")


def test_different_name_and_two_candidates_store_no_suggestion(tmp_path, monkeypatch):
    different = source.EventKeyStore(tmp_path / "different" / "events.db")
    _save_index(different, [_index_row("HO-998878", "Other Hauling LLC", "220250094")])
    monkeypatch.delenv(source.LIVE_ENV, raising=False)
    ctx = HELPERS.driver_ctx()
    ctx.ezlynx_client = TrapEzlynx()
    ctx.discussion_client = DiscussionTrap()
    source.run_once(
        client=HELPERS.FeedClient(_unmatched_pages()),
        store=different,
        driver_ctx=ctx,
        now=NOW,
        since=NOW - timedelta(minutes=15),
    )
    assert different.list_unmatched()[0]["suggestion_client"] == ""

    both = source.EventKeyStore(tmp_path / "both" / "events.db")
    _save_index(
        both,
        [
            _index_row("HO-998878", "Fixture Hauling", "220250093"),
            _index_row("HO-998879", "Fixture Hauling", "220250095"),
        ],
    )
    ctx = HELPERS.driver_ctx()
    ctx.ezlynx_client = TrapEzlynx()
    ctx.discussion_client = DiscussionTrap()
    source.run_once(
        client=HELPERS.FeedClient(_unmatched_pages()),
        store=both,
        driver_ctx=ctx,
        now=NOW,
        since=NOW - timedelta(minutes=15),
    )
    row = both.list_unmatched()[0]
    assert row["suggestion_client"] == ""
    assert row["suggestion_policy"] == ""


def test_no_policy_number_and_incomplete_search_are_stored(tmp_path, monkeypatch):
    store, summary = _run_unmatched(
        tmp_path, monkeypatch, TrapEzlynx(), pages=_unmatched_pages(policy=None)
    )
    assert summary["ask"][0]["unmatched_reason"] == driver.POLICY_OUTCOME_NO_NUMBER
    assert store.list_unmatched()[0]["policy_numbers"] == []

    class Incomplete:
        def search_policy_by_number(self, policy_number):
            return {"data": [{"PolicyNumber": policy_number}], "totalSize": 5}

        def search_applicants_by_name(self, name):
            return {"data": [], "totalSize": 0}

        def search_applicants_by_email(self, email):
            return {"data": [], "totalSize": 0}

        def search_applicants_by_phone(self, phone):
            return {"data": [], "totalSize": 0}

        def __getattr__(self, name):
            raise AssertionError(name)

    store, summary = _run_unmatched(tmp_path, monkeypatch, Incomplete())
    assert summary["ask"][0]["unmatched_reason"] == driver.POLICY_OUTCOME_INCOMPLETE
    reasons = {row["reason"] for row in store.list_unmatched()}
    assert "incomplete search" in reasons


def test_empty_list_and_dry_run_send_nothing(tmp_path, capsys):
    store = source.EventKeyStore(tmp_path / "ascend-api" / "events.db")
    sent: list[dict] = []

    def _mailer(*, to, subject, text_body):
        sent.append({"to": to})
        return {"kind": "gmail"}

    empty = digest.run_digest(stores=[store], now=NOW, live=True, mailer=_mailer, refresh=False)
    assert empty["empty"] is True
    assert empty["sent"] is False
    assert sent == []

    notice = source._invoice_notice(
        {
            "id": HELPERS._iid(11),
            "program_id": HELPERS._pid(1),
            "status": "overdue",
            "due_date": "2026-10-04",
            "invoice_number": "INV-3003",
            "policy_number": HELPERS.POLICY,
            "total_amount_cents": 41210,
        },
        HELPERS._program(1, "payment_overdue", "2026-10-05T13:50:00Z"),
    )
    assert notice is not None
    store.upsert_unmatched(
        notice,
        reason=driver.POLICY_OUTCOME_NOT_IN_EZLYNX,
        seen_at="2026-10-05T13:50:00Z",
        suggestion_client="Fixture Hauling",
        suggestion_policy="HO-998878",
        update_suggestion=True,
    )
    dry = digest.run_digest(stores=[store], now=NOW, live=False, mailer=_mailer, refresh=False)
    assert dry["printed"] is True
    assert dry["sent"] is False
    assert sent == []
    printed = capsys.readouterr().out
    assert "Ascend notices Robie couldn't match (1)" in printed
    assert "Did you mean Fixture Hauling (policy HO-998878)?" in printed
    assert "event_key" not in printed
    assert "late_payment" not in printed
    assert "policy not in EZLynx" not in printed


def test_aged_items_are_mentioned_once(tmp_path, capsys):
    store = source.EventKeyStore(tmp_path / "ascend-api" / "events.db")
    notice = source._invoice_notice(
        {
            "id": HELPERS._iid(11),
            "program_id": HELPERS._pid(1),
            "status": "overdue",
            "invoice_number": "INV-OLD",
            "policy_number": HELPERS.POLICY,
        },
        HELPERS._program(1, "payment_overdue", "2026-09-01T00:00:00Z"),
    )
    assert notice is not None
    store.upsert_unmatched(
        notice,
        reason=driver.POLICY_OUTCOME_NOT_IN_EZLYNX,
        seen_at="2026-09-01T00:00:00Z",
    )
    first = digest.run_digest(stores=[store], now=NOW, live=False, mailer=None, refresh=False)
    assert "Still unmatched after 14 days:" in first["body"]
    assert first["open_count"] == 0
    capsys.readouterr()
    second = digest.run_digest(
        stores=[store],
        now=NOW + timedelta(days=1),
        live=True,
        mailer=lambda **_kwargs: {},
        refresh=False,
    )
    assert second["empty"] is True
    assert second["sent"] is False
    assert "Still unmatched" not in capsys.readouterr().out


def test_sample_digest_is_plain_english():
    subject, body = digest.sample_digest()
    assert subject == "Ascend notices Robie couldn't match (3)"
    assert "Fixture Hauling LLC, past-due payment, $412.10, policy HO-998877." in body
    assert "That policy number is not in EZLynx." in body
    assert "Did you mean Fixture Hauling (policy HO-998878)?" in body
    assert "Northwind Trucking Inc, cancellation, $1,200.00, policy CA-100200." in body
    assert "More than one EZLynx client matched." in body
    assert "Bare Insured, intent to cancel." in body
    assert "Ascend did not include a policy number." in body
    assert "Still unmatched after 14 days: Old Mill LLC, payment." in body
    assert body.endswith(
        "Correct the policy number in EZLynx or Ascend, and Robie files it automatically on its next run."
    )
    for forbidden in ("event_key", "program_id", "late_payment", "applicant_id", "unmatched_reason"):
        assert forbidden not in subject
        assert forbidden not in body


def test_store_permissions_stay_on_the_ascend_api_folder(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    os.chmod(data, 0o770)
    nested = data / "ascend-api" / "events.db"
    store = source.EventKeyStore(nested)
    notice = source._invoice_notice(
        {
            "id": HELPERS._iid(11),
            "program_id": HELPERS._pid(1),
            "status": "overdue",
            "invoice_number": "INV-3003",
            "policy_number": HELPERS.POLICY,
        },
        HELPERS._program(1, "payment_overdue", "2026-10-05T13:50:00Z"),
    )
    assert notice is not None
    store.upsert_unmatched(
        notice,
        reason="policy not in EZLynx",
        seen_at="2026-10-05T13:50:00Z",
    )
    _save_index(store, [_index_row("HO-998878", "Fixture Hauling", "220250093")])
    state = data / "ascend-api" / "unmatched-digest-last-run.json"
    digest.write_last_run({"exit_code": 0, "run_at": "2026-10-05T12:30:00Z"}, state)
    assert (data.stat().st_mode & 0o777) == 0o770
    assert (nested.parent.stat().st_mode & 0o777) == 0o755
    assert (nested.stat().st_mode & 0o777) == 0o644
    assert (state.stat().st_mode & 0o777) == 0o644


def test_policy_page_search_is_a_bounded_get_without_name_filters():
    seen: dict[str, object] = {}

    def fake_urlopen(url, *, data, headers, timeout):
        if "connect/token" in url:
            return _Bytes(json.dumps({"access_token": "tok", "expires_in": 3600}).encode())
        seen["url"] = url
        seen["data"] = data
        return _Bytes(
            json.dumps(
                {"results": [], "pageIndex": 1, "pageSize": 100, "totalSize": 38000}
            ).encode()
        )

    client = EzlynxApiClient(_api_config(), urlopen=fake_urlopen)
    client.search_policy_page(1, 100)
    url = str(seen["url"])
    assert "/PolicyApi/policy/v1/search" in url
    assert "pageIndex=1" in url
    assert "pageSize=100" in url
    assert "ApplicantName" not in url
    assert "Email=" not in url
    assert "PhoneNumber" not in url
    assert seen["data"] is None


def test_index_refresh_counts_calls_and_refuses_a_repeated_page(tmp_path):
    store = source.EventKeyStore(tmp_path / "ascend-api" / "events.db")
    def _page(rows: list[dict]) -> dict:
        return {"status": "success", "data": {"results": rows, "totalSize": 3}}

    pages = {
        1: _page(
            [
                {"PolicyNumber": "HO-1", "ApplicantId": "1", "ApplicantName": "One"},
                {"PolicyNumber": "HO-2", "ApplicantId": "2", "ApplicantName": "Two"},
            ]
        ),
        2: _page(
            [
                {"PolicyNumber": "HO-3", "ApplicantId": "3", "ApplicantName": "Three"},
            ]
        ),
    }

    class Pager:
        def __init__(self):
            self.calls: list[tuple[int, int]] = []
            self.writes: list[str] = []

        def search_policy_page(self, page_index, page_size):
            self.calls.append((page_index, page_size))
            return pages[page_index]

        def create_policy(self, **_kwargs):
            self.writes.append("create_policy")
            raise AssertionError("index refresh must not create a policy")

    pager = Pager()
    report = digest.refresh_policy_index(
        pager, [store], now=NOW, page_size=2, max_pages=5, pause_s=0, sleep=lambda _seconds: None
    )
    assert report["calls"] == 2
    assert report["complete"] is True
    assert report["saved"] is True
    assert pager.writes == []
    rows, meta = store.policy_index()
    assert len(rows) == 3
    assert meta["calls"] == 2

    class Repeat:
        def search_policy_page(self, page_index, page_size):
            return {
                "status": "success",
                "data": {
                    "results": [
                        {"PolicyNumber": "HO-1", "ApplicantId": "1", "ApplicantName": "One"},
                    ],
                    "totalSize": 38000,
                },
            }

        def create_policy(self, **_kwargs):
            raise AssertionError("write")

    fresh = source.EventKeyStore(tmp_path / "ascend-api" / "empty.db")
    repeated = digest.refresh_policy_index(
        Repeat(),
        [fresh],
        now=NOW,
        page_size=1,
        max_pages=5,
        pause_s=0,
        sleep=lambda _seconds: None,
    )
    assert repeated["complete"] is False
    assert repeated["calls"] == 2
    assert fresh.policy_index()[0] == []


def test_digest_health_alerts_on_failure_and_a_missed_business_day(tmp_path):
    path = tmp_path / "unmatched-digest-last-run.json"
    missing = digest.evaluate_digest_health(path, now=NOW)
    assert missing["ok"] is True
    assert missing["installed"] is False

    monday_late = datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)  # 10:00 ET
    monday_early = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)  # 08:00 ET
    saturday = datetime(2026, 10, 3, 15, 0, tzinfo=timezone.utc)
    path.write_text(
        json.dumps({"run_at": "2026-10-02T12:30:00Z", "exit_code": 0}),
        encoding="utf-8",
    )
    assert digest.evaluate_digest_health(path, now=saturday)["ok"] is True
    assert digest.evaluate_digest_health(path, now=monday_early)["ok"] is True
    missed = digest.evaluate_digest_health(path, now=monday_late)
    assert missed["ok"] is False
    assert "did not run today" in missed["detail"]

    path.write_text(
        json.dumps({"run_at": "2026-10-05T12:30:00Z", "exit_code": 1}),
        encoding="utf-8",
    )
    failed = digest.evaluate_digest_health(path, now=monday_early)
    assert failed["ok"] is False
    assert "failed" in failed["detail"]

    path.write_text(
        json.dumps({"run_at": "2026-10-05T12:30:00Z", "exit_code": 0}),
        encoding="utf-8",
    )
    assert digest.evaluate_digest_health(path, now=monday_late)["ok"] is True

    script = ROOT / "scripts" / "robie_health_check.py"
    spec = importlib.util.spec_from_file_location("robie_health_check_digest", script)
    health = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(health)
    assert "ascend_unmatched_digest" in health.PROD_ONLY_CHECK_NAMES
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("ASCEND_UNMATCHED_DIGEST_STATE", str(path))
        path.write_text(
            json.dumps({"run_at": "2026-10-02T12:30:00Z", "exit_code": 0}),
            encoding="utf-8",
        )
        # The health wrapper uses the clock of the machine. Pin the evaluator
        # by calling it directly above; the wrapper must surface a recorded failure.
        path.write_text(
            json.dumps({"run_at": "2026-10-05T12:30:00Z", "exit_code": 1}),
            encoding="utf-8",
        )
        ok, detail, extra = health.check_ascend_unmatched_digest()
    assert ok is False
    assert "failed" in detail
    assert extra["installed"] is True


def test_live_digest_sends_once_through_the_mailer(tmp_path):
    store = source.EventKeyStore(tmp_path / "ascend-api" / "events.db")
    notice = source._invoice_notice(
        {
            "id": HELPERS._iid(11),
            "program_id": HELPERS._pid(1),
            "status": "overdue",
            "invoice_number": "INV-3003",
            "policy_number": HELPERS.POLICY,
            "total_amount_cents": 41210,
        },
        HELPERS._program(1, "payment_overdue", "2026-10-05T13:50:00Z"),
    )
    assert notice is not None
    store.upsert_unmatched(
        notice,
        reason=driver.POLICY_OUTCOME_MULTIPLE,
        seen_at="2026-10-05T13:50:00Z",
    )
    sent: list[str] = []

    def _mailer(*, to, subject, text_body):
        sent.append(to[0])
        assert to == [digest.ACCOUNTING_TO]
        assert subject == "Ascend notices Robie couldn't match (1)"
        assert "More than one EZLynx client matched." in text_body
        return {"kind": "gmail", "message_id": "m1"}

    result = digest.run_digest(stores=[store], now=NOW, live=True, mailer=_mailer, refresh=False)
    assert result["sent"] is True
    assert sent == [digest.ACCOUNTING_TO]
