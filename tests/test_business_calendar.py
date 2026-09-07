from datetime import date, datetime, timezone

import pytest

from robie_job_engine.business_calendar import previous_business_day, us_federal_holidays
from robie_job_engine.accountability_jobs import _previous_business_day
from robie_job_engine.google_sheets_accountability import _resolved_range


@pytest.mark.parametrize('run_date, expected', [
    ('2026-09-08', '2026-09-04'),  # Labor Day Monday
    ('2026-07-06', '2026-07-02'),  # Saturday holiday observed Friday
    ('2027-07-06', '2027-07-02'),  # Sunday holiday observed Monday
    ('2022-01-03', '2021-12-30'),  # New Year observed in the preceding year
    ('2026-09-09', '2026-09-08'),  # Ordinary weekday
    ('2026-06-22', '2026-06-18'),  # Juneteenth
])
def test_federal_business_dates(run_date, expected):
    assert previous_business_day(date.fromisoformat(run_date), holiday_calendar='US-FEDERAL').isoformat() == expected


def test_2026_calendar_matches_opm_observed_dates():
    expected = ['01-01', '01-19', '02-16', '05-25', '06-19', '07-03', '09-07', '10-12', '11-11', '11-26', '12-25']
    assert us_federal_holidays(2026) == {date.fromisoformat('2026-' + item) for item in expected}


def test_missing_calendar_preserves_weekdays_and_unknown_calendar_is_rejected():
    assert previous_business_day(date(2026, 9, 8)) == date(2026, 9, 7)
    with pytest.raises(ValueError, match='Unsupported'):
        previous_business_day(date(2026, 9, 8), holiday_calendar='US-FEDRAL')


def test_job_and_weekly_sheet_use_same_holiday_scope():
    run_at = datetime(2026, 9, 8, 10, 25, tzinfo=timezone.utc)
    assert _previous_business_day(run_at, holiday_calendar='US-FEDERAL') == date(2026, 9, 4)
    assert _resolved_range('{previous_business_week}!A:R', as_of=run_at, holiday_calendar='US-FEDERAL') == '8/31-9/6!A:R'


def test_job_uses_eastern_date_instead_of_utc_day():
    run_at = datetime(2026, 9, 9, 1, tzinfo=timezone.utc)  # Still Tuesday in NJ
    assert _previous_business_day(run_at, holiday_calendar='US-FEDERAL') == date(2026, 9, 4)
