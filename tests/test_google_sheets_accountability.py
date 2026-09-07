from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from robie_job_engine.google_sheets_accountability import (
    collect_allowlisted_tables,
    previous_business_day,
    previous_business_week_tab,
    role_registry_from_snapshot,
    write_allowlisted_table_csv,
)


class _Request:
    def execute(self):
        return {"values": [["Name", "Role", "Email", "Salary"], ["Karla", "Sales Producer", "k@example.com", "secret"]]}


class _Values:
    requested_range = ""

    def get(self, **kwargs):
        self.requested_range = kwargs["range"]
        return _Request()


class _Spreadsheets:
    def values(self):
        return _Values()


class _Service:
    def __init__(self, values=None):
        self._spreadsheets = _Spreadsheets()
        if values is not None:
            self._spreadsheets.values = lambda: values

    def spreadsheets(self):
        return self._spreadsheets


def test_sheet_collector_persists_only_allowlisted_columns():
    config = {
        "spreadsheet_id": "sheet-1",
        "employee_role_table": "employees",
        "employee_role_columns": {"name": "Name", "role": "Role", "email": "Email"},
        "tables": {"employees": {"range": "Employees!A:Z", "allowed_columns": ["Name", "Role", "Email"]}},
    }
    snapshot = collect_allowlisted_tables(config, service=_Service())
    assert "Salary" not in snapshot["tables"]["employees"]["rows"][0]
    roles = role_registry_from_snapshot(snapshot, config)
    assert roles["employees"]["Karla"]["role"] == "Sales Producer"


def test_role_registry_uses_rebuilt_roster_fields_and_active_people_only():
    snapshot = {
        "tables": {
            "employees": {
                "source_status": "available",
                "rows": [
                    {"name": "Active Person", "position": "Account Manager", "email": "active@example.com", "department": "Personal Lines", "employmentStatus": "Active", "departmentHead": "Manager"},
                    {"name": "Former Person", "position": "Producer", "email": "former@example.com", "department": "Sales", "employmentStatus": "Inactive", "departmentHead": "Manager"},
                ],
            }
        }
    }
    config = {
        "employee_role_table": "employees",
        "employee_role_columns": {
            "name": "name", "role": "position", "email": "email", "department": "department",
            "status": "employmentStatus", "manager": "departmentHead", "active_value": "Active",
        },
    }
    roles = role_registry_from_snapshot(snapshot, config)
    assert list(roles["employees"]) == ["Active Person"]
    assert roles["employees"]["Active Person"]["manager"] == "Manager"


def test_coi_tracker_resolves_current_month_and_writes_only_allowlisted_columns(tmp_path: Path):
    values = _Values()
    service = _Service(values)
    config = {
        "spreadsheet_id": "coi-sheet",
        "tables": {
            "coi_endorsements": {
                "range": "{month}!A:H",
                "allowed_columns": ["Name", "Role", "Email"],
            }
        },
    }
    snapshot = collect_allowlisted_tables(
        config,
        service=service,
        as_of=datetime(2026, 8, 15, 12, tzinfo=ZoneInfo("America/New_York")),
    )
    assert values.requested_range == "August!A:H"
    output = write_allowlisted_table_csv(snapshot, "coi_endorsements", tmp_path / "coi.csv")
    content = output.read_text(encoding="utf-8")
    assert content.splitlines()[0] == "Name,Role,Email"
    assert "Salary" not in content


def test_previous_business_day_and_sandeep_week_tab_skip_weekend():
    monday = datetime(2026, 8, 31, 9, tzinfo=ZoneInfo("America/New_York")).date()
    assert previous_business_day(monday).isoformat() == "2026-08-28"
    assert previous_business_week_tab(monday) == "8/24-8/30"


def test_policy_change_tracker_resolves_previous_business_week_tab():
    values = _Values()
    service = _Service(values)
    collect_allowlisted_tables(
        {
            "spreadsheet_id": "policy-change-sheet",
            "tables": {
                "policy_changes": {
                    "range": "{previous_business_week}!A:R",
                    "allowed_columns": ["Name", "Role", "Email"],
                }
            },
        },
        service=service,
        as_of=datetime(2026, 8, 31, 9, tzinfo=ZoneInfo("America/New_York")),
    )
    assert values.requested_range == "8/24-8/30!A:R"
