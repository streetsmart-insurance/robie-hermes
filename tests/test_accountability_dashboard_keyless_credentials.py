from pathlib import Path


def test_dashboard_client_supports_keyless_service_account_credentials():
    source = Path("robie_job_engine/accountability_dashboard.py").read_text(
        encoding="utf-8"
    )
    client_block = source.split("def _client", 1)[1].split("def _metric", 1)[0]

    assert "ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT" in client_block
    assert "iam.Signer(" in client_block
    assert "service_account.Credentials(" in client_block
    assert "scopes=[SHEETS_SCOPE]" in client_block
    assert "subject=" not in client_block
    assert "google.auth.default(scopes=[SHEETS_SCOPE])" in client_block
