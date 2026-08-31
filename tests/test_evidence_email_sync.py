import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from robie_job_engine.evidence_email_sync import EvidenceEmailError, collect_evidence_attachments


class _Execute:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


def _service(now: datetime, attachments: list[tuple[str, bytes]], *, sender="reports@vendor.example"):
    service = MagicMock()
    messages = service.users.return_value.messages.return_value
    messages.list.return_value.execute.return_value = {"messages": [{"id": "m-1"}]}
    messages.get.return_value.execute.return_value = {
        "internalDate": str(int((now - timedelta(hours=1)).timestamp() * 1000)),
        "payload": {
            "headers": [
                {"name": "From", "value": sender},
                {"name": "Subject", "value": "ROBIE_EZLYNX_TASKS scheduled export"},
                {"name": "Authentication-Results", "value": "mx.google.com; dmarc=pass header.from=vendor.example"},
            ],
            "parts": [
                {"filename": filename, "body": {"attachmentId": f"a-{index}"}}
                for index, (filename, _) in enumerate(attachments)
            ],
        },
    }
    encoded = {
        f"a-{index}": base64.urlsafe_b64encode(content).decode()
        for index, (_, content) in enumerate(attachments)
    }

    def attachment(*, id, **kwargs):
        return _Execute({"data": encoded[id]})

    messages.attachments.return_value.get.side_effect = attachment
    return service


def _task_spec():
    return {
        "sender_allowlist": ["reports@vendor.example"],
        "subject_markers": ["ROBIE_EZLYNX_TASKS"],
        "filename_markers": ["tasks"],
        "extensions": [".csv"],
        "required_columns": ["Assigned To", "Status"],
        "max_age_hours": 36,
    }


def test_collector_checksum_binds_explicit_sender_label_filename_and_schema(tmp_path: Path):
    now = datetime(2026, 8, 30, 17, tzinfo=timezone.utc)
    content = b"Assigned To,Status,Due Date\nAlex Example,Open,08/31/2026\n"
    paths, manifest = collect_evidence_attachments(
        _service(now, [("EZLynx_Tasks.csv", content)]),
        output_dir=tmp_path,
        source_specs={"tasks": _task_spec()},
        as_of=now,
    )
    assert Path(paths["tasks"]).read_bytes() == content
    receipt = json.loads(manifest.read_text(encoding="utf-8"))["attachments"][0]
    assert receipt["source"] == "tasks"
    assert receipt["sha256"]
    assert "m-1" not in json.dumps(receipt)
    assert "reports@vendor.example" not in json.dumps(receipt)


def test_collector_refuses_wrong_sender_and_missing_schema(tmp_path: Path):
    now = datetime(2026, 8, 30, 17, tzinfo=timezone.utc)
    with pytest.raises(EvidenceEmailError, match="no fresh explicitly configured tasks"):
        collect_evidence_attachments(
            _service(now, [("EZLynx_Tasks.csv", b"Assigned To,Status\nA,Open\n")], sender="spoof@example.com"),
            output_dir=tmp_path,
            source_specs={"tasks": _task_spec()},
            as_of=now,
        )
    with pytest.raises(EvidenceEmailError, match="missing required columns"):
        collect_evidence_attachments(
            _service(now, [("EZLynx_Tasks.csv", b"Employee,State\nA,Open\n")]),
            output_dir=tmp_path,
            source_specs={"tasks": _task_spec()},
            as_of=now,
        )


def test_collector_refuses_sender_without_dmarc_pass(tmp_path: Path):
    now = datetime(2026, 8, 30, 17, tzinfo=timezone.utc)
    service = _service(now, [("EZLynx_Tasks.csv", b"Assigned To,Status\nA,Open\n")])
    message = service.users.return_value.messages.return_value.get.return_value.execute.return_value
    message["payload"]["headers"][-1]["value"] = "mx.google.com; dmarc=fail"
    with pytest.raises(EvidenceEmailError, match="no fresh explicitly configured tasks"):
        collect_evidence_attachments(
            service,
            output_dir=tmp_path,
            source_specs={"tasks": _task_spec()},
            as_of=now,
        )


def test_collector_refuses_ambiguous_newest_attachments(tmp_path: Path):
    now = datetime(2026, 8, 30, 17, tzinfo=timezone.utc)
    with pytest.raises(EvidenceEmailError, match="ambiguous"):
        collect_evidence_attachments(
            _service(now, [
                ("EZLynx_Tasks_A.csv", b"Assigned To,Status\nA,Open\n"),
                ("EZLynx_Tasks_B.csv", b"Assigned To,Status\nB,Open\n"),
            ]),
            output_dir=tmp_path,
            source_specs={"tasks": _task_spec()},
            as_of=now,
        )
