import json

from robie_job_engine.appsheet_client import AppSheetConfig, AppSheetReadClient


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return json.dumps([{"Employee": "Example User", "Target": 40}]).encode("utf-8")


def test_appsheet_client_is_find_only_and_uses_header_key():
    captured = {}

    def opener(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse()

    client = AppSheetReadClient(AppSheetConfig("app id", "secret-key"), opener=opener)
    rows = client.find_rows("KPI Targets")
    assert rows[0]["Target"] == 40
    request = captured["request"]
    assert request.get_method() == "POST"
    assert request.headers["Applicationaccesskey"] == "secret-key"
    assert b'"Action": "Find"' in request.data
    assert "app%20id" in request.full_url
    assert "KPI%20Targets" in request.full_url
