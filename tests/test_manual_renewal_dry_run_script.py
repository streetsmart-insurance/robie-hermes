"""Tests for evidence derived by the RH-023 Test rehearsal driver."""

from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "run-test-manual-renewal-dry-run.py"
SPEC = importlib.util.spec_from_file_location("manual_renewal_dry_run", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_safe_result_has_no_external_effects():
    detail = {
        "outcomes": [{
            "actions_taken": ["cancellation_check", "initial outreach_email_intent_recorded"],
            "evidence": {
                "email_intent": {"to": "underwriter@example.invalid"},
                "escalation_note": {"posted": False},
                "urgent_csr_task": {"created": False},
                "voice_call": {"call_placed": False},
            },
        }]
    }
    assert MODULE.find_external_effects(detail) == []


def test_external_effect_evidence_is_reported_with_paths():
    detail = {
        "outcomes": [{
            "actions_taken": ["initial outreach_email_sent", "carrier_voice_call_placed"],
            "evidence": {
                "email": {"email_message_id": "message-1"},
                "note": {"posted": True, "ezlynx_note_id": "note-1"},
                "task": {"created": True, "task_id": "task-1"},
                "voice_call": {"call_placed": True},
            },
        }]
    }
    effects = MODULE.find_external_effects(detail)
    assert len(effects) == 8
    assert any("email_message_id" in path for path in effects)
    assert any("ezlynx_note_id" in path for path in effects)
    assert any("call_placed" in path for path in effects)


def test_summary_requires_observed_host_completion_and_dry_run(monkeypatch):
    monkeypatch.setattr(MODULE.socket, "gethostname", lambda: "hermes-test-01")
    job = {
        "action_type": "manual_renewal_verification",
        "status": "completed",
        "payload": {
            "dry_run": True,
            "authorized_actions": ["read_report_rows", "check_cancellation"],
        },
        "result": {
            "detail": {
                "dry_run": True,
                "rows_fetched": 4,
                "rows_in_window": 2,
                "rows_filtered": 2,
                "outcomes": [],
            }
        },
    }
    evidence = MODULE.build_summary("job-1", job)
    assert evidence["read_only_verified"] is True
    assert evidence["rehearsal_verified"] is True
    assert evidence["safety_violations"] == []
    assert evidence["execution_violations"] == []

    job["status"] = "needs_clarification"
    evidence = MODULE.build_summary("job-1", job)
    assert evidence["read_only_verified"] is True
    assert evidence["rehearsal_verified"] is False
    assert evidence["execution_violations"]


def test_summary_fails_closed_on_missing_or_mutating_evidence(monkeypatch):
    monkeypatch.setattr(MODULE.socket, "gethostname", lambda: "wrong-host")
    job = {
        "action_type": "manual_renewal_verification",
        "status": "completed",
        "payload": {
            "dry_run": False,
            "authorized_actions": ["send_underwriter_email"],
        },
        "result": {
            "detail": {
                "dry_run": False,
                "rows_fetched": 1,
                "outcomes": [{"evidence": {"email_message_id": "message-1"}}],
            }
        },
    }
    evidence = MODULE.build_summary("job-unsafe", job)
    assert evidence["read_only_verified"] is False
    assert evidence["rehearsal_verified"] is False
    assert len(evidence["safety_violations"]) == 4
    assert evidence["external_effects"]
