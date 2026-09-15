from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "diagnose-accountability-dedicated.yml"


def test_dedicated_accountability_diagnostic_is_read_only_and_host_locked():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "PRODUCTION_VM: streetsmart-accountability-prod" in text
    assert "DIAGNOSE_ACCOUNTABILITY_PROD" in text
    assert "gcloud compute instances describe" in text
    assert "--tunnel-through-iap" in text
    assert "systemctl show" in text
    assert "systemctl is-enabled" in text
    assert "systemctl is-active" in text
    assert "last_success.json" in text
    assert "systemctl start" not in text
    assert "systemctl restart" not in text
    assert "systemctl enable" not in text
    assert "gmail" not in text.casefold()
    assert "secrets versions access" not in text
