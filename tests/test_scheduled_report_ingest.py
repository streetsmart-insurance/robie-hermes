from datetime import datetime, timezone
from io import BytesIO
from unittest.mock import MagicMock

import pytest
from openpyxl import Workbook

from robie_job_engine.scheduled_report_email_sync import classify_report
from robie_job_engine.scheduled_report_ingest import (
    EXACT_EZLYNX_SUBJECTS,
    MISSING_SUBJECTS_EXIT,
    STRUCTURE_FIELD_MASK,
    MetadataOnlyAttachmentError,
    assert_structure_has_tabular_attachment,
    collect_exact_daily_sources,
    missing_required_subjects,
    read_message_headers,
    read_message_structure,
)


def _xlsx() -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.title = "Detail"
    sheet.append(["Account", "Owner"])
    sheet.append(["Acme", "Ana"])
    output = BytesIO()
    book.save(output)
    return output.getvalue()


class _Exec:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


def _service(messages: dict[str, dict]) -> MagicMock:
    service = MagicMock()
    users = service.users.return_value
    messages_api = users.messages.return_value
    messages_api.list.return_value.execute.return_value = {
        "messages": [{"id": key} for key in messages]
    }

    def get(*, userId, id, format=None, metadataHeaders=None, fields=None):
        item = messages[id]
        if format == "metadata":
            return _Exec({
                "id": id,
                "internalDate": item["internalDate"],
                "payload": {"headers": item["headers"]},
            })
        if fields:
            assert "data" not in fields
            return _Exec({
                "id": id,
                "internalDate": item["internalDate"],
                "payload": item["structure"],
            })
        return _Exec(item["full"])

    messages_api.get.side_effect = lambda **kwargs: get(**kwargs)
    return service


def _message(subject: str, sender: str = "Applied Reporting <DoNotReply@appliedsystems.com>"):
    content = _xlsx()
    headers = [
        {"name": "From", "value": sender},
        {"name": "Subject", "value": subject},
    ]
    structure = {
        "filename": "report.xlsx",
        "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "body": {"attachmentId": "att-1"},
        "parts": [{
            "filename": "report.xlsx",
            "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "body": {"attachmentId": "att-1"},
        }],
    }
    full = {
        "id": subject,
        "internalDate": str(int(datetime(2026, 9, 10, 10, tzinfo=timezone.utc).timestamp() * 1000)),
        "payload": {
            "headers": headers,
            "parts": [{
                "filename": "report.xlsx",
                "body": {"data": __import__("base64").urlsafe_b64encode(content).decode()},
            }],
        },
    }
    return {
        "internalDate": full["internalDate"],
        "headers": headers,
        "structure": structure,
        "full": full,
    }


def test_structure_mask_never_requests_mime_body_data():
    assert "data" not in STRUCTURE_FIELD_MASK.split(",")
    assert "payload/body/data" not in STRUCTURE_FIELD_MASK
    assert "payload/parts/filename" in STRUCTURE_FIELD_MASK
    assert "payload/parts/body/attachmentId" in STRUCTURE_FIELD_MASK


def test_metadata_payload_cannot_prove_xlsx_attachment():
    with pytest.raises(MetadataOnlyAttachmentError):
        assert_structure_has_tabular_attachment({"headers": [{"name": "Subject", "value": "Robie - EZLynx Activities"}]}, subject="Robie - EZLynx Activities")


def test_exact_subjects_are_the_four_production_ezlynx_titles():
    assert missing_required_subjects([]) == list(EXACT_EZLYNX_SUBJECTS.values())
    assert missing_required_subjects(list(EXACT_EZLYNX_SUBJECTS.values())) == []
    assert classify_report("Robie - EZLynx Overdue Tasks", "file.xlsx", {
        "overdue_tasks": {"exact_subject": "Robie - EZLynx Overdue Tasks"}
    }) == "overdue_tasks"


def test_header_plus_structure_split_accepts_mail_that_metadata_alone_misses(tmp_path):
    messages = {
        f"m-{index}": _message(subject)
        for index, subject in enumerate(EXACT_EZLYNX_SUBJECTS.values(), start=1)
    }
    service = _service(messages)
    first_id = next(iter(messages))
    headers = read_message_headers(service, first_id)
    assert "parts" not in (headers.get("payload") or {})
    structure = read_message_structure(service, first_id)
    assert_structure_has_tabular_attachment(structure.get("payload") or {}, subject="Robie - EZLynx Activities")
    snapshot = collect_exact_daily_sources(
        service,
        output_dir=tmp_path,
        as_of=datetime(2026, 9, 10, 10, 45, tzinfo=timezone.utc),
    )
    assert snapshot["ok"] is True
    assert snapshot["report_kind"] == "department_tab_google_doc"
    assert set(snapshot["accepted"]) == set(EXACT_EZLYNX_SUBJECTS)
    assert set(snapshot["sources"]) == set(EXACT_EZLYNX_SUBJECTS)


def test_missing_exact_subjects_use_exit_code_two(tmp_path):
    service = _service({})
    with pytest.raises(Exception, match="no fresh scheduled report"):
        collect_exact_daily_sources(
            service,
            output_dir=tmp_path,
            as_of=datetime(2026, 9, 10, 10, 45, tzinfo=timezone.utc),
        )
    assert MISSING_SUBJECTS_EXIT == 2
