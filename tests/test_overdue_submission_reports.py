from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from robie_job_engine.complete_guard import (
    expected_postcondition_missing,
    postcondition_mismatch,
    require_complete_postcondition,
)
from robie_job_engine.google_sheets_accountability import classify_sheets_auth_error
from robie_job_engine.models import VERIFIER_AUTHORITY, JobStatus
from robie_job_engine.overdue_submission_reports import (
    ACTION,
    CC,
    JOB_TYPE,
    RESOURCE_ID,
    SOP_URL,
    SUBJECT,
    OverdueSubmissionReportVerifier,
    OverdueSubmissionReportWorker,
    SubmissionReportContractError,
    agency_email_directory_from_registry,
    build_producer_report,
    load_approved_producer_directory,
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
    assert result.evidence.expected["exists_in_sent_mailbox"] is True
    assert result.evidence.expected["resource_id"] == RESOURCE_ID
    assert result.evidence.observed["exists_in_sent_mailbox"] is True
    assert expected_postcondition_missing(result.evidence.expected) is None
    assert postcondition_mismatch(result.evidence.expected, result.evidence.observed) is None


def test_verified_gmail_readback_authorizes_complete():
    expected = {
        "resource_id": RESOURCE_ID,
        "exists_in_sent_mailbox": True,
        "gmail_receipt_count": 2,
        "producer_count": 2,
        "qualifying_count": 2,
    }
    observed = {
        **expected,
        "unique_message_ids": 2,
        "delivery": [{"exists_in_sent_mailbox": True}, {"exists_in_sent_mailbox": True}],
    }
    require_complete_postcondition(
        current=JobStatus.VERIFYING,
        authority=VERIFIER_AUTHORITY,
        verified=True,
        authoritative=True,
        expected=expected,
        observed=observed,
        captured_at=datetime.now(timezone.utc).isoformat(),
        evidence_ref="sha",
        locator=RESOURCE_ID,
        job_id="overdue-complete-1",
        verifier_authority=VERIFIER_AUTHORITY,
        intended=RESOURCE_ID,
    )


def test_gmail_readback_without_mailbox_evidence_cannot_complete():
    receipts = [{"kind": "gmail", "message_id": "m-1", "sender": "robie@streetsmart.insurance"}]
    verifier = OverdueSubmissionReportVerifier(
        delivery_readback=lambda value: (True, [{"exists_in_sent_mailbox": False}])
    )
    result = verifier.verify(
        _job(),
        {"destination": {"delivery_receipts": receipts, "producer_count": 1, "qualifying_count": 1}},
    )
    assert result.verified is False
    assert result.evidence.observed["exists_in_sent_mailbox"] is False
    assert postcondition_mismatch(result.evidence.expected, result.evidence.observed)


def test_zero_result_fresh_read_is_complete_postcondition():
    verifier = OverdueSubmissionReportVerifier(
        delivery_readback=lambda value: (True, []),
        audit_reader=lambda: _observation([]),
    )
    result = verifier.verify(
        _job(),
        {"destination": {"delivery_receipts": [], "producer_count": 0, "qualifying_count": 0}},
    )
    assert result.verified is True
    assert result.evidence.method == "EZLYNX_PLAYWRIGHT_FRESH_READBACK"
    assert expected_postcondition_missing(result.evidence.expected) is None
    assert postcondition_mismatch(result.evidence.expected, result.evidence.observed) is None
    require_complete_postcondition(
        current=JobStatus.VERIFYING,
        authority=VERIFIER_AUTHORITY,
        verified=True,
        authoritative=True,
        expected=result.evidence.expected,
        observed=result.evidence.observed,
        captured_at=datetime.now(timezone.utc).isoformat(),
        evidence_ref="sha",
        locator=RESOURCE_ID,
        job_id="overdue-zero-1",
        verifier_authority=VERIFIER_AUTHORITY,
        intended=RESOURCE_ID,
    )


def test_sheets_scope_error_is_fail_closed_and_never_invents_emails():
    classified = classify_sheets_auth_error(RuntimeError("ACCESS_TOKEN_SCOPE_INSUFFICIENT"))
    assert "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in str(classified)
    assert "spreadsheets.readonly" in str(classified)
    assert "Never invent producer emails" in str(classified)
    forbidden = classify_sheets_auth_error(RuntimeError("HttpError 403 PERMISSION_DENIED"))
    assert "403" in str(forbidden)
    assert "Never invent producer emails" in str(forbidden)


def test_roster_load_fails_closed_when_sheets_scope_is_insufficient(tmp_path: Path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"google_sheets": {"enabled": True, "spreadsheet_id": "sheet-1", "tables": {"employees": {"range": "A:Z", "allowed_columns": ["name"]}}}}),
        encoding="utf-8",
    )
    with patch(
        "robie_job_engine.google_sheets_accountability.collect_allowlisted_tables",
        side_effect=RuntimeError("ACCESS_TOKEN_SCOPE_INSUFFICIENT"),
    ):
        try:
            load_approved_producer_directory(str(manifest))
        except SubmissionReportContractError as exc:
            assert "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in str(exc)
            assert "Never invent producer emails" in str(exc)
        else:
            raise AssertionError("roster load invented a directory after a Sheets scope miss")


def test_test_sink_does_not_send_when_roster_resolve_fails():
    sent = []
    worker = OverdueSubmissionReportWorker(
        audit_reader=lambda: _observation([_record()]),
        directory_loader=lambda _path: (_ for _ in ()).throw(
            SubmissionReportContractError(
                "approved active-employee roster is unavailable: ACCESS_TOKEN_SCOPE_INSUFFICIENT. "
                "Never invent producer emails."
            )
        ),
        mailer=lambda **kwargs: sent.append(kwargs),
    )
    with patch.dict(
        "os.environ",
        {"ROBIE_ENV": "TEST", "ROBIE_OVERDUE_SUBMISSION_TEST_RECIPIENT": "carlo@streetsmart.insurance"},
        clear=False,
    ):
        result = worker.perform(_job(), idempotency_key="report-sheets-fail")
    assert result.succeeded is False
    assert sent == []


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


def test_agency_directory_skips_active_gmail_external_producer(caplog):
    registry = {
        "source_status": "available",
        "employees": {
            "Connie Dejesus": {
                "email": "conniesbusinesssolutionsllc@gmail.com",
                "role": "External Producer",
                "department": "Sales",
                "status": "Active",
            },
            "Jazmin Molina": {
                "email": "Jazmin@StreetSmart.Insurance",
                "role": "Sales Producer",
                "status": "Active",
            },
            "Missing Email": {
                "email": "",
                "role": "Producer",
                "status": "Active",
            },
        },
    }
    with caplog.at_level(logging.WARNING):
        directory = agency_email_directory_from_registry(registry)
    assert directory == {"jazmin molina": "jazmin@streetsmart.insurance"}
    assert "connie dejesus" not in directory
    assert "missing email" not in directory
    assert "Connie Dejesus" in caplog.text
    assert "conniesbusinesssolutionsllc@gmail.com" in caplog.text
    assert "non-agency work email" in caplog.text
    assert "External Producer" in caplog.text
    assert "Missing Email: missing work email" in caplog.text


def test_agency_directory_keeps_active_streetsmart_email():
    directory = agency_email_directory_from_registry(
        {
            "source_status": "available",
            "employees": {
                "Jazmin Molina": {
                    "email": "Jazmin@StreetSmart.Insurance",
                    "role": "Sales Producer",
                    "status": "Active",
                }
            },
        }
    )
    assert directory == {"jazmin molina": "jazmin@streetsmart.insurance"}


def test_skipped_external_producer_still_blocks_when_they_need_a_mailbox():
    directory = agency_email_directory_from_registry(
        {
            "source_status": "available",
            "employees": {
                "Connie Dejesus": {
                    "email": "conniesbusinesssolutionsllc@gmail.com",
                    "role": "External Producer",
                    "status": "Active",
                },
                "Jazmin Molina": {
                    "email": "jazmin@streetsmart.insurance",
                    "role": "Sales Producer",
                    "status": "Active",
                },
            },
        }
    )
    try:
        resolve_recipients([_record("Connie Dejesus")], directory)
    except SubmissionReportContractError as exc:
        assert "producer work email could not be resolved for: Connie Dejesus" in str(exc)
    else:
        raise AssertionError("skipped 1099 producer was emailed from a non-agency address")


def test_agency_directory_fails_closed_when_skips_leave_no_agency_emails():
    try:
        agency_email_directory_from_registry(
            {
                "source_status": "available",
                "employees": {
                    "Connie Dejesus": {
                        "email": "conniesbusinesssolutionsllc@gmail.com",
                        "role": "External Producer",
                        "status": "Active",
                    },
                    "Missing Email": {"email": "", "role": "Producer", "status": "Active"},
                },
            }
        )
    except SubmissionReportContractError as exc:
        assert str(exc) == "approved roster contains no active employees"
    else:
        raise AssertionError("empty post-skip directory was accepted")


def test_roster_load_succeeds_with_mixed_active_external_and_agency_rows(tmp_path, caplog):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "google_sheets": {
                    "enabled": True,
                    "spreadsheet_id": "sheet-1",
                    "employee_role_table": "employees",
                    "employee_role_columns": {
                        "name": "Name",
                        "role": "Position",
                        "email": "Email",
                        "department": "Department",
                        "status": "Status",
                        "active_value": "Active",
                    },
                    "tables": {
                        "employees": {
                            "range": "Employees!A:Z",
                            "allowed_columns": ["Name", "Position", "Email", "Department", "Status"],
                        }
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    snapshot = {
        "source_status": "available",
        "tables": {
            "employees": {
                "source_status": "available",
                "rows": [
                    {
                        "Name": "Connie Dejesus",
                        "Position": "External Producer",
                        "Email": "conniesbusinesssolutionsllc@gmail.com",
                        "Department": "Sales",
                        "Status": "Active",
                    },
                    {
                        "Name": "Jazmin Molina",
                        "Position": "Sales Producer",
                        "Email": "Jazmin@StreetSmart.Insurance",
                        "Department": "Sales",
                        "Status": "Active",
                    },
                    {
                        "Name": "Former Person",
                        "Position": "Producer",
                        "Email": "former@streetsmart.insurance",
                        "Department": "Sales",
                        "Status": "Terminated",
                    },
                ],
            }
        },
    }
    with patch(
        "robie_job_engine.google_sheets_accountability.collect_allowlisted_tables",
        return_value=snapshot,
    ), caplog.at_level(logging.WARNING):
        directory = load_approved_producer_directory(str(manifest))
    assert directory == {"jazmin molina": "jazmin@streetsmart.insurance"}
    assert "connie dejesus" not in directory
    assert "Connie Dejesus" in caplog.text
    assert "non-agency work email" in caplog.text
