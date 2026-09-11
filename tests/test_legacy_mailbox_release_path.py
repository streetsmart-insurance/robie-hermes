from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_legacy_test_installer_is_host_guarded_and_non_production():
    script = (ROOT / "scripts/deploy-test-legacy-mailbox-release.sh").read_text()
    assert 'EXPECTED_HOST="hermes-test-01"' in script
    assert 'OPT_ROOT="/opt/renewal-automation-system-test"' in script
    assert "hermes-poc-01" not in script
    assert "/opt/renewal-automation-system/" not in script
    assert '"external_writes": False' in script
    assert '"rollback_rehearsed": True' in script


def test_legacy_artifact_builder_excludes_runtime_data_and_secrets():
    script = (ROOT / "scripts/build-legacy-mailbox-release.sh").read_text()
    assert "data/" not in script
    assert "credentials" in script
    assert "token" in script
    assert "sha256sum" in script


def test_credentialed_workflow_runs_only_from_protected_main():
    workflow = (ROOT / ".github/workflows/deploy-test-legacy-mailbox.yml").read_text()
    assert "id-token: write" in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "DEPLOY_LEGACY_MAILBOX_TO_HERMES_TEST_01" in workflow
    assert "robie-test-deployer@streetsmart-robie-test.iam.gserviceaccount.com" in workflow
    assert "actions/upload-artifact@v4" in workflow
