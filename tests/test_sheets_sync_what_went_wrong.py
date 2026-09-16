"""Tests for the plain-English "What Went Wrong" column in the Jobs sheet sync.

Covers: _plain_english_what_went_wrong for every problem status, error
sanitization (no tracebacks/paths/URLs), the AWAITING_HUMAN_INPUT friendly
status label, and the Jobs!AH{row} write range emitted by upsert_job_rows.
"""

from unittest import mock

import robie_job_engine.sheets_sync as sheets_sync


def _job(status, **overrides):
    job = {
        "id": "job-1",
        "status": status,
        "action_type": "hermes.plain_english",
        "created_at": "2026-09-15T10:00:00+00:00",
        "updated_at": "2026-09-15T10:05:00+00:00",
        "completed_at": None,
        "last_error": "",
        "attempt_count": 1,
        "verification_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "total_tokens": 0,
        "estimated_cost_usd": 0,
        "recording_links": [],
        "recording_status": "",
        "recording_exemption": None,
        "recording_reference_approved": False,
        "authoritative_evidence_count": 0,
        "verified_evidence_count": 0,
        "payload": {
            "task": "do the thing",
            "requested_by": "carlo@streetsmart.insurance",
            "client": "ROBIE Test LLC",
        },
    }
    job.update(overrides)
    return job


def test_failed_mentions_failure_and_next_step():
    text = sheets_sync._plain_english_what_went_wrong(
        _job("FAILED", last_error="carrier portal timed out")
    )
    assert "failed" in text.lower()
    assert "carrier portal timed out" in text
    assert "re-run" in text.lower()


def test_needs_auth_names_who_must_approve():
    text = sheets_sync._plain_english_what_went_wrong(
        _job("NEEDS_AUTH", last_error="needs Jake's sign-off on the quote")
    )
    assert "waiting for authorization" in text.lower()
    assert "Jake" in text
    assert "approve" in text.lower()


def test_needs_clarification_and_awaiting_human_input_name_requester():
    for status in ("NEEDS_CLARIFICATION", "AWAITING_HUMAN_INPUT"):
        text = sheets_sync._plain_english_what_went_wrong(
            _job(status, last_error="which deductible did the client pick?")
        )
        assert "waiting on" in text.lower()
        assert "Carlo" in text
        assert "which deductible did the client pick?" in text


def test_unverified_explains_what_could_not_be_verified():
    text = sheets_sync._plain_english_what_went_wrong(
        _job("UNVERIFIED", last_error="no confirmation from the carrier portal")
    )
    assert "could not be independently verified" in text.lower()
    assert "check it manually" in text.lower()


def test_paused_and_needs_skill_explain_why_stuck():
    paused = sheets_sync._plain_english_what_went_wrong(
        _job("PAUSED", last_error="paused while the carrier portal was down")
    )
    assert "paused" in paused.lower()
    assert "carrier portal was down" in paused

    stuck = sheets_sync._plain_english_what_went_wrong(
        _job("NEEDS_SKILL", payload={"task": "x", "skill": "carrier.acme_quoting"})
    )
    assert "skill" in stuck.lower()
    assert "carrier.acme_quoting" in stuck


def test_terminal_good_states_return_empty_string():
    for status in ("COMPLETE", "PENDING", "RUNNING", "VERIFYING", "RETRY_WAIT", "WAITING"):
        assert sheets_sync._plain_english_what_went_wrong(_job(status)) == ""


def test_traceback_and_paths_are_stripped():
    raw = (
        'Traceback (most recent call last):\n'
        '  File "/opt/robie/robie_job_engine/browser_read.py", line 36, in run\n'
        '    raise TimeoutError("portal did not respond")\n'
        'TimeoutError: portal did not respond, see https://carrier.example.com/status'
    )
    text = sheets_sync._plain_english_what_went_wrong(_job("FAILED", last_error=raw))
    assert "Traceback" not in text
    assert "/opt/robie" not in text
    assert "https://carrier.example.com/status" not in text
    assert "portal did not respond" in text


def test_awaiting_human_input_has_friendly_status_label():
    row = sheets_sync._friendly_jobs([_job("AWAITING_HUMAN_INPUT")], limit=1)[0]
    assert row[8] == "Waiting on you — needs your input"


def _mock_sheet_api(existing_values):
    values = mock.Mock()
    get_request = mock.Mock()
    get_request.execute.return_value = {"values": existing_values}
    values.get.return_value = get_request
    batch_request = mock.Mock()
    batch_request.execute.return_value = {}
    values.batchUpdate.return_value = batch_request
    service = mock.Mock()
    service.spreadsheets.return_value.values.return_value = values
    return service, values


def test_upsert_job_rows_writes_what_went_wrong_column():
    failed = _job("FAILED", last_error="carrier portal timed out")
    failed["id"] = "job-failed"
    complete = _job("COMPLETE")
    complete["id"] = "job-complete"

    ops = mock.Mock()
    ops.dashboard_rows.return_value = {"jobs": [failed, complete]}
    service, values = _mock_sheet_api(existing_values=[])

    with mock.patch.object(sheets_sync, "OperationsStore", return_value=ops), \
         mock.patch.object(sheets_sync, "_service", return_value=service):
        result = sheets_sync.upsert_job_rows("dummy.db", "sheet-id", ["job-failed", "job-complete"])

    assert result == {"jobs": 2, "updated": 0, "appended": 2}
    body = values.batchUpdate.call_args.kwargs["body"]
    ranges = {write["range"]: write["values"] for write in body["data"]}

    # Existing positional ranges are untouched.
    assert ranges["Jobs!A6:Y6"]
    assert ranges["Jobs!Z6:AB6"]
    assert ranges["Jobs!AD6:AE6"]
    assert ranges["Jobs!AG6"]

    # New plain-English column, one per job row.
    assert "Jobs!AH6" in ranges
    assert "Jobs!AH7" in ranges
    assert "failed" in ranges["Jobs!AH6"][0][0].lower()
    assert "carrier portal timed out" in ranges["Jobs!AH6"][0][0]
    assert ranges["Jobs!AH7"] == [[""]]
