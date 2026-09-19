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


def test_repair_install_uses_one_verified_ssh_session():
    source = WORKFLOW.read_text(encoding="utf-8")
    start = source.index(
        "      - name: Repair recipients, Submission Center gate, Docs indexes, and preflight Magellan authentication"
    )
    end = source.index("      - name: Resolve exact prior-business-day target", start)
    step = source[start:end]

    assert "gcloud compute scp" not in step
    assert step.count("gcloud compute ssh") == 1
    assert "sha256sum *.py > SHA256SUMS" in step
    assert "sha256sum -c SHA256SUMS" in step
    assert "tar -xzf - -C" in step
    assert "repair_dedicated_accountability_submission_gate.py" in step
    assert "repair_dedicated_accountability_submission_boundary.py" in step
    assert '--path "${APP_ROOT}/src/reporters/stable_google_doc.py"' in step
    assert '--path "${APP_ROOT}/src/production_main.py"' not in step
    assert "ezlynx_submission_browser.py" in step
    assert "ezlynx_submission_center.py" in step
    assert "preflight_dedicated_accountability_magellan.py" in step
    assert 'sudo bash -s" <<REMOTE_EOF' in step
