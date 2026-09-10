"""Lock the carrier Robie Call to process_due_followups — no second path."""

from pathlib import Path


RUNNER = Path("src/scheduler/daily_runner.py").read_text()
TRACKER = Path("src/email_outreach/thread_tracker.py").read_text()


def _cycle_body() -> str:
    start = RUNNER.index("async def run_daily_cycle")
    end = RUNNER.index("def print_summary_dashboard")
    return RUNNER[start:end]


def test_no_parallel_daily_runner_voice_scan():
    body = _cycle_body()
    assert "process_carrier_voice_cadence" not in body
    assert "process_due_followups" in body
    assert "process_inbound_call_requests" in body
    assert "Step 5c:" not in body
    assert "client_outreach" not in body
    assert "contact underwriter directly" in body


def test_voice_hook_is_the_followup_giveup_branch():
    assert "def process_due_followups" in TRACKER
    assert "_place_one_carrier_voice" in TRACKER
    assert "place_one_carrier_voice" in TRACKER
    assert "VoiceCallDispatcher" in TRACKER or "place_one_carrier_voice" in TRACKER
    assert "URGENT: Review / Call Carrier" in TRACKER
    assert "carrier_voice_after_followups" in TRACKER
    i_voice = TRACKER.index("_place_one_carrier_voice")
    i_escalate = TRACKER.index("URGENT: Review / Call Carrier")
    assert i_voice < i_escalate
