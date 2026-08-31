from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "diagnose-test-gateway-and-browser.yml"


def test_workflow_is_protected_main_test_only_and_read_only():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in text
    assert "pull_request:" not in text
    assert "github.ref == 'refs/heads/main'" in text
    assert "DIAGNOSE_TEST_GATEWAY_AND_BROWSER" in text
    assert "TEST_VM: hermes-test-01" in text
    assert "hermes-poc-01" not in text
    assert "systemctl restart" not in text
    assert "systemctl stop" not in text
    assert "kill " not in text
    assert "rm -" not in text


def test_workflow_only_runs_read_only_inspection_commands():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "systemctl cat robie-gateway" in text
    assert "systemctl list-units" in text
    assert "ss -tln" in text
    assert "pgrep -fa" in text
    assert "::notice::" in text
