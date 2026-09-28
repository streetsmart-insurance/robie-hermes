"""Holiday office-alert parsing, windows, and dry-run send gate."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from robie_job_engine import staff_holiday_alert as holiday
from robie_job_engine.models import JobStatus
from tests.durable_temp import durable_temporary_directory

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "holiday_schedule_2026_2027.html"

MARKDOWN = """
# 2026-2027 Holiday Schedule

| Holiday / event | Date | Office status |
| --- | --- | --- |
| Columbus Day | Monday, Oct 12, 2026 | Closed |
| Memorial Day | Monday, May 25, 2026 | Closed; close early Friday, May 22, 2026 at 3:00 PM |
| New Year's Day 2026 | Thursday, Jan 1, 2026 and Friday, Jan 2, 2026 | Closed |
| Black Friday | Friday, Nov 27, 2026 | Closed |
"""


def _load():
    return holiday.load_schedule_alerts(FIXTURE.read_text(encoding="utf-8"))


def test_fixture_reads_every_row_including_black_friday():
    rows, alerts = _load()
    assert len(rows) == 18
    assert len(alerts) == 19
    black_friday = [alert for alert in alerts if alert.holiday == "Black Friday"]
    assert len(black_friday) == 1
    assert black_friday[0].event_date == date(2026, 11, 27)
    assert black_friday[0].status_kind == "closed"
    assert "Black Friday" in {holiday_name for holiday_name, _, _ in rows}


def test_multi_day_row_expands_both_closed_dates():
    alerts = holiday.expand_status_row(
        "New Year's Day 2026",
        "Thursday, Jan 1, 2026 and Friday, Jan 2, 2026",
        "Closed",
    )
    assert [(alert.event_date, alert.status_kind) for alert in alerts] == [
        (date(2026, 1, 1), "closed"),
        (date(2026, 1, 2), "closed"),
    ]


def test_memorial_day_splits_closed_and_early_close():
    alerts = holiday.expand_status_row(
        "Memorial Day",
        "Monday, May 25, 2026",
        "Closed; close early Friday, May 22, 2026 at 3:00 PM",
    )
    assert [(alert.event_date, alert.status_kind, alert.close_time) for alert in alerts] == [
        (date(2026, 5, 25), "closed", None),
        (date(2026, 5, 22), "early_close", "3:00 PM"),
    ]


def test_observed_day_without_another_row_stays_a_closed_alert():
    document = """
    # 2026-2027 Holiday Schedule
    | Holiday / event | Date | Office status |
    | --- | --- | --- |
    | Independence Day | Saturday, Jul 4, 2026 | Observed Friday, Jul 3, 2026 |
    """
    _, alerts = holiday.load_schedule_alerts(document)
    assert [(alert.event_date, alert.status_kind, alert.source) for alert in alerts] == [
        (date(2026, 7, 3), "closed", "observed"),
    ]


def test_holiday_cron_next_run_is_a_weekday_morning():
    from datetime import datetime, timezone

    from robie_job_engine.chat_admin import next_cron_time

    # Sunday 2026-10-04 12:00 ET. Next fire is Monday 9:00 ET (13:00 UTC, EDT).
    nxt = next_cron_time(
        "0 9 * * 1-5",
        "America/New_York",
        now=datetime(2026, 10, 4, 16, 0, tzinfo=timezone.utc),
    )
    assert nxt.startswith("2026-10-05T13:00:00")


def test_july_3_observed_dedups_with_early_close():
    _, alerts = _load()
    july3 = [alert for alert in alerts if alert.event_date == date(2026, 7, 3)]
    assert len(july3) == 1
    assert july3[0].status_kind == "early_close"
    assert july3[0].close_time == "1:00 PM"
    assert july3[0].holiday == "Independence Day early closing"
    assert all(alert.event_date != date(2026, 7, 4) for alert in alerts)


def test_markdown_table_parses_without_a_hardcoded_holiday_list():
    rows, alerts = holiday.load_schedule_alerts(MARKDOWN)
    assert len(rows) == 4
    names = {alert.holiday for alert in alerts}
    assert names == {
        "Columbus Day",
        "Memorial Day",
        "New Year's Day 2026",
        "Black Friday",
    }
    assert any(alert.event_date == date(2026, 5, 22) for alert in alerts)


def test_lead_time_window_skips_past_and_far_dates():
    _, alerts = _load()
    week_of = holiday.select_due(alerts, date(2026, 10, 5), lead_days=7)
    assert [alert.event_date for alert in week_of] == [date(2026, 10, 12)]

    too_early = holiday.select_due(alerts, date(2026, 10, 4), lead_days=7)
    assert too_early == []

    day_of = holiday.select_due(alerts, date(2026, 10, 12), lead_days=7)
    assert [alert.holiday for alert in day_of] == ["Columbus Day"]

    past = holiday.select_due(alerts, date(2026, 10, 13), lead_days=7)
    assert all(alert.event_date != date(2026, 10, 12) for alert in past)

    wider = holiday.select_due(alerts, date(2026, 10, 4), lead_days=10)
    assert any(alert.event_date == date(2026, 10, 12) for alert in wider)


def test_unreadable_table_fails_closed():
    with pytest.raises(holiday.HolidayScheduleError):
        holiday.load_schedule_alerts("<html><h1>Notes</h1><p>no table</p></html>")
    with pytest.raises(holiday.HolidayScheduleError):
        holiday.load_schedule_alerts(
            "<h1>2026-2027 Holiday Schedule</h1><table>"
            "<tr><th>Holiday / event</th><th>Date</th><th>Office status</th></tr>"
            "<tr><td>Mystery Day</td><td>Monday, Oct 12, 2026</td><td>Maybe open</td></tr>"
            "</table>"
        )
    with pytest.raises(holiday.HolidayScheduleError):
        holiday.expand_status_row("Columbus Day", "Monday, ", "Closed")
    with pytest.raises(holiday.HolidayScheduleError):
        holiday.expand_status_row("Bad weekday", "Monday, Oct 13, 2026", "Closed")


def test_copy_covers_closure_early_close_and_setup_steps():
    _, alerts = _load()
    closed = next(alert for alert in alerts if alert.holiday == "Columbus Day")
    early = next(alert for alert in alerts if alert.holiday == "Good Friday")
    body = holiday.email_body(closed)
    assert "CLOSED" in body
    assert "Gmail" in body
    assert "Google Calendar" in body
    assert "RingCentral" in body
    assert holiday.DOC_URL in body
    assert "Columbus Day" in holiday.email_subject(closed)
    early_body = holiday.email_body(early)
    assert "CLOSE EARLY" in early_body
    assert "12:00 PM" in early_body
    chat = holiday.chat_text(early)
    assert "⏰" in chat
    assert "12:00 PM" in chat
    assert holiday.DOC_URL in chat


def test_live_send_defaults_off(monkeypatch):
    for name in (holiday.SEND_ENV, holiday.EMAIL_ENV, holiday.CHAT_ENV):
        monkeypatch.delenv(name, raising=False)
    channels = holiday.resolve_channels({})
    assert channels["dry_run"] is True
    assert channels["email"] is False
    assert channels["chat"] is False
    still_dry = holiday.resolve_channels({"dry_run": False})
    assert still_dry["dry_run"] is True
    assert still_dry["email"] is False


def test_send_requires_master_and_channel_flags(monkeypatch):
    monkeypatch.setenv(holiday.SEND_ENV, "true")
    monkeypatch.delenv(holiday.EMAIL_ENV, raising=False)
    monkeypatch.delenv(holiday.CHAT_ENV, raising=False)
    blocked = holiday.resolve_channels({})
    assert blocked["dry_run"] is True
    assert "neither" in blocked["reason"]

    monkeypatch.setenv(holiday.EMAIL_ENV, "true")
    live = holiday.resolve_channels({})
    assert live["dry_run"] is False
    assert live["email"] is True
    assert live["chat"] is False

    forced = holiday.resolve_channels({"dry_run": True})
    assert forced["dry_run"] is True
    assert forced["email"] is False


def test_worker_dry_run_prints_plan_and_does_not_send(monkeypatch, capsys):
    monkeypatch.delenv(holiday.SEND_ENV, raising=False)
    monkeypatch.setenv(holiday.EMAIL_ENV, "true")
    sent = []
    monkeypatch.setattr(holiday.common, "send_gmail", lambda **kwargs: sent.append(kwargs) or "msg")
    monkeypatch.setattr(
        holiday.common, "post_chat_webhook", lambda text, **kwargs: sent.append(text) or True
    )
    result = holiday.HolidayAlertWorker().perform(
        {
            "payload": {
                "worker": "staff-holiday-alert",
                "schedule_html": FIXTURE.read_text(encoding="utf-8"),
                "today": "2026-10-05",
            }
        },
        idempotency_key="dry",
    )
    assert result.succeeded
    assert result.destination["dry_run"] is True
    assert result.destination["parsed_row_count"] == 18
    assert result.destination["planned"][0]["event_date"] == "2026-10-12"
    assert sent == []
    assert "HOLIDAY_ALERT_DRY_RUN" in capsys.readouterr().out
    verification = holiday.HolidayAlertVerifier().verify({}, {"destination": result.destination})
    assert verification.verified
    assert verification.evidence.method == "holiday_schedule_dry_run"


def test_worker_live_send_dedups_on_the_ledger(monkeypatch):
    monkeypatch.setenv(holiday.SEND_ENV, "true")
    monkeypatch.setenv(holiday.EMAIL_ENV, "true")
    monkeypatch.setenv(holiday.FIXTURE_ENV, "1")
    monkeypatch.delenv(holiday.CHAT_ENV, raising=False)
    calls = []

    def fake_send(**kwargs):
        calls.append(kwargs["subject"])
        return f"msg-{len(calls)}"

    monkeypatch.setattr(holiday.common, "send_gmail", fake_send)
    monkeypatch.setattr(holiday.common, "gmail_message_exists", lambda *args, **kwargs: True)
    tmp = durable_temporary_directory()
    try:
        payload = {
            "worker": "staff-holiday-alert",
            "schedule_html": FIXTURE.read_text(encoding="utf-8"),
            "today": "2026-10-05",
            "ledger_db": str(Path(tmp.name) / "jobs.db"),
        }
        first = holiday.HolidayAlertWorker().perform({"payload": payload}, idempotency_key="1")
        assert first.succeeded
        assert first.destination["dry_run"] is False
        assert first.destination["email_message_ids"] == ["msg-1"]
        assert "Columbus Day" in calls[0]
        verified = holiday.HolidayAlertVerifier().verify({}, {"destination": first.destination})
        assert verified.verified

        second = holiday.HolidayAlertWorker().perform({"payload": payload}, idempotency_key="2")
        assert second.succeeded
        assert second.destination["email_message_ids"] == []
        assert second.destination["skipped_already_sent"] == ["2026-10-12|closed"]
        assert calls == ["Office closed Monday, October 12 — Columbus Day"]
    finally:
        tmp.cleanup()


def test_pending_ledger_row_without_receipt_does_not_resend(monkeypatch):
    monkeypatch.setenv(holiday.SEND_ENV, "true")
    monkeypatch.setenv(holiday.EMAIL_ENV, "true")
    monkeypatch.setenv(holiday.FIXTURE_ENV, "1")
    calls = []
    monkeypatch.setattr(holiday.common, "send_gmail", lambda **kwargs: calls.append(kwargs) or "msg")
    tmp = durable_temporary_directory()
    try:
        db = str(Path(tmp.name) / "jobs.db")
        alert = holiday.HolidayAlert(
            "Columbus Day", date(2026, 10, 12), "closed", None, "explicit"
        )
        ledger = holiday.HolidaySendLedger(db)
        assert ledger.claim(alert) == "claimed"
        result = holiday.HolidayAlertWorker().perform(
            {
                "payload": {
                    "worker": "staff-holiday-alert",
                    "schedule_html": FIXTURE.read_text(encoding="utf-8"),
                    "today": "2026-10-05",
                    "ledger_db": db,
                }
            },
            idempotency_key="held",
        )
        assert result.succeeded is False
        assert result.retryable is False
        assert result.hold_status is None
        assert "pending" in (result.error or "")
        assert calls == []
        assert result.destination["held_pending"] == ["2026-10-12|closed"]
    finally:
        tmp.cleanup()


def test_parse_failure_is_not_a_silent_skip():
    result = holiday.HolidayAlertWorker().perform(
        {"payload": {"schedule_html": "<html><p>no holiday table</p></html>", "today": "2026-10-05"}},
        idempotency_key="bad",
    )
    assert result.succeeded is False
    assert result.retryable is False
    assert "failed closed" in (result.error or "")
    verification = holiday.HolidayAlertVerifier().verify(
        {},
        {"destination": {"parsed_row_count": 0, "parsed_event_count": 0, "dry_run": True}},
    )
    assert verification.verified is False
    assert verification.hold_status == JobStatus.FAILED
