"""Tests for robie_job_engine.ascend_notice_driver.

All IO is faked: no network, no Secret Manager, no Gmail, no zap-trigger.
Covers the safety contract: dry-run writes nothing, and every failure mode
fails closed per email without touching anything else.
"""

import json
import os
import unittest
from datetime import date
from pathlib import Path
from unittest import mock
from urllib import parse

import pytest

from robie_job_engine import ascend_notice_driver as driver
from robie_job_engine import ascend_notice_triage as triage
from robie_job_engine import ezlynx_discussions as discussions
from robie_job_engine.gmail_accountability import (
    GMAIL_METADATA_SCOPE,
    GMAIL_MODIFY_SCOPE,
    GMAIL_READONLY_SCOPE,
)


ALLOWED_APPLICANT = "220250093"
TOKEN_URL = "https://identity.example.com/connect/token"
API_BASE = "https://app.uatezlynx.com/DiscussionApi/"

CANCELLATION_SUBJECT = (
    "The coverage policy for Stafford Adult Softball League LLC has been "
    "canceled due to non-payment"
)
CANCELLATION_BODY = (
    "Policy ID HO-998877 Effective 01/01/2026\n"
    "Insured: Stafford Adult Softball League LLC\n"
    "Amount due: $412.10\n"
)


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakeAscendClient:
    """Triage only needs get_program / find_program_by_policy."""

    def __init__(self, program=None):
        self.program = (
            program if program is not None else {"status": "active"}
        )

    def get_program(self, program_uuid):
        return dict(self.program)

    def find_program_by_policy(self, policy_number):
        return {"program": dict(self.program), "program_id": "prog-1"}


class FakeEzlynxClient:
    """PolicyApi search_policy_by_number with canned rows."""

    def __init__(self, rows_by_number=None, fail_with=None):
        self.rows_by_number = rows_by_number or {}
        self.fail_with = fail_with
        self.searched = []

    def search_policy_by_number(self, policy_number):
        self.searched.append(policy_number)
        if self.fail_with is not None:
            raise self.fail_with
        rows = self.rows_by_number.get(policy_number, [])
        return {"status": "success", "data": rows}


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
        return [c for c in self.calls if needle in c["url"] and c["data"]]


def make_discussion_client(discussion_rows, note_id="n7"):
    routes = [
        ("connect/token", {"access_token": "tok123", "expires_in": 3600}),
        ("by-applicant", discussion_rows),
        ("/notes", {"noteId": note_id}),
    ]
    config = discussions.DiscussionApiConfig(
        discussion_base_url=API_BASE,
        token_endpoint=TOKEN_URL,
        client_id="street_smart_api",
        client_secret="secret",
        username="SSRobie",
        integration_group_id="159",
    )
    client = discussions.DiscussionApiClient(config, urlopen=FakeUrlopen(routes))
    return client


class FakeSource:
    def __init__(self, notices):
        self._notices = list(notices)
        self.marked = []

    def fetch_notices(self):
        return list(self._notices)

    def mark_processed(self, message_id):
        self.marked.append(message_id)


def make_notice(subject=CANCELLATION_SUBJECT, body=CANCELLATION_BODY, message_id="m1"):
    return driver.EmailNotice(message_id=message_id, subject=subject, body=body)


def make_ctx(
    *,
    notices,
    policy_rows=None,
    discussion_rows=None,
    dry_run=True,
    ascend_client=None,
    fail_policy_search_with=None,
):
    discussion_rows = (
        discussion_rows
        if discussion_rows is not None
        else [{"discussionId": "d1", "title": "PCR"}]
    )
    discussion_client = make_discussion_client(discussion_rows)
    ctx = driver.DriverContext(
        ascend_client=ascend_client or FakeAscendClient(),
        ezlynx_client=FakeEzlynxClient(
            rows_by_number=policy_rows or {}, fail_with=fail_policy_search_with
        ),
        discussion_client=discussion_client,
        source=FakeSource(notices),
        dry_run=dry_run,
        due_days=2,
        today=date(2026, 9, 15),
    )
    return ctx, discussion_client


@pytest.fixture
def no_zap_fire(monkeypatch):
    fired = []

    def fake_fire(payload, *, dry_run=False):
        fired.append({"payload": dict(payload), "dry_run": dry_run})
        return {"ok": True, "dry_run": dry_run}

    monkeypatch.setattr(driver.zapier_tasks, "fire_task", fake_fire)
    return fired


def policy_row(applicant_id=ALLOWED_APPLICANT, csr_username="KarlaSS", number="HO-998877"):
    return {
        "PolicyNumber": number,
        "ApplicantId": applicant_id,
        "AssignedUsername": csr_username,
    }


# ---------------------------------------------------------------------------
# dry-run writes nothing
# ---------------------------------------------------------------------------


def test_dry_run_writes_nothing(no_zap_fire):
    ctx, discussion_client = make_ctx(
        notices=[make_notice()], policy_rows={"HO-998877": [policy_row()]}
    )
    summary = driver.run_driver(ctx)
    assert summary["dry_run"] is True
    assert summary["dry_runs"] == 1
    assert summary["skipped"] == 0
    result = summary["results"][0]
    assert result["status"] == "dry_run"
    assert result["reason"] == "ok"
    # The note was validated against the real discussion lookup but never posted.
    assert result["detail"]["discussion_id"] == "d1"
    assert discussion_client._urlopen.posts_to("/notes") == []
    # The Zapier fire went through the dry-run validation path only.
    assert len(no_zap_fire) == 1
    assert no_zap_fire[0]["dry_run"] is True
    payload = no_zap_fire[0]["payload"]
    assert payload["applicant_id"] == ALLOWED_APPLICANT
    assert payload["assignee"] == "KarlaSS"
    assert payload["due_date"] == "2026-09-17"
    # Dry-run never marks mail read.
    assert ctx.source.marked == []


def test_dry_run_logs_what_it_would_do(no_zap_fire):
    ctx, _ = make_ctx(notices=[make_notice()], policy_rows={"HO-998877": [policy_row()]})
    summary = driver.run_driver(ctx)
    detail = summary["results"][0]["detail"]
    assert detail["applicant_id"] == ALLOWED_APPLICANT
    assert detail["csr_username"] == "KarlaSS"
    assert detail["notice_type"] == triage.CANCELLATION
    assert "Ascend notice: cancellation." in detail["note_text"]
    assert driver.ROBIE_WAS_HERE in detail["note_text"]
    assert detail["task_payload"]["task_title"].startswith("Ascend cancellation notice")


# ---------------------------------------------------------------------------
# fail-closed: applicant / CSR
# ---------------------------------------------------------------------------


def test_missing_applicant_fails_closed(no_zap_fire):
    # Policy number present but matches nothing in EZLynx -> skipped.
    notice = make_notice(body="Policy ID ZZ-000000 Effective 01/01/2026\nInsured: Some LLC")
    ctx, discussion_client = make_ctx(notices=[notice])
    summary = driver.run_driver(ctx)
    result = summary["results"][0]
    assert result["status"] == "skipped"
    assert "applicant_unresolved" in result["reason"]
    assert discussion_client._urlopen.posts_to("/notes") == []
    assert no_zap_fire == []


def test_notice_without_policy_number_fails_closed(no_zap_fire):
    # No policy number at all: triage itself flags human review -> skipped.
    notice = make_notice(body="No policy id here. Insured: Some LLC")
    ctx, discussion_client = make_ctx(notices=[notice])
    summary = driver.run_driver(ctx)
    result = summary["results"][0]
    assert result["status"] == "skipped"
    assert discussion_client._urlopen.posts_to("/notes") == []
    assert no_zap_fire == []


def test_policy_search_failure_fails_closed(no_zap_fire):
    ctx, discussion_client = make_ctx(
        notices=[make_notice()],
        fail_policy_search_with=RuntimeError("boom"),
    )
    summary = driver.run_driver(ctx)
    result = summary["results"][0]
    assert result["status"] == "skipped"
    assert "policy_search_failed" in result["reason"]
    assert discussion_client._urlopen.posts_to("/notes") == []
    assert no_zap_fire == []


def test_missing_csr_fails_closed(no_zap_fire):
    row = {"PolicyNumber": "HO-998877", "ApplicantId": ALLOWED_APPLICANT}
    ctx, discussion_client = make_ctx(
        notices=[make_notice()], policy_rows={"HO-998877": [row]}
    )
    summary = driver.run_driver(ctx)
    result = summary["results"][0]
    assert result["status"] == "skipped"
    assert "csr_unresolved" in result["reason"]
    assert discussion_client._urlopen.posts_to("/notes") == []
    assert no_zap_fire == []


def test_display_name_csr_is_rejected_never_used(no_zap_fire):
    # AssignedUser is a display name, not a login username: must fail closed.
    row = {
        "PolicyNumber": "HO-998877",
        "ApplicantId": ALLOWED_APPLICANT,
        "AssignedUser": "Karla Brown",
        "ProducerName": "Karla Brown",
    }
    ctx, _ = make_ctx(notices=[make_notice()], policy_rows={"HO-998877": [row]})
    summary = driver.run_driver(ctx)
    result = summary["results"][0]
    assert result["status"] == "skipped"
    assert "csr_unresolved" in result["reason"]


def test_csr_username_must_be_username_shaped(no_zap_fire):
    for bad in ["Karla Brown", "karla@x", "", "a"]:
        row = {
            "PolicyNumber": "HO-998877",
            "ApplicantId": ALLOWED_APPLICANT,
            "AssignedUsername": bad,
        }
        ctx, _ = make_ctx(notices=[make_notice()], policy_rows={"HO-998877": [row]})
        result = driver.run_driver(ctx)["results"][0]
        assert result["status"] == "skipped", bad
        assert "csr_unresolved" in result["reason"], bad


# ---------------------------------------------------------------------------
# fail-closed: triage
# ---------------------------------------------------------------------------


def test_needs_human_review_skipped(no_zap_fire):
    notice = make_notice(subject="Your monthly statement is ready", body="hello")
    ctx, discussion_client = make_ctx(notices=[notice])
    summary = driver.run_driver(ctx)
    result = summary["results"][0]
    assert result["status"] == "skipped"
    assert "needs_human_review" in result["reason"]
    assert discussion_client._urlopen.posts_to("/notes") == []
    assert no_zap_fire == []
    assert ctx.ezlynx_client.searched == []  # no lookup attempted


# ---------------------------------------------------------------------------
# fail-closed: phone numbers and write scope
# ---------------------------------------------------------------------------


def test_phone_number_in_note_text_is_rejected(no_zap_fire):
    # The triage note echoes the email subject, so a phone number there must
    # be refused before anything is written.
    subject = (
        "The coverage policy for Stafford Adult Softball League LLC has been "
        "canceled due to non-payment, call 603-769-3995"
    )
    notice = make_notice(subject=subject)
    ctx, discussion_client = make_ctx(
        notices=[notice], policy_rows={"HO-998877": [policy_row()]}
    )
    summary = driver.run_driver(ctx)
    result = summary["results"][0]
    assert result["status"] == "skipped"
    assert "discussion_error" in result["reason"]
    assert "phone-number-like" in result["reason"]
    assert discussion_client._urlopen.posts_to("/notes") == []
    assert no_zap_fire == []


def test_write_scope_refusal_fails_closed(no_zap_fire):
    row = policy_row(applicant_id="999999999", csr_username="KarlaSS")
    ctx, discussion_client = make_ctx(
        notices=[make_notice()], policy_rows={"HO-998877": [row]}
    )
    summary = driver.run_driver(ctx)
    result = summary["results"][0]
    assert result["status"] == "skipped"
    assert "write_scope_refused" in result["reason"]
    assert discussion_client._urlopen.posts_to("/notes") == []
    assert no_zap_fire == []


def test_cancellation_prefers_titled_cancellation_discussion(no_zap_fire):
    rows = [
        {"discussionId": "d0", "title": "Untitled"},
        {"discussionId": "d1", "title": "New Business"},
        {"discussionId": "d2", "title": "Service-Cancellation"},
    ]
    ctx, discussion_client = make_ctx(
        notices=[make_notice()],
        policy_rows={"HO-998877": [policy_row()]},
        discussion_rows=rows,
        dry_run=False,
    )
    summary = driver.run_driver(ctx)
    result = summary["results"][0]
    assert result["status"] == "done"
    assert result["detail"]["discussion_id"] == "d2"
    assert result["detail"]["discussion_title"] == "Service-Cancellation"
    assert driver.ROBIE_WAS_HERE in result["detail"]["note_text"]
    posts = discussion_client._urlopen.posts_to("/notes")
    assert len(posts) == 1
    assert "/v8/discussions/d2/notes" in posts[0]["url"]
    body = json.loads(posts[0]["data"].decode("utf-8"))
    assert driver.ROBIE_WAS_HERE in body["body"]
    assert "bind" not in body["body"].lower()


def test_untitled_only_discussions_are_not_filed(no_zap_fire):
    rows = [{"discussionId": "d1", "title": "Untitled"}]
    ctx, discussion_client = make_ctx(
        notices=[make_notice()],
        policy_rows={"HO-998877": [policy_row()]},
        discussion_rows=rows,
    )
    summary = driver.run_driver(ctx)
    result = summary["results"][0]
    assert result["status"] == "skipped"
    assert "note_not_filed" in result["reason"]
    assert "UNTITLED_FORBIDDEN" in result["reason"]
    assert discussion_client._urlopen.posts_to("/notes") == []
    assert no_zap_fire == []


def test_signed_notice_note_is_plain_and_idempotent():
    raw = "Ascend notice: cancellation.\nInsured: Test LLC"
    signed = driver.signed_notice_note(raw)
    assert signed.endswith(driver.ROBIE_WAS_HERE)
    assert driver.signed_notice_note(signed) == signed
    assert driver.signed_notice_note("") == ""
    assert driver.discussion_title_hint(triage.CANCELLATION) == "cancellation"
    assert driver.discussion_title_hint(triage.LATE_PAYMENT) == "noc"
    assert driver.discussion_title_hint(triage.RETURN_PREMIUM) is None


def test_ambiguous_discussions_are_pending_not_filed(no_zap_fire):
    rows = [
        {"discussionId": "d1", "title": "PCR"},
        {"discussionId": "d2", "title": "COI"},
    ]
    ctx, discussion_client = make_ctx(
        notices=[make_notice()],
        policy_rows={"HO-998877": [policy_row()]},
        discussion_rows=rows,
    )
    summary = driver.run_driver(ctx)
    result = summary["results"][0]
    assert result["status"] == "skipped"
    assert "note_not_filed" in result["reason"]
    assert "AMBIGUOUS_DISCUSSIONS" in result["reason"]
    assert discussion_client._urlopen.posts_to("/notes") == []
    assert no_zap_fire == []


# ---------------------------------------------------------------------------
# non-cancellation notices: note yes, task no
# ---------------------------------------------------------------------------


def test_late_payment_files_note_but_skips_task(no_zap_fire):
    notice = make_notice(
        subject="Past due payment for Shoreline Builders LLC",
        body="Policy ID GL-112233 Effective 02/01/2026\nAmount due: $3,528.22\n",
    )
    ctx, discussion_client = make_ctx(
        notices=[notice], policy_rows={"GL-112233": [policy_row(number="GL-112233")]}
    )
    summary = driver.run_driver(ctx)
    result = summary["results"][0]
    assert result["status"] == "dry_run"
    assert result["detail"]["notice_type"] == triage.LATE_PAYMENT
    assert discussion_client._urlopen.posts_to("/notes") == []  # dry-run: validated only
    assert no_zap_fire == []  # no task builder for late_payment
    assert "no task builder" in result["detail"]["task_skipped"]


# ---------------------------------------------------------------------------
# live mode
# ---------------------------------------------------------------------------


def test_live_mode_files_note_fires_task_and_marks_read(no_zap_fire):
    ctx, discussion_client = make_ctx(
        notices=[make_notice()],
        policy_rows={"HO-998877": [policy_row()]},
        dry_run=False,
    )
    summary = driver.run_driver(ctx)
    assert summary["dry_run"] is False
    assert summary["done"] == 1
    result = summary["results"][0]
    assert result["status"] == "done"
    assert len(discussion_client._urlopen.posts_to("/notes")) == 1
    assert len(no_zap_fire) == 1
    assert no_zap_fire[0]["dry_run"] is False
    assert ctx.source.marked == ["m1"]


def test_live_mode_leaves_failed_email_unread(no_zap_fire):
    good = make_notice(message_id="m1")
    bad = make_notice(message_id="m2", body="No policy id here.")
    ctx, _ = make_ctx(
        notices=[good, bad], policy_rows={"HO-998877": [policy_row()]}
    )
    ctx.dry_run = False
    summary = driver.run_driver(ctx)
    assert summary["done"] == 1
    assert summary["skipped"] == 1
    assert ctx.source.marked == ["m1"]  # the failed one stays unread


# ---------------------------------------------------------------------------
# due date
# ---------------------------------------------------------------------------


def test_due_date_is_real_iso_date():
    assert driver._due_date(date(2026, 9, 15), 2) == "2026-09-17"
    assert driver._due_date(date(2026, 9, 15), 0) == "2026-09-15"


def test_task_payload_requires_valid_due_date():
    from robie_job_engine import zapier_tasks as zt

    with pytest.raises(ValueError, match="due_date"):
        zt.validate_task_payload(
            {
                "applicant_id": ALLOWED_APPLICANT,
                "task_title": "t",
                "assignee": "KarlaSS",
                "source": "inbox-triage",
                "due_date": "tomorrow",
            }
        )


# ---------------------------------------------------------------------------
# no delete surface
# ---------------------------------------------------------------------------


def test_driver_has_no_delete_surface():
    import os

    source = open(os.path.join(os.path.dirname(driver.__file__), "ascend_notice_driver.py")).read()
    assert '"DELETE"' not in source and "'DELETE'" not in source
    for name in dir(driver):
        assert "delete" not in name.lower(), f"unexpected delete API: {name}"


# ---------------------------------------------------------------------------
# stdout contract: the GitHub workflow parses driver stdout as JSON
# ---------------------------------------------------------------------------


def test_main_stdout_is_json_only_on_fatal(monkeypatch, capsys):
    """On a fatal fail-closed, stdout must parse as JSON (log lines go to
    stderr) because the driver workflow feeds stdout to json.tool."""
    monkeypatch.delenv("ROBIE_ENV", raising=False)
    monkeypatch.delenv("ASCEND_DRIVER_LIVE", raising=False)
    rc = driver.main([])
    assert rc == 1
    out, _err = capsys.readouterr()
    payload = json.loads(out)  # raises when stdout is polluted by log lines
    assert payload["dry_run"] is True
    assert "ROBIE_ENV" in payload["fatal"]


# ---------------------------------------------------------------------------
# delegated Gmail scopes: search + body need readonly, not metadata
# ---------------------------------------------------------------------------


def test_notice_source_dry_run_requests_readonly_not_metadata():
    source = driver.GmailNoticeSource(
        mailbox="hello@streetsmart.insurance",
        service_account_email="hermes-poc@example.test",
    )
    assert source.allow_modify is False
    assert source.requested_scopes == (GMAIL_READONLY_SCOPE,)
    assert GMAIL_METADATA_SCOPE not in source.requested_scopes


def test_notice_source_live_requests_modify_for_mark_read():
    source = driver.GmailNoticeSource(
        mailbox="hello@streetsmart.insurance",
        service_account_email="hermes-poc@example.test",
        allow_modify=True,
    )
    assert source.requested_scopes == (GMAIL_READONLY_SCOPE, GMAIL_MODIFY_SCOPE)


def test_notice_source_default_factory_is_notice_not_metadata(monkeypatch):
    seen = {}

    def fake_build(sa, user, *, modify=False):
        seen.update(sa=sa, user=user, modify=modify)
        return object()

    monkeypatch.setattr(
        "robie_job_engine.gmail_accountability.build_notice_gmail_service",
        fake_build,
    )
    source = driver.GmailNoticeSource(
        mailbox="hello@streetsmart.insurance",
        service_account_email="hermes-poc@example.test",
        allow_modify=False,
    )
    source._service_client()
    assert seen == {
        "sa": "hermes-poc@example.test",
        "user": "hello@streetsmart.insurance",
        "modify": False,
    }


def test_notice_source_live_factory_passes_modify(monkeypatch):
    seen = {}

    def fake_build(sa, user, *, modify=False):
        seen.update(sa=sa, user=user, modify=modify)
        return object()

    monkeypatch.setattr(
        "robie_job_engine.gmail_accountability.build_notice_gmail_service",
        fake_build,
    )
    source = driver.GmailNoticeSource(
        mailbox="hello@streetsmart.insurance",
        service_account_email="hermes-poc@example.test",
        allow_modify=True,
    )
    source._service_client()
    assert seen["modify"] is True


def test_notice_driver_source_does_not_import_metadata_factory():
    text = Path(driver.__file__).read_text(encoding="utf-8")
    assert "build_notice_gmail_service" in text
    assert "from .gmail_accountability import build_keyless_delegated_service" not in text


class _FakeApiConfig:
    document_base_url = "https://app.uatezlynx.com"
    token_endpoint = "https://identity.example.com/connect/token"
    client_id = "id"
    client_secret = "secret"
    username = "SSRobie"
    integration_group_id = "159"


def _stub_live_clients(monkeypatch):
    monkeypatch.setenv("ROBIE_GMAIL_DELEGATION_SA", "hermes-poc@example.test")
    monkeypatch.setattr(driver, "load_ezlynx_api_config", lambda: _FakeApiConfig())
    monkeypatch.setattr(driver, "EzlynxApiClient", lambda *_a, **_k: object())
    monkeypatch.setattr(driver, "DiscussionApiClient", lambda *_a, **_k: object())
    monkeypatch.setattr(driver, "configured_ascend_client", lambda: object())


def test_build_live_context_dry_run_does_not_request_modify(monkeypatch):
    _stub_live_clients(monkeypatch)
    monkeypatch.delenv("ASCEND_DRIVER_GMAIL_MODIFY", raising=False)
    ctx = driver.build_live_context(
        mailbox="hello@streetsmart.insurance",
        query=driver.DEFAULT_QUERY,
        dry_run=True,
        due_days=2,
    )
    assert ctx.source.allow_modify is False
    assert ctx.source.requested_scopes == (GMAIL_READONLY_SCOPE,)


def test_build_live_context_live_requests_modify(monkeypatch):
    _stub_live_clients(monkeypatch)
    ctx = driver.build_live_context(
        mailbox="hello@streetsmart.insurance",
        query=driver.DEFAULT_QUERY,
        dry_run=False,
        due_days=2,
    )
    assert ctx.source.allow_modify is True
    assert ctx.source.requested_scopes == (GMAIL_READONLY_SCOPE, GMAIL_MODIFY_SCOPE)


def test_gmail_modify_env_flag_requests_modify_in_dry_run(monkeypatch):
    _stub_live_clients(monkeypatch)
    monkeypatch.setenv("ASCEND_DRIVER_GMAIL_MODIFY", "1")
    ctx = driver.build_live_context(
        mailbox="hello@streetsmart.insurance",
        query=driver.DEFAULT_QUERY,
        dry_run=True,
        due_days=2,
    )
    assert ctx.source.allow_modify is True


class TestNoticeGmailScopeSelection(unittest.TestCase):
    def test_dry_run_scopes_support_unread_search(self):
        source = driver.GmailNoticeSource(
            mailbox="hello@streetsmart.insurance",
            service_account_email="hermes-poc@example.test",
        )
        self.assertEqual(source.requested_scopes, (GMAIL_READONLY_SCOPE,))
        self.assertNotIn(GMAIL_METADATA_SCOPE, source.requested_scopes)

    def test_live_scopes_include_modify(self):
        source = driver.GmailNoticeSource(allow_modify=True)
        self.assertIn(GMAIL_MODIFY_SCOPE, source.requested_scopes)

    def test_modify_flag_follows_live_or_env(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ASCEND_DRIVER_GMAIL_MODIFY", None)
            self.assertFalse(driver._notice_allow_modify(dry_run=True))
            self.assertTrue(driver._notice_allow_modify(dry_run=False))
        with mock.patch.dict(os.environ, {"ASCEND_DRIVER_GMAIL_MODIFY": "1"}):
            self.assertTrue(driver._notice_allow_modify(dry_run=True))
