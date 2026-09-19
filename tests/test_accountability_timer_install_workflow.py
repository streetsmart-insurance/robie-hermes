from pathlib import Path


WORKFLOW = Path(".github/workflows/run-accountability-now.yml")


def test_timer_install_uses_verified_ssh_transport_not_scp():
    source = WORKFLOW.read_text(encoding="utf-8")
    start = source.index("      - name: Install the canonical weekday timer")
    end = source.index("      - name: Repair the installed lock timeout", start)
    step = source[start:end]

    assert "gcloud compute scp" not in step
    assert 'timer_sha="$(sha256sum' in step
    assert 'timer_b64="$(base64 -w0' in step
    assert "base64 -d | sudo tee /etc/systemd/system/streetsmart-accountability.timer" in step
    assert "sudo sha256sum /etc/systemd/system/streetsmart-accountability.timer" in step
    assert "sudo systemctl daemon-reload" in step


def test_repair_install_uses_verified_ssh_transport_not_scp():
    source = WORKFLOW.read_text(encoding="utf-8")
    start = source.index(
        "      - name: Repair recipients, Submission Center gate, Docs indexes, and preflight Magellan authentication"
    )
    end = source.index("      - name: Resolve exact prior-business-day target", start)
    step = source[start:end]

    assert "gcloud compute scp" not in step
    assert "upload_python()" in step
    assert 'source_sha="$(sha256sum' in step
    assert "base64 -d >" in step
    assert "sha256sum" in step
    assert "repair_dedicated_accountability_submission_gate.py" in step
    assert 'sudo bash -s" <<REMOTE_EOF' in step
