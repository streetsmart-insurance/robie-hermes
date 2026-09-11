from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "deploy-production.yml"
INSTALLER = ROOT / "scripts" / "deploy-production-release.sh"


def test_workflow_is_protected_keyless_and_pins_release_inputs():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "github.ref == 'refs/heads/main'" in text
    assert "environment: Production" in text
    assert "id-token: write" in text
    assert "service_account: robie-production-deployer@streetsmart-hermes-poc" in text
    assert "workloadIdentityPools/github-production/providers/github-main" in text
    assert "ref: ${{ inputs.commit }}" in text
    assert 'test "${release_commit}" = "${REQUESTED_COMMIT}"' in text
    assert 'test "${release_sha256}" = "${REQUESTED_SHA256}"' in text
    assert "git pull" not in text


def test_workflow_and_installer_refuse_every_other_target():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    installer = INSTALLER.read_text(encoding="utf-8")
    for expected in (
        "streetsmart-hermes-poc",
        "us-east1-b",
        "hermes-poc-01",
        "5649534881067121807",
    ):
        assert expected in workflow
    assert 'EXPECTED_PROJECT="streetsmart-hermes-poc"' in installer
    assert 'EXPECTED_ZONE="us-east1-b"' in installer
    assert 'EXPECTED_HOST="hermes-poc-01"' in installer
    assert "metadata.google.internal" in installer
    assert "hermes-test-01" not in installer


def test_installer_stops_for_jobs_preserves_rollback_and_uses_official_path():
    text = INSTALLER.read_text(encoding="utf-8")
    assert "status IN ('RUNNING','VERIFYING') OR lease_owner IS NOT NULL" in text
    assert "active Production jobs or leases exist; refuse deploy" in text
    assert 'old_current="$(readlink -f "${OPT_ROOT}/current")"' in text
    assert "install-official-release.sh" in text
    assert 'systemctl restart "${GATEWAY_UNIT}"' in text
    assert "rollback_release" in text
    assert "production-deploy-evidence.json" in text


def test_installer_proves_test_allowlist_and_bound_production_scope_without_live_write():
    text = INSTALLER.read_text(encoding="utf-8")
    assert 'ALLOWED_EZLYNX_WRITE_APPLICANT_IDS == frozenset({allowed})' in text
    assert 'allowed = "220250093"' in text
    assert '"unbound_applicants_refused": True' in text
    assert '"production_scope_requires_active_original_message": True' in text
    assert 'requested_message_applicant' in text
    assert '"live_ezlynx_write_performed": False' in text
    assert "app.ezlynx.com/web/account/220250094/policies" in text
