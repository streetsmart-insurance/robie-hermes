"""Unit tests for the Missed Call Report automation.

Hermetic: no network, no real RingCentral/EZLynx/Sheets. Fakes stand in for
the RingCentral client, the phone index file, and the sheet writer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest

from robie_job_engine.reports.missed_calls import date_rules
from robie_job_engine.reports.missed_calls.cli import _build_parser, main
from robie_job_engine.reports.missed_calls.dedupe import dedupe_by_day
from robie_job_engine.reports.missed_calls.models import MissedCall, RunSummary
from robie_job_engine.reports.missed_calls.normalize import display_phone, normalize_phone
from robie_job_engine.reports.missed_calls.phone_index import PhoneIndex
from robie_job_engine.reports.missed_calls.pipeline import run, run_for_date
from robie_job_engine.reports.missed_calls.ringcentral_pull import MISSED_RESULTS, pull_inbound_missed
from robie_job_engine.reports.missed_calls.rows import build_rows, department_for, hyperlink_formula, row_values
from robie_job_engine.reports.missed_calls.sheet_io import CANONICAL_SPREADSHEET_ID


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeCall:
    call_id: str = "c1"
    direction: str = "Inbound"
    result: str = "Missed"
    from_number: str = "+17324628343"
    to_number: str = "+17324620000"
    duration_seconds: int = 0
    start_time: Any = None
    extension: str = ""
    employee_name: str = ""

    def __post_init__(self):
        if self.start_time is None:
            self.start_time = datetime(2026, 10, 4, 14, 0, tzinfo=timezone.utc)


class FakeRCClient:
    def __init__(self, records):
        self.records = records

    def fetch_call_logs(self, date_from=None, date_to=None, view="Detailed"):
        return self.records


class FakeSheet:
    """Duck-typed SheetIO: records calls, writes nothing anywhere."""

    def __init__(self, existing_phones=None, dry_run=True):
        self.dry_run = dry_run
        self._existing = set(existing_phones or set())
        self.ensure_calls: list[str] = []
        self.appended: list[tuple[str, list]] = []

    def ensure_tab(self, tab, year_hint):
        self.ensure_calls.append(tab)
        return "exists"

    def existing_phones(self, tab):
        return set(self._existing)

    def append_rows(self, tab, rows):
        if self.dry_run:
            return 0
        self.appended.append((tab, [row_values(r) for r in rows]))
        return len(rows)


def _index_payload(built_at: str, phones: dict) -> dict:
    return {
        "built_at": built_at,
        "source_rows": 10,
        "unique_applicants": 5,
        "unique_phones": len(phones),
        "applicants": {},
        "phone_index": phones,
    }


def _write_index(tmp_path, payload) -> str:
    p = tmp_path / "phone_index.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return str(p)


def _missed_call(number="+17324628343", start=None, result="Missed") -> MissedCall:
    return MissedCall(
        call_id="c1",
        from_number=number,
        from_digits=normalize_phone(number),
        start_time=start or datetime(2026, 10, 4, 14, 0, tzinfo=timezone.utc),
        result=result,
    )


# ---------------------------------------------------------------------------
# normalize
# ---------------------------------------------------------------------------


def test_normalize_phone_e164():
    assert normalize_phone("+17324628343") == "7324628343"


def test_normalize_phone_pretty():
    assert normalize_phone("(732) 462-8343") == "7324628343"


def test_normalize_phone_junk_returns_none():
    assert normalize_phone("") is None
    assert normalize_phone(None) is None
    assert normalize_phone("123") is None
    assert normalize_phone("no-number") is None


def test_display_phone_formats_ten_digits():
    assert display_phone("7324628343") == "(732) 462-8343"


# ---------------------------------------------------------------------------
# date rules
# ---------------------------------------------------------------------------


def test_monday_rule_returns_fri_sat_sun():
    # 2026-10-05 is a Monday.
    assert date_rules.target_dates(date(2026, 10, 5)) == [
        date(2026, 10, 2),
        date(2026, 10, 3),
        date(2026, 10, 4),
    ]


def test_weekday_rule_returns_yesterday():
    assert date_rules.target_dates(date(2026, 10, 6)) == [date(2026, 10, 5)]  # Tuesday
    # Sunday run: last open business day was Friday -> Fri + Sat (M3 walk-back).
    assert date_rules.target_dates(date(2026, 10, 4)) == [
        date(2026, 10, 2),
        date(2026, 10, 3),
    ]


def test_m3_tuesday_after_monday_holiday_covers_fri_to_mon():
    # 2026-10-13 is the Tuesday after Columbus Day (2026-10-12, agency closed).
    # M3 LOCKED: every date since the last business day the agency was open.
    assert date_rules.target_dates(date(2026, 10, 13)) == [
        date(2026, 10, 9),
        date(2026, 10, 10),
        date(2026, 10, 11),
        date(2026, 10, 12),
    ]


def test_m3_extra_holidays_extend_the_gap():
    # Agency closed an extra day: Wednesday 2026-10-07. Thursday's run covers
    # Wed + Tue.
    assert date_rules.target_dates(
        date(2026, 10, 8), extra_holidays=frozenset({date(2026, 10, 7)})
    ) == [date(2026, 10, 6), date(2026, 10, 7)]


def test_m3_ignore_default_holidays():
    # With defaults cleared, Columbus Day counts as an open business day:
    # Tuesday's run covers Monday only.
    assert date_rules.target_dates(
        date(2026, 10, 13), use_default_holidays=False
    ) == [date(2026, 10, 12)]


def test_tab_name_md_format():
    assert date_rules.tab_name(date(2026, 10, 4)) == "10/4"
    assert date_rules.tab_name(date(2026, 8, 7)) == "8/7"


def test_day_bounds_are_et_midnights_in_utc():
    start, end = date_rules.day_bounds_utc(date(2026, 10, 4))
    # October = EDT (UTC-4): ET midnight == 04:00 UTC.
    assert start == datetime(2026, 10, 4, 4, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 10, 5, 4, 0, tzinfo=timezone.utc)


def test_parse_tab_name():
    assert date_rules.parse_tab_name("10/4", 2026) == date(2026, 10, 4)
    assert date_rules.parse_tab_name("Notes", 2026) is None
    assert date_rules.parse_tab_name("13/40", 2026) is None


# ---------------------------------------------------------------------------
# dedupe
# ---------------------------------------------------------------------------


def test_dedupe_keeps_one_row_per_number_per_day():
    a1 = _missed_call(start=datetime(2026, 10, 4, 14, 0, tzinfo=timezone.utc))
    a2 = _missed_call(
        number="+1 (732) 462-8343",  # same number, different format
        start=datetime(2026, 10, 4, 18, 0, tzinfo=timezone.utc),
        result="Voicemail",  # voicemail + hang-up still one row (M5)
    )
    b = _missed_call(number="+19085550100")
    unique, removed = dedupe_by_day([a2, b, a1])
    assert removed == 1
    by_digits = {c.from_digits: c for c in unique}
    assert set(by_digits) == {"7324628343", "9085550100"}
    assert by_digits["7324628343"].duplicate_count == 2
    # Earliest call is kept as the representative.
    assert by_digits["7324628343"].start_time == datetime(
        2026, 10, 4, 14, 0, tzinfo=timezone.utc
    )


def test_dedupe_empty():
    assert dedupe_by_day([]) == ([], 0)


# ---------------------------------------------------------------------------
# phone index
# ---------------------------------------------------------------------------


def test_lookup_matched(tmp_path):
    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    path = _write_index(
        tmp_path,
        _index_payload(
            "2026-10-05T10:00:00+00:00",
            {
                "7324628343": [
                    {
                        "applicant_id": "116349171",
                        "account_name": "Fonseca General Contractor LLC",
                        "applicant_type": "Active Client",
                        "matched_via": ["Phone - Cell"],
                    }
                ]
            },
        ),
    )
    idx = PhoneIndex(path, max_age_days=7, now=now)
    assert idx.available
    hit = idx.lookup("+17324628343")
    assert hit.state == "matched"
    assert hit.applicant_id == "116349171"
    assert hit.account_name == "Fonseca General Contractor LLC"


def test_lookup_ambiguous_fails_closed(tmp_path):
    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    path = _write_index(
        tmp_path,
        _index_payload(
            "2026-10-05T10:00:00+00:00",
            {
                "7324628343": [
                    {"applicant_id": "1", "account_name": "A Co"},
                    {"applicant_id": "2", "account_name": "B Co"},
                ]
            },
        ),
    )
    idx = PhoneIndex(path, max_age_days=7, now=now)
    hit = idx.lookup("7324628343")
    assert hit.state == "ambiguous"
    assert len(hit.candidates) == 2


def test_lookup_verified_no_account_only_when_fresh(tmp_path):
    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    path = _write_index(tmp_path, _index_payload("2026-10-05T10:00:00+00:00", {}))
    idx = PhoneIndex(path, max_age_days=7, now=now)
    hit = idx.lookup("7324628343")
    assert hit.state == "verified_no_account"


def test_lookup_stale_index_never_says_no_account(tmp_path):
    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    path = _write_index(
        tmp_path, _index_payload("2026-09-25T14:33:59+00:00", {})
    )
    idx = PhoneIndex(path, max_age_days=7, now=now)
    assert idx.stale
    assert not idx.available
    hit = idx.lookup("7324628343")
    assert hit.state == "unavailable"
    assert "stale" in hit.reason


def test_lookup_missing_file_is_unavailable(tmp_path):
    idx = PhoneIndex(tmp_path / "does-not-exist.json")
    assert not idx.available
    hit = idx.lookup("7324628343")
    assert hit.state == "unavailable"
    assert hit.state != "verified_no_account"


# ---------------------------------------------------------------------------
# ringcentral pull
# ---------------------------------------------------------------------------


def test_pull_keeps_inbound_missed_and_voicemail_only():
    records = [
        FakeCall(result="Missed"),
        FakeCall(result="Voicemail", from_number="+19085550100"),
        FakeCall(result="Call connected", from_number="+19085550101"),  # answered
        FakeCall(result="Missed", direction="Outbound", from_number="+19085550102"),
        FakeCall(result="Busy", from_number="+19085550103"),  # M2: excluded
        FakeCall(result="Missed", from_number=""),  # no usable number: skipped
    ]
    missed, inbound_seen = pull_inbound_missed(
        FakeRCClient(records),
        datetime(2026, 10, 4, 4, 0, tzinfo=timezone.utc),
        datetime(2026, 10, 5, 4, 0, tzinfo=timezone.utc),
    )
    assert inbound_seen == 5  # all inbound records, even the numberless one
    assert {c.from_digits for c in missed} == {"7324628343", "9085550100"}


def test_missed_result_set_is_missed_and_voicemail():
    assert MISSED_RESULTS == {"missed", "voicemail"}


def test_pull_failure_is_fail_closed():
    class Boom:
        def fetch_call_logs(self, **kwargs):
            raise RuntimeError("auth exploded")

    summary = run_for_date(
        Boom(), PhoneIndex("/nonexistent"), FakeSheet(), date(2026, 10, 4)
    )
    assert summary.fail_closed
    assert summary.errors


# ---------------------------------------------------------------------------
# rows
# ---------------------------------------------------------------------------


def test_build_rows_matched_profile_is_hyperlinked(tmp_path):
    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    path = _write_index(
        tmp_path,
        _index_payload(
            "2026-10-05T10:00:00+00:00",
            {
                "7324628343": [
                    {
                        "applicant_id": "116349171",
                        "account_name": "Fonseca General Contractor LLC",
                        "applicant_type": "Active Client",
                        "matched_via": ["Phone - Cell"],
                    }
                ]
            },
        ),
    )
    idx = PhoneIndex(path, max_age_days=7, now=now)
    calls = [_missed_call()]
    rows = build_rows(calls, {c.from_digits: idx.lookup(c.from_digits) for c in calls})
    assert len(rows) == 1
    row = rows[0]
    assert row.profile_text == "Fonseca General Contractor LLC"
    assert row.profile_url == (
        "https://app.ezlynx.com/web/account/116349171/overview"
    )
    values = row_values(row)
    assert len(values) == 5
    assert values[2] == hyperlink_formula(row.profile_url, row.profile_text)
    assert values[2].startswith('=HYPERLINK(')
    # HUMAN-ONLY columns are always blank from automation.
    assert values[3] == ""
    assert values[4] == ""
    assert row.addressed == "" and row.updated_by == ""


def test_build_rows_no_account_is_plain_text():
    from robie_job_engine.reports.missed_calls.models import PhoneLookup

    calls = [_missed_call()]
    rows = build_rows(
        calls, {"7324628343": PhoneLookup(state="verified_no_account")}
    )
    values = row_values(rows[0])
    assert values[2] == "No Account"
    assert not values[2].startswith("=HYPERLINK(")
    assert values[3] == ""  # "Was addressed?" stays blank for No Account too


def test_build_rows_ambiguous_is_human_review_plain_text():
    from robie_job_engine.reports.missed_calls.models import PhoneLookup

    calls = [_missed_call()]
    rows = build_rows(
        calls,
        {"7324628343": PhoneLookup(state="ambiguous", candidates=[{"a": 1}, {"a": 2}])},
    )
    values = row_values(rows[0])
    assert "AMBIGUOUS" in values[2]
    assert "human review" in values[2]
    assert not values[2].startswith("=HYPERLINK(")


def test_hyperlink_formula_escapes_quotes():
    f = hyperlink_formula("https://x.example/?a=1", 'Say "hi"')
    assert f == '=HYPERLINK("https://x.example/?a=1","Say ""hi""")'


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------


def _fresh_index(tmp_path, phones=None):
    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    path = _write_index(
        tmp_path, _index_payload("2026-10-05T10:00:00+00:00", phones or {})
    )
    return PhoneIndex(path, max_age_days=7, now=now)


def test_pipeline_dry_run_appends_nothing_but_counts(tmp_path):
    idx = _fresh_index(tmp_path)
    client = FakeRCClient(
        [
            FakeCall(result="Missed", from_number="+17324628343"),
            FakeCall(result="Missed", from_number="+17324628343"),  # duplicate
            FakeCall(result="Voicemail", from_number="+19085550100"),
        ]
    )
    sheet = FakeSheet(dry_run=True)
    summary = run_for_date(client, idx, sheet, date(2026, 10, 4))
    assert not summary.fail_closed
    assert summary.tab_name == "10/4"
    assert summary.inbound_calls_seen == 3
    assert summary.missed_calls_seen == 3
    assert summary.unique_numbers == 2
    assert summary.duplicates_removed == 1
    assert summary.rows_appended == 0  # dry-run writes nothing
    assert sheet.appended == []
    assert summary.lookup_states == {"verified_no_account": 2}


def test_pipeline_skips_phones_already_on_tab(tmp_path):
    idx = _fresh_index(tmp_path)
    client = FakeRCClient(
        [
            FakeCall(result="Missed", from_number="+17324628343"),
            FakeCall(result="Missed", from_number="+19085550100"),
        ]
    )
    # A human already added 7324628343 to the tab: never touch it again.
    sheet = FakeSheet(existing_phones={"7324628343"}, dry_run=False)
    summary = run_for_date(client, idx, sheet, date(2026, 10, 4))
    assert not summary.fail_closed
    assert summary.existing_rows_skipped == 1
    assert summary.rows_appended == 1
    tab, values = sheet.appended[0]
    assert tab == "10/4"
    assert len(values) == 1
    assert values[0][1] == "(908) 555-0100"


def test_pipeline_fails_closed_on_stale_index_before_any_sheet_call(tmp_path):
    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    path = _write_index(tmp_path, _index_payload("2026-09-25T14:33:59+00:00", {}))
    idx = PhoneIndex(path, max_age_days=7, now=now)
    client = FakeRCClient([FakeCall(result="Missed")])
    sheet = FakeSheet(dry_run=True)
    summary = run_for_date(client, idx, sheet, date(2026, 10, 4))
    assert summary.fail_closed
    assert summary.rows_appended == 0
    assert sheet.ensure_calls == []  # no sheet interaction at all
    assert any("phone index unavailable" in e for e in summary.errors)


class DateAwareFakeClient:
    """FakeRCClient that honors the requested UTC window (for run() tests)."""

    def __init__(self, records):
        self.records = records

    def fetch_call_logs(self, date_from=None, date_to=None, view="Detailed"):
        return [
            r
            for r in self.records
            if (date_from is None or r.start_time >= date_from)
            and (date_to is None or r.start_time < date_to)
        ]


def _call_on(number, ymd, hour=14):
    y, m, d = ymd
    return FakeCall(
        result="Missed",
        from_number=number,
        start_time=datetime(y, m, d, hour, 0, tzinfo=timezone.utc),
    )


def test_m5_number_appears_only_once_across_dates_earliest_wins(tmp_path):
    # M5 LOCKED (Sandeep): a number is present on the report only once.
    # Friday 10/2 has A and B; Saturday 10/3 has A again and C.
    idx = _fresh_index(tmp_path)
    records = [
        _call_on("+17324628343", (2026, 10, 2)),  # A on Fri
        _call_on("+19085550100", (2026, 10, 2)),  # B on Fri
        _call_on("+17324628343", (2026, 10, 3)),  # A again on Sat
        _call_on("+12125550100", (2026, 10, 3)),  # C on Sat
    ]
    client = DateAwareFakeClient(records)
    sheet = FakeSheet(dry_run=True)
    summaries = run(lambda: client, idx, sheet, [date(2026, 10, 2), date(2026, 10, 3)])
    fri, sat = summaries
    assert fri.unique_numbers == 2
    assert sat.unique_numbers == 1  # only C; A was already emitted Friday
    assert sat.cross_date_duplicates_removed == 1
    assert set(fri.emitted_numbers) == {"7324628343", "9085550100"}
    assert sat.emitted_numbers == ["2125550100"]


def test_m5_skip_numbers_param_filters_directly(tmp_path):
    idx = _fresh_index(tmp_path)
    client = FakeRCClient([FakeCall(result="Missed", from_number="+17324628343")])
    sheet = FakeSheet(dry_run=True)
    summary = run_for_date(
        client, idx, sheet, date(2026, 10, 3), skip_numbers=frozenset({"7324628343"})
    )
    assert summary.unique_numbers == 0
    assert summary.cross_date_duplicates_removed == 1
    assert summary.emitted_numbers == []


def test_m4_department_blank_when_unmapped():
    # M4 LOCKED (Sandeep): unmappable department cell stays blank.
    assert department_for(_missed_call()) == ""
    ext_only = _missed_call()
    ext_only.extension = "9005"
    assert department_for(ext_only) == ""


def test_m4_department_keeps_resolved_employee_name():
    call = _missed_call()
    call.employee_name = "Daniela Aguilar"
    assert department_for(call) == "Daniela Aguilar"


def test_m4_unmapped_extensions_surfaced_in_summary(tmp_path):
    # M4 follow-up (Sandeep 2026-10-05): unmappable departments stay blank
    # and Sandeep gets an email in that case. The pipeline does not send
    # the email; it surfaces the unmapped extensions in the run output.
    idx = _fresh_index(tmp_path)
    unmapped = FakeCall(
        result="Missed", from_number="+17324628343", extension="9005",
        employee_name="",
    )
    mapped = FakeCall(
        result="Missed", from_number="+19085550100", extension="101",
        employee_name="Daniela Aguilar",
    )
    client = FakeRCClient([unmapped, mapped])
    sheet = FakeSheet(dry_run=True)
    summary = run_for_date(client, idx, sheet, date(2026, 10, 4))
    assert summary.unmapped_departments == ["9005"]
    assert any("Sandeep" in n for n in summary.notes)


def test_m5_prior_run_tabs_do_not_suppress_new_run(tmp_path):
    # M5 confirmed 2026-10-05 ("yes"): the "only once" rule is per RUN. A
    # number on a prior run's tab does not suppress a new row in a later run.
    idx = _fresh_index(tmp_path)
    records = [_call_on("+17324628343", (2026, 10, 2))]
    client = DateAwareFakeClient(records)
    sheet = FakeSheet(dry_run=True)
    first = run(lambda: client, idx, sheet, [date(2026, 10, 2)])
    second = run(lambda: client, idx, sheet, [date(2026, 10, 2)])
    assert first[0].unique_numbers == 1
    assert second[0].unique_numbers == 1
    assert second[0].emitted_numbers == ["7324628343"]


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------


def test_cli_defaults_to_dry_run():
    args = _build_parser().parse_args([])
    assert args.dry_run is True


def test_cli_no_dry_run_flag():
    args = _build_parser().parse_args(["--no-dry-run"])
    assert args.dry_run is False


def test_cli_dates_override():
    args = _build_parser().parse_args(["--dates", "2026-10-02,2026-10-04"])
    assert args.dates == "2026-10-02,2026-10-04"


def test_cli_bad_dates_exit_1(capsys):
    assert main(["--dates", "not-a-date"]) == 1


def test_x1_canonical_spreadsheet_id_is_default():
    # X1 LOCKED (Sandeep 2026-10-05): the canonical workbook id is the
    # default --spreadsheet-id; --dry-run stays the default mode.
    assert CANONICAL_SPREADSHEET_ID == "1POQ9oAop1AOa6gWsaNVQX3I540WvvX0gmw9XNPK1L5Y"
    args = _build_parser().parse_args([])
    assert args.spreadsheet_id == "1POQ9oAop1AOa6gWsaNVQX3I540WvvX0gmw9XNPK1L5Y"
    assert args.dry_run is True
