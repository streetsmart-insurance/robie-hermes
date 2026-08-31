import base64
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from openpyxl import Workbook

from robie_job_engine.scheduled_report_email_sync import (
    ScheduledReportEvidenceError,
    classify_report,
    collect_scheduled_tabular_reports,
)


def _xlsx(headers, row, sheet="Submissions"):
    book = Workbook()
    page = book.active
    page.title = sheet
    page.append(headers)
    page.append(row)
    output = BytesIO()
    book.save(output)
    return output.getvalue()


def _service(filename, content, subject="ROBIE_WEEKLY_SUBMISSIONS", sender="reports@example.test"):
    service = MagicMock()
    users = service.users.return_value
    users.messages.return_value.list.return_value.execute.return_value = {"messages": [{"id": "m1"}]}
    users.messages.return_value.get.return_value.execute.return_value = {
        "internalDate": str(int(datetime(2026, 8, 31, tzinfo=timezone.utc).timestamp() * 1000)),
        "payload": {"headers": [
                        {"name": "From", "value": sender},
                        {"name": "Subject", "value": subject},
                        {"name": "Authentication-Results", "value": "mx.google.com; dmarc=pass header.from=example.test"},
                    ],
                    "parts": [{"filename": filename, "body": {"data": base64.urlsafe_b64encode(content).decode()}}]},
    }
    return service


def _config():
    return {"allowed_sender_domains": ["example.test"], "max_age_hours": 48, "reports": {
        "submissions": {"label": "ROBIE_WEEKLY_SUBMISSIONS", "worksheet": "Submissions",
                        "required_columns": ["Submission URL", "Quote Due Date"], "source": "submissions"}
    }}


def test_classifier_is_exact_and_rejects_conflicting_labels():
    reports = {"a": {"label": "ROBIE_A"}, "b": {"label": "ROBIE_B"}}
    assert classify_report("ROBIE_A", "file.xlsx", reports) == "a"
    assert classify_report("ROBIE_A_EXTRA", "file.xlsx", reports) is None
    with pytest.raises(ScheduledReportEvidenceError, match="conflicting"):
        classify_report("ROBIE_A ROBIE_B", "file.xlsx", reports)


def test_collects_fresh_xlsx_with_checksum_bound_manifest(tmp_path: Path):
    content = _xlsx(["Submission URL", "Quote Due Date"], ["https://example.test/sub/1", "2026-07-01"])
    result = collect_scheduled_tabular_reports(
        _service("ROBIE_WEEKLY_SUBMISSIONS.xlsx", content), output_dir=tmp_path, config=_config(),
        as_of=datetime(2026, 8, 31, 1, tzinfo=timezone.utc),
    )
    assert Path(result["sources"]["submissions"]).is_file()
    assert result["evidence"]["submissions"]["worksheet"] == "Submissions"
    assert len(result["evidence"]["submissions"]["attachment_sha256"]) == 64


def test_missing_required_column_fails_closed(tmp_path: Path):
    content = _xlsx(["Submission URL"], ["https://example.test/sub/1"])
    with pytest.raises(ScheduledReportEvidenceError, match="Quote Due Date"):
        collect_scheduled_tabular_reports(
            _service("ROBIE_WEEKLY_SUBMISSIONS.xlsx", content), output_dir=tmp_path, config=_config(),
            as_of=datetime(2026, 8, 31, 1, tzinfo=timezone.utc),
        )


def test_untrusted_sender_or_stale_message_cannot_satisfy_required_report(tmp_path: Path):
    content = _xlsx(["Submission URL", "Quote Due Date"], ["https://example.test/sub/1", "2026-07-01"])
    with pytest.raises(ScheduledReportEvidenceError, match="no fresh scheduled report"):
        collect_scheduled_tabular_reports(
            _service("ROBIE_WEEKLY_SUBMISSIONS.xlsx", content, sender="attacker@invalid.test"),
            output_dir=tmp_path, config=_config(), as_of=datetime(2026, 8, 31, 1, tzinfo=timezone.utc),
        )


def test_sender_without_aligned_dmarc_cannot_satisfy_required_report(tmp_path: Path):
    content = _xlsx(["Submission URL", "Quote Due Date"], ["https://example.test/sub/1", "2026-07-01"])
    service = _service("ROBIE_WEEKLY_SUBMISSIONS.xlsx", content)
    message = service.users.return_value.messages.return_value.get.return_value.execute.return_value
    message["payload"]["headers"][-1]["value"] = "mx.google.com; dmarc=fail header.from=example.test"
    with pytest.raises(ScheduledReportEvidenceError, match="no fresh scheduled report"):
        collect_scheduled_tabular_reports(
            service, output_dir=tmp_path, config=_config(),
            as_of=datetime(2026, 8, 31, 1, tzinfo=timezone.utc),
        )
