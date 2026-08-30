from robie_job_engine.google_sheets_accountability import collect_allowlisted_tables, role_registry_from_snapshot


class _Request:
    def execute(self):
        return {"values": [["Name", "Role", "Email", "Salary"], ["Karla", "Sales Producer", "k@example.com", "secret"]]}


class _Values:
    def get(self, **kwargs):
        return _Request()


class _Spreadsheets:
    def values(self):
        return _Values()


class _Service:
    def spreadsheets(self):
        return _Spreadsheets()


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
