import json
from pathlib import Path

from robie_job_engine.accountability_connections import check_connections


def test_connection_check_is_redacted_and_requires_core_sources(tmp_path: Path):
    ringcentral = tmp_path / "ringcentral.csv"
    tasks = tmp_path / "tasks.csv"
    ringcentral.write_text("Direction,Start Time\n", encoding="utf-8")
    tasks.write_text("Assigned To,Status\n", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "output_dir": str(tmp_path / "reports"),
        "sources": {"ringcentral": str(ringcentral), "tasks": str(tasks), "trackers": {}},
        "appsheet": {"enabled": True},
    }), encoding="utf-8")
    environment = {
        "ROBIE_EZLYNX_USERNAME_SECRET": "projects/example/secrets/user",
        "ROBIE_EZLYNX_PASSWORD_SECRET": "projects/example/secrets/password",
        "APPSHEET_APP_ID": "app-1",
        "APPSHEET_APPLICATION_ACCESS_KEY": "do-not-print-this",
    }
    result = check_connections(str(manifest), environment=environment)
    assert result["ready"]
    assert result["connections"]["appsheet"]["ready"]
    assert "do-not-print-this" not in json.dumps(result)


def test_connection_check_reports_scheduled_reports_and_dashboard_without_ids(tmp_path: Path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "collection": {"scheduled_reports_email": {
            "enabled": True, "mailbox": "reports@example.test", "allowed_sender_domains": ["example.test"],
            "reports": {"tasks": {"label": "ROBIE_TASKS"}},
        }},
        "dashboard": {"enabled": True, "spreadsheet_id": "private-sheet-id", "sheet_name": "Agency Accountability"},
    }), encoding="utf-8")
    result = check_connections(str(manifest), environment={
        "ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT": "delegated@example.test",
    })
    assert result["connections"]["ezlynx"]["scheduled_reports_configured"]
    assert result["connections"]["dashboard"] == {"enabled": True, "ready": True, "aggregate_only": True}
    assert "private-sheet-id" not in json.dumps(result)


def test_ezlynx_secret_references_alone_are_not_fresh_report_evidence(tmp_path: Path):
    ringcentral = tmp_path / "ringcentral.csv"
    ringcentral.write_text("Direction,Start Time\n", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"sources": {"ringcentral": str(ringcentral)}}), encoding="utf-8")
    result = check_connections(str(manifest), environment={
        "ROBIE_EZLYNX_USERNAME_SECRET": "projects/example/secrets/user",
        "ROBIE_EZLYNX_PASSWORD_SECRET": "projects/example/secrets/password",
    })
    assert not result["ready"]
    assert result["connections"]["ezlynx"]["browser_secret_references_configured"]
    assert not result["connections"]["ezlynx"]["evidence_ready"]


def test_required_connections_need_confirmed_gmail_scopes_and_observed_sources(tmp_path: Path):
    for name in ("ringcentral.json", "tasks.csv", "gmail.json", "magellan.json"):
        (tmp_path / name).write_text("{}" if name.endswith(".json") else "Assigned To,Status\n", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "sources": {
            "ringcentral": str(tmp_path / "ringcentral.json"),
            "tasks": str(tmp_path / "tasks.csv"),
            "email_json": str(tmp_path / "gmail.json"),
            "magellan_json": str(tmp_path / "magellan.json"),
        },
        "rules": {"required_connection_sources": ["ringcentral", "ezlynx", "gmail", "magellan"]},
        "collection": {"gmail": {"confirmed_scopes": ["gmail.metadata", "gmail.readonly", "gmail.send"]}},
    }), encoding="utf-8")
    result = check_connections(str(manifest), environment={})
    assert result["ready"]
    assert result["connections"]["gmail"]["required_scopes"] == ["gmail.metadata", "gmail.readonly", "gmail.send"]
