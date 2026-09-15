from datetime import datetime, timezone
from pathlib import Path

from robie_job_engine.accountability_pipeline import (
    LATE_CHECK_HOUR,
    LATE_CHECK_MINUTE,
    TEAM_LEAD_DOC_URL,
    collect,
    deliver,
    late_check,
    needs_gmail_service,
    publish,
    reporting_date,
    run_stages,
)
from robie_job_engine.scheduled_report_ingest import EXACT_EZLYNX_SUBJECTS, MISSING_SUBJECTS_EXIT


def test_reporting_date_skips_labor_day_weekend():
    monday = datetime(2026, 9, 8, 13, 0, tzinfo=timezone.utc)
    assert reporting_date(monday) == "2026-09-04"


def test_publish_and_deliver_use_the_existing_department_doc(tmp_path: Path):
    day = "2026-09-09"
    collect_dir = tmp_path / day / "collect"
    collect_dir.mkdir(parents=True)
    (collect_dir / "collect-20260910.json").write_text(
        '{"sources": {"activities": "/tmp/a.csv"}, "ok": true}',
        encoding="utf-8",
    )
    published = publish(state_root=tmp_path, report_date=day)
    assert published["document_url"] == TEAM_LEAD_DOC_URL
    assert published["report_kind"] == "department_tab_google_doc"

    def fake_send(body, config):
        assert "department-only Google Doc tabs" in body
        assert TEAM_LEAD_DOC_URL in body
        assert "carlo@streetsmart.insurance" in config["email_recipients"]["daily"]
        return {"kind": "gmail", "message_id": "sent-1", "delivered": True}

    result = deliver(state_root=tmp_path, report_date=day, send=fake_send)
    assert result["delivered"] is True
    assert result["subject"] == "StreetSmart Yesterday Accountability — 2026-09-09"
    assert (tmp_path / day / "success.json").is_file()


def test_late_check_uses_nine_twenty_not_six_fifty(tmp_path: Path):
    assert LATE_CHECK_HOUR == 9 and LATE_CHECK_MINUTE == 20
    late = late_check(state_root=tmp_path, report_date="2026-09-10")
    assert late["late"] is True
    assert "9:20 AM Eastern" in late["body"]
    assert "6:50" not in late["body"]
    assert late["subject"] == "LATE — StreetSmart accountability report not delivered — 2026-09-10"


def test_gmail_send_readback_marks_delivered(monkeypatch, tmp_path: Path):
    from robie_job_engine import accountability_delivery

    class _Exec:
        def __init__(self, payload):
            self.payload = payload

        def execute(self):
            return self.payload

    class FakeGmail:
        def users(self):
            return self

        def messages(self):
            return self

        def send(self, userId, body):
            return _Exec({"id": "gmail-123"})

        def get(self, userId, id, format=None):
            assert format == "minimal"
            return _Exec({"id": id})

    monkeypatch.setattr(accountability_delivery, "_delegated_gmail_sender", lambda account, sender: FakeGmail())
    report = tmp_path / "note.txt"
    report.write_text("ready", encoding="utf-8")
    receipts = accountability_delivery.deliver_report(
        report,
        mode="daily",
        delivery={
            "email_sender": "robie@streetsmart.insurance",
            "email_recipients": {"daily": ["carlo@streetsmart.insurance"]},
        },
        environment={"ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT": "sa@test"},
        subject="StreetSmart Yesterday Accountability — 2026-09-09",
    )
    assert receipts[0]["delivered"] is True
    ok, observed = accountability_delivery.verify_delivery_receipts(receipts)
    assert ok is True
    assert observed[0]["delivered"] is True


def test_collect_missing_subjects_exits_two(tmp_path: Path):
    from unittest.mock import MagicMock

    service = MagicMock()
    service.users.return_value.messages.return_value.list.return_value.execute.return_value = {
        "messages": []
    }
    now = datetime(2026, 9, 10, 10, 45, tzinfo=timezone.utc)
    try:
        collect(service, state_root=tmp_path, now=now)
    except SystemExit as exc:
        assert exc.code == MISSING_SUBJECTS_EXIT
    else:
        raise AssertionError("collect must fail closed with exit 2")
    failed = tmp_path / "2026-09-09" / "collect-failed.json"
    assert failed.is_file()
    assert "Robie - EZLynx Activities" in failed.read_text(encoding="utf-8")


def test_publish_only_needs_gmail_only_when_collect_snapshot_missing(tmp_path: Path):
    now = datetime(2026, 9, 10, 13, tzinfo=timezone.utc)
    assert needs_gmail_service(
        collect_enabled=False, publish_enabled=True, state_root=tmp_path, now=now
    ) is True
    folder = tmp_path / "2026-09-09" / "collect"
    folder.mkdir(parents=True)
    (folder / "collect-20260910.json").write_text("{}", encoding="utf-8")
    assert needs_gmail_service(
        collect_enabled=False, publish_enabled=True, state_root=tmp_path, now=now
    ) is False
    assert needs_gmail_service(
        collect_enabled=True, publish_enabled=False, state_root=tmp_path, now=now
    ) is True
    assert needs_gmail_service(
        collect_enabled=False, publish_enabled=False, state_root=tmp_path, now=now
    ) is False


def test_nine_am_publish_retries_collect_when_six_forty_five_failed(tmp_path: Path, monkeypatch):
    captured = {}

    def fake_collect(service, *, state_root, now=None, config=None):
        captured["retried"] = True
        day = "2026-09-09"
        folder = Path(state_root) / day / "collect"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "collect-20260910.json").write_text("{}", encoding="utf-8")
        return {"ok": True, "report_date": day, "subjects": EXACT_EZLYNX_SUBJECTS}

    monkeypatch.setattr("robie_job_engine.accountability_pipeline.collect", fake_collect)
    monkeypatch.setattr(
        "robie_job_engine.accountability_pipeline.reporting_date",
        lambda now=None, holiday_calendar=None: "2026-09-09",
    )
    result = run_stages(
        collect_enabled=False,
        publish_enabled=True,
        deliver_enabled=False,
        late_check_enabled=False,
        service=object(),
        state_root=tmp_path,
        now=datetime(2026, 9, 10, 13, tzinfo=timezone.utc),
    )
    assert captured["retried"] is True
    assert result["publish"]["ok"] is True
