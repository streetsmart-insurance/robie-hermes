from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "diagnose-test-secret-access.yml"


def test_workflow_is_protected_main_test_only_and_read_only():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in text
    assert "pull_request:" not in text
    assert "github.ref == 'refs/heads/main'" in text
    assert "DIAGNOSE_TEST_SECRET_ACCESS" in text
    assert "TEST_VM: hermes-test-01" in text
    assert "hermes-poc-01" not in text
    assert "gcloud secrets versions access" not in text
    assert "gcloud secrets create" not in text
    assert "gcloud secrets add-iam-policy-binding" not in text
    assert "systemctl restart" not in text
    assert "sudo" not in text


def test_workflow_only_reads_names_and_bindings_never_payloads():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "gcloud secrets list" in text
    assert "gcloud secrets get-iam-policy" in text
    assert "no payloads" in text
    assert "--format='value(bindings)'" in text
    assert "--format='value(name)'" in text
