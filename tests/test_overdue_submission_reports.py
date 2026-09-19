from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from robie_job_engine.models import JobStatus
from robie_job_engine.overdue_submission_reports import (
    ACTION,
    CC,
    JOB_TYPE,
    SOP_URL,
    SUBJECT,
    OverdueSubmissionReportVerifier,
    OverdueSubmissionReportWorker,
    build_producer_report,
    resolve_recipients,
    validate_submission_observation,
)
from robie_job_engine.request_routing import classify_request
from robie_job_engine.submission_report_schedule import (
    CRON_SPEC,
    install_submission_report_schedule,
)


def _record(producer: str = "Producer One", suffix: str = "one") -> dict:
    return {
        "applicant": f"Applicant {suffix}",
        "assigned_producer": producer,
        "status": "Quote Submitted",
        "quote_due_date": "2026-07-01",
        "effective_date": "2026-07-15",
        "age_days": 80,
        "submission_url": f"https://app.ezlynx.com/web/submission-center/submissions/{suffix}",
        "red_state_evidence": {
            "overdue_class": True,
            "computed_color": "rgb(211, 47, 47)",
        },
        "source_page": 1,
    }


def _observation(records: list[dict] | None = None) -> dict:
    records = list(records or [])
    return {
        "read_only": True,
        "source_status": "available",
        "scope_time_frame": "All Submissions",
        "scope_assigned_producer": "Streetsmart Insurance",
        "scope_my_submissions": False,
        "mat_row_count": 100,
        "pager_total_present": True,
        "pager_total": 101,
        "status_aria_sort": "ascending",
        "first_row_non_closed": True,
        "day_31_qualifies": True,
        "headers_present": True,
        "email_delivery_enabled": False,
        "emails_sent": 0,
        "first_closed_row_inspected": True,
        "full_dataset_exhausted": False,
        "first_closed_row_status": "Closed - Bound",
        "pages_reviewed": 1,
        "rows_inspected_through_boundary": 2,
        "non_closed_rows_inspected": 1,
        "open_over_30_count": len(records),
        "qualifying_records": records,
        "counts_by_producer": dict(__import__("collections").Counter(item["assigned_producer"] for item in records)),
        "counts_by_status": dict(__import__("collections").Counter(item["status"] for item in records)),
    }


def _job(manifest: str = "/approved/manifest.json", *, authorized: bool = True) -> dict:
    return {
        "action_type": JOB_TYPE,
        "payload": {
            "manifest_path": manifest,
            "authorized_actions": [ACTION] if authorized else [],
        },
    }


def test_report_contract_and_email_content():
    record = _record()
    summary = validate_submission_observation(_observation([record]))
    body = build_producer_report("Producer One", [record])
    assert summary["qualifying_count"] == 1
    assert SUBJECT == "Action required: EZLynx submissions 31+ days overdue"
    assert record["submission_url"] in body
    assert SOP_URL in body
    assert "-ROBIE AI on behalf of Carlo" in body
    assert CC == ("carlo@streetsmart.insurance", "jake@streetsmart.insurance")


def test_unresolved_producer_blocks_all_email():
    calls = []
    worker = OverdueSubmissionReportWorker(
        audit_reader=lambda: _observation([_record()]),
        directory_loader=lambda _path: {},
        mailer=lambda **kwargs: calls.append(kwargs),
    )
    result = worker.perform(_job(), idempotency_key="report-1")
    assert result.succeeded is False
    assert result.hold_status == JobStatus.NEEDS_CLARIFICATION
    assert calls == []


def test_authorized_worker_sends_one_email_per_producer_and_redacts_destination():
    sent = []

    def mailer(**kwargs):
        sent.append(kwargs)
        return {"kind": "gmail", "message_id": f"m-{len(sent)}", "sender": "robie@streetsmart.insurance"}

    records = [_record("Producer One", "one"), _record("Producer Two", "two")]
    worker = OverdueSubmissionReportWorker(
        audit_reader=lambda: _observation(records),
        directory_loader=lambda _path: {
            "producer one": "one@streetsmart.insurance",
            "producer two": "two@streetsmart.insurance",
        },
        mailer=mailer,
    )
    result = worker.perform(_job(), idempotency_key="report-2")
    assert result.succeeded is True
    assert len(sent) == 2
    assert all(item["cc"] == list(CC) and item["subject"] == SUBJECT for item in sent)
    assert "one@streetsmart.insurance" not in str(result.destination)
    assert "two@streetsmart.insurance" not in str(result.destination)


def test_unauthorized_worker_sends_nothing():
    calls = []
    worker = OverdueSubmissionReportWorker(
        audit_reader=lambda: _observation([_record()]),
        directory_loader=lambda _path: {"producer one": "one@streetsmart.insurance"},
        mailer=lambda **kwargs: calls.append(kwargs),
    )
    result = worker.perform(_job(authorized=False), idempotency_key="report-3")
    assert result.succeeded is False
    assert calls == []


def test_test_environment_uses_only_configured_safe_sink():
    sent = []
    worker = OverdueSubmissionReportWorker(
        audit_reader=lambda: _observation([_record()]),
        directory_loader=lambda _path: {"producer one": "producer@streetsmart.insurance"},
        mailer=lambda **kwargs: sent.append(kwargs) or {
            "kind": "gmail", "message_id": "test-1", "sender": "robie@streetsmart.insurance"
        },
    )
    with patch.dict(
        "os.environ",
        {"ROBIE_ENV": "TEST", "ROBIE_OVERDUE_SUBMISSION_TEST_RECIPIENT": "qa@streetsmart.insurance"},
        clear=False,
    ):
        result = worker.perform(_job(), idempotency_key="report-test-sink")
    assert result.succeeded is True
    assert sent[0]["to"] == ["qa@streetsmart.insurance"]
    assert sent[0]["cc"] == []
    assert sent[0]["subject"].startswith("TEST ONLY - ")
    assert "producer@streetsmart.insurance" not in str(sent)


def test_test_environment_fails_closed_without_safe_sink():
    sent = []
    worker = OverdueSubmissionReportWorker(
        audit_reader=lambda: _observation([_record()]),
        directory_loader=lambda _path: {"producer one": "producer@streetsmart.insurance"},
        mailer=lambda **kwargs: sent.append(kwargs),
    )
    with patch.dict("os.environ", {"ROBIE_ENV": "TEST", "ROBIE_OVERDUE_SUBMISSION_TEST_RECIPIENT": ""}, clear=False):
        result = worker.perform(_job(), idempotency_key="report-test-no-sink")
    assert result.succeeded is False
    assert sent == []


def test_verifier_freshly_reads_all_gmail_receipts():
    receipts = [
        {"kind": "gmail", "message_id": "m-1", "sender": "robie@streetsmart.insurance"},
        {"kind": "gmail", "message_id": "m-2", "sender": "robie@streetsmart.insurance"},
    ]
    verifier = OverdueSubmissionReportVerifier(
        delivery_readback=lambda value: (value == receipts, [{"exists_in_sent_mailbox": True}] * 2)
    )
    result = verifier.verify(
        _job(),
        {"destination": {"delivery_receipts": receipts, "producer_count": 2, "qualifying_count": 2}},
    )
    assert result.verified is True
    assert result.evidence.method == "GMAIL_SENT_READBACK"


def test_routing_separates_send_from_read_only_audit():
    assert classify_request("Audit the EZLynx Submission Center overdue list").action_type == "ezlynx.submission_audit"
    assert classify_request("Audit the Submission Center overdue list and email each producer").action_type == JOB_TYPE
    assert classify_request("Audit the overdue Submission Center list but do not send emails").action_type == "ezlynx.submission_audit"


def test_schedule_preserves_existing_monday_9am_eastern_cadence(tmp_path: Path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"google_sheets": {"enabled": True}}), encoding="utf-8")
    scheduled = install_submission_report_schedule(
        str(tmp_path / "jobs.db"),
        str(manifest),
        now=datetime.fromisoformat("2026-09-18T12:00:00-04:00"),
    )
    assert scheduled["action_type"] == JOB_TYPE
    assert scheduled["cron_spec"] == CRON_SPEC == "0 9 * * 1"
    assert scheduled["timezone"] == "America/New_York"
    assert scheduled["parameters"]["authorized_actions"] == [ACTION]


def test_schedule_cannot_be_installed_in_production_before_test_promotion(tmp_path: Path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"google_sheets": {"enabled": True}}), encoding="utf-8")
    with patch.dict(os.environ, {"ROBIE_ENV": "PRODUCTION"}, clear=False):
        try:
            install_submission_report_schedule(str(tmp_path / "jobs.db"), str(manifest))
        except Exception as exc:
            assert "not Production-ready" in str(exc)
        else:
            raise AssertionError("unpromoted report schedule was installed in Production")


def test_recipient_resolution_is_exact_casefolded_name_match():
    assert resolve_recipients([_record("Producer One")], {"producer one": "one@streetsmart.insurance"}) == {
        "Producer One": "one@streetsmart.insurance"
    }
