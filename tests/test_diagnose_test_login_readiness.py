from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "diagnose-test-login-readiness.yml"


def test_workflow_is_protected_main_test_only_and_read_only():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in text
    assert "pull_request:" not in text
    assert "github.ref == 'refs/heads/main'" in text
    assert "DIAGNOSE_TEST_LOGIN_READINESS" in text
    assert "TEST_VM: hermes-test-01" in text
    assert "hermes-poc-01" not in text
    assert "systemctl restart" not in text
    assert "rm -" not in text
    assert "cat " not in text
    assert "sudo" not in text


def test_workflow_checks_existence_only_never_reads_token_contents():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "test -f /opt/streetsmart-hermes/.hermes/robie_google_token.json" in text
    assert "test -f /opt/streetsmart-hermes-test/.hermes/robie_google_token.json" in text
    assert "ls -la" in text
    # Every remote command must be existence/listing only - never content reads.
    for forbidden in ("cat /opt", "grep", "base64", "python3 -c"):
        assert forbidden not in text
