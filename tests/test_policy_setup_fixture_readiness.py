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
    assert "EXPECTED_TEST_SHA: 9efcc8d374cc" in text
    assert "AUDIT_POLICY_SETUP_FIXTURES_ON_9EFCC8D374CC" in text
    assert "id-token: write" in text
    assert "hermes-poc-01" not in text
    assert "systemctl restart" not in text
    assert "deploy-test-release" not in text
    assert "::error::fixture-readiness audit failed: ssh_status=" in text
    assert "log_bytes=" in text


def test_remote_audit_requires_test_runtime_and_zero_active_jobs():
    text = REMOTE.read_text(encoding="utf-8")
    assert "test \"$(hostname -s)\" = hermes-test-01" in text
    assert 'test "${ROBIE_ENV:-}" = TEST' in text
    assert 'test "${TEST_ROOT:-}" = /opt/streetsmart-hermes-test' in text
    assert "mode=ro" in text
    assert "active Test jobs/leases" in text
    assert "systemctl is-active --quiet robie-gateway" in text
    assert (
        "ROBIE_CANONICAL_JOB_ENGINE_ROOT=/opt/streetsmart-hermes-test/releases/current"
        in text
    )
    assert "/opt/streetsmart-hermes/releases/current" in text
    assert "Test gateway references the Production release root" in text
    assert "Test gateway is not active" in text
    assert "Test gateway canonical root is missing" in text
    assert "systemctl show robie-gateway -p ExecStart" in text
    assert "systemctl show robie-gateway -p MainPID" in text
    assert 'pathlib.Path(f"/proc/{sys.argv[1]}/environ")' in text
    assert 'PYTHONPATH="${gateway_pythonpath}"' in text
    assert "active Test gateway Playwright runtime is unavailable" in text
    assert "-c 'import playwright'" in text
    assert "sudo -u streetsmart-hermes" not in text
    assert "systemctl restart" not in text


def test_remote_audit_does_not_require_unconfigured_service_environment_marker():
    """The live Test unit identifies itself by its canonical Test release root.

    ROBIE_ENV is injected into the one-shot audit process; it is not currently
    configured on robie-gateway itself. Requiring it in systemd caused the
    initial read-only audit to stop before the browser checks.
    """
    text = REMOTE.read_text(encoding="utf-8")
    assert "systemctl show robie-gateway" in text
    assert "grep -q 'ROBIE_ENV=TEST'" not in text


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
