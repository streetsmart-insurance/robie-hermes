from pathlib import Path


def test_ascend_disabled_workflow_is_main_and_test_only():
    workflow = Path(".github/workflows/ascend-disabled-test.yml").read_text()
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "VERIFY_ASCEND_DISABLED_ON_HERMES_TEST_01" in workflow
    assert "TEST_VM: hermes-test-01" in workflow
    assert "hermes-poc-01" not in workflow
    assert "ASCEND DISABLED TEST VERIFIED" in workflow
    assert "gcloud compute scp" in workflow
    assert "bash -s" not in workflow


def test_remote_proof_checks_process_configuration_and_no_network_path():
    script = Path("scripts/verify-ascend-disabled-test.sh").read_text()
    assert "/proc/${gateway_pid}/environ" in script
    assert 'readlink -f "/proc/${gateway_pid}/exe"' in script
    assert "sudo -u streetsmart-hermes" not in script
    assert "ROBIE_ASCEND_API_KEY_SECRET" in script
    assert "Ascend API execution is not enabled" in script
    assert '"outbound_ascend_post_requests": 0' in script
    assert '"production_touched": False' in script
