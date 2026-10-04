"""Tests for robie_job_engine/publish_4359_liveness.py — fake clients, no network, no secrets."""

from datetime import date

import pytest

from robie_job_engine.overdue_policy_change_reports import (
    PolicyChangeReportContractError,
    parse_4359_csv,
)
from robie_job_engine.publish_4359_liveness import (
    LIVENESS_COLUMNS,
    LIVENESS_TAB_DEFAULT,
    build_liveness_rows,
    default_spreadsheet_id,
    ensure_tab,
    open_status_rows,
    run,
    write_liveness_tab,
)

TODAY = date(2026, 10, 1)

HEADER = (
    "Account Name,Applicant ID,Policy Number,Line Of Business,Effective Date,"
    "Master Company,Request Status,Created By,Written Premium,Premium - Annualized,"
    "Branch,Department,Service Team,Assigned Producer,CSR,Preferred Language,"
    "Applicant Labels,Policy Labels,Change Request Created Date"
)


def csv_bytes(*rows: str) -> bytes:
    return ("\n".join([HEADER, *rows]) + "\n").encode("utf-8")


def row(account="SAPP Construction Corp", applicant="41055091", policy="S 2391821",
        created="2026-08-17", csr="Eimy Ramos", status="Open",
        carrier="Selective Insurance") -> str:
    return (
        f"{account},{applicant},{policy},Commercial Pkg,2025-12-10,{carrier},{status},"
        f"\"Quezada, Zeus\",$28936.00,$28936.00,Streetsmart Insurance,"
        f"Commercial Lines,,Sandy Santana,{csr},English,,,{created}"
    )


def parsed(*rows: str) -> list[dict[str, str]]:
    return parse_4359_csv(csv_bytes(*rows))


# -- Open-row filter (no 14-day cutoff) -----------------------------------------


def test_open_rows_include_every_age():
    rows = parsed(
        row(policy="P1", created="2026-09-29"),  # 2 days old
        row(policy="P2", created="2025-03-01"),  # 214 days old
    )
    selected = open_status_rows(rows)
    assert [r["Policy Number"] for r in selected] == ["P1", "P2"]


def test_open_rows_exclude_non_open_statuses():
    rows = parsed(
        row(policy="P1", status="Open"),
        row(policy="P2", status="Closed"),
        row(policy="P3", status="Completed"),
        row(policy="P4", status="Cancelled"),
        row(policy="P5", status="open"),  # case-insensitive
    )
    assert [r["Policy Number"] for r in open_status_rows(rows)] == ["P1", "P5"]


# -- build_liveness_rows --------------------------------------------------------


def fake_search(mapping):
    def search(number):
        return [dict(r) for r in mapping.get(number, [])]
    return search


def policy_row(number, account="41055091", status="Active", expiration="2026-12-10"):
    return {"policyNumber": number, "accountId": account, "policyStatus": status,
            "expirationDate": expiration, "premium": 28936.0}


def test_build_rows_has_the_six_required_columns():
    liveness = build_liveness_rows(
        parsed(row()), fake_search({"S 2391821": [policy_row("S 2391821")]}), TODAY)
    assert len(liveness) == 1
    assert list(liveness[0].keys()) == LIVENESS_COLUMNS
    assert LIVENESS_COLUMNS == ["Policy Number", "Account Name", "Applicant ID",
                                "Verdict", "Reason", "Checked Date"]


def test_build_rows_live_verdict():
    liveness = build_liveness_rows(
        parsed(row()), fake_search({"S 2391821": [policy_row("S 2391821")]}), TODAY)
    assert liveness[0]["Policy Number"] == "S 2391821"
    assert liveness[0]["Account Name"] == "SAPP Construction Corp"
    assert liveness[0]["Applicant ID"] == "41055091"
    assert liveness[0]["Verdict"] == "LIVE"
    assert liveness[0]["Reason"]
    assert liveness[0]["Checked Date"] == "2026-10-01"


def test_build_rows_dead_verdict():
    liveness = build_liveness_rows(
        parsed(row()), fake_search({"S 2391821": [policy_row("S 2391821", status="Cancelled")]}), TODAY)
    assert liveness[0]["Verdict"] == "DEAD"


def test_build_rows_hold_when_policy_api_has_no_results():
    liveness = build_liveness_rows(parsed(row()), fake_search({}), TODAY)
    assert liveness[0]["Verdict"] == "HOLD"
    assert "no results" in liveness[0]["Reason"]


def test_build_rows_hold_when_search_raises_instead_of_dropping():
    def broken_search(number):
        raise ConnectionError("PolicyApi down")

    liveness = build_liveness_rows(parsed(row()), broken_search, TODAY)
    assert liveness[0]["Verdict"] == "HOLD"
    assert "liveness lookup failed" in liveness[0]["Reason"]


def test_build_rows_hold_when_applicant_is_blank():
    liveness = build_liveness_rows(parsed(row(applicant="")), fake_search({}), TODAY)
    assert liveness[0]["Verdict"] == "HOLD"


# -- fake Google Sheets service -------------------------------------------------


class _FakeExecutable:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class FakeSheetsService:
    """Minimal fake of the Sheets v4 resource chain used by the publisher."""

    def __init__(self, existing_tabs=()):
        self.existing_tabs = list(existing_tabs)
        self.cleared = []
        self.updated = []
        self.added_sheets = []

    def spreadsheets(self):
        return self

    def get(self, spreadsheetId, fields=None):
        return _FakeExecutable({
            "sheets": [{"properties": {"title": title}} for title in self.existing_tabs]
        })

    def batchUpdate(self, spreadsheetId, body):
        for request in body.get("requests", []):
            title = request.get("addSheet", {}).get("properties", {}).get("title")
            if title:
                self.added_sheets.append(title)
                self.existing_tabs.append(title)
        return _FakeExecutable({})

    def values(self):
        return self

    def clear(self, spreadsheetId, range):
        self.cleared.append(range)
        return _FakeExecutable({})

    def update(self, spreadsheetId, range, valueInputOption, body):
        self.updated.append({"range": range, "values": body["values"]})
        return _FakeExecutable({"updatedCells": len(body["values"]) * 6})


# -- write_liveness_tab ----------------------------------------------------------


def sample_liveness_rows():
    return [
        {"Policy Number": "S 2391821", "Account Name": "SAPP Construction Corp",
         "Applicant ID": "41055091", "Verdict": "LIVE",
         "Reason": "policy-number match, applicant account confirmed",
         "Checked Date": "2026-10-01"},
        {"Policy Number": "OLD123", "Account Name": "Old Corp",
         "Applicant ID": "999", "Verdict": "DEAD",
         "Reason": "policy-number match is dead (Cancelled)",
         "Checked Date": "2026-10-01"},
    ]


def test_write_tab_creates_missing_tab_and_writes_header_plus_rows():
    service = FakeSheetsService(existing_tabs=())
    receipt = write_liveness_tab(service, "sheet-1", "4359-Liveness", sample_liveness_rows())
    assert service.added_sheets == ["4359-Liveness"]
    assert receipt["tab_created"] is True
    assert receipt["rows_written"] == 2
    assert service.cleared == ["'4359-Liveness'!A1:F"]
    assert len(service.updated) == 1
    values = service.updated[0]["values"]
    assert values[0] == LIVENESS_COLUMNS
    assert values[1][0] == "S 2391821"
    assert values[1][3] == "LIVE"
    assert values[2][3] == "DEAD"
    assert service.updated[0]["range"] == "'4359-Liveness'!A1:F"


def test_write_tab_does_not_recreate_existing_tab():
    service = FakeSheetsService(existing_tabs=["4359-Liveness"])
    receipt = write_liveness_tab(service, "sheet-1", "4359-Liveness", sample_liveness_rows())
    assert service.added_sheets == []
    assert receipt["tab_created"] is False
    assert receipt["rows_written"] == 2


def test_ensure_tab_is_idempotent():
    service = FakeSheetsService(existing_tabs=["4359-Liveness"])
    assert ensure_tab(service, "sheet-1", "4359-Liveness") is False
    assert service.added_sheets == []


# -- run() orchestration ---------------------------------------------------------


def live_search(number):
    return [policy_row(number)]


def test_run_dry_run_writes_nothing_and_reports_counts():
    service = FakeSheetsService(existing_tabs=("4359-Liveness",))
    exit_code, summary = run(
        queue_reader=lambda payload: parsed(
            row(policy="S 2391821", applicant="41055091"),
            row(policy="OLD123", applicant="999", status="Closed"),
        ),
        policy_search=live_search,
        spreadsheet_id="sheet-1",
        tab="4359-Liveness",
        dry_run=True,
        sheets_service=service,
        today=TODAY,
    )
    assert exit_code == 0
    assert summary["queue_rows"] == 2
    assert summary["open_rows"] == 1  # the Closed row is skipped
    assert summary["live"] == 1
    assert summary["published"] is False
    assert service.updated == []  # nothing written in dry-run


def test_run_live_publishes_every_open_row():
    search = fake_search({
        "S 2391821": [policy_row("S 2391821", account="41055091")],
        "OLD123": [policy_row("OLD123", account="999", status="Cancelled")],
    })
    service = FakeSheetsService(existing_tabs=("4359-Liveness",))
    exit_code, summary = run(
        queue_reader=lambda payload: parsed(
            row(policy="S 2391821", applicant="41055091"),
            row(policy="OLD123", applicant="999"),
        ),
        policy_search=search,
        spreadsheet_id="sheet-1",
        tab="4359-Liveness",
        dry_run=False,
        sheets_service=service,
        today=TODAY,
    )
    assert exit_code == 0
    assert summary["open_rows"] == 2
    assert summary["live"] == 1
    assert summary["dead"] == 1
    assert summary["hold"] == 0
    assert summary["published"] is True
    assert summary["spreadsheet_id"] == "sheet-1"
    values = service.updated[0]["values"]
    assert [v[3] for v in values[1:]] == ["LIVE", "DEAD"]


def test_run_live_with_empty_queue_writes_header_only():
    service = FakeSheetsService(existing_tabs=("4359-Liveness",))
    exit_code, summary = run(
        queue_reader=lambda payload: [],
        policy_search=live_search,
        spreadsheet_id="sheet-1",
        tab="4359-Liveness",
        dry_run=False,
        sheets_service=service,
        today=TODAY,
    )
    assert exit_code == 0
    assert summary["open_rows"] == 0
    assert summary["published"] is True
    assert service.updated[0]["values"] == [LIVENESS_COLUMNS]


def test_run_missing_report_fails_closed_with_exit_2():
    def missing_report(payload):
        raise PolicyChangeReportContractError("no fresh 4359 report was collected")

    service = FakeSheetsService()
    exit_code, summary = run(
        queue_reader=missing_report,
        policy_search=live_search,
        spreadsheet_id="sheet-1",
        tab="4359-Liveness",
        dry_run=False,
        sheets_service=service,
        today=TODAY,
    )
    assert exit_code == 2
    assert "no fresh 4359 report" in summary["error"]
    assert service.updated == []


def test_run_unexpected_error_exits_1():
    def exploding_search(number):
        raise AssertionError("boom")

    # force the lookup path to explode outside the per-row guard
    service = FakeSheetsService()
    exit_code, summary = run(
        queue_reader=lambda payload: (_ for _ in ()).throw(RuntimeError("gmail down")),
        policy_search=exploding_search,
        spreadsheet_id="sheet-1",
        tab="4359-Liveness",
        dry_run=False,
        sheets_service=service,
        today=TODAY,
    )
    assert exit_code == 1
    assert "RuntimeError" in summary["error"]


# -- spreadsheet id default ------------------------------------------------------


def test_default_spreadsheet_id_env_override(monkeypatch):
    monkeypatch.setenv("ROBIE_4359_LIVENESS_SPREADSHEET_ID", "env-sheet-id")
    assert default_spreadsheet_id() == "env-sheet-id"


def test_default_tab_name():
    assert LIVENESS_TAB_DEFAULT == "4359-Liveness"
