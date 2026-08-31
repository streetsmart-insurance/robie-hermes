import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from openpyxl import Workbook

from robie_job_engine.accountability_cli import load_ringcentral_source
from robie_job_engine.productivity import ProductivityAuditor
from robie_job_engine.ringcentral_workbooks import (
    DEFAULT_REQUIRED_SHEETS,
    REQUIRED_COLUMNS,
    RingCentralEvidenceError,
    classify_report,
    read_workbook,
    write_evidence_manifest,
)


FIXTURE = Path(__file__).parent / "fixtures" / "ringcentral" / "sanitized_performance_reports.json"


def _fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _workbook(path: Path, *sheets: str, omit_call_column: str | None = None) -> Path:
    data = _fixture()
    workbook = Workbook()
    workbook.remove(workbook.active)
    for name in sheets:
        sheet = workbook.create_sheet(name)
        if name == "Filters":
            for row in data["filters"]:
                sheet.append(row)
            continue
        key = name.casefold()
        columns = list(REQUIRED_COLUMNS[name])
        if name == "Calls" and omit_call_column:
            columns.remove(omit_call_column)
        sheet.append(columns)
        for row in data[key]:
            sheet.append([row.get(column, "") for column in columns])
    workbook.save(path)
    return path


def _manifest(path: Path, kind: str, attachments: list[Path]) -> Path:
    return write_evidence_manifest(
        path,
        report_kind=kind,
        attachments=[
            {
                "path": str(item),
                "sha256": read_workbook(item)["sha256"],
                "received_at": "2026-08-28T21:00:00+00:00",
                "sheets": read_workbook(item)["sheets"],
            }
            for item in attachments
        ],
        required_sheets=DEFAULT_REQUIRED_SHEETS[kind],
        required_users=["Alex Example", "Blair Example"],
        required_queues=["Commercial Test", "Personal Test"],
        required_queue_members={
            "Commercial Test": ["Alex Example", "Blair Example"],
            "Personal Test": [],
        },
        collected_at=datetime(2026, 8, 28, 21, tzinfo=timezone.utc),
    )


def test_subscription_label_classifier_is_exact_and_refuses_ambiguity():
    assert classify_report("ROBIE_DAILY_CALLS", "report.xlsx") == "daily"
    assert classify_report("scheduled", "ROBIE_WEEKLY_CALLS.xlsx") == "weekly"
    assert classify_report("generic calls", "report.xlsx") is None
    with pytest.raises(RingCentralEvidenceError, match="conflicting"):
        classify_report("ROBIE_DAILY_CALLS and ROBIE_WEEKLY_CALLS", "report.xlsx")


def test_workbook_missing_observed_call_column_fails_closed(tmp_path: Path):
    path = _workbook(tmp_path / "bad.xlsx", "Calls", omit_call_column="Session Id")
    with pytest.raises(RingCentralEvidenceError, match="Session Id"):
        read_workbook(path, required_sheets=("Calls",))


def test_weekly_bundle_ingests_separate_tabs_deduplicates_and_reconciles(tmp_path: Path):
    users = _workbook(tmp_path / "users.xlsx", "Filters", "Users")
    queues = _workbook(tmp_path / "queues.xlsx", "Filters", "Queues")
    calls_a = _workbook(tmp_path / "calls-a.xlsx", "Filters", "Calls")
    calls_b = _workbook(tmp_path / "calls-b.xlsx", "Filters", "Calls")
    manifest = _manifest(tmp_path / "weekly.json", "weekly", [users, queues, calls_a, calls_b])

    calls, errors, evidence = load_ringcentral_source(
        manifest, expected_kind="weekly", as_of=datetime(2026, 8, 28, 22, tzinfo=timezone.utc)
    )

    assert errors == []
    assert len(calls) == 6
    assert evidence["coverage_verified"] is True
    incidents = ProductivityAuditor().reconcile_missed_calls(
        calls, reference_time=datetime(2026, 8, 28, 22, tzinfo=timezone.utc)
    )
    direct = next(item for item in incidents if item.call_id == "direct-missed")
    assert direct.status == "RESOLVED"
    assert direct.returned_by == "Alex Example"


def test_bundle_refuses_missing_current_user_or_queue(tmp_path: Path):
    calls = _workbook(tmp_path / "calls.xlsx", "Filters", "Calls")
    manifest = write_evidence_manifest(
        tmp_path / "daily.json",
        report_kind="daily",
        attachments=[{
            "path": str(calls),
            "sha256": read_workbook(calls)["sha256"],
            "received_at": "2026-08-28T21:00:00+00:00",
            "sheets": ["Filters", "Calls"],
        }],
        required_sheets=("Calls",),
        required_users=["Alex Example", "Missing Person"],
        required_queues=["Commercial Test", "Missing Queue"],
        required_queue_members={"Commercial Test": ["Alex Example"], "Missing Queue": []},
    )
    with pytest.raises(RingCentralEvidenceError, match="missing current users.*missing current queues"):
        load_ringcentral_source(
            manifest, expected_kind="daily", as_of=datetime(2026, 8, 28, 22, tzinfo=timezone.utc)
        )


def test_bundle_refuses_wrong_daily_weekly_label(tmp_path: Path):
    calls = _workbook(tmp_path / "calls.xlsx", "Filters", "Calls")
    manifest = _manifest(tmp_path / "daily.json", "daily", [calls])
    with pytest.raises(RingCentralEvidenceError, match="expected ROBIE_WEEKLY_CALLS"):
        load_ringcentral_source(
            manifest, expected_kind="weekly", as_of=datetime(2026, 8, 28, 22, tzinfo=timezone.utc)
        )


def test_bundle_refuses_stale_attachment_even_when_workbook_is_complete(tmp_path: Path):
    calls = _workbook(tmp_path / "calls.xlsx", "Filters", "Calls")
    manifest = _manifest(tmp_path / "daily.json", "daily", [calls])
    with pytest.raises(RingCentralEvidenceError, match="stale or future-dated"):
        load_ringcentral_source(
            manifest, expected_kind="daily", as_of=datetime(2026, 8, 31, 12, tzinfo=timezone.utc)
        )
