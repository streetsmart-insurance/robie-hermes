"""Created Date is America/Chicago and is converted before the ET window."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from robie_job_engine.call_pickup import calling_day
from robie_job_engine.report_clock import (
    eastern_day,
    in_calling_window,
    report_created_et,
    within_default_call_age,
)

CHICAGO = ZoneInfo("America/Chicago")
NEW_YORK = ZoneInfo("America/New_York")


def test_nine_eleven_central_is_ten_eleven_eastern():
    converted = report_created_et("2026-10-05T09:11:00")
    assert converted is not None
    assert converted.tzname() == "EDT"
    assert (converted.hour, converted.minute) == (10, 11)
    assert converted.date().isoformat() == "2026-10-05"


def test_window_edges_use_the_converted_eastern_instant():
    # Monday 2026-10-05. 8:00 CT is 9:00 ET, inside. 7:59 CT is outside.
    assert in_calling_window(report_created_et("2026-10-05T08:00:00")) is True
    assert in_calling_window(report_created_et("2026-10-05T07:59:00")) is False
    # 5:00 PM CT is 6:00 PM ET, and the window end is exclusive.
    assert in_calling_window(report_created_et("2026-10-05T16:59:00")) is True
    assert in_calling_window(report_created_et("2026-10-05T17:00:00")) is False


def test_weekend_is_outside_even_when_the_clock_is_inside():
    # 2026-10-04 is Sunday. 10:00 CT is 11:00 ET.
    assert in_calling_window(report_created_et("2026-10-04T10:00:00")) is False


def test_dst_spring_and_fall_keep_the_one_hour_gap():
    spring = report_created_et("2026-03-09T08:00:00")
    assert spring is not None
    assert spring.tzname() == "EDT"
    assert (spring.hour, spring.minute) == (9, 0)
    assert in_calling_window(spring) is True

    fall = report_created_et("2026-11-02T08:00:00")
    assert fall is not None
    assert fall.tzname() == "EST"
    assert (fall.hour, fall.minute) == (9, 0)
    assert in_calling_window(fall) is True

    # The transition Sundays themselves are outside the weekday window,
    # and the wall-clock conversion is still one hour ahead.
    spring_sunday = report_created_et("2026-03-08T08:00:00")
    fall_sunday = report_created_et("2026-11-01T08:00:00")
    assert spring_sunday is not None and spring_sunday.tzname() == "EDT"
    assert fall_sunday is not None and fall_sunday.tzname() == "EST"
    assert (spring_sunday.hour, fall_sunday.hour) == (9, 9)
    assert in_calling_window(spring_sunday) is False
    assert in_calling_window(fall_sunday) is False


def test_late_central_evening_rolls_the_eastern_same_day_key():
    # Monday 11:30 PM CT is Tuesday 12:30 AM ET.
    converted = report_created_et("2026-10-05T23:30:00")
    assert converted is not None
    assert eastern_day(converted) == "2026-10-06"
    assert calling_day(converted) == "2026-10-06"
    assert converted.astimezone(CHICAGO).date().isoformat() == "2026-10-05"


def test_aware_eastern_timestamp_is_not_shifted_again():
    converted = report_created_et("2026-10-05T10:11:00-04:00")
    assert converted is not None
    assert (converted.hour, converted.minute) == (10, 11)
    assert converted.utcoffset() == NEW_YORK.utcoffset(datetime(2026, 10, 5, 10, 11))


def test_date_only_midnight_chicago_stays_on_the_same_eastern_date():
    converted = report_created_et("2026-10-05")
    assert converted is not None
    assert (converted.hour, converted.minute) == (1, 0)
    assert converted.date().isoformat() == "2026-10-05"


def test_weekend_and_holiday_tasks_count_from_the_previous_business_day():
    monday = datetime(2026, 10, 5, 10, 0, tzinfo=NEW_YORK)
    assert within_default_call_age(datetime(2026, 10, 2, 0, 0, tzinfo=NEW_YORK), monday)
    assert within_default_call_age(datetime(2026, 10, 3, 15, 0, tzinfo=NEW_YORK), monday)
    assert within_default_call_age(datetime(2026, 10, 4, 11, 0, tzinfo=NEW_YORK), monday)
    assert not within_default_call_age(
        datetime(2026, 10, 1, 23, 59, tzinfo=NEW_YORK), monday,
    )
    # Columbus Day 2026-10-12. Tuesday the 13th accepts the holiday and the weekend.
    tuesday = datetime(2026, 10, 13, 10, 0, tzinfo=NEW_YORK)
    assert within_default_call_age(datetime(2026, 10, 12, 11, 0, tzinfo=NEW_YORK), tuesday)
    assert within_default_call_age(datetime(2026, 10, 10, 9, 0, tzinfo=NEW_YORK), tuesday)
    assert within_default_call_age(datetime(2026, 10, 9, 0, 0, tzinfo=NEW_YORK), tuesday)
    assert not within_default_call_age(
        datetime(2026, 10, 8, 23, 59, tzinfo=NEW_YORK), tuesday,
    )


def test_empty_created_date_does_not_guess():
    assert report_created_et("") is None
    assert report_created_et("not-a-date") is None
