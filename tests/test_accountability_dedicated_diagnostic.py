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


def test_dedicated_auth_checks_run_independently_and_preserve_evidence():
    text = WORKFLOW.read_text(encoding="utf-8")
    for step_name in (
        "Verify reusable Magellan session without delivery",
        "Verify reusable EZLynx Submission Center session without delivery",
        "Capture DWD identity mapping and redacted failure tracebacks",
        "Read timer, runtime, and last-success metadata",
    ):
        step = text.index(f"- name: {step_name}")
        run = text.index("        run: |", step)
        assert "        if: always()" in text[step:run]

def test_dedicated_ezlynx_preflight_receives_production_dwd_secret():
    text = WORKFLOW.read_text(encoding="utf-8")
    step = text.index("Verify reusable EZLynx Submission Center session without delivery")
    next_step = text.index("- name:", step + 1)
    block = text[step:next_step]
    assert 'GOOGLE_DWD_SECRET=accountability-google-dwd-key' in block
    assert (
        'sudo -u ubuntu env PYTHONPATH="${APP_ROOT}" '
        'GOOGLE_DWD_SECRET=accountability-google-dwd-key'
    ) in block

