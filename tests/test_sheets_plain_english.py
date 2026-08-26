from robie_job_engine.sheets_sync import (
    _friendly_datetime,
    _friendly_jobs,
    _friendly_person,
    _runtime_job_fields,
)
from function_loader import load_function_tests


def load_tests(loader, tests, pattern):
    return load_function_tests(globals())


def test_job_ledger_starts_with_date_and_plain_english_identity():
    rows = _friendly_jobs([{
        "id": "technical-id",
        "action_type": "hermes.google_chat_task",
        "payload": {
            "text": "Upload Hartford quote and add the renewal note",
            "requested_by": "jake.smith@streetsmart.insurance",
            "client": "Razza",
            "job_name": "Razza Renewal — Hartford Quote",
            "source": "Google Chat",
        },
        "status": "VERIFYING",
        "attempt_count": 1,
        "verification_count": 1,
        "created_at": "2026-08-22T14:52:15+00:00",
        "updated_at": "2026-08-22T14:55:15+00:00",
        "completed_at": None,
        "last_error": None,
    }])
    row = rows[0]
    assert row[0].startswith("Aug 22, 2026")
    assert row[1] == "Razza Renewal — Hartford Quote"
    assert row[2] == "Razza"
    assert row[3] == "Jake Smith"
    assert row[4] == "Upload Hartford quote and add the renewal note"
    assert row[5] == "ROBIE"
    assert row[8] == "Checking the result"
    assert row[9] == "MEDIUM — 65%"
    assert "No diagnostic recording" in row[10]
    assert row[17] == "technical-id"
    assert row[20:25] == [0, 0, 0, 0, 0]


def test_name_is_preserved_when_chat_supplies_display_name():
    assert _friendly_person("Carlo Ferrara") == "Carlo Ferrara"


def test_ledger_uses_streetsmart_eastern_time_not_server_utc():
    assert _friendly_datetime("2026-08-23T00:33:00+00:00") == "Aug 22, 2026, 8:33 PM"


def test_recording_column_lists_all_numbered_segments():
    row = _friendly_jobs([{
        "id": "retry-job",
        "action_type": "ezlynx.apply_label",
        "payload": {"text": "Apply label"},
        "status": "COMPLETE",
        "recording_status": "READY",
        "recording_links": [
            {"segment": 1, "url": "https://drive.google.com/file/d/one/view"},
            {"segment": 2, "url": "https://drive.google.com/file/d/two/view"},
        ],
    }])[0]
    assert "Segment 1:" in row[11]
    assert "Segment 2:" in row[11]


def test_recording_column_surfaces_upload_failure():
    row = _friendly_jobs([{
        "id": "upload-failed-job",
        "action_type": "ezlynx.apply_label",
        "payload": {"text": "Apply label"},
        "status": "FAILED",
        "recording_status": "FAILED",
        "recording_failure_stage": "UPLOAD",
    }])[0]
    assert row[11] == "Recording upload failed"


def test_sensitive_recording_exemption_is_explicit_and_not_an_issue():
    job = {
        "id": "auth-refresh",
        "action_type": "ezlynx.session_refresh",
        "payload": {"task": "ezlynx.session_refresh"},
        "status": "COMPLETE",
        "verification_count": 1,
        "verified_evidence_count": 1,
        "authoritative_evidence_count": 1,
        "recording_exemption": {"reason": "authentication may display credentials or MFA data"},
    }
    row = _friendly_jobs([job])[0]
    assert row[9] == "HIGH — 98%"
    assert row[10] == ""
    assert row[11].startswith("Recording exempt — sensitive authentication flow")


def test_complete_runtime_columns_are_derived_from_authoritative_evidence():
    fields = _runtime_job_fields({
        "status": "COMPLETE",
        "verification_count": 1,
        "verified_evidence_count": 3,
        "authoritative_evidence_count": 1,
        "updated_at": "2026-08-25T20:46:00+00:00",
    })
    assert fields["current_step"] == "Completed and independently verified"
    assert fields["verification_status"] == "Verified"
    assert fields["evidence_count"] == 3
