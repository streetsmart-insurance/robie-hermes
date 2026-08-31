import base64
import json
from io import BytesIO
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

from openpyxl import Workbook

from robie_job_engine.ringcentral_email_sync import RingCentralEmailSync


class _Execute:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class _Attachments:
    def get(self, **kwargs):
        return _Execute({"data": base64.urlsafe_b64encode(b"Call ID,Direction\n1,Inbound\n").decode()})


class _Messages:
    def __init__(self, internal_date):
        self.internal_date = internal_date

    def list(self, **kwargs):
        return _Execute({"messages": [{"id": "m-1"}]})

    def get(self, **kwargs):
        return _Execute({
            "internalDate": str(self.internal_date),
            "payload": {"parts": [{"filename": "Call Log.csv", "body": {"attachmentId": "a-1"}}]},
        })

    def attachments(self):
        return _Attachments()


class _Users:
    def __init__(self, internal_date):
        self.internal_date = internal_date

    def messages(self):
        return _Messages(self.internal_date)


class _Gmail:
    def __init__(self, received):
        self.internal_date = int(received.timestamp() * 1000)

    def users(self):
        return _Users(self.internal_date)


def test_ringcentral_email_collection_accepts_fresh_report_and_rejects_stale(tmp_path: Path):
    now = datetime(2026, 8, 30, 17, tzinfo=timezone.utc)
    crawler = RingCentralEmailSync(tmp_path)
    fresh = crawler.fetch_latest_call_log_attachment(_Gmail(now - timedelta(hours=2)), as_of=now)
    assert fresh and fresh.read_text().startswith("Call ID")
    stale = crawler.fetch_latest_call_log_attachment(
        _Gmail(now - timedelta(hours=72)), as_of=now, max_age_hours=36
    )
    assert stale is None


def _daily_workbook_bytes() -> bytes:
    workbook = Workbook()
    filters = workbook.active
    filters.title = "Filters"
    filters.append(["Selected Users", "Alex Example"])
    filters.append(["Selected Queues", "Commercial Test"])
    calls = workbook.create_sheet("Calls")
    calls.append([
        "Session Id", "From Name", "From Number", "To Name", "To Number",
        "Result", "Call Length", "Handle Time", "Call Start Time",
        "Call Direction", "Queue",
    ])
    calls.append([
        "call-1", "Caller", "+1 202-555-0101", "Alex Example", "+1 202-555-0191",
        "Answered", "00:01:00", "00:01:05", "08/28/2026 09:00 AM",
        "Inbound", "Commercial Test",
    ])
    stream = BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def test_xlsx_collector_selects_explicit_daily_label_and_checksum_binds(tmp_path: Path):
    now = datetime(2026, 8, 30, 17, tzinfo=timezone.utc)
    encoded = base64.urlsafe_b64encode(_daily_workbook_bytes()).decode()
    service = MagicMock()
    messages = service.users.return_value.messages.return_value
    messages.list.return_value.execute.return_value = {"messages": [{"id": "daily"}, {"id": "weekly"}]}

    def get_message(*, id, **kwargs):
        label = "ROBIE_DAILY_CALLS" if id == "daily" else "ROBIE_WEEKLY_CALLS"
        return _Execute({
            "internalDate": str(int((now - timedelta(hours=1)).timestamp() * 1000)),
            "payload": {
                "headers": [{"name": "Subject", "value": label}],
                "parts": [{"filename": f"{label}.xlsx", "body": {"attachmentId": id}}],
            },
        })

    messages.get.side_effect = get_message
    messages.attachments.return_value.get.return_value.execute.return_value = {"data": encoded}
    manifest = RingCentralEmailSync(tmp_path).fetch_report_bundle(
        service,
        report_kind="daily",
        required_users=["Alex Example"],
        required_queues=["Commercial Test"],
        required_queue_members={"Commercial Test": ["Alex Example"]},
        as_of=now,
    )
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["report_label"] == "ROBIE_DAILY_CALLS"
    assert len(data["attachments"]) == 1
    assert data["attachments"][0]["sha256"]
