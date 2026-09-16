import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_accountability_watchdog.py"
REPORT_SCRIPT = ROOT / "scripts" / "verify_accountability_report_content.py"
WORKFLOW = ROOT / ".github" / "workflows" / "accountability-delivery-watchdog.yml"


def _module():
    spec = importlib.util.spec_from_file_location("accountability_watchdog", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _report_module():
    spec = importlib.util.spec_from_file_location(
        "accountability_report_content", REPORT_SCRIPT
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _valid_snapshot():
    return {
        "host": "streetsmart-accountability-prod",
        "timer_load": "loaded",
        "timer_enabled": "enabled",
        "timer_active": "active",
        "timer_next": "Thu 2026-09-17 13:00:00 UTC",
        "service_result": "success",
        "service_exit_status": "0",
        "run_date": "2026-09-17",
        "target_date": "2026-09-16",
        "document_url_present": True,
        "document_id": "1lnhIplYM8DLdFyITL9tCUVrImYzYy1Vbfax7eeAkdhg",
    }


def test_watchdog_accepts_only_complete_authoritative_evidence():
    evidence = _module().validate_snapshot(
        _valid_snapshot(),
        today="2026-09-17",
        expected_target="2026-09-16",
    )
    assert evidence == {
        "verified": True,
        "host": "streetsmart-accountability-prod",
        "run_date": "2026-09-17",
        "target_date": "2026-09-16",
        "timer_enabled": True,
        "timer_active": True,
        "timer_next_present": True,
        "service_result": "success",
        "service_exit_status": 0,
        "document_url_present": True,
        "document_id_present": True,
    }


@pytest.mark.parametrize(
    ("field", "bad_value", "failed_check"),
    [
        ("host", "hermes-poc-01", "host"),
        ("timer_load", "not-found", "timer_loaded"),
        ("timer_enabled", "disabled", "timer_enabled"),
        ("timer_active", "inactive", "timer_active"),
        ("timer_next", "", "timer_next_present"),
        ("service_result", "exit-code", "service_result"),
        ("service_exit_status", "1", "service_exit_status"),
        ("run_date", "2026-09-16", "run_date"),
        ("target_date", "2026-09-15", "target_date"),
        ("document_url_present", False, "document_url_present"),
        ("document_id", "", "document_id_present"),
    ],
)
def test_watchdog_fails_closed_for_each_missing_proof(field, bad_value, failed_check):
    snapshot = _valid_snapshot()
    snapshot[field] = bad_value
    with pytest.raises(RuntimeError, match=failed_check):
        _module().validate_snapshot(
            snapshot,
            today="2026-09-17",
            expected_target="2026-09-16",
        )


def test_report_content_gate_accepts_populated_submission_center():
    evidence = _report_module().validate_submission_content(
        "Sales / service — Submission Center\n"
        "Open over 30 days: 4\n"
        "Evidence source: live EZLynx Submission Center\n"
        "Customer sentiment — Magellan\n"
        "Verified zero — full target-date pagination completed"
    )
    assert evidence["verified"] is True
    assert evidence["submission_center_section_present"] is True
    assert evidence["submission_center_unverified_marker_present"] is False
    assert evidence["magellan_section_present"] is True
    assert evidence["magellan_unverified_marker_present"] is False
    assert len(evidence["document_content_sha256"]) == 64


@pytest.mark.parametrize(
    "text",
    [
        "Executive summary only",
        "Submission Center was not verified: Locator.click timeout\nMagellan verified zero",
        "UNVERIFIED — server Submission Center audit unavailable\nMagellan verified zero",
        "Submission Center verified\nCustomer feedback only",
        "Submission Center verified\nMagellan\nNo verified records for this report date.",
        "Submission Center verified\nMagellan unavailable",
    ],
)
def test_report_content_gate_fails_closed_for_missing_or_unverified_evidence(text):
    with pytest.raises(RuntimeError):
        _report_module().validate_submission_content(text)


def test_watchdog_schedule_is_dst_safe_and_read_only():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert 'cron: "15 14,15 * * 1-5"' in text
    assert "TZ=America/New_York" in text
    assert 'local_hour}" = "10"' in text
    assert "environment: Production" in text
    assert "VERIFY_ACCOUNTABILITY_DELIVERY" in text
    assert "systemctl start" not in text
    assert "systemctl restart" not in text
    assert "--publish" not in text
    assert "--deliver" not in text
    assert "gmail.send" not in text


def test_watchdog_requires_marker_doc_and_exact_gmail_delivery():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "verify_accountability_watchdog.py" in text
    assert "verify_dedicated_accountability_delivery.py" in text
    assert "verify_accountability_report_content.py" in text
    assert "Verify Submission Center report content" in text
    assert "--mode verify" in text
    assert "last_success" not in text  # The verifier owns marker parsing.
    assert "--recipient carlo@streetsmart.insurance" in text
    assert "--recipient jake@streetsmart.insurance" in text
    assert "--recipient ashley@streetsmart.insurance" in text
    assert "--recipient gabrielac@streetsmart.insurance" in text
    assert "document_url_present" in SCRIPT.read_text(encoding="utf-8")


def test_watchdog_records_redacted_evidence_and_alerts_without_resending():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "Upload redacted watchdog evidence" in text
    assert "retention-days: 30" in text
    assert "Open or update a watchdog failure issue" in text
    assert "failed closed" in text
    assert "did not start the service or resend the report" in text
    assert "qualifying_scheduled_proof" in text
    assert "three-run reliability gate" in text
