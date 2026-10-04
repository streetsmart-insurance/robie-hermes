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
        label_list_error=None,
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
        self.label_list_error = label_list_error
        self.label_list_calls = 0
        self.applied_labels = []

    def search_policy_by_number(self, policy_number):
        self.searched.append(policy_number)
        if self.fail_with is not None:
            raise self.fail_with
        rows = self.rows_by_number.get(policy_number, [])
        return {"status": "success", "data": rows}

    def list_organization_labels(self):
        self.label_list_calls += 1
        if self.label_list_error is not None:
            raise self.label_list_error
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

    def discussion_detail_gets(self):
        return [
            c
            for c in self.calls
            if "v8/discussions/" in c["url"]
            and "by-applicant" not in c["url"]
            and not c["data"]
        ]


def make_discussion_client(discussion_rows, note_id="n7", discussion_detail=None):
    detail = (
        discussion_detail
        if discussion_detail is not None
        else {"discussionId": "d1", "notes": [{"noteId": note_id, "body": "filed"}]}
    )
    routes = [
        ("connect/token", {"access_token": "tok123", "expires_in": 3600}),
        ("by-applicant", discussion_rows),
        ("/notes", {"noteId": note_id}),
        ("v8/discussions/", detail),
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
    label_list_error=None,
    discussion_detail=None,
):
    discussion_rows = (
        discussion_rows
        if discussion_rows is not None
        else [{"discussionId": "d1", "title": "Ascend - Cancellation Notices"}]
    )
    discussion_client = make_discussion_client(
        discussion_rows, discussion_detail=discussion_detail
    )
    ctx = driver.DriverContext(
        ascend_client=ascend_client or FakeAscendClient(),
        ezlynx_client=FakeEzlynxClient(
            rows_by_number=policy_rows or {},
            fail_with=fail_policy_search_with,
            org_labels=org_labels,
            apply_label_error=apply_label_error,
            label_list_error=label_list_error,
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
    assert result["reason"] == "label_skipped_by_policy"
    assert result["detail"]["label"]["status"] == "label_skipped_by_policy"
    assert "label_id" not in result["detail"]["label"]
    assert ctx.ezlynx_client.label_list_calls == 0
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
    assert detail["note_text"].startswith(
        "NON-PAY CANCELLATION notice from Ascend. Policy HO-998877 was canceled for non-payment."
    )
    assert "Email subject:" not in detail["note_text"]
    assert "Insured:" not in detail["note_text"]
    assert "Ascend program" not in detail["note_text"]
    assert "prog-1" not in detail["note_text"]
    assert detail["program_uuid"] == "prog-1"
    assert driver.ROBIE_WAS_HERE in detail["note_text"]
    assert detail["task_payload"]["task_title"].startswith("Ascend cancellation notice")
    assert detail["label"]["status"] == "label_skipped_by_policy"
    assert "label_id" not in detail
    assert "label_name" not in detail
    assert ctx.ezlynx_client.label_list_calls == 0
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
    assert entry["program_uuid"] == "prog-1"
    assert entry["category"] == driver.CATEGORY_CANCELLATION_NOTICES
    assert entry["discussion_title"] == "Ascend - Cancellation Notices"
    assert entry["discussion_plan"] == "use_existing"
    assert entry["task"] == {"type": "cancellation", "assignee": "KarlaSS"}
    assert "insured" not in entry
    assert summary["would_file_if_write_scope_allowed_count"] == 0
    assert summary["would_file_if_write_scope_allowed"] == []
    assert summary["breakdown"]["would_file_if_write_scope_allowed"] == 0


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
    # The insured name is copied into the note. A phone number there must
    # be refused before anything is written.
    subject = (
        "The coverage policy for Stafford 603-769-3995 LLC has been "
        "canceled due to non-payment"
    )
    notice = make_notice(
        subject=subject,
        body=(
            "Policy ID HO-998877 Effective 01/01/2026\n"
            "Insured: Stafford 603-769-3995 LLC\n"
            "Amount due: $412.10\n"
        ),
    )
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
    # Scope is decided before the label list, so a list failure cannot hide
    # the match and the list is not called.
    assert ctx.ezlynx_client.label_list_calls == 0
    assert summary["would_file"] == []
    assert summary["would_file_if_write_scope_allowed_count"] == 1
    assert summary["breakdown"]["would_file_if_write_scope_allowed"] == 1
    blocked = summary["would_file_if_write_scope_allowed"][0]
    assert blocked["applicant_id"] == "999999999"
    assert blocked["notice_type"] == triage.CANCELLATION
    assert blocked["policy_number"] == "HO-998877"
    assert blocked["csr_login"] == "KarlaSS"
    # Dry-run still reads the existing note for a blocked applicant.
    assert blocked["existing_note_read"] is True
    assert blocked["existing_note_duplicate"] is False
    assert len(discussion_client._urlopen.discussion_detail_gets()) == 1
    assert discussion_client._urlopen.posts_to("/notes") == []


def _intent_notice(body, message_id="m1", mailbox="hello@streetsmart.insurance"):
    return make_notice(
        subject=(
            "[URGENT] Fixture Insured A LLC - StreetSmart Insurance Agency: "
            "Policy(s) at risk for cancellation"
        ),
        body=body,
        message_id=message_id,
        mailbox=mailbox,
        gmail_message_id=message_id,
    )


_INTENT_BODY = (
    "Hi Mike,\n"
    "Your loan payment of $525.30 was due on 09/21/2026. "
    "Please see the attached Notice of Intent to Cancel document. "
    "Failure to pay will result in the cancelation of your coverage on 10/14/2026.\n"
    "Policy ID ABC123-00\n"
    "Effective date 08/21/2026\n"
)


def test_due_or_cancel_dates_ignore_effective_date():
    dates = driver.due_or_cancel_dates(_INTENT_BODY)
    assert dates == frozenset({"09/21/2026", "10/14/2026"})
    assert "08/21/2026" not in dates


def test_in_run_dedupe_collapses_same_event_without_a_second_note_read(no_zap_fire):
    first = _intent_notice(_INTENT_BODY, message_id="m1", mailbox="mike@streetsmart.insurance")
    # Same due and cancel dates, different greeting and mailbox. Not an EZLynx read.
    second_body = _INTENT_BODY.replace("Hi Mike,", "Hi Taylor,")
    second = _intent_notice(
        second_body, message_id="m2", mailbox="taylor@streetsmart.insurance"
    )
    # Byte-identical copy of the first.
    third = _intent_notice(_INTENT_BODY, message_id="m3", mailbox="mike@streetsmart.insurance")
    ctx, discussion_client = make_ctx(
        notices=[first, second, third],
        policy_rows={
            "ABC123-00": [{"policyNumber": "ABC123", "accountId": ALLOWED_APPLICANT}]
        },
        discussion_rows=[
            {"discussionId": "d-noc", "title": "Ascend - Cancellation Notices"}
        ],
        ascend_client=FakeAscendClient(program={"status": "overdue"}),
    )
    summary = driver.run_driver(ctx)
    assert summary["would_file_count"] == 1
    assert summary["duplicate_in_run"] == 2
    assert summary["breakdown"]["duplicate_in_run"] == 2
    assert [item["reason"] for item in summary["results"]] == [
        "ok",
        "duplicate_in_run",
        "duplicate_in_run",
    ]
    collapsed = summary["duplicate_in_run_notices"]
    assert {item["gmail_message_id"] for item in collapsed} == {"m2", "m3"}
    assert all(item["applicant_id"] == ALLOWED_APPLICANT for item in collapsed)
    assert all(item["notice_type"] == triage.INTENT_TO_CANCEL for item in collapsed)
    # Only the keeper reads EZLynx. Collapsed copies do not.
    assert len(discussion_client._urlopen.discussion_detail_gets()) == 1
    assert discussion_client._urlopen.posts_to("/notes") == []
    assert no_zap_fire == []
    assert ctx.source.marked == []


def test_in_run_dedupe_collapses_identical_rendered_notes_without_dates(no_zap_fire):
    subject = "Payment failed for Shoreline Builders LLC"
    first = make_notice(
        subject=subject,
        body=(
            "Hi Mike,\n"
            "We couldn't process your payment of $152.03 this morning.\n"
            "Policy ID GL-112233\n"
            "Customer Shoreline Builders LLC\n"
        ),
        message_id="pay-1",
    )
    second = make_notice(
        subject=subject,
        body=(
            "Hello from accounting.\n"
            "The payment failed. See the amount on the next line.\n"
            "payment of $152.03\n"
            "Policy ID GL-112233\n"
            "Customer Shoreline Builders LLC\n"
            "Please call the office when you can.\n"
        ),
        message_id="pay-2",
        mailbox="mike@streetsmart.insurance",
    )
    ctx, discussion_client = make_ctx(
        notices=[first, second],
        policy_rows={"GL-112233": [policy_row(number="GL-112233")]},
        discussion_rows=[{"discussionId": "d-gl", "title": "Ascend - Payments"}],
        ascend_client=FakeAscendClient(program={"status": "past_due"}),
    )
    summary = driver.run_driver(ctx)
    assert summary["would_file_count"] == 1
    assert summary["duplicate_in_run"] == 1
    assert [item["reason"] for item in summary["results"]] == ["ok", "duplicate_in_run"]
    note = summary["results"][0]["detail"]["note_text"]
    assert "payment failed: $152.03" in note
    assert note.endswith("Robie was here")
    assert summary["duplicate_in_run_notices"][0]["gmail_message_id"] == "pay-2"
    assert len(discussion_client._urlopen.discussion_detail_gets()) == 1
    assert discussion_client._urlopen.posts_to("/notes") == []


def test_in_run_dedupe_keeps_a_different_due_date(no_zap_fire):
    first = _intent_notice(_INTENT_BODY, message_id="m1")
    other = _intent_notice(
        _INTENT_BODY.replace("09/21/2026", "10/02/2026").replace(
            "10/14/2026", "10/28/2026"
        ).replace("Hi Mike,", "Hi Sandy,"),
        message_id="m2",
        mailbox="sandy@streetsmart.insurance",
    )
    ctx, _ = make_ctx(
        notices=[first, other],
        policy_rows={
            "ABC123-00": [{"policyNumber": "ABC123", "accountId": ALLOWED_APPLICANT}]
        },
        discussion_rows=[
            {"discussionId": "d-noc", "title": "Ascend - Cancellation Notices"}
        ],
        ascend_client=FakeAscendClient(program={"status": "overdue"}),
    )
    summary = driver.run_driver(ctx)
    assert summary["duplicate_in_run"] == 0
    assert summary["would_file_count"] == 2
    assert {item["gmail_message_id"] for item in summary["would_file"]} == {"m1", "m2"}


def test_existing_note_duplicate_is_not_filed(no_zap_fire):
    notice = _intent_notice(_INTENT_BODY)
    triaged = triage.triage_notice(FakeAscendClient(program={"status": "overdue"}), notice.subject, notice.body)
    signed = driver.signed_notice_note(triaged["note_text"])
    ctx, discussion_client = make_ctx(
        notices=[notice],
        policy_rows={
            "ABC123-00": [{"policyNumber": "ABC123", "accountId": ALLOWED_APPLICANT}]
        },
        discussion_rows=[
            {"discussionId": "d-noc", "title": "Ascend - Cancellation Notices"}
        ],
        discussion_detail={
            "discussionId": "d-noc",
            "notes": [{"noteId": "n-already", "body": signed}],
        },
        ascend_client=FakeAscendClient(program={"status": "overdue"}),
    )
    summary = driver.run_driver(ctx)
    result = summary["results"][0]
    assert result["status"] == "skipped"
    assert result["reason"].startswith("existing_note_duplicate")
    assert result["detail"]["existing_note_id"] == "n-already"
    assert summary["would_file"] == []
    assert summary["duplicate_in_run"] == 0
    assert discussion_client._urlopen.posts_to("/notes") == []
    assert len(discussion_client._urlopen.discussion_detail_gets()) == 1
    assert no_zap_fire == []


def test_dry_run_reports_existing_note_duplicate_for_blocked_applicant(no_zap_fire, monkeypatch):
    def _boom(*_args, **_kwargs):
        raise AssertionError("dry-run write gate called during note read")

    monkeypatch.setattr("robie_job_engine.safety_seal.driver_gate_for_write", _boom)
    notice = make_notice()
    triaged = triage.triage_notice(FakeAscendClient(), notice.subject, notice.body)
    signed = driver.signed_notice_note(triaged["note_text"])
    ctx, discussion_client = make_ctx(
        notices=[notice],
        policy_rows={"HO-998877": [policy_row(applicant_id="175448994")]},
        discussion_detail={
            "discussionId": "d1",
            "notes": [{"noteId": "n-existing", "body": signed}],
        },
    )
    with mock.patch(
        "robie_job_engine.ezlynx_write_scope.ALLOWED_EZLYNX_WRITE_APPLICANT_IDS",
        frozenset({ALLOWED_APPLICANT}),
    ):
        summary = driver.run_driver(ctx)
    result = summary["results"][0]
    assert result["status"] == "skipped"
    assert "write_scope_refused" in result["reason"]
    assert result["detail"]["existing_note_read"] is True
    assert result["detail"]["existing_note_duplicate"] is True
    assert result["detail"]["existing_note_id"] == "n-existing"
    blocked = summary["would_file_if_write_scope_allowed"][0]
    assert blocked["applicant_id"] == "175448994"
    assert blocked["existing_note_duplicate"] is True
    assert blocked["existing_note_id"] == "n-existing"
    assert blocked["discussion_title"] == "Ascend - Cancellation Notices"
    assert blocked["discussion_plan"] == "use_existing"
    assert blocked["task"]["type"] == "cancellation"
    assert summary["would_file"] == []
    assert ctx.ezlynx_client.label_list_calls == 0
    assert ctx.ezlynx_client.applied_labels == []
    assert discussion_client._urlopen.posts_to("/notes") == []
    assert len(discussion_client._urlopen.discussion_detail_gets()) == 1
    assert no_zap_fire == []


def test_cancellation_does_not_look_up_or_apply_ascend_noc(no_zap_fire):
    ctx, discussion_client = make_ctx(
        notices=[make_notice()],
        policy_rows={"HO-998877": [policy_row(applicant_id="175448994")]},
        org_labels=[{"id": "c1", "name": "Cancellation"}],
        label_list_error=RuntimeError("portal label list HTTP 500"),
    )
    with mock.patch(
        "robie_job_engine.ezlynx_write_scope.ALLOWED_EZLYNX_WRITE_APPLICANT_IDS",
        frozenset({ALLOWED_APPLICANT, "175448994"}),
    ):
        summary = driver.run_driver(ctx)
    result = summary["results"][0]
    assert result["status"] == "dry_run"
    assert result["reason"] == "label_skipped_by_policy"
    label = result["detail"]["label"]
    assert label["status"] == "label_skipped_by_policy"
    assert "label_id" not in label
    assert "label_id" not in result["detail"]
    assert "110248" not in json.dumps(result)
    assert "Ascend NOC" not in result["detail"]["note_text"]
    assert summary["would_file_count"] == 1
    assert summary["skipped"] == 0
    entry = summary["would_file"][0]
    assert entry["applicant_id"] == "175448994"
    assert entry["notice_type"] == triage.CANCELLATION
    assert entry["csr_login"] == "KarlaSS"
    assert discussion_client._urlopen.posts_to("/notes") == []
    assert ctx.ezlynx_client.applied_labels == []
    assert ctx.ezlynx_client.label_list_calls == 0
    assert ctx.source.marked == []
    assert no_zap_fire == []


def test_cancellation_live_files_note_without_calling_the_label_api(no_zap_fire):
    ctx, discussion_client = make_ctx(
        notices=[make_notice()],
        policy_rows={"HO-998877": [policy_row()]},
        dry_run=False,
        label_list_error=RuntimeError("portal label list must not be called"),
        apply_label_error=org_labels.OrgLabelError(
            org_labels.LABEL_APPLY_FAILED, "organization label apply failed: HTTP 403"
        ),
    )
    summary = driver.run_driver(ctx)
    result = summary["results"][0]
    assert result["status"] == "done"
    assert result["reason"] == "label_skipped_by_policy"
    assert result["detail"]["label"]["status"] == "label_skipped_by_policy"
    assert "label_id" not in result["detail"]["label"]
    assert len(discussion_client._urlopen.posts_to("/notes")) == 1
    assert ctx.ezlynx_client.applied_labels == []
    assert ctx.ezlynx_client.label_list_calls == 0
    assert len(no_zap_fire) == 1
    assert ctx.source.marked == ["m1"]


def test_duplicate_category_titles_use_the_newest(no_zap_fire, caplog):
    rows = [
        {"discussionId": "d0", "title": "Untitled", "updatedAt": "2026-12-01T00:00:00Z"},
        {"discussionId": "d1", "title": "New Business", "updatedAt": "2026-08-01T00:00:00Z"},
        {
            "discussionId": "d-old",
            "title": "ascend - cancellation notices",
            "updatedAt": "2026-01-01T00:00:00Z",
        },
        {
            "discussionId": "d2",
            "title": "Ascend - Cancellation Notices",
            "updatedAt": "2026-09-01T00:00:00Z",
        },
        {
            "discussionId": "d-policy",
            "title": "Policy HO-998877",
            "updatedAt": "2026-11-01T00:00:00Z",
        },
    ]
    ctx, discussion_client = make_ctx(
        notices=[make_notice()],
        policy_rows={"HO-998877": [policy_row()]},
        discussion_rows=rows,
        dry_run=False,
    )
    with caplog.at_level("WARNING"):
        summary = driver.run_driver(ctx)
    result = summary["results"][0]
    assert result["status"] == "done"
    assert result["detail"]["discussion_id"] == "d2"
    assert result["detail"]["discussion_title"] == "Ascend - Cancellation Notices"
    assert result["detail"]["discussion_plan"] == "use_existing"
    assert "using the newest d2" in caplog.text
    assert driver.ROBIE_WAS_HERE in result["detail"]["note_text"]
    posts = discussion_client._urlopen.posts_to("/notes")
    assert len(posts) == 1
    assert "/v8/discussions/d2/notes" in posts[0]["url"]
    body = json.loads(posts[0]["data"].decode("utf-8"))
    assert driver.ROBIE_WAS_HERE in body["body"]
    assert "bind" not in body["body"].lower()
    assert result["reason"] == "label_skipped_by_policy"
    assert result["detail"]["label"]["status"] == "label_skipped_by_policy"
    assert "label_id" not in result["detail"]["label"]
    assert ctx.ezlynx_client.label_list_calls == 0
    assert ctx.ezlynx_client.applied_labels == []


def test_untitled_discussions_do_not_receive_the_note(no_zap_fire):
    rows = [{"discussionId": "d1", "title": "Untitled"}]
    ctx, discussion_client = make_ctx(
        notices=[make_notice()],
        policy_rows={"HO-998877": [policy_row()]},
        discussion_rows=rows,
    )
    summary = driver.run_driver(ctx)
    result = summary["results"][0]
    assert result["status"] == "dry_run"
    assert result["detail"]["discussion_plan"] == "create"
    assert result["detail"]["discussion_title"] == "Ascend - Cancellation Notices"
    assert result["detail"].get("discussion_id") in (None, "")
    assert summary["would_file"][0]["discussion_plan"] == "create"
    assert summary["would_file"][0]["task"] == {
        "type": "cancellation",
        "assignee": "KarlaSS",
    }
    assert discussion_client._urlopen.posts_to("/notes") == []
    assert discussion_client._urlopen.posts_to("with-note") == []
    assert no_zap_fire == []


def test_missing_or_duplicate_ascend_noc_catalog_does_not_block_the_note(no_zap_fire):
    ctx, discussion_client = make_ctx(
        notices=[make_notice()],
        policy_rows={"HO-998877": [policy_row()]},
        org_labels=[
            {"id": "c1", "name": "Cancellation"},
            {"id": "a", "name": "Ascend NOC"},
            {"id": "b", "name": "Ascend NOC"},
        ],
    )
    summary = driver.run_driver(ctx)
    result = summary["results"][0]
    assert result["status"] == "dry_run"
    assert result["reason"] == "label_skipped_by_policy"
    assert result["detail"]["label"]["status"] == "label_skipped_by_policy"
    assert discussion_client._urlopen.posts_to("/notes") == []
    assert ctx.ezlynx_client.label_list_calls == 0
    assert ctx.ezlynx_client.applied_labels == []
    assert summary["would_file_count"] == 1
    assert no_zap_fire == []


def test_driver_does_not_apply_org_labels():
    source = Path(driver.__file__).read_text(encoding="utf-8")
    assert "ezlynx_org_labels" not in source
    assert "plan_exact_label" not in source
    assert "apply_planned_label" not in source
    assert "apply_account_label" not in source
    assert "playwright" not in source.casefold()
    assert "bland" not in source.casefold()


def test_signed_notice_note_is_plain_and_idempotent():
    raw = "Ascend notice: cancellation.\nInsured: Test LLC"
    signed = driver.signed_notice_note(raw)
    assert signed.endswith(driver.ROBIE_WAS_HERE)
    assert driver.signed_notice_note(signed) == signed
    assert driver.signed_notice_note("") == ""
    assert driver.category_for(triage.CANCELLATION) == driver.CATEGORY_CANCELLATION_NOTICES
    assert driver.category_title(driver.CATEGORY_PAYMENTS) == "Ascend - Payments"
    assert driver.category_for(triage.LATE_PAYMENT) == driver.CATEGORY_PAYMENTS
    assert driver.category_for(triage.RETURN_PREMIUM) == driver.CATEGORY_RETURN_PREMIUM
    assert driver.category_for(triage.NEW_PROGRAM) == ""


def test_non_category_titles_do_not_receive_the_note(no_zap_fire):
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
    assert result["status"] == "dry_run"
    assert result["detail"]["discussion_plan"] == "create"
    assert result["detail"]["discussion_title"] == "Ascend - Cancellation Notices"
    assert summary["would_file"][0]["discussion_plan"] == "create"
    assert discussion_client._urlopen.posts_to("/notes") == []
    assert discussion_client._urlopen.posts_to("with-note") == []
    assert no_zap_fire == []


def test_category_title_match_ignores_case_and_other_ascend_titles(no_zap_fire):
    rows = [
        {
            "discussionId": "d-old",
            "title": "Premium finance 09/23/2026",
            "updatedAt": "2026-02-01T00:00:00Z",
        },
        {
            "discussionId": "d-new",
            "title": "  ascend - cancellation notices  ",
            "updatedAt": "2026-08-01T00:00:00Z",
        },
        {
            "discussionId": "d-undated",
            "title": "Ascend NOC",
            "updatedAt": "2026-12-01T00:00:00Z",
        },
        {"discussionId": "d-other", "title": "Certificates", "applicantId": "999"},
    ]
    ctx, _ = make_ctx(
        notices=[
            make_notice(
                body=CANCELLATION_BODY + "The loan has been canceled effective 09/23/2026.\n"
            )
        ],
        policy_rows={"HO-998877": [policy_row()]},
        discussion_rows=rows,
    )
    summary = driver.run_driver(ctx)
    result = summary["results"][0]
    assert result["status"] == "dry_run"
    assert result["detail"]["discussion_id"] == "d-new"
    assert result["detail"]["discussion_plan"] == "use_existing"
    assert result["detail"]["discussion_title"] == "ascend - cancellation notices"
    assert summary["would_file"][0]["discussion_title"] == "ascend - cancellation notices"


def test_fallback_keeps_220111302_when_title_cancel_date_matches(no_zap_fire, monkeypatch):
    monkeypatch.setattr(
        "robie_job_engine.ezlynx_write_scope.ALLOWED_EZLYNX_WRITE_APPLICANT_IDS",
        frozenset({"220111302"}),
    )
    notice = make_notice(
        subject=(
            "[URGENT] Fixture Insured A LLC - StreetSmart Insurance Agency: "
            "Policy(s) at risk for cancellation"
        ),
        body=(
            "Your loan payment of $525.30 was due on 09/21/2026. "
            "Failure to pay will result in the cancelation of your coverage on 10/14/2026.\n"
            "Policy ID DSLA97258206-00\n"
            "Effective date 08/21/2026\n"
        ),
    )
    ctx, discussion_client = make_ctx(
        notices=[notice],
        policy_rows={
            "DSLA97258206-00": [
                {"policyNumber": "DSLA97258206", "accountId": "220111302"}
            ]
        },
        discussion_rows=[
            {
                "discussionId": "d-wrong",
                "title": "Ascend Past Due Date: 09/06/2026",
                "updatedAt": "2026-12-01T00:00:00Z",
            },
            {
                "discussionId": "d-plain",
                "title": "Premium finance",
                "updatedAt": "2026-11-01T00:00:00Z",
            },
            {
                "discussionId": "d-match",
                "title": "Ascend - Cancellation Notices",
                "updatedAt": "2026-08-01T00:00:00Z",
            },
        ],
        ascend_client=FakeAscendClient(program={"status": "overdue"}),
    )
    summary = driver.run_driver(ctx)
    result = summary["results"][0]
    assert result["status"] == "dry_run"
    assert result["detail"]["applicant_id"] == "220111302"
    assert result["detail"]["discussion_id"] == "d-match"
    assert result["detail"]["discussion_plan"] == "use_existing"
    assert result["detail"]["discussion_title"] == "Ascend - Cancellation Notices"
    assert result["detail"]["category"] == driver.CATEGORY_CANCELLATION_NOTICES
    assert summary["would_file"][0]["discussion_title"] == "Ascend - Cancellation Notices"
    assert discussion_client._urlopen.posts_to("/notes") == []


def test_fallback_refuses_175448994_when_title_date_does_not_match(no_zap_fire):
    notice = make_notice(
        subject=(
            "The coverage policy for Fixture Insured has been canceled due to non-payment"
        ),
        body=(
            "Fixture Insured canceled for non-payment and the loan has been "
            "canceled effective 09/23/2026.\n"
            "Policy ID CPS6534227\n"
            "Effective date 01/06/2026\n"
            "Insured: Fixture Insured\n"
        ),
    )
    title = "Ascend Past Due Date: 09/06/2026"
    ctx, discussion_client = make_ctx(
        notices=[notice],
        policy_rows={
            "CPS6534227": [{"policyNumber": "CPS6534227", "accountId": "175448994"}]
        },
        discussion_rows=[
            {
                "discussionId": "d-past",
                "title": title,
                "updatedAt": "2026-12-01T00:00:00Z",
            },
            {
                "discussionId": "d-plain",
                "title": "Premium finance",
                "updatedAt": "2026-11-01T00:00:00Z",
            },
        ],
    )
    summary = driver.run_driver(ctx)
    result = summary["results"][0]
    assert "Past Due Date: 09/06/2026" in title
    assert result["status"] == "skipped"
    assert result["reason"].startswith("write_scope_refused")
    assert result["detail"]["applicant_id"] == "175448994"
    assert result["detail"]["discussion_plan"] == "create"
    assert result["detail"]["discussion_title"] == "Ascend - Cancellation Notices"
    assert summary["would_file"] == []
    blocked = summary["would_file_if_write_scope_allowed"][0]
    assert blocked["discussion_plan"] == "create"
    assert blocked["discussion_title"] == "Ascend - Cancellation Notices"
    assert blocked["task"]["type"] == "cancellation"
    assert discussion_client._urlopen.posts_to("/notes") == []
    assert discussion_client._urlopen.posts_to("with-note") == []


def test_other_applicants_rows_do_not_win_the_category_title(no_zap_fire):
    rows = [
        {
            "discussionId": "d-other",
            "title": "Ascend - Cancellation Notices",
            "applicantId": "999999999",
            "updatedAt": "2026-12-01T00:00:00Z",
        },
        {
            "discussionId": "d-ours",
            "title": "Ascend - Cancellation Notices",
            "applicantId": ALLOWED_APPLICANT,
            "updatedAt": "2026-01-01T00:00:00Z",
        },
    ]
    ctx, _ = make_ctx(
        notices=[
            make_notice(
                body=CANCELLATION_BODY + "The loan has been canceled effective 09/23/2026.\n"
            )
        ],
        policy_rows={"HO-998877": [policy_row()]},
        discussion_rows=rows,
    )
    summary = driver.run_driver(ctx)
    assert summary["results"][0]["detail"]["discussion_id"] == "d-ours"
    assert summary["would_file"][0]["discussion_title"] == "Ascend - Cancellation Notices"
    assert summary["would_file"][0]["discussion_plan"] == "use_existing"


def test_discussion_base_url_is_host_only():
    class _DocumentApiConfig:
        document_base_url = "https://app.ezlynx.com/DocumentApi/"
        token_endpoint = "https://app.ezlynx.com/auth/connect/token"
        client_id = "id"
        client_secret = "secret"
        username = "SSRobie"
        integration_group_id = "159"

    config = driver.discussion_config_from_api_config(_DocumentApiConfig())
    assert config.discussion_base_url == "https://app.ezlynx.com/DiscussionApi/"
    assert "DocumentApi" not in config.discussion_base_url


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
        discussion_rows=[{"discussionId": "d-gl", "title": "Ascend - Payments"}],
        ascend_client=FakeAscendClient(program={"status": "past_due"}),
    )
    summary = driver.run_driver(ctx)
    result = summary["results"][0]
    assert result["status"] == "dry_run"
    assert result["detail"]["discussion_title"] == "Ascend - Payments"
    assert result["detail"]["discussion_plan"] == "use_existing"
    assert result["detail"]["category"] == driver.CATEGORY_PAYMENTS
    assert result["detail"]["notice_type"] == triage.LATE_PAYMENT
    assert result["detail"]["applicant_id"] == ALLOWED_APPLICANT
    assert "csr_username" not in result["detail"]
    assert discussion_client._urlopen.posts_to("/notes") == []  # dry-run: validated only
    assert no_zap_fire == []  # no task builder for late_payment
    assert "no task for notice type" in result["detail"]["task_skipped"]
    assert "label" not in result["detail"]
    assert ctx.ezlynx_client.applied_labels == []
    entry = summary["would_file"][0]
    assert entry["notice_type"] == triage.LATE_PAYMENT
    assert entry["discussion_title"] == "Ascend - Payments"
    assert entry["category"] == driver.CATEGORY_PAYMENTS
    assert "task" not in entry
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
    assert result["reason"] == "label_skipped_by_policy"
    assert result["detail"]["label"]["status"] == "label_skipped_by_policy"
    assert "label_id" not in result["detail"]["label"]
    assert ctx.ezlynx_client.label_list_calls == 0
    assert ctx.ezlynx_client.applied_labels == []


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


def test_label_apply_error_is_never_reached_for_a_cancellation(no_zap_fire):
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
    assert result["status"] == "done"
    assert result["reason"] == "label_skipped_by_policy"
    assert "403" not in result["reason"]
    assert ctx.source.marked == ["m1"]
    assert ctx.ezlynx_client.label_list_calls == 0
    assert ctx.ezlynx_client.applied_labels == []
    assert len(discussion_client._urlopen.posts_to("/notes")) == 1
    assert len(no_zap_fire) == 1


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


def test_ezlynx_lob_suffix_matches_bare_ascend_number():
    client = FakeEzlynxClient(
        rows_by_number={
            "DSLA123456": [
                {"policyNumber": "DSLA123456 APD", "accountId": ALLOWED_APPLICANT}
            ]
        }
    )
    resolution, reason = driver.resolve_applicant(client, ["DSLA123456"], None)
    assert reason == ""
    assert resolution is not None
    assert resolution.applicant_id == ALLOWED_APPLICANT
    assert driver.normalize_ezlynx_policy_number("DSLA123456 APD") == "DSLA123456"
    assert driver.normalize_ezlynx_policy_number("DSLA123456 ABCD") == "DSLA123456"
    assert driver.normalize_ezlynx_policy_number("DSLA123456 AP") == "DSLA123456"


def test_ezlynx_lob_suffix_does_not_strip_other_shapes():
    assert driver.strip_ezlynx_lob_suffix("ABC123 A") == "ABC123 A"
    assert driver.strip_ezlynx_lob_suffix("ABC123 ABCDE") == "ABC123 ABCDE"
    assert driver.strip_ezlynx_lob_suffix("ABC123 apd") == "ABC123 apd"
    client = FakeEzlynxClient(
        rows_by_number={
            "ABC123": [
                {"policyNumber": "ABC123 ABCDE", "accountId": ALLOWED_APPLICANT},
                {"policyNumber": "ABC123 A", "accountId": "222"},
            ]
        }
    )
    resolution, reason = driver.resolve_applicant(client, ["ABC123"], None)
    assert resolution is None
    assert reason.startswith("applicant_unresolved:")
    assert "0 candidate" in reason


def test_ezlynx_lob_suffix_two_rows_fail_closed():
    client = FakeEzlynxClient(
        rows_by_number={
            "ABC123": [
                {"policyNumber": "ABC123 APD", "accountId": "111"},
                {"policyNumber": "ABC123 BOP", "accountId": "222"},
            ]
        }
    )
    resolution, reason = driver.resolve_applicant(client, ["ABC123"], None)
    assert resolution is None
    assert "2 candidate" in reason


def test_ezlynx_lob_suffix_and_term_suffix_match_one_row():
    client = FakeEzlynxClient(
        rows_by_number={
            "ABC123-00": [
                {"policyNumber": "ABC123 APD", "accountId": ALLOWED_APPLICANT}
            ]
        }
    )
    resolution, reason = driver.resolve_applicant(client, ["ABC123-00"], None)
    assert reason == ""
    assert resolution.applicant_id == ALLOWED_APPLICANT

    exact = FakeEzlynxClient(
        rows_by_number={
            "ABC123-00": [
                {"policyNumber": "ABC123-00 APD", "accountId": "111"},
                {"policyNumber": "ABC123 BOP", "accountId": "222"},
            ]
        }
    )
    resolution, reason = driver.resolve_applicant(exact, ["ABC123-00"], None)
    assert reason == ""
    assert resolution.applicant_id == "111"


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


def test_intent_to_cancel_never_builds_cancellation_task(no_zap_fire, monkeypatch):
    monkeypatch.delenv(driver.INTENT_CSR_TASK_ENV, raising=False)
    notice = make_notice(
        subject=(
            "[URGENT] Fixture Insured A LLC - StreetSmart Insurance Agency: "
            "Policy(s) at risk for cancellation"
        ),
        body=(
            "Please see the attached Notice of Intent to Cancel document. "
            "Your loan payment of $525.30 was due on 09/21/2026. "
            "Failure to pay will result in the cancelation of your coverage on 10/14/2026.\n"
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
            {"discussionId": "d-noc", "title": "Ascend - Cancellation Notices"},
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
        subject="Updates to Your Ascend Master Services Agreement",
        body="The master services agreement was updated.",
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
    assert summary["breakdown"]["by_notice_type"]["msa"] == 1
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
    assert "# schedule:" in text
    assert "ROBIE_GMAIL_DELEGATION_SA=hermes-poc@streetsmart-hermes-poc.iam.gserviceaccount.com" in text
    assert "secrets/ascend-prod-api-key/versions/latest" in text
    assert "secrets/ascend-api-key/versions/latest" not in text
    assert "ROBIE_EZLYNX_WRITE_SCOPE=all" in text
    assert "ROBIE_PLAYGROUND=1" in text


def test_write_scope_all_is_only_on_the_ascend_notice_unit():
    root = Path(__file__).resolve().parents[1]
    unit = (root / "deploy/systemd/robie-ascend-notice-driver.service").read_text(encoding="utf-8")
    drop_in = (
        root / "deploy/systemd/robie-ascend-notice-driver.service.d/10-write-scope.conf"
    ).read_text(encoding="utf-8")
    assert "Environment=ROBIE_EZLYNX_WRITE_SCOPE=all" in unit
    assert "Environment=ROBIE_PLAYGROUND=1" in unit
    assert "Environment=ROBIE_EZLYNX_WRITE_SCOPE=all" in drop_in
    assert "Environment=ROBIE_PLAYGROUND=1" in drop_in
    assert "\n[Install]\n" not in unit
    assert not (root / "deploy/systemd/robie-ascend-notice-driver.timer").exists()
    scope_line = "Environment=ROBIE_EZLYNX_WRITE_SCOPE=all"
    hits = []
    for folder in (root / "deploy/systemd", root / "systemd"):
        for path in folder.rglob("*"):
            if not path.is_file():
                continue
            if scope_line in path.read_text(encoding="utf-8"):
                hits.append(path.relative_to(root).as_posix())
    assert sorted(hits) == [
        "deploy/systemd/robie-ascend-notice-driver.service",
        "deploy/systemd/robie-ascend-notice-driver.service.d/10-write-scope.conf",
    ]
    example = (root / "deploy/systemd/robie-playground.env.example").read_text(encoding="utf-8")
    assert "\nROBIE_EZLYNX_WRITE_SCOPE=\n" in example
    workflow_hits = []
    for path in (root / ".github/workflows").glob("*.yml"):
        if "ROBIE_EZLYNX_WRITE_SCOPE=all" in path.read_text(encoding="utf-8"):
            workflow_hits.append(path.name)
    assert workflow_hits == ["ascend-notice-driver.yml"]
    scope_source = (root / "robie_job_engine/ezlynx_write_scope.py").read_text(encoding="utf-8")
    assert 'os.environ["ROBIE_EZLYNX_WRITE_SCOPE"]' not in scope_source
    assert "os.environ.setdefault" not in scope_source


def test_driver_refuses_writes_outside_category_note_create_and_task():
    allowed = dict(discussion_id="d1", discussion_title="Ascend - Payments")
    driver.authorize_notice_write("discussion_note", **allowed)
    driver.authorize_notice_write(
        "discussion_create_with_note",
        discussion_title="Ascend - Cancellation Notices",
    )
    driver.authorize_notice_write("task_create", task_kind="cancellation")
    driver.authorize_notice_write("task_create", task_kind="disputed_charge")
    driver.authorize_notice_write("task_create", task_kind="intent_to_cancel")
    for action in (
        "document_upload",
        "discussion_create",
        "label_apply",
        "policy_create",
        "policy_change",
        "bind",
        "delete",
        "",
    ):
        with pytest.raises(driver.NonNoteWriteRefused, match="non_note_write_refused"):
            driver.authorize_notice_write(action, **allowed)
    with pytest.raises(driver.NonNoteWriteRefused, match="existing discussion"):
        driver.authorize_notice_write(
            "discussion_note",
            discussion_id="",
            discussion_title="Ascend - Payments",
        )
    for title in ("", "Untitled", "untitled", "HO-998877", "Ascend NOC"):
        with pytest.raises(driver.NonNoteWriteRefused, match="category discussion"):
            driver.authorize_notice_write(
                "discussion_note", discussion_id="d1", discussion_title=title
            )
    with pytest.raises(driver.NonNoteWriteRefused, match="category title"):
        driver.authorize_notice_write(
            "discussion_create_with_note",
            discussion_title="Untitled",
        )
    with pytest.raises(driver.NonNoteWriteRefused, match="task_create"):
        driver.authorize_notice_write("task_create", task_kind="refund")


def _disputed_notice():
    return make_notice(
        subject="[Action Needed] Disputed charge for Fixture Insured A LLC",
        body=(
            "Your customer, Fixture Insured A LLC, disputed the following payment.\n"
            "Payment amount $165.27\n"
            "Policy ID HO-998877\n"
        ),
        message_id="dispute-1",
    )


def test_agency_remittance_is_ignored(no_zap_fire):
    class _RaisingAscend:
        def get_program(self, *_args, **_kwargs):
            raise AssertionError("remittance must not call Ascend")

        def find_program_by_policy(self, *_args, **_kwargs):
            raise AssertionError("remittance must not call Ascend")

    notice = make_notice(
        subject="Remittance Notification - Payment of $1,250.00 for StreetSmart Insurance Agency",
        body="Remittance Notification - Payment of $1,250.00 for the agency.",
        message_id="remit-1",
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
    assert summary["would_file"] == []
    assert discussion_client._urlopen.posts_to("/notes") == []
    assert discussion_client._urlopen.posts_to("with-note") == []
    assert no_zap_fire == []
    assert "ROBIE_ASCEND_AGENCY_APPLICANT_ID" not in Path(driver.__file__).read_text(
        encoding="utf-8"
    )


def test_disputed_charge_fails_closed_when_assignee_login_has_no_id(no_zap_fire, monkeypatch):
    monkeypatch.delenv("ROBIE_EZLYNX_LOGIN_NOBODY1", raising=False)
    monkeypatch.setenv("ROBIE_ACCOUNTING_ASSIGNEE", "Nobody1")
    assert driver.ezlynx_user_id_for_login("Markley1") == 263046
    assert driver.ezlynx_user_id_for_login("Nobody1") is None
    assert "eca8cba27574021b7b0e924b9cf389d720cce745" in driver.ACCOUNTING_EZLYNX_USER["source"]
    assert "849801097" in driver.ACCOUNTING_EZLYNX_USER["source"]
    ctx, discussion_client = make_ctx(
        notices=[_disputed_notice()],
        policy_rows={"HO-998877": [policy_row()]},
        discussion_rows=[{"discussionId": "d-pay", "title": "Ascend - Payments"}],
    )
    summary = driver.run_driver(ctx)
    result = summary["results"][0]
    assert result["status"] == "skipped"
    assert result["reason"] == "accounting assignee id unknown"
    assert result["detail"]["needs_human_review"] is True
    assert result["detail"]["accounting_login"] == "Nobody1"
    assert summary["would_file"] == []
    assert discussion_client._urlopen.posts_to("/notes") == []
    assert discussion_client._urlopen.posts_to("with-note") == []
    assert no_zap_fire == []


def test_disputed_charge_dry_run_assigns_markley_263046(no_zap_fire, monkeypatch):
    monkeypatch.delenv("ROBIE_ACCOUNTING_ASSIGNEE", raising=False)
    ctx, discussion_client = make_ctx(
        notices=[_disputed_notice()],
        policy_rows={"HO-998877": [policy_row()]},
        discussion_rows=[{"discussionId": "d-pay", "title": "Ascend - Payments"}],
    )
    summary = driver.run_driver(ctx)
    result = summary["results"][0]
    assert result["status"] == "dry_run"
    note = result["detail"]["task_payload"]
    assert note["type"] == "TaskCreationNote"
    assert note["task"]["assignedUserId"] == 263046
    assert "applicantId" not in note
    assert summary["would_file"][0]["task"] == {
        "type": "disputed_charge",
        "assignee": "Markley1",
    }
    assert discussion_client._urlopen.posts_to("/notes") == []
    assert discussion_client._urlopen.posts_to("with-note") == []
    assert no_zap_fire == []


def test_disputed_charge_tasks_when_the_user_id_is_known(no_zap_fire, monkeypatch):
    monkeypatch.setitem(driver.ACCOUNTING_EZLYNX_USER, "ezlynx_user_id", 424242)
    ctx, discussion_client = make_ctx(
        notices=[_disputed_notice()],
        policy_rows={"HO-998877": [policy_row()]},
        discussion_rows=[{"discussionId": "d-pay", "title": "Ascend - Payments"}],
    )
    summary = driver.run_driver(ctx)
    result = summary["results"][0]
    assert result["status"] == "dry_run"
    assert result["detail"]["category"] == driver.CATEGORY_PAYMENTS
    assert result["detail"]["discussion_title"] == "Ascend - Payments"
    assert result["detail"]["discussion_plan"] == "use_existing"
    assert "DISPUTED CHARGE notice from Ascend." in result["detail"]["note_text"]
    entry = summary["would_file"][0]
    assert entry["task"] == {"type": "disputed_charge", "assignee": "Markley1"}
    note = result["detail"]["task_payload"]
    assert note["type"] == "TaskCreationNote"
    assert note["task"]["assignedUserId"] == 424242
    assert "applicantId" not in note
    assert discussion_client._urlopen.posts_to("/notes") == []
    assert discussion_client._urlopen.posts_to("with-note") == []
    assert no_zap_fire == []


def _late_payment(message_id, policy, amount):
    return make_notice(
        subject=f"Past due payment for Shoreline Builders LLC {policy}",
        body=f"Policy ID {policy} Effective 02/01/2026\nAmount due: {amount}\n",
        message_id=message_id,
    )


def test_dry_run_labels_a_discussion_planned_earlier_this_run(no_zap_fire):
    notices = [
        _late_payment("pay-1", "GL-112233", "$3,528.22"),
        _late_payment("pay-2", "GL-445566", "$90.00"),
    ]
    ctx, discussion_client = make_ctx(
        notices=notices,
        policy_rows={
            "GL-112233": [{"policyNumber": "GL-112233", "accountId": ALLOWED_APPLICANT}],
            "GL-445566": [{"policyNumber": "GL-445566", "accountId": ALLOWED_APPLICANT}],
        },
        discussion_rows=[{"discussionId": "d-other", "title": "Untitled"}],
        ascend_client=FakeAscendClient(program={"status": "past_due"}),
    )
    summary = driver.run_driver(ctx)
    assert [item["status"] for item in summary["results"]] == ["dry_run", "dry_run"]
    first, second = summary["would_file"]
    assert first["discussion_plan"] == "create"
    assert first["discussion_title"] == "Ascend - Payments"
    assert "discussion_id" not in first
    assert second["discussion_plan"] == "create (planned earlier this run)"
    assert second["discussion_title"] == "Ascend - Payments"
    assert second["discussion_plan"] != "use_existing"
    assert discussion_client._urlopen.posts_to("/notes") == []
    assert discussion_client._urlopen.posts_to("with-note") == []
    assert no_zap_fire == []


class _TimeoutThenFound:
    """with-note times out. Later applicant reads can return the titled row."""

    def __init__(self, *, reveal_on_list):
        self.reveal_on_list = reveal_on_list
        self.calls = []
        self.list_calls = 0
        self.with_note_attempts = 0

    def __call__(self, url, *, data=None, headers=None, timeout=None):
        self.calls.append({"url": url, "data": data, "headers": dict(headers or {})})
        if "connect/token" in url:
            return FakeResponse({"access_token": "tok123", "expires_in": 3600})
        if "by-applicant" in url:
            self.list_calls += 1
            if self.list_calls >= self.reveal_on_list:
                return FakeResponse(
                    [
                        {
                            "discussionId": "d-found",
                            "title": "Ascend - Payments",
                            "applicantId": ALLOWED_APPLICANT,
                        }
                    ]
                )
            return FakeResponse([])
        if "with-note" in url and data:
            self.with_note_attempts += 1
            raise TimeoutError("with-note timed out")
        if "/notes" in url and data:
            return FakeResponse({"noteId": "n-found"})
        if "v8/discussions/" in url and not data:
            return FakeResponse(
                {
                    "discussionId": "d-found",
                    "title": "Ascend - Payments",
                    "notes": [{"noteId": "n-found", "body": "filed"}],
                }
            )
        raise AssertionError(f"unexpected Discussion API URL: {url}")

    def posts_to(self, needle):
        return [c for c in self.calls if needle in c["url"] and c["data"]]


def _live_payments_ctx(notices, urlopen):
    config = discussions.DiscussionApiConfig(
        discussion_base_url=API_BASE,
        token_endpoint=TOKEN_URL,
        client_id="street_smart_api",
        client_secret="secret",
        username="SSRobie",
        integration_group_id="159",
    )
    client = discussions.DiscussionApiClient(config, urlopen=urlopen)
    policy_rows = {}
    for notice in notices:
        number = notice.body.split("Policy ID ", 1)[1].split(" ", 1)[0]
        policy_rows[number] = [{"policyNumber": number, "accountId": ALLOWED_APPLICANT}]
    ctx = driver.DriverContext(
        ascend_client=FakeAscendClient(program={"status": "past_due"}),
        ezlynx_client=FakeEzlynxClient(rows_by_number=policy_rows),
        discussion_client=client,
        source=FakeSource(notices),
        dry_run=False,
        due_days=2,
        today=date(2026, 9, 15),
    )
    return ctx


def test_create_timeout_rereads_and_uses_the_discussion(no_zap_fire):
    urlopen = _TimeoutThenFound(reveal_on_list=2)
    ctx = _live_payments_ctx([_late_payment("pay-1", "GL-112233", "$3,528.22")], urlopen)
    summary = driver.run_driver(ctx)
    result = summary["results"][0]
    assert result["status"] == "done"
    assert result["detail"]["discussion_id"] == "d-found"
    assert result["detail"]["discussion_plan"] == "use_existing"
    assert result["detail"]["discussion_recovered_after_create_error"] is True
    assert urlopen.with_note_attempts == 1
    assert len(urlopen.posts_to("/notes")) == 1
    assert "/v8/discussions/d-found/notes" in urlopen.posts_to("/notes")[0]["url"]
    assert no_zap_fire == []


def test_next_notice_rereads_before_creating_a_second_discussion(no_zap_fire):
    # The timed-out create is not visible on the immediate re-read. The next
    # notice in the category must re-read before it posts with-note again.
    urlopen = _TimeoutThenFound(reveal_on_list=3)
    ctx = _live_payments_ctx(
        [
            _late_payment("pay-1", "GL-112233", "$3,528.22"),
            _late_payment("pay-2", "GL-445566", "$90.00"),
        ],
        urlopen,
    )
    summary = driver.run_driver(ctx)
    first, second = summary["results"]
    assert first["status"] == "skipped"
    assert first["reason"].startswith("discussion_error:")
    assert second["status"] == "done"
    assert second["detail"]["discussion_id"] == "d-found"
    assert second["detail"]["discussion_plan"] == "use_existing"
    assert urlopen.with_note_attempts == 1
    assert len(urlopen.posts_to("/notes")) == 1
    assert no_zap_fire == []


def test_intent_to_cancel_csr_task_flag_off_is_note_only(no_zap_fire, monkeypatch):
    monkeypatch.delenv(driver.INTENT_CSR_TASK_ENV, raising=False)
    notice = make_notice(
        subject=(
            "[URGENT] Fixture Insured A LLC - StreetSmart Insurance Agency: "
            "Policy(s) at risk for cancellation"
        ),
        body=(
            "Notice of Intent to Cancel. Your loan payment of $525.30 was due on 09/21/2026.\n"
            "Policy ID HO-998877\n"
        ),
        message_id="intent-off",
    )
    ctx, discussion_client = make_ctx(
        notices=[notice],
        policy_rows={"HO-998877": [policy_row()]},
    )
    summary = driver.run_driver(ctx)
    result = summary["results"][0]
    assert result["status"] == "dry_run"
    assert result["detail"]["notice_type"] == triage.INTENT_TO_CANCEL
    assert "task_payload" not in result["detail"]
    assert "task" not in summary["would_file"][0]
    assert discussion_client._urlopen.posts_to("/notes") == []
    assert no_zap_fire == []


def test_intent_to_cancel_csr_task_flag_on_assigns_the_csr(no_zap_fire, monkeypatch):
    monkeypatch.setenv(driver.INTENT_CSR_TASK_ENV, "1")
    notice = make_notice(
        subject=(
            "[URGENT] Fixture Insured A LLC - StreetSmart Insurance Agency: "
            "Policy(s) at risk for cancellation"
        ),
        body=(
            "Notice of Intent to Cancel. Your loan payment of $525.30 was due on 09/21/2026.\n"
            "Policy ID HO-998877\n"
        ),
        message_id="intent-on",
    )
    ctx, discussion_client = make_ctx(
        notices=[notice],
        policy_rows={"HO-998877": [policy_row()]},
        ascend_client=FakeAscendClient(program={"status": "overdue", "producer": _mapped_producer()}),
    )
    summary = driver.run_driver(ctx)
    result = summary["results"][0]
    assert result["status"] == "dry_run"
    assert result["detail"]["csr_username"] == "KarlaSS"
    assert summary["would_file"][0]["task"] == {
        "type": "intent_to_cancel",
        "assignee": "KarlaSS",
    }
    assert result["detail"]["task_payload"]["assignee"] == "KarlaSS"
    assert result["detail"]["task_payload"]["notice_type"] == triage.INTENT_TO_CANCEL
    assert "label" not in result["detail"]
    assert discussion_client._urlopen.posts_to("/notes") == []
    assert no_zap_fire == []
