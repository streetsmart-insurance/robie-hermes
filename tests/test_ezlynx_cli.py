"""CLI output tests for scripts/ezlynx_cli.py documents listing."""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SCRIPTS_CLI = Path(__file__).resolve().parents[1] / "scripts" / "ezlynx_cli.py"


def _load_cli():
    spec = importlib.util.spec_from_file_location("ezlynx_cli", SCRIPTS_CLI)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def cli_mod():
    return _load_cli()


PRODUCTION_LIKE_RESPONSE = {
    "status": "success",
    "data": {
        "TotalRecords": 154,
        "Documents": [
            {
                "Id": 881122,
                "Description": "2026-27 Renewal Offer - Coterie CBB-00113127-02.pdf",
                "PolicyId": 99012,
                "CreatedDate": "2026-08-01T14:22:00",
            },
            {
                "Id": 881123,
                "Description": "Applications",
                "PolicyId": 0,
                "CreatedDate": None,
            },
        ],
    },
}


def _run_documents(cli_mod, monkeypatch, payload, extra_args=None):
    mock_client = MagicMock()
    mock_client.list_applicant_documents.return_value = payload
    monkeypatch.setattr(cli_mod, "EZLynxApiClient", lambda: mock_client)
    argv = ["ezlynx_cli.py", "documents", "151445306"]
    if extra_args:
        argv.extend(extra_args)
    monkeypatch.setattr(sys, "argv", argv)
    cli_mod.main()
    return mock_client


def test_documents_command_lists_filenames_and_policy(cli_mod, monkeypatch, capsys):
    """Live Classic rows use Description / Id / PolicyId, not DocumentName / PolicyNumber."""
    _run_documents(cli_mod, monkeypatch, PRODUCTION_LIKE_RESPONSE)
    out = capsys.readouterr().out
    assert "Document Library for Applicant #151445306:" in out
    assert "Total records: 154" in out
    assert "Showing 2 on page 1" in out
    assert "2026-27 Renewal Offer - Coterie CBB-00113127-02.pdf" in out
    assert "ID: 881122" in out
    assert "PolicyId: 99012" in out
    assert "Uploaded: 2026-08-01" in out
    assert "Name: Applications" in out
    assert "Policy: —" in out
    assert "PolicyId: 0" not in out
    # Must not stop at the count line the way the old CLI did.
    assert out.count("  • ") == 2


def test_documents_command_json_dumps_full_payload(cli_mod, monkeypatch, capsys):
    _run_documents(cli_mod, monkeypatch, PRODUCTION_LIKE_RESPONSE, extra_args=["--json"])
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["status"] == "success"
    assert parsed["data"]["TotalRecords"] == 154
    assert parsed["data"]["Documents"][0]["Description"].endswith(".pdf")
    assert parsed["data"]["Documents"][0]["PolicyId"] == 99012
    assert parsed["data"]["Documents"][0]["Id"] == 881122


def test_documents_command_legacy_records_key_still_lists(cli_mod, monkeypatch, capsys):
    payload = {
        "status": "success",
        "data": {
            "TotalRecords": 1,
            "Records": [
                {
                    "Id": 44,
                    "Name": "via-records.pdf",
                    "PolicyNumber": "R2WC681352",
                    "CreatedDate": "2026-09-01",
                }
            ],
        },
    }
    _run_documents(cli_mod, monkeypatch, payload)
    out = capsys.readouterr().out
    assert "via-records.pdf" in out
    assert "Policy: R2WC681352" in out
    assert "ID: 44" in out


def test_documents_command_empty_page(cli_mod, monkeypatch, capsys):
    payload = {"status": "success", "data": {"TotalRecords": 1480, "Documents": []}}
    _run_documents(cli_mod, monkeypatch, payload, extra_args=["--page", "2", "--size", "20"])
    out = capsys.readouterr().out
    assert "Total records: 1480" in out
    assert "(no documents returned on this page)" in out


def test_documents_command_error_exits(cli_mod, monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        _run_documents(cli_mod, monkeypatch, {"status": "error", "error": "boom"})
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "Error listing documents: boom" in err
