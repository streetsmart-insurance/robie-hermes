"""Lock the carrier Robie Call to the live daily_runner insertion point."""

from pathlib import Path


RUNNER = Path("src/scheduler/daily_runner.py").read_text()


def _cycle_body() -> str:
    start = RUNNER.index("async def run_daily_cycle")
    end = RUNNER.index("def print_summary_dashboard")
    return RUNNER[start:end]


def test_voice_hook_sits_after_step4_followups_before_step5b_and_step6():
    body = _cycle_body()
    i4 = body.index("process_due_followups")
    i_voice = body.index("process_carrier_voice_cadence")
    i5b = body.index("process_inbound_call_requests")
    i6 = body.index("20-25 Day CSR Escalation")
    assert i4 < i_voice < i5b < i6


def test_no_parallel_client_call_step_5c():
    body = _cycle_body()
    assert "Step 5c" not in body
    assert "client_outreach" not in body
    assert "process_inbound_call_requests" in body
    assert "contact underwriter directly" in body


def test_sacred_daily_cycle_still_owns_the_hook():
    assert "from src.voice.renewal_cadence import process_carrier_voice_cadence" in RUNNER
    assert "reference_date=ref_date" in RUNNER
