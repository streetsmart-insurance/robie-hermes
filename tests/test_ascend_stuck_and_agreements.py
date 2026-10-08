"""Stuck 'name search incomplete' notices, the review list, and new finance agreements."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import tempfile
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from robie_job_engine import ascend_api_notice_source as source
from robie_job_engine import ascend_notice_driver as driver
from robie_job_engine import ascend_notice_triage as triage
from robie_job_engine import ascend_unmatched_digest as digest
from robie_job_engine.ezlynx_api import EzlynxApiClient

ROOT = Path(__file__).resolve().parents[1]


def _helpers():
    path = Path(__file__).with_name("test_ascend_api_notice_source.py")
    spec = importlib.util.spec_from_file_location("ascend_notice_source_helpers_stuck", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


H = _helpers()
NOW = datetime(2026, 10, 7, 18, 0, tzinfo=timezone.utc)


class BookOnlyEzlynx:
    """Like the real PolicyApi: exact policy search only, no name search."""

    identity_search_supported = False

    def __init__(self, book: dict[str, str] | None = None):
        self.book = dict(book or {})
        self.calls: list[str] = []

    def search_policy_by_number(self, number):
        self.calls.append(f"policy:{number}")
        applicant = self.book.get(number)
        if applicant is None:
            return {"data": [], "totalSize": 0}
        return {"data": [{"policyNumber": number, "accountId": applicant}], "totalSize": 1}

    def __getattr__(self, name):
        if name.startswith("search_applicants"):
            raise AssertionError(f"identity search must not be called: {name}")
        raise AttributeError(name)


# ---------------------------------------------------------------- item 7


def test_real_client_says_identity_search_cannot_work():
    assert EzlynxApiClient.identity_search_supported is False


def test_policy_miss_goes_to_review_with_a_plain_reason_and_no_name_search():
    client = BookOnlyEzlynx()
    resolution, reason = driver.resolve_applicant(
        client, ["GS1344166"], "MTZ Racks LLC", "owner@example.test", "732-555-0100"
    )
    assert resolution is None
    assert reason == "applicant_unresolved: policy not in EZLynx"
    assert "incomplete" not in reason
    assert client.calls == ["policy:GS1344166"]
    assert driver.current_policy_search_outcome() == driver.POLICY_OUTCOME_NOT_IN_EZLYNX


def test_no_policy_number_goes_to_review_with_a_plain_reason():
    client = BookOnlyEzlynx()
    resolution, reason = driver.resolve_applicant(client, [], "Joseph's Tree N Landscaping LLC")
    assert resolution is None
    assert reason == "applicant_unresolved: no policy number"
    assert client.calls == []


def test_a_client_without_the_flag_keeps_the_old_cascade():
    calls = []

    class Old:
        def search_policy_by_number(self, number):
            return {"data": [], "totalSize": 0}

        def search_applicants_by_name(self, name):
            calls.append(name)
            return {"data": [{"ApplicantName": name, "accountId": "1"}], "totalSize": 1}

    resolution, _reason = driver.resolve_applicant(Old(), ["X1"], "Fixture Hauling LLC")
    assert resolution is not None and resolution.applicant_id == "1"
    assert calls == ["Fixture Hauling LLC"]


def _email(subject: str, body: str, mid: str = "mike@streetsmart.insurance:1") -> driver.EmailNotice:
    return driver.EmailNotice(
        message_id=mid, subject=subject, body=body, internal_date="2026-10-07T17:00:00Z"
    )


def _email_ctx(ezlynx, program_id: str) -> driver.DriverContext:
    ctx = H.driver_ctx()
    ctx.ascend_client = H.ProgramClient({"id": program_id, "producer": H.PRODUCER})
    ctx.ezlynx_client = ezlynx
    ctx.now = NOW
    return ctx


def _api_store_with_program_policies(path: Path, program_id: str, numbers: list[str]) -> None:
    store = source.EventKeyStore(path)
    with store._connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO program_policies (program_id, policy_numbers, updated_at)"
            " VALUES (?, ?, ?)",
            (program_id, json.dumps(numbers), "2026-10-07T12:00:00Z"),
        )


def test_email_with_no_policy_uses_the_ascend_program_policy_and_matches(tmp_path, monkeypatch):
    program = H._pid(31)
    api_db = tmp_path / "ascend-api" / "events.db"
    _api_store_with_program_policies(api_db, program, ["AEXL100000453", "ACBS700002917"])
    monkeypatch.setenv(source.DB_ENV, str(api_db))
    monkeypatch.setenv(source.DRY_RUN_DB_ENV, str(tmp_path / "ascend-api" / "dry.db"))
    monkeypatch.setenv(driver.EMAIL_REVIEW_DB_ENV, str(tmp_path / "email-unmatched.db"))
    ezlynx = BookOnlyEzlynx({"ACBS700002917": H.APPLICANT})
    body = (
        "We are processing a payment of $1,234.56.\n"
        f"https://dashboard.useascend.com/programs/{program}\n"
    )
    result = driver.process_notice(
        _email("Processing payment for Phoenix Realty Investment LLC", body), _email_ctx(ezlynx, program)
    )
    assert result.status == "dry_run", result.reason
    assert result.detail["applicant_id"] == H.APPLICANT
    assert result.detail["policy_number"] == "ACBS700002917"
    assert result.detail["policy_numbers_from"] == "ascend_program"
    assert result.detail["discussion_title"] == "Ascend - Payments"


def test_unmatched_email_lands_on_the_review_list_once_and_closes_when_matched(tmp_path, monkeypatch):
    program = H._pid(32)
    review = tmp_path / "email-unmatched.db"
    monkeypatch.setenv(source.DB_ENV, str(tmp_path / "ascend-api" / "events.db"))
    monkeypatch.setenv(source.DRY_RUN_DB_ENV, str(tmp_path / "ascend-api" / "dry.db"))
    monkeypatch.setenv(driver.EMAIL_REVIEW_DB_ENV, str(review))
    body = (
        "We are processing a payment of $50.00.\nPolicy ID ZZ-123\n"
        f"https://dashboard.useascend.com/programs/{program}\n"
    )
    subject = "Processing payment for Abstract and Aligned, LLC"
    first = driver.process_notice(
        _email(subject, body, "sandy@streetsmart.insurance:a"), _email_ctx(BookOnlyEzlynx(), program)
    )
    second = driver.process_notice(
        _email(subject, body, "carlo@streetsmart.insurance:b"), _email_ctx(BookOnlyEzlynx(), program)
    )
    for res in (first, second):
        assert res.status == "skipped"
        assert res.reason == "applicant_unresolved: policy not in EZLynx"
        assert res.detail["review_list"] is True
    rows = source.EventKeyStore(review).list_unmatched()
    assert len(rows) == 1
    assert rows[0]["reason"] == digest.POLICY_OUTCOME_NOT_IN_EZLYNX
    assert rows[0]["event_key"].startswith(driver.EMAIL_REVIEW_KEY_PREFIX)
    # The digest reads the review file and prints a plain line.
    monkeypatch.delenv(digest.DB_ENV, raising=False)
    result = digest.run_digest(
        stores=[source.EventKeyStore(review)], now=NOW, live=False, refresh=False
    )
    assert result["open_count"] == 1
    assert "Abstract and Aligned, LLC, processing payment, $50.00, policy ZZ-123." in result["body"]
    # Fixed in EZLynx: the next run files and the row closes.
    third = driver.process_notice(
        _email(subject, body, "sandy@streetsmart.insurance:a"),
        _email_ctx(BookOnlyEzlynx({"ZZ-123": "220250093"}), program),
    )
    assert third.status == "dry_run"
    assert not str(source.EventKeyStore(review).list_unmatched()[0]["resolved_at"] or "") == ""


def test_digest_drops_the_email_copy_of_an_api_row():
    api_row = {"event_key": "p1|late_payment|INV-1", "program_id": "P1", "notice_type": "late_payment"}
    email_row = {"event_key": "email|p1|late_payment|abc", "program_id": "p1", "notice_type": "late_payment"}
    other = {"event_key": "email|p2|processing_payment|def", "program_id": "p2", "notice_type": "processing_payment"}
    kept = digest.drop_email_duplicates([api_row, email_row, other])
    assert kept == [api_row, other]


def test_review_path_defaults():
    os.environ.pop(driver.EMAIL_REVIEW_DB_ENV, None)
    os.environ.pop("ASCEND_DRIVER_STATE_DIR", None)
    assert driver.email_review_db_path() is None
    os.environ["ASCEND_DRIVER_STATE_DIR"] = "/var/lib/robie-ascend-notice-driver"
    try:
        assert driver.email_review_db_path() == Path(
            "/var/lib/robie-ascend-notice-driver/email-unmatched.db"
        )
    finally:
        os.environ.pop("ASCEND_DRIVER_STATE_DIR", None)


def test_ascend_to_prefix_is_dropped_from_the_name():
    assert source.insured_name_of({"insured": {"business_name": "To: Barschy, LLC DBA Margo's"}}) == (
        "Barschy, LLC DBA Margo's"
    )


# ---------------------------------------------------------------- item 6


def test_digest_unit_can_read_ascend_and_the_email_review_file():
    unit = (ROOT / "deploy" / "systemd" / "robie-ascend-unmatched-digest.service").read_text()
    assert (
        "Environment=ROBIE_ASCEND_API_KEY_SECRET=projects/751771086524/secrets/ascend-prod-api-key/versions/latest"
        in unit
    )
    assert "ReadWritePaths=-/var/lib/robie-ascend-notice-driver" in unit
    assert "ASCEND_UNMATCHED_DIGEST_LIVE=" not in unit


def _stub(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _install(prefix: Path, *args: str) -> subprocess.CompletedProcess[str]:
    systemctl = prefix / "systemctl"
    analyze = prefix / "systemd-analyze"
    _stub(systemctl, "#!/bin/bash\ncase \"$1\" in is-active|is-enabled) echo inactive;; esac\nexit 0\n")
    _stub(analyze, "#!/bin/bash\nexit 0\n")
    return subprocess.run(
        [
            "bash",
            str(ROOT / "scripts" / "install-ascend-unmatched-digest.sh"),
            "--prefix",
            str(prefix),
            "--release-dir",
            str(ROOT),
            "--systemctl",
            str(systemctl),
            "--systemd-analyze",
            str(analyze),
            *args,
        ],
        check=False,
        text=True,
        capture_output=True,
    )


def test_live_with_to_writes_the_recipient_drop_in():
    with tempfile.TemporaryDirectory() as raw:
        prefix = Path(raw)
        done = _install(prefix, "--live", "--to", "accounting@streetsmart.insurance")
        assert done.returncode == 0, done.stderr
        dropins = prefix / "etc" / "systemd" / "system" / "robie-ascend-unmatched-digest.service.d"
        assert "ASCEND_UNMATCHED_DIGEST_LIVE=1" in (dropins / "30-live.conf").read_text()
        assert (
            "Environment=ASCEND_UNMATCHED_DIGEST_TO=accounting@streetsmart.insurance"
            in (dropins / "40-recipient.conf").read_text()
        )
        assert "RECIPIENT=accounting@streetsmart.insurance" in done.stdout
        again = _install(prefix, "--live")
        assert again.returncode == 0, again.stderr
        assert not (dropins / "40-recipient.conf").exists()


@pytest.mark.parametrize(
    "args",
    [
        ("--dry-run-once", "--to", "accounting@streetsmart.insurance"),
        ("--live", "--to", "someone@gmail.com"),
        ("--live", "--to", "a@streetsmart.insurance,b@streetsmart.insurance"),
    ],
)
def test_to_is_refused_without_live_or_outside_the_agency(args):
    with tempfile.TemporaryDirectory() as raw:
        done = _install(Path(raw), *args)
        assert done.returncode == 2
        assert not (
            Path(raw) / "etc" / "systemd" / "system" / "robie-ascend-unmatched-digest.service.d" / "40-recipient.conf"
        ).exists()


# ---------------------------------------------------------------- item 8


def _agreement_program(n: int, *, when: str, option: str = "monthly_financed", **extra) -> dict:
    return H._program(
        n,
        "purchased",
        when,
        purchased_at=when,
        checkedout_at=when,
        selected_payment_option_type=option,
        premium_cents=2878000,
        **extra,
    )


def _agreement_loan(program_n: int, when: str) -> dict:
    loan = H._loan(40 + program_n, program_n, "ready", when)
    loan.update(
        amount_financed_cents=2302400,
        downpayment_cents=575600,
        number_of_payments=10,
        term_payment_cents=240520,
    )
    return loan


def test_a_financed_checkout_is_one_new_agreement_notice():
    when = "2026-10-07T17:55:00Z"
    notices = source.notices_from_snapshot(
        programs=[_agreement_program(1, when=when)],
        loans=[_agreement_loan(1, when)],
        invoices=[],
        payouts=[],
        since=NOW - timedelta(minutes=20),
        now=NOW,
    )
    new = [n for n in notices if n.event_type == triage.NEW_PROGRAM]
    assert len(new) == 1
    notice = new[0]
    assert notice.event_key == f"{H._pid(1)}|new_program|agreement"
    assert triage.classify_notice(notice.subject, notice.body) == triage.NEW_PROGRAM
    note = triage.build_staff_note(
        triage.NEW_PROGRAM, notice.subject, notice.body, list(notice.policy_numbers), notice.insured_name
    )
    assert note.splitlines()[0] == (
        "NEW FINANCE AGREEMENT notice from Ascend. Policy HO-998877. The client signed a new "
        "finance agreement with Ascend: $23,024.00 financed, $5,756.00 down, 10 payments of $2,405.20."
    )
    for forbidden in ("program_id", "amount_financed", "purchased_at", "selected_payment"):
        assert forbidden not in note


def test_pay_in_full_and_old_purchases_are_not_new_agreements():
    old = "2026-09-01T12:00:00Z"
    fresh = "2026-10-07T17:55:00Z"
    notices = source.notices_from_snapshot(
        programs=[
            _agreement_program(2, when=fresh, option="annual_pay_in_full"),
            _agreement_program(3, when=old),
            H._program(4, "ready_for_checkout", fresh, selected_payment_option_type="monthly_financed"),
        ],
        loans=[],
        invoices=[],
        payouts=[],
        since=NOW - timedelta(minutes=20),
        now=NOW,
    )
    assert [n for n in notices if n.event_type == triage.NEW_PROGRAM] == []


def test_new_agreement_is_filed_on_ascend_payments_in_a_dry_run(tmp_path, monkeypatch):
    monkeypatch.delenv(source.LIVE_ENV, raising=False)
    when = "2026-10-07T17:55:00Z"
    pages = {
        source.FEED_PROGRAMS: {"data": [_agreement_program(1, when=when, renews_id="r1")], "meta": {"next": None}},
        source.FEED_LOANS: {"data": [_agreement_loan(1, when)], "meta": {"next": None}},
        source.FEED_INVOICES: {"data": [], "meta": {"next": None}},
        source.FEED_PAYOUTS: {"data": [], "meta": {"next": None}},
    }
    store = source.EventKeyStore(tmp_path / "ascend-api" / "events.db")
    summary = source.run_once(
        client=H.FeedClient(pages),
        store=store,
        driver_ctx=H.driver_ctx(),
        now=NOW,
        since=NOW - timedelta(minutes=20),
    )
    rows = [r for r in summary["results"] if r.get("event_type") == triage.NEW_PROGRAM]
    assert len(rows) == 1, summary["results"]
    assert rows[0]["status"] == "dry_run", rows[0]
    assert rows[0]["detail"]["discussion_title"] == "Ascend - Payments"
    assert rows[0]["detail"]["applicant_id"] == H.APPLICANT
    assert "task" not in rows[0]["detail"]
    assert any("renewal finance agreement" in line.lower() or "new_program" in line for line in summary["would_file_lines"])
