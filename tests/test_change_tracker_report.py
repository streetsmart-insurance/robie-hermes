"""Unit tests for robie_job_engine.reports.change_tracker. Fakes only — no live browser."""

from __future__ import annotations

import csv
import io
import sys
import tempfile
import unittest.mock
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _sibling_fakes import ensure_real_module

ensure_real_module("robie_job_engine.report_registry")
ensure_real_module("robie_job_engine.report_fetcher")
ensure_real_module("robie_job_engine.ezlynx_session")
ensure_real_module("robie_job_engine.store")
ensure_real_module("robie_job_engine.reports.change_tracker.config")
ensure_real_module("robie_job_engine.reports.change_tracker.transform")
ensure_real_module("robie_job_engine.reports.change_tracker.sheets")
ensure_real_module("robie_job_engine.reports.change_tracker.run")

from robie_job_engine import report_fetcher
from robie_job_engine.report_registry import (
    LOOK_ID_BY_REPORT,
    ReportRegistryError,
    get_report_spec,
)
from robie_job_engine.reports.change_tracker import config as cfg
from robie_job_engine.reports.change_tracker import run as run_mod
from robie_job_engine.reports.change_tracker import sheets as sheets_mod
from robie_job_engine.reports.change_tracker import transform as tf

EXPECTED_COLS = list(cfg.EXPECTED_4659_COLUMNS)


def _csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=EXPECTED_COLS)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


def _row(**over):
    base = {
        "Applicant ID": "12345",
        "Account Name": "Acme LLC",
        "Assigned Producer": "Jazmin Molina",
        "Branch": "Main",
        "Policy Effective Date": "01/01/2026",
        "Expiration Date": "01/01/2027",
        "Line of Business": "Commercial Auto",
        "Master Company": "Progressive",
        "Policy Number": "203663134700",
        "Written Premium": "1234.56",
        "Change Request Created By": "Erika Palacios",
        "Change Request Created Date": "08/01/2026",
        "Request Status": "Task Created",  # P1 LOCKED: an open status
        "Policy Change Request ID": "CR-9001",
        "Days Open": "65",
    }
    base.update(over)
    return base


def _matrix_fixture():
    rows = [
        _row(**{"Policy Change Request ID": "CR-1", "Assigned Producer": "Daniela Aguilar",
                "Account Name": "Alpha", "Days Open": "10", "Policy Number": "111"}),
        _row(**{"Policy Change Request ID": "CR-2", "Assigned Producer": "Jazmin Molina",
                "Account Name": "Beta", "Days Open": "65", "Policy Number": "222"}),
        _row(**{"Policy Change Request ID": "CR-3", "Assigned Producer": "Taylor Cimei",
                "Account Name": "Gamma", "Days Open": "5", "Policy Number": "333"}),
        _row(**{"Policy Change Request ID": "CR-4", "Assigned Producer": "Mike Sosa",
                "Account Name": "Delta", "Days Open": "31", "Policy Number": "444"}),
        _row(**{"Policy Change Request ID": "CR-5", "Assigned Producer": "Jake Ferrara",
                "Account Name": "Epsilon", "Days Open": "2", "Policy Number": "555"}),
    ]
    return _csv(rows)


# --- schema fingerprint (P11) -------------------------------------------------


def test_schema_fingerprint_exact_passes():
    rows = tf.parse_export_rows(_csv([_row()]))
    assert len(rows) == 1
    assert rows[0]["Account Name"] == "Acme LLC"


def test_schema_fingerprint_reorder_fails():
    cols = list(EXPECTED_COLS)
    cols[1], cols[2] = cols[2], cols[1]
    with pytest.raises(tf.SchemaMismatchError) as exc:
        tf.assert_schema_fingerprint(cols)
    assert "order expected" in str(exc.value)


def test_schema_fingerprint_rename_fails():
    cols = [c if c != "Days Open" else "Days Since Open" for c in EXPECTED_COLS]
    with pytest.raises(tf.SchemaMismatchError) as exc:
        tf.assert_schema_fingerprint(cols)
    assert "missing columns=['Days Open']" in str(exc.value)


def test_schema_fingerprint_extra_column_fails():
    with pytest.raises(tf.SchemaMismatchError) as exc:
        tf.assert_schema_fingerprint(EXPECTED_COLS + ["New Column"])
    assert "unexpected columns=['New Column']" in str(exc.value)


# --- matrix build --------------------------------------------------------------


def test_build_matrix_structure_and_sort():
    matrix, stats = tf.build_week_matrix(_csv_text(), total_requests=7, total_open=5)
    assert matrix[0] == list(cfg.DEST_HEADERS)
    # Team header rows in team order: Personal, Commercial, Trucking.
    b_col = [r[1] for r in matrix[1:]]
    header_rows = [b for b in b_col if b.startswith(("Personal", "Commercial", "Trucking"))]
    assert header_rows == [
        "Personal - Jazmin, Daniela",
        "Commercial - Taylor, Zeus, Sandy, Carlo, Andrea",
        "Trucking - Angie, Ricardo, Jake, Mike, Maria",
    ]
    # Personal rows sorted by Days Open desc: Beta(65) then Alpha(10).
    personal_accounts = [r[3] for r in matrix[1:] if r[4] in ("Jazmin Molina", "Daniela Aguilar")]
    assert personal_accounts == ["Beta", "Alpha"]
    trucking_accounts = [r[3] for r in matrix[1:] if r[4] in ("Mike Sosa", "Jake Ferrara")]
    assert trucking_accounts == ["Delta", "Epsilon"]  # 31 then 2
    assert stats.teams == {"Personal": 2, "Commercial": 1, "Trucking": 2}
    assert stats.stale_rows == 2  # Beta 65, Delta 31
    # Applicant ID is dropped; Action is blank; P/Q carry the tiles.
    first_data = matrix[2]
    assert first_data[0] == ""
    assert first_data[15] == "7" and first_data[16] == "5"
    # Effective Date mapped from Policy Effective Date (column G).
    assert first_data[6] == "01/01/2026"


def test_policy_number_kept_as_string_not_float():
    long_policy = "203663134700"
    text = _csv([_row(**{"Policy Number": long_policy})])
    matrix, _ = tf.build_week_matrix(text, total_requests=1, total_open=1)
    policy_cell = matrix[2][9]  # column J
    assert policy_cell == long_policy
    assert isinstance(policy_cell, str)
    assert "E+" not in policy_cell and policy_cell != "203663000000"


def test_unmapped_producer_fails_closed():
    text = _csv([_row(**{"Assigned Producer": "Nobody New", "Policy Change Request ID": "CR-9"})])
    with pytest.raises(tf.UnmappedProducerError) as exc:
        tf.build_week_matrix(text, total_requests=1, total_open=1)
    assert "Nobody New" in str(exc.value)


def test_count_mismatch_fails_closed():
    with pytest.raises(tf.CountMismatchError) as exc:
        tf.build_week_matrix(_matrix_fixture(), total_requests=5, total_open=4)
    assert "tile=4" in str(exc.value)


def test_identical_duplicate_request_id_deduped():
    rows = [_row(**{"Policy Change Request ID": "CR-D"}), _row(**{"Policy Change Request ID": "CR-D"})]
    matrix, stats = tf.build_week_matrix(_csv(rows), total_requests=1, total_open=1)
    assert stats.duplicates_dropped == 1
    assert stats.rows_after_dedupe == 1


def test_conflicting_duplicate_request_id_fails():
    rows = [
        _row(**{"Policy Change Request ID": "CR-D", "Account Name": "One"}),
        _row(**{"Policy Change Request ID": "CR-D", "Account Name": "Two"}),
    ]
    with pytest.raises(tf.DuplicateRequestError):
        tf.build_week_matrix(_csv(rows), total_requests=1, total_open=1)


def test_row_without_request_id_fails():
    text = _csv([_row(**{"Policy Change Request ID": ""})])
    with pytest.raises(tf.ChangeTrackerError, match="missing Policy Change Request ID"):
        tf.build_week_matrix(text, total_requests=1, total_open=1)


# --- P1 open/closed statuses (LOCKED 2026-10-05) --------------------------------


def test_done_rows_filtered_but_counted_toward_tile():
    rows = [
        _row(**{"Policy Change Request ID": "CR-1", "Request Status": "Task Created"}),
        _row(**{"Policy Change Request ID": "CR-2", "Request Status": "ongoing"}),
        _row(**{"Policy Change Request ID": "CR-3", "Request Status": "Done"}),
    ]
    # The tile counts ALL rows in the export, including Done rows, so the
    # check runs BEFORE the not-open filter.
    matrix, stats = tf.build_week_matrix(_csv(rows), total_requests=3, total_open=3)
    assert stats.rows_raw == 3
    assert stats.dropped_not_open == 1
    assert stats.rows_open == 2
    # The Done row is not in the matrix.
    ids = [r[13] for r in matrix[1:] if len(r) > 13 and r[13]]
    assert "CR-3" not in ids
    assert set(ids) == {"CR-1", "CR-2"}


def test_unknown_status_fails_closed_listing_status():
    rows = [_row(**{"Policy Change Request ID": "CR-X", "Request Status": "Mystery"})]
    with pytest.raises(tf.ChangeTrackerError) as exc:
        tf.build_week_matrix(_csv(rows), total_requests=1, total_open=1)
    assert "Mystery" in str(exc.value)
    assert "fail closed" in str(exc.value)


# --- P6 roster fail-closed (verified: ALL unmapped names are listed) -------------


def test_all_unmapped_producers_listed():
    rows = [
        _row(**{"Policy Change Request ID": "CR-A", "Assigned Producer": "Nobody New"}),
        _row(**{"Policy Change Request ID": "CR-B", "Assigned Producer": "Someone Else"}),
    ]
    with pytest.raises(tf.UnmappedProducerError) as exc:
        tf.build_week_matrix(_csv(rows), total_requests=2, total_open=2)
    assert "Nobody New" in str(exc.value)
    assert "Someone Else" in str(exc.value)


# --- P10 retry on tile mismatch (live path only) ---------------------------------


def _live_args(tmp_path):
    return ["--live", "--db-path", str(tmp_path / "j.db"), "--dry-run"]


def _two_row_open_csv():
    return _csv([
        _row(**{"Policy Change Request ID": "CR-101", "Assigned Producer": "Jazmin Molina"}),
        _row(**{"Policy Change Request ID": "CR-102", "Assigned Producer": "Daniela Aguilar"}),
    ])


def test_retry_on_count_mismatch_succeeds_on_second_attempt(tmp_path, capsys, monkeypatch):
    csv_text = _two_row_open_csv()
    responses = [
        (csv_text, "2", "3"),  # tile says 3, download holds 2 -> mismatch
        (csv_text, "2", "2"),  # second download matches
    ]
    calls = []

    def fake_fetch(*, db_path):
        calls.append(db_path)
        return responses.pop(0)

    monkeypatch.setattr(run_mod, "fetch_4659_export", fake_fetch)
    stats_path = tmp_path / "stats.json"
    rc = run_mod.main(
        _live_args(tmp_path) + ["--stats-json", str(stats_path)]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert len(calls) == 2
    assert "count cross-check passed on attempt 2" in out
    import json

    stats = json.loads(stats_path.read_text())
    assert stats["download_attempts"] == 2
    assert stats["rows_open"] == 2


def test_retry_gives_up_after_three_attempts(tmp_path, capsys, monkeypatch):
    csv_text = _two_row_open_csv()
    calls = []

    def fake_fetch(*, db_path):
        calls.append(db_path)
        return (csv_text, "2", "9")  # always mismatches

    monkeypatch.setattr(run_mod, "fetch_4659_export", fake_fetch)
    rc = run_mod.main(_live_args(tmp_path))
    err = capsys.readouterr().err
    assert rc == 2  # fail closed (exit 2) after 3 attempts
    assert len(calls) == 3
    assert "FAIL_CLOSED" in err
    assert "attempt 3/3" in err


def _csv_text():
    return _matrix_fixture()


# --- week tab naming (P12) ------------------------------------------------------


def test_week_tab_name_monday():
    # Mon 2026-10-05 -> the week just closed is 9/28-10/4.
    assert tf.week_tab_name(date(2026, 10, 5)) == "9/28-10/4"


def test_week_tab_name_wednesday():
    # Wed 2026-10-07 run still covers the most recent completed week.
    assert tf.week_tab_name(date(2026, 10, 7)) == "9/28-10/4"


def test_week_tab_name_sunday():
    # P12 LOCKED: a Sunday run labels the week ending TODAY.
    # Sun 2026-10-04 -> "9/28-10/4".
    assert tf.week_tab_name(date(2026, 10, 4)) == "9/28-10/4"


def test_week_tab_name_saturday():
    # Sat 2026-10-03 -> the week just closed is 9/21-9/27.
    assert tf.week_tab_name(date(2026, 10, 3)) == "9/21-9/27"


def test_week_tab_name_next_sunday():
    # Sun 2026-10-11 -> "10/5-10/11".
    assert tf.week_tab_name(date(2026, 10, 11)) == "10/5-10/11"


# --- registry wiring (finding 1) -------------------------------------------------


def test_registry_4659_wired():
    assert LOOK_ID_BY_REPORT["4659"] == "4659"
    spec = get_report_spec("4659")
    assert spec.look_id == "4659"
    assert spec.identity_fields == ("policy_change_request_id",)
    assert spec.filter_name == cfg.LOOKER_4659_TITLE
    assert spec.schema_verified is True


def test_fetch_report_rows_4659_points_at_dedicated_driver():
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(ReportRegistryError, match="fetch_4659_export"):
            report_fetcher.fetch_report_rows(report_id="4659", db_path=str(Path(tmp) / "j.db"))


# --- sheets layer ---------------------------------------------------------------


class _FakeSheetsService:
    """Minimal fake capturing the calls the sheets layer makes."""

    def __init__(self, existing_tabs=None, tab_values=None):
        self.existing_tabs = existing_tabs or {}  # title -> sheetId
        self.tab_values = tab_values or {}  # title -> list of row lists
        self.calls = []

    # -- service.spreadsheets() chain --
    def spreadsheets(self):
        return self

    def get(self, spreadsheetId, fields=None, range=None):
        self.calls.append(("get", spreadsheetId, fields, range))
        outer = self

        class _Exec:
            def execute(inner):
                if range:
                    tab = range.split("!")[0].strip("'")
                    return {"values": outer.tab_values.get(tab, [])}
                return {
                    "sheets": [
                        {"properties": {"title": t, "sheetId": sid}}
                        for t, sid in outer.existing_tabs.items()
                    ]
                }

        return _Exec()

    def batchUpdate(self, spreadsheetId, body):
        self.calls.append(("batchUpdate", spreadsheetId, body))
        outer = self

        class _Exec:
            def execute(inner):
                for req in body.get("requests", []):
                    if "addSheet" in req:
                        title = req["addSheet"]["properties"]["title"]
                        outer.existing_tabs[title] = 424242
                if any("addSheet" in r for r in body.get("requests", [])):
                    return {"replies": [{"addSheet": {"properties": {"sheetId": 424242}}}]}
                return {}

        return _Exec()

    def values(self):
        return self

    def clear(self, spreadsheetId, range, body):
        self.calls.append(("clear", spreadsheetId, range))

        class _Exec:
            def execute(inner):
                return {}

        return _Exec()

    def update(self, spreadsheetId, range, valueInputOption, body):
        self.calls.append(("update", spreadsheetId, range, valueInputOption, len(body["values"])))
        outer = self

        class _Exec:
            def execute(inner):
                tab = range.split("!")[0].strip("'")
                outer.tab_values[tab] = body["values"]
                cells = sum(len(r) for r in body["values"])
                return {"updatedCells": cells, "updatedRange": f"'{tab}'!A1"}

        return _Exec()


def _sample_matrix():
    matrix, _ = tf.build_week_matrix(_matrix_fixture(), total_requests=7, total_open=5)
    return matrix


def test_format_requests_shapes():
    matrix = _sample_matrix()
    requests = sheets_mod.build_format_requests(matrix, sheet_id=99)
    kinds = [next(iter(r)) for r in requests]
    # 1 plain-text number format (J only, P9 LOCKED) + 3 team header rows + 1 rule.
    assert kinds.count("repeatCell") == 4
    assert kinds.count("addConditionalFormatRule") == 1
    text_rules = [
        r["repeatCell"] for r in requests if "repeatCell" in r
        and r["repeatCell"]["cell"].get("userEnteredFormat", {}).get("numberFormat")
    ]
    assert len(text_rules) == 1
    for rule in text_rules:
        assert rule["cell"]["userEnteredFormat"]["numberFormat"] == {"type": "TEXT"}
    # The TEXT range is column J (9) only.
    cols = sorted(
        r["range"]["startColumnIndex"] for r in text_rules
    )
    assert cols == [9]
    cond = next(r["addConditionalFormatRule"] for r in requests if "addConditionalFormatRule" in r)
    assert cond["rule"]["booleanRule"]["condition"]["values"] == [
        {"userEnteredValue": "=$C2>30"}
    ]
    red = cond["rule"]["booleanRule"]["format"]["backgroundColor"]
    assert abs(red["red"] - 0xF4 / 255) < 0.01  # light red 3 #f4cccc
    assert abs(red["green"] - 0xCC / 255) < 0.01
    assert abs(red["blue"] - 0xCC / 255) < 0.01
    # Range covers A2:Q<last>.
    rng = cond["rule"]["ranges"][0]
    assert rng["sheetId"] == 99
    assert (rng["startRowIndex"], rng["endRowIndex"]) == (1, len(matrix))
    assert (rng["startColumnIndex"], rng["endColumnIndex"]) == (0, 17)
    # Team header rows are bold + light blue.
    bold_rules = [
        r["repeatCell"] for r in requests if "repeatCell" in r
        and r["repeatCell"]["cell"].get("userEnteredFormat", {}).get("textFormat", {}).get("bold")
    ]
    assert len(bold_rules) == 3
    blue = bold_rules[0]["cell"]["userEnteredFormat"]["backgroundColor"]
    assert abs(blue["red"] - 0xCF / 255) < 0.01  # #cfe2f3


def test_write_week_tab_dry_run_touches_nothing():
    service = unittest.mock.MagicMock()
    result = sheets_mod.write_week_tab(
        service, "<id>", "9/28-10/4", _sample_matrix(), dry_run=True
    )
    assert result["wrote"] is False
    assert result["dry_run"] is True
    assert service.method_calls == []  # no service calls at all
    assert result["format_requests_planned"] > 0


def test_write_week_tab_refuses_nonempty_tab_without_force():
    service = _FakeSheetsService(
        existing_tabs={"9/28-10/4": 7},
        tab_values={"9/28-10/4": [["h"], ["old data"]]},
    )
    with pytest.raises(tf.RefuseOverwriteError):
        sheets_mod.write_week_tab(
            service, "<id>", "9/28-10/4", _sample_matrix(), dry_run=False
        )
    assert all(c[0] != "update" for c in service.calls)


def test_write_week_tab_live_with_force():
    service = _FakeSheetsService(
        existing_tabs={"9/28-10/4": 7},
        tab_values={"9/28-10/4": [["h"], ["old data"]]},
    )
    matrix = _sample_matrix()
    result = sheets_mod.write_week_tab(
        service, "<id>", "9/28-10/4", matrix, dry_run=False, force=True
    )
    assert result["wrote"] is True
    assert result["readback_rows"] == len(matrix)
    assert result["formats_applied"] == result["format_requests_planned"]
    kinds = [c[0] for c in service.calls]
    assert "clear" in kinds and "update" in kinds and "batchUpdate" in kinds


def test_write_week_tab_creates_missing_tab():
    service = _FakeSheetsService(existing_tabs={})
    result = sheets_mod.write_week_tab(
        service, "<id>", "9/28-10/4", _sample_matrix(), dry_run=False
    )
    assert result["wrote"] is True
    assert "9/28-10/4" in service.existing_tabs


def test_resolve_spreadsheet_id_fails_closed_when_unset():
    config = cfg.ChangeTrackerConfig(spreadsheet_id="PENDING_SANDEEP_X1_UNSET")
    with pytest.raises(sheets_mod.MissingSpreadsheetError):
        sheets_mod.resolve_spreadsheet_id(config)


def test_config_spreadsheet_id_default_is_x1_canonical():
    # X1 LOCKED (Sandeep 2026-10-06): "New Change Request Tracker 2" id.
    assert cfg.CHANGE_TRACKER_SPREADSHEET_ID == "1-_Egm_2K16aAQv8u_MqKzWJGFTEr4KA8reh_oFm3rg4"


def test_resolve_spreadsheet_id_accepts_locked_x1_default():
    # X1 locked: the default no longer raises; env override still wins.
    assert sheets_mod.resolve_spreadsheet_id(cfg.DEFAULT_CONFIG) == \
        "1-_Egm_2K16aAQv8u_MqKzWJGFTEr4KA8reh_oFm3rg4"


def _prev_tab_service(headers):
    return _FakeSheetsService(
        existing_tabs={"9/21-9/27": 11},
        tab_values={"9/21-9/27": [headers, ["old", "data"]]},
    )


def test_write_week_tab_maps_headers_from_previous_tab():
    # P13 LOCKED (Sandeep 2026-10-06): "Map the headers as per the previous
    # week report." The new tab's row 0 comes from the previous week's tab.
    service = _prev_tab_service(list(cfg.DEST_HEADERS))
    matrix = _sample_matrix()
    result = sheets_mod.write_week_tab(
        service, "<id>", "9/28-10/4", matrix, dry_run=False,
        previous_tab="9/21-9/27",
    )
    assert result["wrote"] is True
    assert result["headers_from_previous_tab"] == "9/21-9/27"
    # The previous tab's header row was actually read (a values.get call).
    get_ranges = [c[3] for c in service.calls if c[0] == "get" and c[3]]
    assert any("9/21-9/27" in r for r in get_ranges)
    # And it became the written tab's row 0.
    assert service.tab_values["9/28-10/4"][0] == list(cfg.DEST_HEADERS)


def test_write_week_tab_fails_when_previous_headers_differ():
    # A mismatched previous-week header row would mislabel every data
    # column (writes are positional) — fail closed, ship nothing.
    service = _prev_tab_service(["Bogus"] * len(cfg.DEST_HEADERS))
    with pytest.raises(tf.HeaderMappingError):
        sheets_mod.write_week_tab(
            service, "<id>", "9/28-10/4", _sample_matrix(), dry_run=False,
            previous_tab="9/21-9/27",
        )
    assert all(c[0] != "update" for c in service.calls)


def test_write_week_tab_fails_when_previous_tab_missing():
    # No previous tab to map headers from — fail closed, not guessed.
    service = _FakeSheetsService(existing_tabs={})
    with pytest.raises(tf.HeaderMappingError):
        sheets_mod.write_week_tab(
            service, "<id>", "9/28-10/4", _sample_matrix(), dry_run=False,
            previous_tab="9/21-9/27",
        )
    assert all(c[0] != "update" for c in service.calls)


def test_write_week_tab_dry_run_reports_previous_tab_without_touching_service():
    service = unittest.mock.MagicMock()
    result = sheets_mod.write_week_tab(
        service, "<id>", "9/28-10/4", _sample_matrix(), dry_run=True,
        previous_tab="9/21-9/27",
    )
    assert result["dry_run"] is True
    assert result["previous_tab"] == "9/21-9/27"
    assert service.method_calls == []  # no service calls at all
