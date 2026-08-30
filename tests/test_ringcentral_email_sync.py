import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
