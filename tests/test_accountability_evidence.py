from datetime import datetime
from zoneinfo import ZoneInfo

from robie_job_engine.accountability_evidence import parse_activity_evidence, reconcile_service_calls
from robie_job_engine.accountability_cli import _calls_from_rows
from robie_job_engine.ezlynx_reports_5 import validate_reports_5_config
from robie_job_engine.productivity import RingCentralCall


EASTERN = ZoneInfo("America/New_York")


def _call(call_id, when, result, employee="Example CSR", *, direction="Inbound", source="2025550101"):
    return RingCentralCall(
        call_id=call_id,
        direction=direction,
        from_number=source if direction == "Inbound" else "2025550199",
        to_number="2025550199" if direction == "Inbound" else source,
        result=result,
        duration_seconds=30 if result == "Call connected" else 0,
        start_time=datetime.fromisoformat(when).replace(tzinfo=EASTERN),
        extension="100",
        employee_name=employee,
        queue_name="Service Queue",
        answered_by=employee if result == "Call connected" else "",
    )


def test_parent_call_answered_on_child_leg_is_not_a_failure():
    calls = [
        _call("parent-1", "2026-08-31T10:00:00", "Missed"),
        _call("parent-1", "2026-08-31T10:00:10", "Call connected", "Teammate"),
    ]
    assert reconcile_service_calls(calls, as_of=datetime(2026, 8, 31, 11, tzinfo=EASTERN)) == []


def test_client_redial_is_not_classified_as_callback_success():
    calls = [
        _call("parent-1", "2026-08-31T10:00:00", "Voicemail"),
        _call("parent-2", "2026-08-31T10:40:00", "Call connected", "Teammate"),
    ]
    item = reconcile_service_calls(calls, as_of=datetime(2026, 8, 31, 11, tzinfo=EASTERN))[0]
    assert item.resolution == "CLIENT_REACHED_AGENCY_LATER"
    assert item.caller_had_to_redial is True


def test_automated_text_does_not_count_as_human_response():
    events = parse_activity_evidence(
        "Activity ID,Activity Date,Activity Type,Direction,Phone,Created By,Source,Producer,CSR\n"
        "A1,08/31/2026 10:10 AM,Text Sent,Outbound,202-555-0101,System,Automated Workflow,Producer A,CSR A\n"
    )
    item = reconcile_service_calls(
        [_call("parent-1", "2026-08-31T10:00:00", "Voicemail")],
        as_of=datetime(2026, 8, 31, 11, tzinfo=EASTERN),
        activity_events=events,
    )[0]
    assert item.status == "UNRESOLVED"
    assert item.automated_text_detected is True


def test_after_hours_call_waits_until_next_business_day_sla():
    calls = [_call("parent-1", "2026-08-31T18:00:00", "Voicemail")]
    pending = reconcile_service_calls(calls, as_of=datetime(2026, 9, 1, 9, 20, tzinfo=EASTERN))[0]
    overdue = reconcile_service_calls(calls, as_of=datetime(2026, 9, 1, 9, 31, tzinfo=EASTERN))[0]
    assert pending.status == "PENDING_SLA"
    assert pending.business_hours_flag is False
    assert overdue.status == "UNRESOLVED"


def test_detailed_ringcentral_rows_inherit_parent_call_identity():
    calls, errors = _calls_from_rows([
        {"Type": "Voice", "Direction": "Incoming", "From": "202-555-0101", "To": "Main", "Date": "Mon 08/31/2026", "Time": "10:00 AM", "Action Result": "Accepted"},
        {"Type": "", "Direction": "", "Extension": "100", "Name": "Example CSR", "Date": "Mon 08/31/2026", "Time": "10:00 AM", "Action Result": "Call connected"},
    ])
    assert not errors
    assert len(calls) == 2
    assert calls[0].call_id == calls[1].call_id
    assert calls[1].direction == "Inbound"


def test_reports_5_configuration_rejects_legacy_fallback():
    try:
        validate_reports_5_config({"enabled": True, "version": "5.0", "legacy_saved_reports_enabled": True})
    except ValueError as exc:
        assert "legacy" in str(exc)
    else:
        raise AssertionError("legacy report fallback must fail closed")
