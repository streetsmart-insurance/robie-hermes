from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "scripts" / "audit-policy-setup-fixture-readiness.py"
REMOTE = ROOT / "scripts" / "run-policy-setup-fixture-readiness-remote.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "policy-setup-fixture-readiness.yml"


def test_workflow_is_protected_main_test_only_and_read_only():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in text
    assert "pull_request:" not in text
    assert "github.ref == 'refs/heads/main'" in text
    assert "TEST_VM: hermes-test-01" in text
    assert "TEST_ROOT: /opt/streetsmart-hermes-test" in text
    assert "EXPECTED_TEST_SHA: e18fcfa2ed13" in text
    assert "AUDIT_POLICY_SETUP_FIXTURES_ON_E18FCFA2ED13" in text
    assert "id-token: write" in text
    assert "hermes-poc-01" not in text
    assert "systemctl restart" not in text
    assert "deploy-test-release" not in text


def test_remote_audit_requires_test_runtime_and_zero_active_jobs():
    text = REMOTE.read_text(encoding="utf-8")
    assert "test \"$(hostname -s)\" = hermes-test-01" in text
    assert 'test "${ROBIE_ENV:-}" = TEST' in text
    assert 'test "${TEST_ROOT:-}" = /opt/streetsmart-hermes-test' in text
    assert "mode=ro" in text
    assert "active Test jobs/leases" in text
    assert "systemctl is-active --quiet robie-gateway" in text
    assert "ROBIE_ENV=TEST" in text
    assert "systemctl restart" not in text


def test_browser_audit_has_no_fill_click_or_sensitive_dom_dump():
    text = AUDIT.read_text(encoding="utf-8")
    assert "context.new_page()" in text
    assert "page.close()" in text
    assert '"consequential_writes": 0' in text
    assert '"production_touched": False' in text
    assert ".fill(" not in text
    assert ".click(" not in text
    assert "page.content(" not in text
    assert "inner_html(" not in text
    assert "<id>" in text
