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
from robie_job_engine import ezlynx_org_labels as org_labels
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


def _mapped_producer():
    """Karla Brown maps to the EZLynx login KarlaSS. Not a shared mailbox."""
    return {
        "email": "karla@streetsmart.insurance",
        "first_name": "Karla",
        "last_name": "Brown",
    }


class FakeAscendClient:
    """Triage only needs get_program / find_program_by_policy."""

    def __init__(self, program=None):
        self.program = (
            program
            if program is not None
            else {"status": "active", "producer": _mapped_producer()}
        )
        self.calls = []

    def get_program(self, program_uuid):
        self.calls.append(("get_program", program_uuid))
        return dict(self.program)

    def find_program_by_policy(self, policy_number):
        self.calls.append(("find_program_by_policy", policy_number))
        return {"program": dict(self.program), "program_id": "prog-1"}


class FakeEzlynxClient:
    """PolicyApi search_policy_by_number with canned rows."""

    def __init__(
        self,
        rows_by_number=None,
        fail_with=None,
        org_labels=None,
        apply_label_error=None,
    ):
        self.rows_by_number = rows_by_number or {}
        self.fail_with = fail_with
        self.searched = []
        self.org_labels = (
            list(org_labels)
            if org_labels is not None
            else [{"id": "noc-1", "name": "Ascend NOC"}]
        )
        self.apply_label_error = apply_label_error
        self.applied_labels = []

    def search_policy_by_number(self, policy_number):
        self.searched.append(policy_number)
        if self.fail_with is not None:
            raise self.fail_with
        rows = self.rows_by_number.get(policy_number, [])
        return {"status": "success", "data": rows}

    def list_organization_labels(self):
        return list(self.org_labels)

    def apply_applicant_organization_label(self, applicant_id, label_id):
        raise AssertionError(
            "OAuth applicant OrganizationLabels is the 403 path; do not call"
        )

    def apply_note_organization_label(self, note_id, label_id):
        if self.apply_label_error is not None:
            raise self.apply_label_error
        self.applied_labels.append({"note_id": note_id, "label_id": label_id})
        return {"status": "applied"}


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
        (
            "v8/discussions/",
            {"discussionId": "d1", "notes": [{"noteId": note_id, "body": "filed"}]},
        ),
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


def make_notice(
    subject=CANCELLATION_SUBJECT,
    body=CANCELLATION_BODY,
    message_id="m1",
    mailbox="hello@streetsmart.insurance",
    gmail_message_id="",
):
    return driver.EmailNotice(
        message_id=message_id,
        subject=subject,
        body=body,
        mailbox=mailbox,
        gmail_message_id=gmail_message_id or message_id,
    )


def make_ctx(
    *,
    notices,
    policy_rows=None,
    discussion_rows=None,
    dry_run=True,
    ascend_client=None,
    fail_policy_search_with=None,
    org_labels=None,
    apply_label_error=None,
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
            rows_by_number=policy_rows or {},
            fail_with=fail_policy_search_with,
            org_labels=org_labels,
            apply_label_error=apply_label_error,
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
    # Dry-run builds the task payload and does not call Zapier.
    assert no_zap_fire == []
    payload = result["detail"]["task_payload"]
    assert payload["applicant_id"] == ALLOWED_APPLICANT
    assert payload["assignee"] == "KarlaSS"
    assert payload["due_date"] == "2026-09-17"
    assert result["detail"]["zapier_result"]["dry_run"] is True
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
    assert detail["label_name"] == "Ascend NOC"
    assert detail["label"]["status"] == "dry_run"
    assert detail["label"]["method"] == "api"
    assert detail["label"]["auth_path"] == "cdp_session_cookie"
    assert ctx.ezlynx_client.applied_labels == []
    assert summary["would_file_count"] == 1
    assert summary["breakdown"]["would_file"] == 1
    assert summary["breakdown"]["by_notice_type"] == {triage.CANCELLATION: 1}
    entry = summary["would_file"][0]
    assert entry["gmail_message_id"] == "m1"
    assert entry["mailbox"] == "hello@streetsmart.insurance"
    assert entry["notice_type"] == triage.CANCELLATION
    assert entry["policy_number"] == "HO-998877"
    assert entry["applicant_id"] == ALLOWED_APPLICANT
    assert entry["csr_login"] == "KarlaSS"


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
    assert ctx.ezlynx_client.applied_labels == []
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
    # PolicyApi has no CSR field. A cancellation with no Ascend producer
    # or account_manager fails closed even when the row has an applicant.
    row = {
        "PolicyNumber": "HO-998877",
        "ApplicantId": ALLOWED_APPLICANT,
        "AssignedUsername": "KarlaSS",
    }
    ctx, discussion_client = make_ctx(
        notices=[make_notice()],
        policy_rows={"HO-998877": [row]},
        ascend_client=FakeAscendClient(program={"status": "canceled"}),
    )
    summary = driver.run_driver(ctx)
    result = summary["results"][0]
    assert result["status"] == "skipped"
    assert "csr_unresolved" in result["reason"]
    assert discussion_client._urlopen.posts_to("/notes") == []
    assert no_zap_fire == []
    assert summary["skipped_by_reason"].get("csr_unresolved") == 1


def test_shared_mailbox_csr_fails_closed(no_zap_fire):
    # hello@ / accounting@ / robie@ are shared. A policy-row username is ignored.
    for email, first, last in (
        ("hello@streetsmart.insurance", "Karla", "Brown"),
        ("accounting@streetsmart.insurance", "Accounting", "Team"),
        ("robie@streetsmart.insurance", "Robie", "AI"),
    ):
        row = {
            "PolicyNumber": "HO-998877",
            "accountId": ALLOWED_APPLICANT,
            "AssignedUsername": "KarlaSS",
        }
        program = {
            "status": "canceled",
            "producer": {"email": email, "first_name": first, "last_name": last},
            "account_manager": _mapped_producer(),
        }
        ctx, _ = make_ctx(
            notices=[make_notice()],
            policy_rows={"HO-998877": [row]},
            ascend_client=FakeAscendClient(program=program),
        )
        result = driver.run_driver(ctx)["results"][0]
        assert result["status"] == "skipped", email
        assert "csr_unresolved" in result["reason"], email
        assert "shared mailbox" in result["reason"], email
        assert result["detail"].get("csr_username") in {None, ""}


def test_unmapped_producer_fails_closed(no_zap_fire):
    program = {
        "status": "canceled",
        "producer": {
            "email": "somebody.nobody@example.com",
            "first_name": "Somebody",
            "last_name": "Nobody",
        },
    }
    ctx, _ = make_ctx(
        notices=[make_notice()],
        policy_rows={"HO-998877": [policy_row()]},
        ascend_client=FakeAscendClient(program=program),
    )
    result = driver.run_driver(ctx)["results"][0]
    assert result["status"] == "skipped"
    assert "csr_unresolved" in result["reason"]
    assert "unmapped" in result["reason"]


def test_cancellation_csr_falls_back_to_account_manager(no_zap_fire):
    program = {
        "status": "canceled",
        "account_manager": {
            "email": "jake@streetsmart.insurance",
            "first_name": "Jake",
            "last_name": "Ferrara",
        },
    }
    ctx, _ = make_ctx(
        notices=[make_notice()],
        policy_rows={"HO-998877": [policy_row(csr_username="")]},
        ascend_client=FakeAscendClient(program=program),
    )
    result = driver.run_driver(ctx)["results"][0]
    assert result["status"] == "dry_run"
    assert result["detail"]["csr_username"] == "jferrara3"
    assert result["detail"]["task_payload"]["assignee"] == "jferrara3"


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
    # Agency-wide is the unset default (PR 468). Pin a restricted list so
    # this still proves the driver skips when the applicant is not allowed.
    row = policy_row(applicant_id="999999999", csr_username="KarlaSS")
    ctx, discussion_client = make_ctx(
        notices=[make_notice()], policy_rows={"HO-998877": [row]}
    )
    with mock.patch(
        "robie_job_engine.ezlynx_write_scope.ALLOWED_EZLYNX_WRITE_APPLICANT_IDS",
        frozenset({ALLOWED_APPLICANT}),
    ):
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
    assert result["detail"]["label"]["label_name"] == "Ascend NOC"
    assert result["detail"]["label"]["auth_path"] == "cdp_session_cookie"
    assert ctx.ezlynx_client.applied_labels == [
        {"note_id": "n7", "label_id": "noc-1"}
    ]


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


def test_missing_or_ambiguous_ascend_noc_does_not_file_or_label(no_zap_fire):
    ctx, discussion_client = make_ctx(
        notices=[make_notice()],
        policy_rows={"HO-998877": [policy_row()]},
        org_labels=[{"id": "c1", "name": "Cancellation"}, {"id": "n1", "name": "Ascend noc"}],
    )
    summary = driver.run_driver(ctx)
    result = summary["results"][0]
    assert result["status"] == "skipped"
    assert "label_not_applied" in result["reason"]
    assert "LABEL_NOT_FOUND" in result["reason"]
    assert discussion_client._urlopen.posts_to("/notes") == []
    assert ctx.ezlynx_client.applied_labels == []
    assert no_zap_fire == []


def test_duplicate_ascend_noc_labels_fail_closed(no_zap_fire):
    ctx, discussion_client = make_ctx(
        notices=[make_notice()],
        policy_rows={"HO-998877": [policy_row()]},
        org_labels=[
            {"id": "a", "name": "Ascend NOC"},
            {"id": "b", "name": "Ascend NOC"},
        ],
    )
    summary = driver.run_driver(ctx)
    result = summary["results"][0]
    assert result["status"] == "skipped"
    assert "LABEL_NOT_UNIQUE" in result["reason"]
    assert discussion_client._urlopen.posts_to("/notes") == []
    assert ctx.ezlynx_client.applied_labels == []


def test_driver_label_path_is_api_not_playwright():
    source = Path(driver.__file__).read_text(encoding="utf-8")
    assert "ezlynx_org_labels" in source
    assert "apply_account_label" not in source
    assert "playwright" not in source.casefold()
    assert "bland" not in source.casefold()


def test_signed_notice_note_is_plain_and_idempotent():
    raw = "Ascend notice: cancellation.\nInsured: Test LLC"
    signed = driver.signed_notice_note(raw)
    assert signed.endswith(driver.ROBIE_WAS_HERE)
    assert driver.signed_notice_note(signed) == signed
    assert driver.signed_notice_note("") == ""
    assert driver.discussion_title_hint(triage.CANCELLATION) == "cancellation"
    assert driver.discussion_title_hint(triage.LATE_PAYMENT) == "noc"
    assert driver.discussion_title_hint(triage.INTENT_TO_CANCEL) == "noc"
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
    # No CSR on the policy row and no producer on the program. Late payment
    # files from applicant_id alone.
    row = {"policyNumber": "GL-112233", "accountId": ALLOWED_APPLICANT}
    ctx, discussion_client = make_ctx(
        notices=[notice],
        policy_rows={"GL-112233": [row]},
        ascend_client=FakeAscendClient(program={"status": "past_due"}),
    )
    summary = driver.run_driver(ctx)
    result = summary["results"][0]
    assert result["status"] == "dry_run"
    assert result["detail"]["notice_type"] == triage.LATE_PAYMENT
    assert result["detail"]["applicant_id"] == ALLOWED_APPLICANT
    assert "csr_username" not in result["detail"]
    assert discussion_client._urlopen.posts_to("/notes") == []  # dry-run: validated only
    assert no_zap_fire == []  # no task builder for late_payment
    assert "no task builder" in result["detail"]["task_skipped"]
    assert "label" not in result["detail"]
    assert ctx.ezlynx_client.applied_labels == []
    entry = summary["would_file"][0]
    assert entry["notice_type"] == triage.LATE_PAYMENT
    assert "csr_login" not in entry


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
    assert result["detail"]["label"]["label_name"] == "Ascend NOC"
    assert result["detail"]["label"]["status"] == "applied"
    assert result["detail"]["label"]["auth_path"] == "cdp_session_cookie"
    assert ctx.ezlynx_client.applied_labels == [
        {"note_id": "n7", "label_id": "noc-1"}
    ]


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


def test_label_apply_403_fail_closed_leaves_unread(no_zap_fire):
    ctx, discussion_client = make_ctx(
        notices=[make_notice()],
        policy_rows={"HO-998877": [policy_row()]},
        dry_run=False,
        apply_label_error=org_labels.OrgLabelError(
            org_labels.LABEL_APPLY_FAILED, "organization label apply failed: HTTP 403"
        ),
    )
    summary = driver.run_driver(ctx)
    result = summary["results"][0]
    assert result["status"] == "skipped"
    assert "LABEL_APPLY_FAILED" in result["reason"]
    assert "403" in result["reason"]
    assert ctx.source.marked == []
    assert ctx.ezlynx_client.applied_labels == []
    assert len(discussion_client._urlopen.posts_to("/notes")) == 1
    assert no_zap_fire == []


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
    monkeypatch.delenv("ASCEND_DRIVER_MAILBOX", raising=False)
    monkeypatch.delenv("ASCEND_DRIVER_MAILBOXES", raising=False)
    monkeypatch.delenv("ASCEND_DRIVER_ALLOW_EXTRA_MAILBOXES", raising=False)
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


def test_gmail_modify_env_flag_does_not_widen_dry_run(monkeypatch):
    _stub_live_clients(monkeypatch)
    monkeypatch.setenv("ASCEND_DRIVER_GMAIL_MODIFY", "1")
    ctx = driver.build_live_context(
        mailbox="hello@streetsmart.insurance",
        query=driver.DEFAULT_QUERY,
        dry_run=True,
        due_days=2,
    )
    assert ctx.source.allow_modify is False
    assert ctx.source.requested_scopes == (GMAIL_READONLY_SCOPE,)


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

    def test_modify_flag_follows_live_not_dry_run(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ASCEND_DRIVER_GMAIL_MODIFY", None)
            self.assertFalse(driver._notice_allow_modify(dry_run=True))
            self.assertTrue(driver._notice_allow_modify(dry_run=False))
        with mock.patch.dict(os.environ, {"ASCEND_DRIVER_GMAIL_MODIFY": "1"}):
            self.assertFalse(driver._notice_allow_modify(dry_run=True))
            self.assertTrue(driver._notice_allow_modify(dry_run=False))


# ---------------------------------------------------------------------------
# policy match, CSR source, defaults, dry-run gate
# ---------------------------------------------------------------------------


def test_policy_suffix_matches_single_account_row():
    client = FakeEzlynxClient(
        rows_by_number={
            "ABC123-00": [{"policyNumber": "ABC123", "accountId": ALLOWED_APPLICANT}]
        }
    )
    resolution, reason = driver.resolve_applicant(client, ["ABC123-00"], None)
    assert reason == ""
    assert resolution is not None
    assert resolution.applicant_id == ALLOWED_APPLICANT
    assert resolution.csr_username == ""
    assert resolution.policy_number == "ABC123-00"


def test_spaced_policy_number_normalizes():
    client = FakeEzlynxClient(
        rows_by_number={
            "ABC123": [{"policyNumber": "abc 123", "accountId": ALLOWED_APPLICANT}]
        }
    )
    resolution, reason = driver.resolve_applicant(client, ["ABC123"], None)
    assert reason == ""
    assert resolution.applicant_id == ALLOWED_APPLICANT


def test_two_candidate_rows_fail_closed():
    client = FakeEzlynxClient(
        rows_by_number={
            "ABC123-00": [
                {"policyNumber": "ABC123", "accountId": "111"},
                {"policyNumber": "ABC123-01", "accountId": "222"},
            ]
        }
    )
    resolution, reason = driver.resolve_applicant(client, ["ABC123-00"], "Fixture Insured")
    assert resolution is None
    assert reason.startswith("applicant_unresolved:")
    assert "2 candidate" in reason


def test_matched_row_without_account_id_fails_closed():
    client = FakeEzlynxClient(
        rows_by_number={"ABC123": [{"policyNumber": "ABC123", "policyStatus": "Active"}]}
    )
    resolution, reason = driver.resolve_applicant(client, ["ABC123"], None)
    assert resolution is None
    assert "1 candidate" in reason
    assert "accountId" in reason


def test_intent_to_cancel_never_builds_cancellation_task(no_zap_fire):
    notice = make_notice(
        subject=(
            "[URGENT] Fixture Insured A LLC - StreetSmart Insurance Agency: "
            "Policy(s) at risk for cancellation"
        ),
        body=(
            "Please see the attached Notice of Intent to Cancel document. "
            "Failure to pay will result in the cancelation of your coverage.\n"
            "Policy ID ABC123-00\nEffective date 01/01/2026\n"
        ),
    )
    ctx, discussion_client = make_ctx(
        notices=[notice],
        policy_rows={
            "ABC123-00": [{"policyNumber": "ABC123", "accountId": ALLOWED_APPLICANT}]
        },
        discussion_rows=[
            {"discussionId": "d-can", "title": "Service-Cancellation"},
            {"discussionId": "d-noc", "title": "Ascend NOC"},
        ],
        ascend_client=FakeAscendClient(program={"status": "overdue"}),
    )
    summary = driver.run_driver(ctx)
    result = summary["results"][0]
    assert result["status"] == "dry_run"
    assert result["detail"]["notice_type"] == triage.INTENT_TO_CANCEL
    assert result["detail"]["discussion_id"] == "d-noc"
    assert "label" not in result["detail"]
    assert "task_payload" not in result["detail"]
    assert no_zap_fire == []
    assert discussion_client._urlopen.posts_to("/notes") == []
    assert ctx.ezlynx_client.applied_labels == []
    with pytest.raises(ValueError, match="cancellation"):
        triage.build_cancellation_task_payload(
            {"notice_type": triage.INTENT_TO_CANCEL},
            applicant_id=ALLOWED_APPLICANT,
            account_csr="KarlaSS",
            due_date="2026-09-17",
        )


def test_informational_mail_is_ignored_not_skipped(no_zap_fire):
    class _RaisingAscend:
        def get_program(self, *_args, **_kwargs):
            raise AssertionError("ignored mail must not call Ascend")

        def find_program_by_policy(self, *_args, **_kwargs):
            raise AssertionError("ignored mail must not call Ascend")

    notice = make_notice(
        subject="Processing payment for Fixture Insured A LLC",
        body="Your customer has just initiated their payment.",
        message_id="ign-1",
    )
    ctx, discussion_client = make_ctx(
        notices=[notice],
        ascend_client=_RaisingAscend(),
    )
    summary = driver.run_driver(ctx)
    result = summary["results"][0]
    assert result["status"] == "ignored"
    assert result["reason"] == "ignored"
    assert "needs_human_review" not in result["reason"]
    assert summary["ignored"] == 1
    assert summary["skipped"] == 0
    assert summary["would_file"] == []
    assert summary["breakdown"]["ignored"] == 1
    assert summary["breakdown"]["by_notice_type"]["processing_payment"] == 1
    assert discussion_client._urlopen.posts_to("/notes") == []
    assert ctx.ezlynx_client.searched == []
    assert no_zap_fire == []


def test_dry_run_does_not_call_driver_gate(no_zap_fire, monkeypatch):
    def _boom(*_args, **_kwargs):
        raise AssertionError("dry-run called the write gate")

    monkeypatch.setattr("robie_job_engine.safety_seal.driver_gate_for_write", _boom)
    monkeypatch.setattr(
        "robie_job_engine.ezlynx_write_scope.require_allowed_ezlynx_write_applicant",
        _boom,
    )
    ctx, discussion_client = make_ctx(
        notices=[make_notice()],
        policy_rows={"HO-998877": [policy_row()]},
    )
    summary = driver.run_driver(ctx)
    assert summary["results"][0]["status"] == "dry_run"
    assert discussion_client._urlopen.posts_to("/notes") == []
    assert ctx.ezlynx_client.applied_labels == []


def test_default_query_and_mailboxes(monkeypatch):
    for name in (
        "ASCEND_DRIVER_QUERY",
        "ASCEND_DRIVER_MAILBOX",
        "ASCEND_DRIVER_MAILBOXES",
        "ASCEND_DRIVER_ALLOW_EXTRA_MAILBOXES",
    ):
        monkeypatch.delenv(name, raising=False)
    assert driver.DEFAULT_QUERY == (
        "is:unread newer_than:2d from:(no-reply@useascend.com OR accounting@useascend.com)"
    )
    assert driver.configured_query() == driver.DEFAULT_QUERY
    assert driver.resolve_mailboxes() == [
        "hello@streetsmart.insurance",
        "mike@streetsmart.insurance",
        "angie@streetsmart.insurance",
        "eimy@streetsmart.insurance",
        "sandy@streetsmart.insurance",
        "zeus@streetsmart.insurance",
        "taylor@streetsmart.insurance",
        "jake@streetsmart.insurance",
    ]
    monkeypatch.setenv("ASCEND_DRIVER_QUERY", "is:unread newer_than:1d")
    assert driver.configured_query() == "is:unread newer_than:1d"
    assert driver.configured_query("is:unread from:accounting@useascend.com") == (
        "is:unread from:accounting@useascend.com"
    )


def test_mailbox_outside_allowlist_is_refused(monkeypatch):
    monkeypatch.delenv("ASCEND_DRIVER_ALLOW_EXTRA_MAILBOXES", raising=False)
    with pytest.raises(driver.MailboxAllowlistError):
        driver.resolve_mailboxes(mailbox="robie@streetsmart.insurance")
    monkeypatch.setenv("ASCEND_DRIVER_ALLOW_EXTRA_MAILBOXES", "1")
    assert driver.resolve_mailboxes(mailbox="robie@streetsmart.insurance") == [
        "robie@streetsmart.insurance"
    ]


def test_workflow_sets_delegation_sa_and_prod_ascend_secret():
    import yaml

    root = Path(__file__).resolve().parents[1]
    text = (root / ".github/workflows/ascend-notice-driver.yml").read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    # PyYAML 1.1 reads the bare key "on" as boolean True.
    trigger = workflow.get(True) or workflow.get("on") or {}
    assert "schedule" not in trigger
    assert "workflow_dispatch" in trigger
    assert "ROBIE_GMAIL_DELEGATION_SA=hermes-poc@streetsmart-hermes-poc.iam.gserviceaccount.com" in text
    assert "secrets/ascend-prod-api-key/versions/latest" in text
    assert "secrets/ascend-api-key/versions/latest" not in text
