"""Unmatched Ascend notices: store, near match, and the accounting digest.

Synthetic fixtures only. No network, no EZLynx write, no email send.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
from urllib import error as urlerror
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from robie_job_engine import ascend_api_notice_source as source
from robie_job_engine import ascend_notice_driver as driver
from robie_job_engine import ascend_notice_triage as triage
from robie_job_engine import ascend_unmatched_digest as digest
from robie_job_engine.ezlynx_api import EzlynxApiClient, EzlynxApiConfig, EzlynxApiError

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
    other = _index_row("HO-998879", "Other Hauling LLC", "220250094")
    assert digest.suggest_near_match([close, other], ["HO-998877"], "Fixture Hauling LLC") is None


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


def test_aged_items_are_mentioned_once_and_only_after_a_live_send(tmp_path, capsys):
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
    assert store.list_unmatched()[0]["aged_out_notified_at"] in (None, "")
    capsys.readouterr()
    again = digest.run_digest(stores=[store], now=NOW, live=False, mailer=None, refresh=False)
    assert "Still unmatched after 14 days:" in again["body"]
    assert store.list_unmatched()[0]["aged_out_notified_at"] in (None, "")

    def _refuse(*, to, subject, text_body):
        raise ValueError("duplicate verification email refused: an identical message is already in Sent")

    refused = digest.run_digest(stores=[store], now=NOW, live=True, mailer=_refuse, refresh=False)
    assert refused["sent"] is False
    assert refused["error"] == "ValueError"
    assert store.list_unmatched()[0]["aged_out_notified_at"] in (None, "")
    sent = digest.run_digest(
        stores=[store],
        now=NOW,
        live=True,
        mailer=lambda **_kwargs: {"kind": "gmail"},
        refresh=False,
    )
    assert sent["sent"] is True
    assert "for October 5, 2026" in sent["subject"]
    assert store.list_unmatched()[0]["aged_out_notified_at"]
    following = digest.run_digest(
        stores=[store],
        now=NOW + timedelta(days=1),
        live=True,
        mailer=lambda **_kwargs: {"kind": "gmail"},
        refresh=False,
    )
    assert following["empty"] is True
    assert following["sent"] is False


def test_sample_digest_is_plain_english():
    subject, body = digest.sample_digest()
    assert subject == "Ascend notices Robie couldn't match (3) for October 5, 2026"
    assert "Fixture Hauling LLC, past-due payment, $412.10, policy HO-998877." in body
    assert "That policy number is not in EZLynx." in body
    assert "Did you mean Fixture Hauling (policy HO-998878)?" in body
    assert "Northwind Trucking Inc, cancellation, $1,200.00, policy CA-100200." in body
    assert "More than one EZLynx client matched." in body
    assert "Bare Insured, intent to cancel." in body
    assert "Ascend did not include a policy number." in body
    assert "Still unmatched after 14 days: Old Mill LLC, payment." in body
    assert body.endswith(
        "Fix the policy number in EZLynx and it drops off this list once Robie matches it and files the note. "
        "The policy number is as Ascend sent it."
    )
    assert "or Ascend" not in body
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


def test_digest_health_alerts_on_failure_and_a_missed_business_day(tmp_path, monkeypatch):
    path = tmp_path / "unmatched-digest-last-run.json"
    marker = tmp_path / "unmatched-digest-installed"
    marker.write_text("enabled\n", encoding="utf-8")
    monkeypatch.setenv(digest.MARKER_ENV, str(marker))
    monkeypatch.setattr(digest, "timer_is_enabled", lambda: False)
    missing_early = digest.evaluate_digest_health(path, now=datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc))
    assert missing_early["ok"] is True
    assert missing_early["installed"] is True
    missing_late = digest.evaluate_digest_health(path, now=NOW)
    assert missing_late["ok"] is False
    assert "did not run today" in missing_late["detail"]

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
        patch.setenv(digest.MARKER_ENV, str(marker))
        patch.setattr(digest, "timer_is_enabled", lambda: False)
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
        patch.setenv(digest.MARKER_ENV, str(tmp_path / "absent-marker"))
        quiet_ok, quiet_detail, quiet_extra = health.check_ascend_unmatched_digest()
    assert ok is False
    assert "failed" in detail
    assert extra["installed"] is True
    assert quiet_ok is True
    assert quiet_extra["installed"] is False
    assert "not installed" in quiet_detail


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
        assert subject == "Ascend notices Robie couldn't match (1) for October 5, 2026"
        assert "More than one EZLynx client matched." in text_body
        return {"kind": "gmail", "message_id": "m1"}

    result = digest.run_digest(stores=[store], now=NOW, live=True, mailer=_mailer, refresh=False)
    assert result["sent"] is True
    assert sent == [digest.ACCOUNTING_TO]


def _page(rows, total=None):
    data = {"results": rows}
    if total is not None:
        data["totalSize"] = total
    return {"status": "success", "data": data}


def test_index_counts_raw_rows_and_refuses_a_guess(tmp_path):
    store = source.EventKeyStore(tmp_path / "ascend-api" / "events.db")

    class Dropped:
        def search_policy_page(self, page_index, page_size):
            return _page(
                [
                    {"PolicyNumber": "HO-1", "ApplicantId": "1", "ApplicantName": "One"},
                    {"ApplicantName": "No policy number"},
                ],
                total=2,
            )

    covered = digest.refresh_policy_index(
        Dropped(), [store], now=NOW, page_size=2, max_pages=3, pause_s=0, sleep=lambda _s: None
    )
    assert covered["complete"] is True
    assert covered["raw_rows"] == 2
    assert covered["rows"] == 1
    assert len(store.policy_index()[0]) == 1

    class Short:
        def __init__(self):
            self.calls = 0

        def search_policy_page(self, page_index, page_size):
            self.calls += 1
            return _page(
                [{"PolicyNumber": f"HO-{page_index}", "ApplicantId": "1", "ApplicantName": "One"}]
            )

    short_store = source.EventKeyStore(tmp_path / "ascend-api" / "short.db")
    short = Short()
    guessed = digest.refresh_policy_index(
        short, [short_store], now=NOW, page_size=2, max_pages=3, pause_s=0, sleep=lambda _s: None
    )
    assert guessed["complete"] is False
    assert guessed["stopped"] == "page_cap"
    assert short.calls == 3
    assert short_store.policy_index()[0] == []

    class UntilEmpty:
        def search_policy_page(self, page_index, page_size):
            if page_index == 1:
                return _page([{"PolicyNumber": "HO-1", "ApplicantId": "1", "ApplicantName": "One"}])
            return _page([])

    empty_store = source.EventKeyStore(tmp_path / "ascend-api" / "empty-end.db")
    ended = digest.refresh_policy_index(
        UntilEmpty(),
        [empty_store],
        now=NOW,
        page_size=2,
        max_pages=5,
        pause_s=0,
        sleep=lambda _s: None,
    )
    assert ended["complete"] is True
    assert ended["stopped"] == "empty_page"
    assert len(empty_store.policy_index()[0]) == 1


def test_policy_paging_backs_off_and_reauths_once(tmp_path):
    store = source.EventKeyStore(tmp_path / "ascend-api" / "events.db")
    waits: list[float] = []

    class Slow:
        def __init__(self):
            self.calls = 0

        def search_policy_page(self, page_index, page_size):
            self.calls += 1
            if self.calls == 1:
                raise EzlynxApiError(429, "slow down")
            return _page([], total=0)

    slow = Slow()
    report = digest.refresh_policy_index(
        slow, [store], now=NOW, page_size=1, max_pages=2, pause_s=0, sleep=waits.append
    )
    assert slow.calls == 2
    assert waits == [1.0]
    assert report["complete"] is True
    assert report["calls"] == 2

    class Expired:
        def __init__(self):
            self.calls = 0
            self.cleared = 0

        def clear_cached_token(self):
            self.cleared += 1

        def search_policy_page(self, page_index, page_size):
            self.calls += 1
            if self.calls == 1:
                raise EzlynxApiError(401, "expired")
            return _page([], total=0)

    expired = Expired()
    authed = digest.refresh_policy_index(
        expired, [store], now=NOW, page_size=1, max_pages=2, pause_s=0, sleep=lambda _s: None
    )
    assert expired.cleared == 1
    assert expired.calls == 2
    assert authed["complete"] is True

    class StillExpired(Expired):
        def search_policy_page(self, page_index, page_size):
            self.calls += 1
            raise EzlynxApiError(401, "expired")

    fresh = source.EventKeyStore(tmp_path / "ascend-api" / "unauth.db")
    with pytest.raises(EzlynxApiError):
        digest.refresh_policy_index(
            StillExpired(),
            [fresh],
            now=NOW,
            page_size=1,
            max_pages=2,
            pause_s=0,
            sleep=lambda _s: None,
        )
    assert fresh.policy_index()[0] == []


def test_index_stops_at_the_time_budget(tmp_path):
    store = source.EventKeyStore(tmp_path / "ascend-api" / "events.db")
    ticks = iter([0.0, 0.0, 999.0])

    class Wide:
        def search_policy_page(self, page_index, page_size):
            return _page(
                [{"PolicyNumber": "HO-1", "ApplicantId": "1", "ApplicantName": "One"}],
                total=5000,
            )

    report = digest.refresh_policy_index(
        Wide(),
        [store],
        now=NOW,
        page_size=1,
        max_pages=50,
        pause_s=0,
        sleep=lambda _s: None,
        budget_s=10,
        clock=lambda: next(ticks),
    )
    assert report["complete"] is False
    assert report["stopped"] == "time_budget"
    assert report["saved"] is False


def test_main_writes_the_state_file_before_paging(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    monkeypatch.setenv(digest.STATE_ENV, str(state))
    monkeypatch.delenv(digest.LIVE_ENV, raising=False)
    seen: dict = {}

    def fake_run(**kwargs):
        seen["during"] = json.loads(state.read_text(encoding="utf-8"))
        return {"error": "", "dry_run": True, "sent": False, "open_count": 0}

    monkeypatch.setattr(digest, "run_digest", fake_run)
    assert digest.main(["--no-refresh"]) == 0
    assert seen["during"]["error"] == "started"
    assert seen["during"]["exit_code"] == 1
    final = json.loads(state.read_text(encoding="utf-8"))
    assert final["exit_code"] == 0
    assert final["error"] == ""


def test_store_errors_do_not_stop_the_poll(tmp_path, monkeypatch):
    def boom(*_args, **_kwargs):
        raise source.sqlite3.OperationalError("locked")

    monkeypatch.setattr(digest, "persist_unmatched_notice", boom)
    monkeypatch.setattr(digest, "resolve_unmatched_notice", boom)
    store, summary = _run_unmatched(tmp_path, monkeypatch, TrapEzlynx())
    assert summary["ask"]
    assert summary["results"]
    assert store.list_unmatched() == []


def test_a_later_match_resolves_without_a_live_file(tmp_path, monkeypatch):
    monkeypatch.delenv(source.LIVE_ENV, raising=False)
    monkeypatch.setattr(
        driver.zapier_tasks, "fire_task", lambda payload, *, dry_run=False: {"ok": True}
    )
    store, _summary = _run_unmatched(tmp_path, monkeypatch, TrapEzlynx())
    assert store.list_unmatched()[0]["resolved_at"] in (None, "")
    ctx = HELPERS.driver_ctx()
    matched = source.run_once(
        client=HELPERS.FeedClient(_unmatched_pages()),
        store=store,
        driver_ctx=ctx,
        now=NOW + timedelta(minutes=20),
        since=NOW - timedelta(hours=2),
    )
    assert any(row["status"] == "dry_run" for row in matched["results"])
    assert store.list_unmatched()[0]["resolved_at"]
    assert store.is_filed(store.list_unmatched()[0]["event_key"]) is False


def _open_unmatched(store, *, key, policy, name, notice_type="late_payment"):
    store.upsert_unmatched(
        source.ApiNotice(
            event_key=key,
            event_type=notice_type,
            program_id="prog-1",
            anchor="2026-10-01T00:00:00Z",
            occurred_at="2026-10-01T00:00:00Z",
            policy_numbers=(policy,) if policy else (),
            insured_name=name,
        ),
        reason=driver.POLICY_OUTCOME_NOT_IN_EZLYNX,
        seen_at="2026-10-01T00:00:00Z",
    )


class _PolicySearch:
    """PolicyApi number search only. Any write, including a note, fails the test."""

    def __init__(self, pages):
        self.pages = pages
        self.calls: list[str] = []

    def search_policy_by_number(self, policy_number):
        self.calls.append(policy_number)
        page = self.pages.get(policy_number, {"data": [], "totalSize": 0})
        if isinstance(page, Exception):
            raise page
        return page

    def __getattr__(self, name):
        raise AssertionError(f"digest recheck must not call {name}")


def _policy_hit(number, applicant, name="Fixture Hauling"):
    return {
        "data": [
            {
                "PolicyNumber": number,
                "ApplicantId": applicant,
                "ApplicantName": name,
            }
        ],
        "totalSize": 1,
    }


def test_digest_recheck_keeps_a_match_on_the_email_until_it_is_filed(tmp_path, monkeypatch):
    monkeypatch.delenv(source.LIVE_ENV, raising=False)
    store = source.EventKeyStore(tmp_path / "ascend-api" / "events.db")
    _open_unmatched(store, key="matched", policy="HO-998877", name="Fixture Hauling LLC")
    _open_unmatched(store, key="still-open", policy="CA-100200", name="Northwind Trucking Inc")
    client = _PolicySearch({"HO-998877": _policy_hit("HO-998877", "220250093")})
    result = digest.run_digest(
        stores=[store],
        now=NOW,
        live=False,
        ezlynx_client=client,
        refresh=False,
    )
    rows = {row["event_key"]: row for row in store.list_unmatched()}
    assert rows["matched"]["resolved_at"] in (None, "")
    assert not str(rows["matched"].get("ready_at") or "").strip()
    assert rows["still-open"]["resolved_at"] in (None, "")
    assert result["ready_count"] == 0
    assert result["resolved_count"] == 0
    assert "Fixture Hauling LLC" in result["body"]
    assert "Robie's Ascend notes haven't run recently." in result["body"]
    assert "Northwind Trucking Inc" in result["body"]
    assert "files the note." in result["body"]
    assert client.calls == ["HO-998877", "CA-100200"]

    sent: dict[str, str] = {}
    live = digest.run_digest(
        stores=[store],
        now=NOW,
        live=True,
        mailer=lambda **kwargs: sent.update(kwargs),
        ezlynx_client=client,
        refresh=False,
    )
    rows = {row["event_key"]: row for row in store.list_unmatched()}
    assert live["sent"] is True
    assert live["ready_count"] == 1
    assert rows["matched"]["ready_at"]
    assert rows["matched"]["resolved_at"] in (None, "")
    assert not str(rows["still-open"].get("ready_at") or "").strip()
    assert "Robie's Ascend notes haven't run recently." in live["body"]


def test_digest_recheck_leaves_two_candidates_and_a_failed_search_open(tmp_path):
    store = source.EventKeyStore(tmp_path / "ascend-api" / "events.db")
    _open_unmatched(store, key="two", policy="HO-998877", name="Fixture Hauling LLC")
    _open_unmatched(store, key="down", policy="CA-100200", name="Northwind Trucking Inc")
    _save_index(store, [_index_row("HO-998877", "Fixture Hauling", "220250093")])
    client = _PolicySearch(
        {
            "HO-998877": {
                "data": [
                    {"PolicyNumber": "HO-998877", "ApplicantId": "1"},
                    {"PolicyNumber": "HO-998877", "ApplicantId": "2"},
                ],
                "totalSize": 2,
            },
            "CA-100200": RuntimeError("policy api down"),
        }
    )
    result = digest.run_digest(
        stores=[store],
        now=NOW,
        live=False,
        ezlynx_client=client,
        refresh=False,
    )
    rows = {row["event_key"]: row for row in store.list_unmatched()}
    assert rows["two"]["resolved_at"] in (None, "")
    assert rows["down"]["resolved_at"] in (None, "")
    assert not str(rows["two"].get("ready_at") or "").strip()
    assert result["ready_count"] == 0
    assert result["resolved_count"] == 0
    assert "Fixture Hauling LLC" in result["body"]
    assert "Northwind Trucking Inc" in result["body"]


def test_digest_recheck_uses_a_complete_index_and_ignores_a_zero_search(tmp_path):
    store = source.EventKeyStore(tmp_path / "ascend-api" / "events.db")
    _open_unmatched(store, key="indexed", policy="HO-998877", name="Fixture Hauling LLC")
    _open_unmatched(store, key="two-index", policy="CA-100200", name="Northwind Trucking Inc")
    _save_index(
        store,
        [
            _index_row("HO-998877", "Fixture Hauling", "220250093"),
            _index_row("CA-100200", "Northwind", "1"),
            _index_row("CA-100200", "Other Wind", "2"),
        ],
    )
    from_index = digest.run_digest(stores=[store], now=NOW, live=False, refresh=False)
    rows = {row["event_key"]: row for row in store.list_unmatched()}
    assert rows["indexed"]["resolved_at"] in (None, "")
    assert not str(rows["indexed"].get("ready_at") or "").strip()
    assert rows["two-index"]["resolved_at"] in (None, "")
    assert from_index["ready_count"] == 0
    assert "Fixture Hauling LLC" in from_index["body"]
    assert "Robie's Ascend notes haven't run recently." in from_index["body"]

    missed = source.EventKeyStore(tmp_path / "ascend-api" / "missed.db")
    _open_unmatched(missed, key="stale", policy="HO-998877", name="Fixture Hauling LLC")
    _save_index(missed, [_index_row("HO-998877", "Fixture Hauling", "220250093")])
    client = _PolicySearch({})
    stayed = digest.run_digest(
        stores=[missed],
        now=NOW,
        live=False,
        ezlynx_client=client,
        refresh=False,
    )
    assert missed.list_unmatched()[0]["resolved_at"] in (None, "")
    assert not str(missed.list_unmatched()[0].get("ready_at") or "").strip()
    assert stayed["ready_count"] == 0
    assert stayed["resolved_count"] == 0
    assert "That policy number is not in EZLynx." in stayed["body"]


def test_recheck_backs_off_on_429_and_regrants_once_on_401(tmp_path, monkeypatch):
    monkeypatch.delenv(source.LIVE_ENV, raising=False)
    store = source.EventKeyStore(tmp_path / "ascend-api" / "events.db")
    _open_unmatched(store, key="a-one", policy="HO-998877", name="Fixture Hauling LLC")
    waits: list[float] = []

    class Slow:
        def __init__(self):
            self.calls = 0

        def search_policy_by_number(self, policy_number):
            self.calls += 1
            if self.calls == 1:
                raise EzlynxApiError(429, "slow down")
            return _policy_hit(policy_number, "220250093")

    slow = Slow()
    slowed = digest.run_digest(
        stores=[store],
        now=NOW,
        live=False,
        ezlynx_client=slow,
        refresh=False,
        sleep=waits.append,
    )
    assert slow.calls == 2
    assert waits == [1.0]
    assert "Robie's Ascend notes haven't run recently." in slowed["body"]
    assert not str(store.list_unmatched()[0].get("ready_at") or "").strip()

    second = source.EventKeyStore(tmp_path / "ascend-api" / "reauth.db")
    _open_unmatched(second, key="a-one", policy="HO-998877", name="Fixture Hauling LLC")
    _open_unmatched(second, key="b-two", policy="CA-100200", name="Northwind Trucking Inc")

    class Expired:
        def __init__(self):
            self.calls = 0
            self.cleared = 0

        def clear_cached_token(self):
            self.cleared += 1

        def search_policy_by_number(self, policy_number):
            self.calls += 1
            if self.calls == 1:
                raise EzlynxApiError(401, "expired")
            return _policy_hit(policy_number, "220250093", "Matched Client")

    expired = Expired()
    continued = digest.run_digest(
        stores=[second],
        now=NOW,
        live=False,
        ezlynx_client=expired,
        refresh=False,
    )
    assert expired.cleared == 1
    assert expired.calls == 3
    assert continued["body"].count("Robie's Ascend notes haven't run recently.") == 2
    assert all(not str(row.get("ready_at") or "").strip() for row in second.list_unmatched())
    assert all(not str(row.get("resolved_at") or "").strip() for row in second.list_unmatched())


def test_recheck_stops_when_the_shared_deadline_is_already_spent(tmp_path):
    store = source.EventKeyStore(tmp_path / "ascend-api" / "events.db")
    _open_unmatched(store, key="matched", policy="HO-998877", name="Fixture Hauling LLC")
    _open_unmatched(store, key="still-open", policy="CA-100200", name="Northwind Trucking Inc")
    client = _PolicySearch(
        {
            "HO-998877": _policy_hit("HO-998877", "220250093"),
            "CA-100200": _policy_hit("CA-100200", "220250094", "Northwind"),
        }
    )
    result = digest.run_digest(
        stores=[store],
        now=NOW,
        live=True,
        mailer=lambda **_kwargs: {"kind": "gmail"},
        ezlynx_client=client,
        refresh=False,
        deadline_s=0,
    )
    assert result["sent"] is True
    assert result["ready_count"] == 0
    assert client.calls == []
    assert "Fixture Hauling LLC" in result["body"]
    assert "Northwind Trucking Inc" in result["body"]
    for row in store.list_unmatched():
        assert row["resolved_at"] in (None, "")
        assert not str(row.get("ready_at") or "").strip()


def test_live_api_source_keeps_a_ready_row_on_the_email_until_the_poll_files_it(tmp_path, monkeypatch):
    monkeypatch.delenv(source.LIVE_ENV, raising=False)
    store = source.EventKeyStore(tmp_path / "ascend-api" / "events.db")
    store.note_poll_result(
        ok=True, error="", started_at="2026-10-05T13:00:00Z", live=True
    )
    _open_unmatched(store, key="matched", policy="HO-998877", name="Fixture Hauling LLC")
    client = _PolicySearch({"HO-998877": _policy_hit("HO-998877", "220250093")})
    result = digest.run_digest(
        stores=[store],
        now=NOW,
        live=True,
        mailer=lambda **_kwargs: {"kind": "gmail"},
        ezlynx_client=client,
        refresh=False,
    )
    row = store.list_unmatched()[0]
    assert result["ready_count"] == 1
    assert row["ready_at"]
    assert row["resolved_at"] in (None, "")
    assert "will file this on the next Ascend run." in result["body"]
    assert "haven't run recently" not in result["body"]
    assert os.environ.get(source.LIVE_ENV) != "1"


def _empty_feeds():
    return {path: {"data": [], "meta": {"next": None}} for path in source.FEEDS}


def test_poll_files_ready_rows_only_when_the_api_source_is_live(tmp_path, monkeypatch):
    monkeypatch.delenv(source.LIVE_ENV, raising=False)
    monkeypatch.setattr(
        driver.zapier_tasks, "fire_task", lambda payload, *, dry_run=False: {"ok": True}
    )
    store, _summary = _run_unmatched(tmp_path, monkeypatch, TrapEzlynx())
    saved = store.list_unmatched()[0]
    assert saved["subject"]
    assert "Policy ID" in saved["body"]
    store.mark_ready_to_file(saved["event_key"], "2026-10-05T14:00:00Z")
    talks = [{"discussionId": "d1", "title": "Ascend - Payments"}]
    dry_ctx = HELPERS.driver_ctx(talks)
    dry = source.run_once(
        client=HELPERS.FeedClient(_empty_feeds()),
        store=store,
        driver_ctx=dry_ctx,
        now=NOW,
    )
    assert dry["live"] is False
    assert dry["ready_checked"] == 0
    assert store.list_unmatched()[0]["resolved_at"] in (None, "")
    assert dry_ctx.discussion_client._urlopen.posts_to("/notes") == []

    monkeypatch.setenv(source.LIVE_ENV, "1")
    live_ctx = HELPERS.driver_ctx(talks)
    live = source.run_once(
        client=HELPERS.FeedClient(_empty_feeds()),
        store=store,
        driver_ctx=live_ctx,
        now=NOW,
    )
    assert live["ready_filed"] == 1
    assert store.is_filed(saved["event_key"])
    assert store.list_unmatched()[0]["resolved_at"]
    assert len(live_ctx.discussion_client._urlopen.posts_to("/notes")) == 1


def test_poll_does_not_resolve_a_ready_row_when_filing_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(
        driver.zapier_tasks, "fire_task", lambda payload, *, dry_run=False: {"ok": True}
    )
    store, _summary = _run_unmatched(tmp_path, monkeypatch, TrapEzlynx())
    saved = store.list_unmatched()[0]
    store.mark_ready_to_file(saved["event_key"], "2026-10-05T14:00:00Z")
    monkeypatch.setenv(source.LIVE_ENV, "1")
    ctx = HELPERS.driver_ctx([{"discussionId": "d1", "title": "Ascend - Payments"}])
    ctx.ezlynx_client = TrapEzlynx()
    live = source.run_once(
        client=HELPERS.FeedClient(_empty_feeds()),
        store=store,
        driver_ctx=ctx,
        now=NOW,
    )
    assert live["ready_checked"] == 1
    assert live["ready_filed"] == 0
    row = store.list_unmatched()[0]
    assert row["resolved_at"] in (None, "")
    assert row["ready_at"]
    assert row["file_attempts"] == 1
    assert row["last_attempt_at"]
    assert row["file_failure"] == "The policy number still did not match one client."
    assert ctx.discussion_client._urlopen.posts_to("/notes") == []


def test_poll_files_at_most_25_ready_rows(tmp_path, monkeypatch):
    monkeypatch.setenv(source.LIVE_ENV, "1")
    store = source.EventKeyStore(tmp_path / "ascend-api" / "events.db")
    program = {"id": HELPERS._pid(1), "status": "payment_overdue", "producer": {"email": "karla@streetsmart.insurance"}}
    for index in range(26):
        key = f"ready-{index:02d}"
        notice = source.ApiNotice(
            event_key=key,
            event_type="late_payment",
            program_id=HELPERS._pid(1),
            anchor="2026-10-01T00:00:00Z",
            occurred_at="2026-10-01T00:00:00Z",
            policy_numbers=(f"HO-{index:04d}",),
            insured_name=f"Insured {index}",
            subject=f"Past due payment for Insured {index}",
            body=f"Policy ID HO-{index:04d}\nCustomer Insured {index}\n",
            program=program,
        )
        store.upsert_unmatched(
            notice,
            reason=driver.POLICY_OUTCOME_NOT_IN_EZLYNX,
            seen_at="2026-10-01T00:00:00Z",
        )
        store.mark_ready_to_file(key, f"2026-10-05T14:00:{index:02d}Z")
    seen: list[str] = []

    def _skip(ctx, notice):
        seen.append(notice.event_key)
        return {
            "event_key": notice.event_key,
            "event_type": notice.event_type,
            "status": "skipped",
            "reason": "applicant_unresolved",
            "detail": {},
        }

    monkeypatch.setattr(source, "_file_through_driver", _skip)
    summary = source.run_once(
        client=HELPERS.FeedClient(_empty_feeds()),
        store=store,
        driver_ctx=HELPERS.driver_ctx(),
        now=NOW,
    )
    assert summary["ready_checked"] == 25
    assert summary["ready_filed"] == 0
    assert len(seen) == 25
    assert "ready-25" not in seen
    left = {row["event_key"]: row for row in store.list_unmatched()}
    assert left["ready-25"]["resolved_at"] in (None, "")
    assert left["ready-25"]["ready_at"]
    assert all(not str(row.get("resolved_at") or "").strip() for row in left.values())


def test_a_duplicate_ready_row_is_recorded_as_filed(tmp_path, monkeypatch):
    monkeypatch.setenv(source.LIVE_ENV, "1")
    store = source.EventKeyStore(tmp_path / "ascend-api" / "events.db")
    notice = source.ApiNotice(
        event_key="dup-1",
        event_type="late_payment",
        program_id=HELPERS._pid(1),
        anchor="2026-10-01T00:00:00Z",
        occurred_at="2026-10-01T00:00:00Z",
        policy_numbers=("HO-998877",),
        insured_name="Fixture Hauling LLC",
        subject="Past due payment for Fixture Hauling LLC",
        body="Policy ID HO-998877\nCustomer Fixture Hauling LLC\n",
        program={"id": HELPERS._pid(1), "policy_number": "HO-998877"},
    )
    store.upsert_unmatched(
        notice,
        reason=driver.POLICY_OUTCOME_NOT_IN_EZLYNX,
        seen_at="2026-10-01T00:00:00Z",
    )
    store.mark_ready_to_file("dup-1", "2026-10-05T14:00:00Z")

    def _already(ctx, item):
        return {
            "event_key": item.event_key,
            "event_type": item.event_type,
            "status": "skipped",
            "reason": "existing_note_duplicate: n7",
        }

    monkeypatch.setattr(source, "_file_through_driver", _already)
    summary = source.run_once(
        client=HELPERS.FeedClient(_empty_feeds()),
        store=store,
        driver_ctx=HELPERS.driver_ctx(),
        now=NOW,
    )
    row = store.list_unmatched()[0]
    assert summary["ready_filed"] == 1
    assert store.is_filed("dup-1")
    assert row["resolved_at"]
    assert int(row["file_attempts"] or 0) == 0
    assert summary["ready_checked"] == 1


def test_one_failing_ready_row_does_not_starve_the_rest(tmp_path, monkeypatch):
    monkeypatch.setenv(source.LIVE_ENV, "1")
    store = source.EventKeyStore(tmp_path / "ascend-api" / "events.db")
    program = {"id": HELPERS._pid(1), "policy_number": "HO-0000"}
    for index in range(26):
        key = f"ready-{index:02d}"
        notice = source.ApiNotice(
            event_key=key,
            event_type="late_payment",
            program_id=HELPERS._pid(1),
            anchor="2026-10-01T00:00:00Z",
            occurred_at="2026-10-01T00:00:00Z",
            policy_numbers=(f"HO-{index:04d}",),
            insured_name=f"Insured {index}",
            subject=f"Past due payment for Insured {index}",
            body=f"Policy ID HO-{index:04d}\nCustomer Insured {index}\n",
            program=program,
        )
        store.upsert_unmatched(
            notice,
            reason=driver.POLICY_OUTCOME_NOT_IN_EZLYNX,
            seen_at="2026-10-01T00:00:00Z",
        )
        store.mark_ready_to_file(key, f"2026-10-05T14:00:{index:02d}Z")
    batches: list[list[str]] = []

    def _skip(ctx, notice):
        batches[-1].append(notice.event_key)
        return {
            "event_key": notice.event_key,
            "event_type": notice.event_type,
            "status": "skipped",
            "reason": "applicant_unresolved",
        }

    monkeypatch.setattr(source, "_file_through_driver", _skip)
    batches.append([])
    first = source.run_once(
        client=HELPERS.FeedClient(_empty_feeds()),
        store=store,
        driver_ctx=HELPERS.driver_ctx(),
        now=NOW,
    )
    assert first["ready_checked"] == 25
    assert "ready-25" not in batches[0]
    batches.append([])
    second = source.run_once(
        client=HELPERS.FeedClient(_empty_feeds()),
        store=store,
        driver_ctx=HELPERS.driver_ctx(),
        now=NOW + timedelta(minutes=15),
    )
    assert second["ready_checked"] == 25
    assert "ready-25" in batches[1]
    assert "ready-24" not in batches[1]
    left = {row["event_key"]: row for row in store.list_unmatched()}
    assert int(left["ready-25"]["file_attempts"]) == 1
    assert all(not str(row.get("resolved_at") or "").strip() for row in left.values())


def test_five_failed_attempts_leave_the_row_for_a_person(tmp_path, monkeypatch):
    monkeypatch.setenv(source.LIVE_ENV, "1")
    store = source.EventKeyStore(tmp_path / "ascend-api" / "events.db")
    notice = source.ApiNotice(
        event_key="stuck",
        event_type="late_payment",
        program_id=HELPERS._pid(1),
        anchor="2026-10-01T00:00:00Z",
        occurred_at="2026-10-01T00:00:00Z",
        policy_numbers=("HO-998877",),
        insured_name="Fixture Hauling LLC",
        subject="Past due payment for Fixture Hauling LLC",
        body="Policy ID HO-998877\nCustomer Fixture Hauling LLC\n",
        program={"id": HELPERS._pid(1)},
    )
    store.upsert_unmatched(
        notice,
        reason=driver.POLICY_OUTCOME_NOT_IN_EZLYNX,
        seen_at="2026-10-01T00:00:00Z",
    )
    store.mark_ready_to_file("stuck", "2026-10-05T14:00:00Z")
    calls = {"n": 0}

    def _skip(ctx, item):
        calls["n"] += 1
        return {
            "event_key": item.event_key,
            "event_type": item.event_type,
            "status": "skipped",
            "reason": "applicant_unresolved",
        }

    monkeypatch.setattr(source, "_file_through_driver", _skip)
    for _ in range(5):
        source.run_once(
            client=HELPERS.FeedClient(_empty_feeds()),
            store=store,
            driver_ctx=HELPERS.driver_ctx(),
            now=NOW,
        )
    assert calls["n"] == 5
    row = store.list_unmatched()[0]
    assert row["file_attempts"] == 5
    assert row["resolved_at"] in (None, "")
    monkeypatch.delenv(source.LIVE_ENV, raising=False)
    quiet = source.run_once(
        client=HELPERS.FeedClient(_empty_feeds()),
        store=store,
        driver_ctx=HELPERS.driver_ctx(),
        now=NOW,
    )
    assert quiet["ready_checked"] == 0
    assert calls["n"] == 5
    monkeypatch.setenv(source.LIVE_ENV, "1")
    sixth = source.run_once(
        client=HELPERS.FeedClient(_empty_feeds()),
        store=store,
        driver_ctx=HELPERS.driver_ctx(),
        now=NOW + timedelta(minutes=15),
    )
    assert sixth["ready_checked"] == 0
    assert calls["n"] == 5
    monkeypatch.delenv(source.LIVE_ENV, raising=False)
    mailed = digest.run_digest(stores=[store], now=NOW, live=False, refresh=False)
    assert "couldn't file it" in mailed["body"]
    assert "please file by hand" in mailed["body"]
    assert "The policy number still did not match one client." in mailed["body"]
    assert os.environ.get(source.LIVE_ENV) != "1"


def _discussion_with(note):
    from robie_job_engine import ezlynx_discussions as discussions

    routes = [
        ("connect/token", {"access_token": "tok123", "expires_in": 3600}),
        ("by-applicant", [{"discussionId": "d1", "title": "Ascend - Payments"}]),
        ("/notes", {"noteId": "n-new"}),
        (
            "v8/discussions/",
            {
                "discussionId": "d1",
                "title": "Ascend - Payments",
                "notes": [note, {"noteId": "n-new", "body": "posted"}],
            },
        ),
    ]
    config = discussions.DiscussionApiConfig(
        discussion_base_url="https://app.uatezlynx.com/DiscussionApi/",
        token_endpoint="https://identity.example.com/connect/token",
        client_id="street_smart_api",
        client_secret="secret",
        username="SSRobie",
        integration_group_id="159",
    )
    return discussions.DiscussionApiClient(config, urlopen=HELPERS.FakeUrlopen(routes))


def test_a_recent_same_notice_on_the_discussion_counts_as_filed(tmp_path, monkeypatch):
    monkeypatch.setattr(
        driver.zapier_tasks, "fire_task", lambda payload, *, dry_run=False: {"ok": True}
    )
    store, _summary = _run_unmatched(tmp_path, monkeypatch, TrapEzlynx())
    saved = store.list_unmatched()[0]
    store.mark_ready_to_file(saved["event_key"], "2026-10-05T14:00:00Z")
    monkeypatch.setenv(source.LIVE_ENV, "1")
    ctx = HELPERS.driver_ctx([{"discussionId": "d1", "title": "Ascend - Payments"}])
    ctx.discussion_client = _discussion_with(
        {
            "noteId": "n-recent",
            "body": (
                "LATE PAYMENT notice from Ascend. Policy HO-998877 is past due: "
                "$412.10 was due 10/04/2026. Invoice No. INV-3003."
            ),
            "createdAt": saved["first_seen"],
        }
    )
    live = source.run_once(
        client=HELPERS.FeedClient(_empty_feeds()),
        store=store,
        driver_ctx=ctx,
        now=NOW,
    )
    assert live["ready_filed"] == 1
    assert store.is_filed(saved["event_key"])
    assert store.list_unmatched()[0]["resolved_at"]
    assert ctx.discussion_client._urlopen.posts_to("/notes") == []


def test_recheck_reads_the_policy_number_from_ascend(tmp_path, monkeypatch):
    monkeypatch.delenv(source.LIVE_ENV, raising=False)
    store = source.EventKeyStore(tmp_path / "ascend-api" / "events.db")
    store.upsert_unmatched(
        source.ApiNotice(
            event_key="moved",
            event_type="late_payment",
            program_id="prog-1",
            anchor="2026-10-01T00:00:00Z",
            occurred_at="2026-10-01T00:00:00Z",
            policy_numbers=("HO-OLD",),
            insured_name="Fixture Hauling LLC",
            subject="Past due payment for Fixture Hauling LLC",
            body="Policy ID HO-OLD\nCustomer Fixture Hauling LLC\n",
            program={"id": "prog-1", "policy_number": "HO-OLD"},
        ),
        reason=driver.POLICY_OUTCOME_NOT_IN_EZLYNX,
        seen_at="2026-10-01T00:00:00Z",
    )

    class AscendRead:
        def __init__(self):
            self.calls: list[str] = []

        def get_program(self, program_id):
            self.calls.append(program_id)
            return {"id": program_id, "policy_number": "HO-NEW"}

    ascend = AscendRead()
    ezlynx = _PolicySearch({"HO-NEW": _policy_hit("HO-NEW", "220250093")})
    result = digest.run_digest(
        stores=[store],
        now=NOW,
        live=True,
        mailer=lambda **_kwargs: {"kind": "gmail"},
        ezlynx_client=ezlynx,
        ascend_client=ascend,
        refresh=False,
    )
    row = store.list_unmatched()[0]
    assert ascend.calls == ["prog-1"]
    assert ezlynx.calls == ["HO-NEW"]
    assert row["ready_at"]
    assert row["resolved_at"] in (None, "")
    assert row["policy_numbers"] == ["HO-NEW"]
    assert "Policy ID HO-NEW" in row["body"]
    assert "HO-OLD" not in row["body"]
    assert "HO-NEW" in result["body"]
    assert "or Ascend" in result["body"]
    assert os.environ.get(source.LIVE_ENV) != "1"

    class Spent:
        def get_program(self, program_id):
            raise AssertionError("the deadline was already spent")

    spent = source.EventKeyStore(tmp_path / "ascend-api" / "spent.db")
    _open_unmatched(spent, key="matched", policy="HO-998877", name="Fixture Hauling LLC")
    held = _PolicySearch({"HO-998877": _policy_hit("HO-998877", "220250093")})
    mailed = digest.run_digest(
        stores=[spent],
        now=NOW,
        live=True,
        mailer=lambda **_kwargs: {"kind": "gmail"},
        ezlynx_client=held,
        ascend_client=Spent(),
        refresh=False,
        deadline_s=0,
    )
    assert mailed["sent"] is True
    assert held.calls == []
    assert "Fixture Hauling LLC" in mailed["body"]


def test_an_older_late_notice_is_not_this_months_filing(tmp_path, monkeypatch):
    """Sep 9 $398.00 does not file the Oct 4 $412.10 ready row."""
    monkeypatch.setattr(
        driver.zapier_tasks, "fire_task", lambda payload, *, dry_run=False: {"ok": True}
    )
    store, _summary = _run_unmatched(tmp_path, monkeypatch, TrapEzlynx())
    saved = store.list_unmatched()[0]
    assert "412.10" in saved["body"]
    assert saved["first_seen"] >= "2026-10-04"
    store.mark_ready_to_file(saved["event_key"], "2026-10-05T14:00:00Z")
    monkeypatch.setenv(source.LIVE_ENV, "1")
    ctx = HELPERS.driver_ctx([{"discussionId": "d1", "title": "Ascend - Payments"}])
    ctx.discussion_client = _discussion_with(
        {
            "noteId": "n-sept",
            "body": (
                "LATE PAYMENT notice from Ascend. Policy HO-998877 is past due: "
                "$398.00 was due 09/04/2026."
            ),
            "createdAt": "2026-09-09T15:00:00Z",
        }
    )
    live = source.run_once(
        client=HELPERS.FeedClient(_empty_feeds()),
        store=store,
        driver_ctx=ctx,
        now=NOW,
    )
    posts = ctx.discussion_client._urlopen.posts_to("/notes")
    assert len(posts) == 1
    assert live["ready_filed"] == 1
    assert all("recent_same_notice" not in str(row.get("reason") or "") for row in live["results"])


def test_an_older_ledger_row_is_not_this_months_filing(tmp_path, monkeypatch):
    """A Sep 10 notice-filing row does not file the Oct 4 $412.10 ready row."""
    from robie_job_engine.discussion_note_ledger import (
        find_recent_notice_filing,
        remember_notice_filing,
    )

    monkeypatch.setattr(
        driver.zapier_tasks, "fire_task", lambda payload, *, dry_run=False: {"ok": True}
    )
    store, _summary = _run_unmatched(tmp_path, monkeypatch, TrapEzlynx())
    saved = store.list_unmatched()[0]
    store.mark_ready_to_file(saved["event_key"], "2026-10-05T14:00:00Z")
    remember_notice_filing(
        HELPERS.APPLICANT,
        "d1",
        policy_numbers=["HO-998877"],
        notice_type=triage.LATE_PAYMENT,
        posted_at="2026-09-10T15:00:00Z",
    )
    first_seen = source.parse_time(saved["first_seen"])
    notice_text = f"{saved['subject']}\n{saved['body']}"
    assert (
        find_recent_notice_filing(
            HELPERS.APPLICANT,
            "d1",
            ["HO-998877"],
            triage.LATE_PAYMENT,
            now=NOW,
            not_before=first_seen,
            notice_text=notice_text,
        )
        is None
    )
    matched = tmp_path / "same-bill.json"
    remember_notice_filing(
        HELPERS.APPLICANT,
        "d1",
        policy_numbers=["HO-998877"],
        notice_type=triage.LATE_PAYMENT,
        posted_at=saved["first_seen"],
        notice_text=notice_text,
        ledger_path=matched,
    )
    assert (
        find_recent_notice_filing(
            HELPERS.APPLICANT,
            "d1",
            ["HO-998877"],
            triage.LATE_PAYMENT,
            now=NOW,
            not_before=first_seen,
            notice_text=notice_text,
            ledger_path=matched,
        )
        is not None
    )
    monkeypatch.setenv(source.LIVE_ENV, "1")
    ctx = HELPERS.driver_ctx([{"discussionId": "d1", "title": "Ascend - Payments"}])
    live = source.run_once(
        client=HELPERS.FeedClient(_empty_feeds()),
        store=store,
        driver_ctx=ctx,
        now=NOW,
    )
    posts = ctx.discussion_client._urlopen.posts_to("/notes")
    assert len(posts) == 1
    assert live["ready_filed"] == 1
    assert all("recent_same_notice" not in str(row.get("reason") or "") for row in live["results"])


def test_recheck_reads_the_policy_number_from_the_billable(tmp_path, monkeypatch):
    """A program record has no policy number. The billable does."""
    monkeypatch.delenv(source.LIVE_ENV, raising=False)
    store = source.EventKeyStore(tmp_path / "ascend-api" / "events.db")
    store.upsert_unmatched(
        source.ApiNotice(
            event_key="moved",
            event_type="late_payment",
            program_id="prog-1",
            anchor="2026-10-01T00:00:00Z",
            occurred_at="2026-10-01T00:00:00Z",
            policy_numbers=("HO-OLD",),
            insured_name="Fixture Hauling LLC",
            subject="Past due payment for Fixture Hauling LLC",
            body="Policy ID HO-OLD\nCustomer Fixture Hauling LLC\n",
            program={"id": "prog-1"},
        ),
        reason=driver.POLICY_OUTCOME_NOT_IN_EZLYNX,
        seen_at="2026-10-01T00:00:00Z",
    )

    class AscendRead:
        def __init__(self):
            self.programs: list[str] = []
            self.billables: list[str] = []

        def get_program(self, program_id):
            self.programs.append(program_id)
            return {"id": program_id, "insured": {"business_name": "Fixture Hauling LLC"}}

        def get_billables(self, program_id):
            self.billables.append(program_id)
            return {"data": [{"id": "bill-1", "program_id": program_id, "policy_number": "HO-NEW"}]}

    ascend = AscendRead()
    result = digest.run_digest(
        stores=[store],
        now=NOW,
        live=True,
        mailer=lambda **_kwargs: {"kind": "gmail"},
        ezlynx_client=_PolicySearch({"HO-NEW": _policy_hit("HO-NEW", "220250093")}),
        ascend_client=ascend,
        refresh=False,
    )
    row = store.list_unmatched()[0]
    assert ascend.programs == ["prog-1"]
    assert ascend.billables == ["prog-1"]
    assert row["policy_numbers"] == ["HO-NEW"]
    assert "Policy ID HO-NEW" in row["body"]
    assert "HO-NEW" in result["body"]
    assert os.environ.get(source.LIVE_ENV) != "1"


def test_same_amount_and_a_different_due_date_is_not_the_same_bill():
    from robie_job_engine.discussion_note_ledger import anchors_overlap, notice_anchors

    october = notice_anchors(
        "LATE PAYMENT notice from Ascend. Policy HO-998877 is past due: "
        "$412.10 was due 10/04/2026."
    )
    november = notice_anchors(
        "LATE PAYMENT notice from Ascend. Policy HO-998877 is past due: "
        "$412.10 was due 11/04/2026."
    )
    assert anchors_overlap(october, november) is False
    assert anchors_overlap(october, october) is True
    amount_only = notice_anchors("The balance is $412.10.")
    assert anchors_overlap(amount_only, notice_anchors("Past due $412.10.")) is True
    assert anchors_overlap(october, amount_only) is False
    invoices = notice_anchors("Invoice No. INV-3003 $412.10")
    other = notice_anchors("Invoice No. INV-3004 $412.10")
    assert anchors_overlap(invoices, other) is False
    assert anchors_overlap(invoices, amount_only) is False
    other_amount = notice_anchors(
        "LATE PAYMENT notice from Ascend. Policy HO-998877 is past due: "
        "$398.00 was due 10/04/2026."
    )
    assert anchors_overlap(october, other_amount) is False


def test_a_later_due_date_does_not_close_this_bill(tmp_path, monkeypatch):
    monkeypatch.setattr(
        driver.zapier_tasks, "fire_task", lambda payload, *, dry_run=False: {"ok": True}
    )
    store, _summary = _run_unmatched(tmp_path, monkeypatch, TrapEzlynx())
    saved = store.list_unmatched()[0]
    store.mark_ready_to_file(saved["event_key"], "2026-10-05T14:00:00Z")
    monkeypatch.setenv(source.LIVE_ENV, "1")
    ctx = HELPERS.driver_ctx([{"discussionId": "d1", "title": "Ascend - Payments"}])
    ctx.discussion_client = _discussion_with(
        {
            "noteId": "n-next",
            "body": (
                "LATE PAYMENT notice from Ascend. Policy HO-998877 is past due: "
                "$412.10 was due 11/04/2026."
            ),
            "createdAt": saved["first_seen"],
        }
    )
    live = source.run_once(
        client=HELPERS.FeedClient(_empty_feeds()),
        store=store,
        driver_ctx=ctx,
        now=NOW,
    )
    assert len(ctx.discussion_client._urlopen.posts_to("/notes")) == 1
    assert all("recent_same_notice" not in str(row.get("reason") or "") for row in live["results"])


def test_a_zone_less_ezlynx_note_time_is_eastern():
    posted = driver._note_posted_at({"createdAt": "2026-10-05T10:30:00"})
    assert posted == datetime(2026, 10, 5, 14, 30, tzinfo=timezone.utc)

    class Talk:
        def get_discussion(self, discussion_id):
            return {
                "discussionId": discussion_id,
                "notes": [
                    {
                        "noteId": "n-east",
                        "body": (
                            "LATE PAYMENT notice from Ascend. Policy HO-998877 is past due: "
                            "$412.10 was due 10/04/2026."
                        ),
                        "createdAt": "2026-10-05T10:30:00",
                    }
                ],
            }

    hit = driver.recent_same_notice(
        Talk(),
        applicant_id=HELPERS.APPLICANT,
        discussion_id="d1",
        policy_numbers=["HO-998877"],
        notice_type=triage.LATE_PAYMENT,
        now=datetime(2026, 10, 5, 16, 0, tzinfo=timezone.utc),
        not_before=datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc),
        notice_text=(
            "This policy has a past-due payment of $412.10 which was due on 10/04/2026.\n"
            "Invoice No. INV-3003"
        ),
    )
    assert hit == {"note_id": "n-east", "source": "discussion"}


def test_a_transient_failure_does_not_spend_a_filing_attempt(tmp_path, monkeypatch):
    monkeypatch.setenv(source.LIVE_ENV, "1")
    store = source.EventKeyStore(tmp_path / "ascend-api" / "events.db")
    store.upsert_unmatched(
        source.ApiNotice(
            event_key="ready-1",
            event_type="late_payment",
            program_id=HELPERS._pid(1),
            anchor="2026-10-01T00:00:00Z",
            occurred_at="2026-10-01T00:00:00Z",
            policy_numbers=("HO-998877",),
            insured_name="Fixture Hauling LLC",
            subject="Past due payment for Fixture Hauling LLC",
            body="Policy ID HO-998877\nCustomer Fixture Hauling LLC\n",
            program={"id": HELPERS._pid(1)},
        ),
        reason=driver.POLICY_OUTCOME_NOT_IN_EZLYNX,
        seen_at="2026-10-01T00:00:00Z",
    )
    store.mark_ready_to_file("ready-1", "2026-10-05T14:00:00Z")
    calls: list[str] = []

    def _down(_ctx, item):
        calls.append(str(item.event_key))
        return {
            "event_key": item.event_key,
            "event_type": item.event_type,
            "status": "skipped",
            "reason": "discussion_error: Discussion API POST failed: HTTP 500",
        }

    monkeypatch.setattr(source, "_file_through_driver", _down)
    source.run_once(
        client=HELPERS.FeedClient(_empty_feeds()),
        store=store,
        driver_ctx=HELPERS.driver_ctx(),
        now=NOW,
    )
    assert calls == ["ready-1"]
    assert int(store.list_unmatched()[0]["file_attempts"] or 0) == 0
    calls.clear()
    source.run_once(
        client=HELPERS.FeedClient(_empty_feeds()),
        store=store,
        driver_ctx=HELPERS.driver_ctx(),
        now=NOW + timedelta(minutes=15),
    )
    assert calls == []
    assert int(store.list_unmatched()[0]["file_attempts"] or 0) == 0
    source.run_once(
        client=HELPERS.FeedClient(_empty_feeds()),
        store=store,
        driver_ctx=HELPERS.driver_ctx(),
        now=NOW + timedelta(hours=2),
    )
    assert calls == ["ready-1"]
    assert int(store.list_unmatched()[0]["file_attempts"] or 0) == 0

    def _mismatch(_ctx, item):
        return {
            "event_key": item.event_key,
            "event_type": item.event_type,
            "status": "skipped",
            "reason": "applicant_unresolved",
        }

    monkeypatch.setattr(source, "_file_through_driver", _mismatch)
    # The retry at +2h was still an HTTP 500, so it armed another two-hour hold.
    source.run_once(
        client=HELPERS.FeedClient(_empty_feeds()),
        store=store,
        driver_ctx=HELPERS.driver_ctx(),
        now=NOW + timedelta(hours=4),
    )
    assert int(store.list_unmatched()[0]["file_attempts"] or 0) == 1


def test_an_old_catchup_notice_is_listed_instead_of_dropped(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv(source.LIVE_ENV, "1")
    store = source.EventKeyStore(tmp_path / "ascend-api" / "events.db")
    store.advance_cursor(NOW - timedelta(days=10))
    old = (NOW - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
    pages = _unmatched_pages()
    pages[source.FEED_PROGRAMS]["data"][0]["updated_at"] = old
    pages[source.FEED_INVOICES]["data"][0]["updated_at"] = old
    seen: list[str] = []
    monkeypatch.setattr(
        source,
        "_file_through_driver",
        lambda _ctx, item: seen.append(item.event_key),
    )
    caplog.set_level("WARNING")
    summary = source.run_once(
        client=HELPERS.FeedClient(pages),
        store=store,
        driver_ctx=HELPERS.driver_ctx(),
        now=NOW,
    )
    assert summary["catchup_capped"] is True
    assert "capped at 72 hours" in caplog.text
    assert seen == []
    rows = store.list_unmatched()
    assert len(rows) == 1
    assert rows[0]["reason"] == source.CATCHUP_REVIEW
    assert rows[0]["resolved_at"] in (None, "")
    assert store.cursor() == NOW
    mailed = digest.run_digest(stores=[store], now=NOW, live=False, refresh=False)
    assert "too old to file automatically, please check by hand" in mailed["body"]


def test_a_catchup_row_is_not_filed_when_one_client_matches(tmp_path, monkeypatch):
    monkeypatch.setenv(source.LIVE_ENV, "1")
    store = source.EventKeyStore(tmp_path / "ascend-api" / "events.db")
    store.advance_cursor(NOW - timedelta(hours=80))
    old = (NOW - timedelta(hours=75)).strftime("%Y-%m-%dT%H:%M:%SZ")
    pages = _unmatched_pages()
    pages[source.FEED_PROGRAMS]["data"][0]["updated_at"] = old
    pages[source.FEED_INVOICES]["data"][0]["updated_at"] = old
    seen: list[str] = []

    def _spy(_ctx, item):
        seen.append(item.event_key)
        return {"event_key": item.event_key, "status": "done", "reason": ""}

    monkeypatch.setattr(source, "_file_through_driver", _spy)
    source.run_once(
        client=HELPERS.FeedClient(pages),
        store=store,
        driver_ctx=HELPERS.driver_ctx(),
        now=NOW,
    )
    assert seen == []
    row = store.list_unmatched()[0]
    assert row["reason"] == source.CATCHUP_REVIEW
    mailed = digest.run_digest(
        stores=[store],
        now=NOW,
        live=True,
        mailer=lambda **_kwargs: {"kind": "gmail"},
        ezlynx_client=_PolicySearch({HELPERS.POLICY: _policy_hit(HELPERS.POLICY, HELPERS.APPLICANT)}),
        refresh=False,
    )
    assert mailed["ready_count"] == 0
    assert "too old to file automatically, please check by hand" in mailed["body"]
    assert not str(store.list_unmatched()[0].get("ready_at") or "").strip()
    store.mark_ready_to_file(row["event_key"], "2026-10-05T14:00:00Z")
    source.run_once(
        client=HELPERS.FeedClient(_empty_feeds()),
        store=store,
        driver_ctx=HELPERS.driver_ctx(),
        now=NOW + timedelta(minutes=15),
    )
    assert seen == []
    assert store.list_unmatched()[0]["resolved_at"] in (None, "")


def test_an_already_filed_notice_is_not_reopened_by_catchup(tmp_path, monkeypatch):
    monkeypatch.setenv(source.LIVE_ENV, "1")
    store = source.EventKeyStore(tmp_path / "ascend-api" / "events.db")
    store.advance_cursor(NOW - timedelta(hours=80))
    old = (NOW - timedelta(hours=75)).strftime("%Y-%m-%dT%H:%M:%SZ")
    pages = _unmatched_pages()
    pages[source.FEED_PROGRAMS]["data"][0]["updated_at"] = old
    pages[source.FEED_INVOICES]["data"][0]["updated_at"] = old
    source.run_once(
        client=HELPERS.FeedClient(pages),
        store=store,
        driver_ctx=HELPERS.driver_ctx(),
        now=NOW,
    )
    saved = store.list_unmatched()[0]
    store.record_filed(
        source.ApiNotice(
            event_key=saved["event_key"],
            event_type=saved["notice_type"],
            program_id=saved["program_id"],
            anchor=saved["first_seen"],
            occurred_at=saved["first_seen"],
        )
    )
    store.resolve_unmatched(saved["event_key"], "2026-10-05T14:00:00Z")
    store.advance_cursor(NOW - timedelta(hours=80))
    seen: list[str] = []
    monkeypatch.setattr(source, "_file_through_driver", lambda _ctx, item: seen.append(item.event_key))
    summary = source.run_once(
        client=HELPERS.FeedClient(pages),
        store=store,
        driver_ctx=HELPERS.driver_ctx(),
        now=NOW + timedelta(minutes=15),
    )
    assert seen == []
    assert any(row["reason"] == "api_already_filed" for row in summary["results"])
    reopened = [
        row for row in store.list_unmatched() if not str(row.get("resolved_at") or "").strip()
    ]
    assert reopened == []


def test_the_unconfirmed_guard_after_a_timeout_does_not_spend_an_attempt(tmp_path, monkeypatch):
    monkeypatch.setenv(source.LIVE_ENV, "1")
    store = source.EventKeyStore(tmp_path / "ascend-api" / "events.db")
    store.upsert_unmatched(
        source.ApiNotice(
            event_key="ready-1",
            event_type="late_payment",
            program_id=HELPERS._pid(1),
            anchor="2026-10-01T00:00:00Z",
            occurred_at="2026-10-01T00:00:00Z",
            policy_numbers=("HO-998877",),
            insured_name="Fixture Hauling LLC",
            subject="Past due payment for Fixture Hauling LLC",
            body="Policy ID HO-998877\nCustomer Fixture Hauling LLC\n",
            program={"id": HELPERS._pid(1)},
        ),
        reason=driver.POLICY_OUTCOME_NOT_IN_EZLYNX,
        seen_at="2026-10-01T00:00:00Z",
    )
    store.mark_ready_to_file("ready-1", "2026-10-05T14:00:00Z")

    def _down(_ctx, item):
        return {
            "event_key": item.event_key,
            "status": "skipped",
            "reason": "discussion_error: Discussion API POST failed: HTTP 500",
        }

    monkeypatch.setattr(source, "_file_through_driver", _down)
    source.run_once(
        client=HELPERS.FeedClient(_empty_feeds()),
        store=store,
        driver_ctx=HELPERS.driver_ctx(),
        now=NOW,
    )
    assert int(store.list_unmatched()[0]["file_attempts"] or 0) == 0

    def _guard(_ctx, item):
        return {
            "event_key": item.event_key,
            "status": "skipped",
            "reason": "note_not_filed: None: I couldn't confirm that note was added.",
        }

    monkeypatch.setattr(source, "_file_through_driver", _guard)
    source.run_once(
        client=HELPERS.FeedClient(_empty_feeds()),
        store=store,
        driver_ctx=HELPERS.driver_ctx(),
        now=NOW + timedelta(hours=2),
    )
    paused = store.list_unmatched()[0]
    assert int(paused["file_attempts"] or 0) == 0
    assert int(paused["transient_retry_used"] or 0) == 1
    assert not str(paused.get("transient_hold_until") or "").strip()
    assert paused["resolved_at"] in (None, "")

    # The free retry is spent. A later 5xx counts, and does not park the row again.
    calls = {"n": 0}

    def _down_again(_ctx, item):
        calls["n"] += 1
        return _down(_ctx, item)

    monkeypatch.setattr(source, "_file_through_driver", _down_again)
    source.run_once(
        client=HELPERS.FeedClient(_empty_feeds()),
        store=store,
        driver_ctx=HELPERS.driver_ctx(),
        now=NOW + timedelta(hours=2, minutes=15),
    )
    counted = store.list_unmatched()[0]
    assert calls["n"] == 1
    assert int(counted["file_attempts"] or 0) == 1
    assert not str(counted.get("transient_hold_until") or "").strip()

    def _guard_again(_ctx, item):
        calls["n"] += 1
        return _guard(_ctx, item)

    monkeypatch.setattr(source, "_file_through_driver", _guard_again)
    for step in range(2, 6):
        source.run_once(
            client=HELPERS.FeedClient(_empty_feeds()),
            store=store,
            driver_ctx=HELPERS.driver_ctx(),
            now=NOW + timedelta(hours=2, minutes=15 * step),
        )
    handed = store.list_unmatched()[0]
    assert calls["n"] == 5
    assert int(handed["file_attempts"] or 0) == 5
    assert handed["resolved_at"] in (None, "")
    monkeypatch.delenv(source.LIVE_ENV, raising=False)
    mailed = digest.run_digest(stores=[store], now=NOW + timedelta(hours=4), live=False, refresh=False)
    assert "couldn't file it" in mailed["body"]
    assert "please file by hand" in mailed["body"]


class _FlakyNotePost:
    """The first note POST is HTTP 500. A later post succeeds.

    The plain discussion read has a title, a note count, and the latest
    note id. It has no note bodies. Bodies are returned only by with-notes.
    """

    def __init__(self, mode: str):
        if mode not in {"landed", "absent", "unavailable", "truncated"}:
            raise ValueError(mode)
        self.mode = mode
        self.calls: list[dict] = []
        self.posted_body = ""
        self.failures_left = 1
        self.note_count = 1
        self.latest_id = "n-seed"

    def __call__(self, url, *, data=None, headers=None, timeout=None):
        self.calls.append({"url": url, "data": data})
        if "connect/token" in url:
            return HELPERS.FakeResponse({"access_token": "tok123", "expires_in": 3600})
        if "by-applicant" in url:
            return HELPERS.FakeResponse([{"discussionId": "d1", "title": "Ascend - Payments"}])
        if data and "/notes" in url and "with-notes" not in url:
            self.posted_body = json.loads(data.decode("utf-8")).get("body") or ""
            if self.failures_left:
                self.failures_left -= 1
                raise urlerror.HTTPError(
                    url, 500, "Server Error", {}, io.BytesIO(b"down")
                )
            self.note_count += 1
            self.latest_id = "n-retry"
            return HELPERS.FakeResponse({"noteId": "n-retry"})
        if "with-notes" in url:
            return self._with_notes(url)
        if "v8/discussions/" in url:
            return HELPERS.FakeResponse(
                {
                    "discussionId": "d1",
                    "title": "Ascend - Payments",
                    "noteCount": self.note_count,
                    "mostRecentNoteId": self.latest_id,
                }
            )
        raise AssertionError(url)

    def _with_notes(self, url):
        if self.mode == "unavailable":
            raise urlerror.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b"missing"))
        other = {"noteId": "n-other", "body": "A different note that is not this bill."}
        if self.mode == "landed":
            notes = [{"noteId": "n-landed", "body": self.posted_body}]
            count = 1
            extra = {}
        elif self.mode == "absent":
            notes = [other]
            count = 1
            extra = {}
        else:
            notes = [other]
            count = 4
            extra = {"next": "page-2"}
        return HELPERS.FakeResponse(
            {
                "discussionId": "d1",
                "title": "Ascend - Payments",
                "noteCount": count,
                "notes": notes,
                **extra,
            }
        )

    def posts_to(self, needle):
        return [
            call
            for call in self.calls
            if needle in call["url"] and "with-notes" not in call["url"] and call["data"]
        ]

    def plain_reads(self):
        return [
            call
            for call in self.calls
            if "v8/discussions/" in call["url"]
            and "with-notes" not in call["url"]
            and "by-applicant" not in call["url"]
            and not call["data"]
        ]


def _discussion_that_fails_the_first_note(mode: str):
    from robie_job_engine import ezlynx_discussions as discussions

    poster = _FlakyNotePost(mode)
    config = discussions.DiscussionApiConfig(
        discussion_base_url="https://app.uatezlynx.com/DiscussionApi/",
        token_endpoint="https://identity.example.com/connect/token",
        client_id="street_smart_api",
        client_secret="secret",
        username="SSRobie",
        integration_group_id="159",
    )
    client = discussions.DiscussionApiClient(config, urlopen=poster)
    return client, poster


def _ready_row(tmp_path, monkeypatch):
    monkeypatch.setattr(
        driver.zapier_tasks, "fire_task", lambda payload, *, dry_run=False: {"ok": True}
    )
    store, _summary = _run_unmatched(tmp_path, monkeypatch, TrapEzlynx())
    saved = store.list_unmatched()[0]
    store.mark_ready_to_file(saved["event_key"], "2026-10-05T14:00:00Z")
    monkeypatch.setenv(source.LIVE_ENV, "1")
    return store


def _poll_ready(store, ctx, when):
    return source.run_once(
        client=HELPERS.FeedClient(_empty_feeds()),
        store=store,
        driver_ctx=ctx,
        now=when,
    )


def _ready_discussion(tmp_path, monkeypatch, mode: str):
    store = _ready_row(tmp_path, monkeypatch)
    client, poster = _discussion_that_fails_the_first_note(mode)
    ctx = HELPERS.driver_ctx([{"discussionId": "d1", "title": "Ascend - Payments"}])
    ctx.discussion_client = client
    return store, ctx, poster


def test_a_retry_marks_filed_when_the_note_body_is_already_there(tmp_path, monkeypatch):
    store, ctx, poster = _ready_discussion(tmp_path, monkeypatch, "landed")
    _poll_ready(store, ctx, NOW)
    assert len(poster.posts_to("/notes")) == 1
    assert poster.plain_reads()
    assert int(store.list_unmatched()[0]["file_attempts"] or 0) == 0
    _poll_ready(store, ctx, NOW + timedelta(hours=2))
    assert len(poster.posts_to("/notes")) == 1
    assert int(store.list_unmatched()[0]["file_attempts"] or 0) == 0
    assert store.list_unmatched()[0]["resolved_at"]


def test_a_retry_posts_once_when_with_notes_shows_the_body_is_absent(tmp_path, monkeypatch):
    store, ctx, poster = _ready_discussion(tmp_path, monkeypatch, "absent")
    _poll_ready(store, ctx, NOW)
    assert len(poster.posts_to("/notes")) == 1
    assert poster.plain_reads()
    assert int(store.list_unmatched()[0]["file_attempts"] or 0) == 0
    _poll_ready(store, ctx, NOW + timedelta(hours=2))
    assert len(poster.posts_to("/notes")) == 2
    assert int(store.list_unmatched()[0]["file_attempts"] or 0) == 0
    assert store.list_unmatched()[0]["resolved_at"]
    _poll_ready(store, ctx, NOW + timedelta(hours=2, minutes=15))
    assert len(poster.posts_to("/notes")) == 2


def test_a_retry_does_not_post_when_with_notes_is_unavailable(tmp_path, monkeypatch):
    store, ctx, poster = _ready_discussion(tmp_path, monkeypatch, "unavailable")
    _poll_ready(store, ctx, NOW)
    assert len(poster.posts_to("/notes")) == 1
    _poll_ready(store, ctx, NOW + timedelta(hours=2))
    row = store.list_unmatched()[0]
    assert len(poster.posts_to("/notes")) == 1
    assert row["resolved_at"] in (None, "")
    assert int(row["file_attempts"] or 0) == 0
    assert int(row["transient_retry_used"] or 0) == 1
    assert row["file_failure"] == digest.MAYBE_NOTE_LINE
    monkeypatch.delenv(source.LIVE_ENV, raising=False)
    mailed = digest.run_digest(
        stores=[store], now=NOW + timedelta(hours=2), live=False, refresh=False
    )
    assert digest.MAYBE_NOTE_LINE in mailed["body"]
    assert len(poster.posts_to("/notes")) == 1


def test_a_short_with_notes_page_does_not_clear_the_unconfirmed_note(tmp_path, monkeypatch):
    store, ctx, poster = _ready_discussion(tmp_path, monkeypatch, "truncated")
    _poll_ready(store, ctx, NOW)
    _poll_ready(store, ctx, NOW + timedelta(hours=2))
    row = store.list_unmatched()[0]
    assert len(poster.posts_to("/notes")) == 1
    assert row["resolved_at"] in (None, "")
    assert int(row["file_attempts"] or 0) == 0
    assert row["file_failure"] == digest.MAYBE_NOTE_LINE


def test_settle_keeps_the_guard_unless_this_row_is_on_its_free_retry():
    filed = {
        "status": "already_posted",
        "reason": "I couldn't confirm that note was added.",
    }

    class Client:
        def get_discussion_with_notes(self, discussion_id):
            raise AssertionError(discussion_id)

    kept = driver._settle_unconfirmed_note(
        Client(), "220250093", "d1", "note text", filed, allow=False
    )
    assert kept is filed
    assert "maybe_note" not in kept
    jumped = {
        "status": "already_posted",
        "reason": (
            "The note was sent, but the discussion did not show exactly one new note. "
            "It was not sent again."
        ),
    }
    still = driver._settle_unconfirmed_note(
        Client(), "220250093", "d1", "note text", jumped, allow=True
    )
    assert still is jumped
    assert "maybe_note" not in still


def test_dry_run_does_not_store_the_reread_policy_number(tmp_path, monkeypatch):
    store = source.EventKeyStore(tmp_path / "ascend-api" / "events.db")
    store.upsert_unmatched(
        source.ApiNotice(
            event_key="moved",
            event_type="late_payment",
            program_id="prog-1",
            anchor="2026-10-01T00:00:00Z",
            occurred_at="2026-10-01T00:00:00Z",
            policy_numbers=("HO-OLD",),
            insured_name="Fixture Hauling LLC",
            subject="Past due payment for Fixture Hauling LLC",
            body="Policy ID HO-OLD\nCustomer Fixture Hauling LLC\n",
            program={"id": "prog-1", "policy_number": "HO-OLD"},
        ),
        reason=driver.POLICY_OUTCOME_NOT_IN_EZLYNX,
        seen_at="2026-10-01T00:00:00Z",
    )

    class AscendRead:
        def get_program(self, program_id):
            return {"id": program_id, "policy_number": "HO-NEW"}

    result = digest.run_digest(
        stores=[store],
        now=NOW,
        live=False,
        ezlynx_client=_PolicySearch({"HO-NEW": _policy_hit("HO-NEW", "220250093")}),
        ascend_client=AscendRead(),
        refresh=False,
    )
    row = store.list_unmatched()[0]
    assert row["policy_numbers"] == ["HO-OLD"]
    assert "Policy ID HO-OLD" in row["body"]
    assert "HO-NEW" not in row["body"]
    assert json.loads(row["program_json"])["policy_number"] == "HO-OLD"
    assert row["ready_at"] in (None, "")
    assert "HO-NEW" in result["body"]
    assert "or Ascend" in result["body"]


def test_production_digest_uses_the_poll_read_client(tmp_path, monkeypatch, capsys):
    from robie_job_engine.secret_manager import GoogleSecretManagerAccessor

    monkeypatch.setenv("ROBIE_ENV", "PRODUCTION")
    for name in (
        "ROBIE_ASCEND_API_ENABLED",
        "ROBIE_ASCEND_API_KEY",
        "ROBIE_ASCEND_API_KEY_SECRET",
        "ROBIE_ASCEND_API_BASE_URL",
        "ROBIE_ASCEND_API_PRODUCTION_ENABLED",
        "ASCEND_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(source.LIVE_ENV, raising=False)
    db = tmp_path / "events.db"
    monkeypatch.setenv(digest.DB_ENV, str(db))
    monkeypatch.setenv(digest.STATE_ENV, str(tmp_path / "last-run.json"))
    store = source.EventKeyStore(db)
    store.upsert_unmatched(
        source.ApiNotice(
            event_key="prod-read",
            event_type="late_payment",
            program_id="prog-1",
            anchor="2026-10-01T00:00:00Z",
            occurred_at="2026-10-01T00:00:00Z",
            policy_numbers=("HO-998877",),
            insured_name="Fixture Hauling LLC",
            subject="Past due payment for Fixture Hauling LLC",
            body="Policy ID HO-998877\nCustomer Fixture Hauling LLC\n",
            program={"id": "prog-1", "policy_number": "HO-998877"},
        ),
        reason=driver.POLICY_OUTCOME_NOT_IN_EZLYNX,
        seen_at="2026-10-01T00:00:00Z",
    )
    calls = {"configured": 0, "gets": []}

    def _configured():
        calls["configured"] += 1
        raise AssertionError("the write-capable Ascend client must not be built")

    monkeypatch.setattr("robie_job_engine.ascend_api.configured_client", _configured)

    def _quiet_secret(self, client=None):
        self._client = None

    def _no_secret(self, resource_name):
        raise RuntimeError("no ascend credential in this test")

    monkeypatch.setattr(GoogleSecretManagerAccessor, "__init__", _quiet_secret)
    monkeypatch.setattr(GoogleSecretManagerAccessor, "access", _no_secret)
    real_build = source.build_client

    def _build():
        client = real_build()
        original = client.get

        def _get(path, query=None):
            calls["gets"].append(str(path))
            return original(path, query)

        client.get = _get
        return client

    monkeypatch.setattr(source, "build_client", _build)
    code = digest.main(["--no-refresh"])
    printed = capsys.readouterr().out
    assert code == 0
    assert calls["configured"] == 0
    assert calls["gets"] == ["/v1/programs/prog-1"]
    assert "The policy number is as Ascend sent it." in printed
    assert "or Ascend" not in printed
    row = store.list_unmatched()[0]
    assert row["policy_numbers"] == ["HO-998877"]


def test_a_locked_attempt_record_does_not_stop_the_poll(tmp_path, monkeypatch):
    import sqlite3

    monkeypatch.setenv(source.LIVE_ENV, "1")
    store = source.EventKeyStore(tmp_path / "ascend-api" / "events.db")
    for index in (1, 2):
        key = f"ready-{index}"
        store.upsert_unmatched(
            source.ApiNotice(
                event_key=key,
                event_type="late_payment",
                program_id=HELPERS._pid(1),
                anchor="2026-10-01T00:00:00Z",
                occurred_at="2026-10-01T00:00:00Z",
                policy_numbers=(f"HO-{index:04d}",),
                insured_name=f"Insured {index}",
                subject=f"Past due payment for Insured {index}",
                body=f"Policy ID HO-{index:04d}\nCustomer Insured {index}\n",
                program={"id": HELPERS._pid(1)},
            ),
            reason=driver.POLICY_OUTCOME_NOT_IN_EZLYNX,
            seen_at="2026-10-01T00:00:00Z",
        )
        store.mark_ready_to_file(key, "2026-10-05T14:00:00Z")
    seen: list[str] = []

    def _boom(_ctx, item):
        seen.append(item.event_key)
        raise sqlite3.OperationalError("database is locked")

    def _locked(self, event_key, attempted_at, plain_reason):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(source, "_file_through_driver", _boom)
    monkeypatch.setattr(source.EventKeyStore, "note_ready_attempt", _locked)
    summary = source.run_once(
        client=HELPERS.FeedClient(_empty_feeds()),
        store=store,
        driver_ctx=HELPERS.driver_ctx(),
        now=NOW,
    )
    assert seen == ["ready-1", "ready-2"]
    assert summary["ready_checked"] == 2
    assert summary["errors"] == []
    assert store.latest_poll_run()["ok"] is True
    assert store.list_unmatched()[0]["resolved_at"] in (None, "")
    assert store.list_unmatched()[1]["resolved_at"] in (None, "")


def test_a_refused_lease_records_the_poll_as_not_filing(tmp_path, monkeypatch):
    monkeypatch.setenv(source.LIVE_ENV, "1")
    monkeypatch.setattr(driver, "_driver_gate_refusal", lambda: "driver_gate_refused: lease")
    store = source.EventKeyStore(tmp_path / "ascend-api" / "events.db")
    store.upsert_unmatched(
        source.ApiNotice(
            event_key="held",
            event_type="late_payment",
            program_id=HELPERS._pid(1),
            anchor="2026-10-01T00:00:00Z",
            occurred_at="2026-10-01T00:00:00Z",
            policy_numbers=("HO-998877",),
            insured_name="Fixture Hauling LLC",
            subject="Past due payment for Fixture Hauling LLC",
            body="Policy ID HO-998877\nCustomer Fixture Hauling LLC\n",
            program={"id": HELPERS._pid(1)},
        ),
        reason=driver.POLICY_OUTCOME_NOT_IN_EZLYNX,
        seen_at="2026-10-01T00:00:00Z",
    )
    store.mark_ready_to_file("held", "2026-10-05T14:00:00Z")
    seen: list[str] = []
    monkeypatch.setattr(
        source, "_file_through_driver", lambda _ctx, item: seen.append(item.event_key)
    )
    summary = source.run_once(
        client=HELPERS.FeedClient(_empty_feeds()),
        store=store,
        driver_ctx=HELPERS.driver_ctx(),
        now=NOW,
    )
    assert seen == []
    assert summary["live"] is False
    assert summary["ready_checked"] == 0
    assert store.latest_poll_run()["live"] is False
    assert store.list_unmatched()[0]["resolved_at"] in (None, "")
    assert store.list_unmatched()[0]["file_attempts"] in (None, 0)


def test_health_stays_quiet_when_the_digest_is_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(digest, "timer_is_enabled", lambda: False)
    marker = tmp_path / "unmatched-digest-installed"
    monkeypatch.setenv(digest.MARKER_ENV, str(marker))
    state = tmp_path / "unmatched-digest-last-run.json"
    missing = digest.evaluate_digest_health(state, now=NOW)
    assert missing["ok"] is True
    assert missing["installed"] is False
    assert "not installed" in missing["detail"]
    state.write_text(
        json.dumps({"run_at": "2026-10-05T12:30:00Z", "exit_code": 1, "error": "send failed"}),
        encoding="utf-8",
    )
    leftover = digest.evaluate_digest_health(state, now=NOW)
    assert leftover["ok"] is True
    assert leftover["installed"] is False
    monkeypatch.setattr(digest, "timer_is_enabled", lambda: True)
    watched = digest.evaluate_digest_health(state, now=NOW)
    assert watched["ok"] is False
    assert watched["installed"] is True
    assert "failed" in watched["detail"]


def test_existing_database_gains_unmatched_tables(tmp_path):
    import sqlite3

    path = tmp_path / "ascend-api" / "events.db"
    source.EventKeyStore(path)
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE unmatched_notices")
    conn.execute("DROP TABLE ezlynx_policy_index")
    conn.execute("DROP TABLE ezlynx_policy_index_meta")
    conn.commit()
    conn.close()
    store = source.EventKeyStore(path)
    assert store.list_unmatched() == []
    again = source.EventKeyStore(path)
    assert again.list_unmatched() == []

    older = tmp_path / "ascend-api" / "older.db"
    conn = sqlite3.connect(older)
    conn.execute(
        """
        CREATE TABLE poll_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            ok INTEGER NOT NULL,
            error TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE unmatched_notices (
            event_key TEXT PRIMARY KEY,
            insured_name TEXT NOT NULL DEFAULT '',
            program_id TEXT NOT NULL DEFAULT '',
            loan_id TEXT NOT NULL DEFAULT '',
            policy_numbers TEXT NOT NULL DEFAULT '[]',
            notice_type TEXT NOT NULL DEFAULT '',
            amount_cents INTEGER,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            reason TEXT NOT NULL,
            resolved_at TEXT,
            suggestion_client TEXT NOT NULL DEFAULT '',
            suggestion_policy TEXT NOT NULL DEFAULT '',
            aged_out_notified_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()
    upgraded = source.EventKeyStore(older)
    upgraded.mark_ready_to_file("missing", "2026-10-05T14:00:00Z")
    assert upgraded.list_ready_to_file() == []
    conn = sqlite3.connect(older)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(unmatched_notices)")}
    poll_columns = {row[1] for row in conn.execute("PRAGMA table_info(poll_runs)")}
    conn.close()
    assert {
        "ready_at",
        "subject",
        "body",
        "program_json",
        "file_attempts",
        "last_attempt_at",
        "file_failure",
        "transient_hold_until",
        "transient_retry_used",
    } <= columns
    assert "live" in poll_columns
