from pathlib import Path


SCRIPT = Path("scripts/deploy-test-overdue-submission-skill.sh")
WORKFLOW = Path(".github/workflows/deploy-test-overdue-submission-skill.yml")


def test_deployer_is_test_only_and_uses_atomic_pointer():
    content = SCRIPT.read_text(encoding="utf-8")
    assert 'EXPECTED_HOST="hermes-test-01"' in content
    assert 'OPT_ROOT="/opt/streetsmart-hermes-test"' in content
    assert 'production_ready: false' in content
    assert "os.replace(temporary, link)" in content
    assert '"production_touched": False' in content
    assert '"email_sent": False' in content
    assert "/opt/streetsmart-hermes/" not in content


def test_workflow_uses_short_lived_test_identity_and_exact_hash():
    content = WORKFLOW.read_text(encoding="utf-8")
    assert "id-token: write" in content
    assert "robie-test-deployer@streetsmart-robie-test.iam.gserviceaccount.com" in content
    assert 'TEST_VM: hermes-test-01' in content
    assert "sha256sum" in content
    assert "Production deployment:" in content
    assert "false" in content
    assert "hermes-poc-01" not in content
