import sys
from types import ModuleType

from robie_job_engine.google_sheets_accountability import (
    CLOUD_PLATFORM_SCOPE,
    SHEETS_READONLY_SCOPE,
    _sheets_client,
    collect_allowlisted_tables,
    role_registry_from_snapshot,
)


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


def test_sheet_collector_uses_keyless_impersonation_for_configured_reader(monkeypatch):
    calls = {}

    google = ModuleType("google")
    google_auth = ModuleType("google.auth")
    google_auth_iam = ModuleType("google.auth.iam")
    google_auth_transport = ModuleType("google.auth.transport")
    google_auth_requests = ModuleType("google.auth.transport.requests")
    google_oauth2 = ModuleType("google.oauth2")
    google_service_account = ModuleType("google.oauth2.service_account")
    googleapiclient = ModuleType("googleapiclient")
    googleapiclient_discovery = ModuleType("googleapiclient.discovery")

    def default(*, scopes):
        calls["source_scopes"] = scopes
        return "source-credentials", "test-project"

    class Request:
        pass

    class Signer:
        def __init__(self, request, source, service_account_email):
            calls["signer"] = (request, source, service_account_email)

    class Credentials:
        def __init__(self, **kwargs):
            calls["credentials"] = kwargs

    def build(api, version, *, credentials, cache_discovery):
        calls["build"] = (api, version, credentials, cache_discovery)
        return "sheets-client"

    google_auth.default = default
    google_auth.iam = google_auth_iam
    google_auth_iam.Signer = Signer
    google_auth_requests.Request = Request
    google_service_account.Credentials = Credentials
    google_auth_transport.requests = google_auth_requests
    google_oauth2.service_account = google_service_account
    google.auth = google_auth
    google.oauth2 = google_oauth2
    googleapiclient.discovery = googleapiclient_discovery
    googleapiclient_discovery.build = build

    for name, module in {
        "google": google,
        "google.auth": google_auth,
        "google.auth.iam": google_auth_iam,
        "google.auth.transport": google_auth_transport,
        "google.auth.transport.requests": google_auth_requests,
        "google.oauth2": google_oauth2,
        "google.oauth2.service_account": google_service_account,
        "googleapiclient": googleapiclient,
        "googleapiclient.discovery": googleapiclient_discovery,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    reader = "robie-test-drive-reader@example.iam.gserviceaccount.com"
    monkeypatch.setenv("ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT", reader)

    assert _sheets_client() == "sheets-client"
    assert calls["source_scopes"] == [CLOUD_PLATFORM_SCOPE]
    assert calls["signer"][1:] == ("source-credentials", reader)
    assert calls["credentials"]["service_account_email"] == reader
    assert calls["credentials"]["scopes"] == [SHEETS_READONLY_SCOPE]
    assert "subject" not in calls["credentials"]
