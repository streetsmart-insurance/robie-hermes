from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "run-accountability-now.yml"


def test_manual_accountability_workflow_targets_only_the_dedicated_server():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "PRODUCTION_VM: streetsmart-accountability-prod" in text
    assert "APP_ROOT: /opt/streetsmart-daily-accountability" in text
    assert "hermes-poc-01" not in text
    assert "/opt/streetsmart-hermes/accountability" not in text


def test_manual_accountability_workflow_does_not_rewrite_live_configuration():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "tee " not in text
    assert "connection-manifest.json" not in text
    assert "robie-accountability.env" not in text
    assert "systemctl start ${UNIT}" in text
    assert "systemctl enable" not in text


def test_manual_accountability_workflow_verifies_date_and_delivery_marker():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "get_previous_business_day" in text
    assert "stale report date" in text
    assert "last_success.json" in text
    assert "success marker was not created by today's run" in text
    assert "document_url" in text
    assert "cancel-in-progress: false" in text
