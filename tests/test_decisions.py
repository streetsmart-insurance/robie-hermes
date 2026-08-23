from __future__ import annotations

from datetime import datetime, timedelta, timezone

from robie_job_engine.decisions import (
    DecisionStore,
    decision_card,
    parse_google_chat_interaction,
    resolve_google_chat_interaction,
)
from robie_job_engine.models import JobStatus
from robie_job_engine.store import JobStore


def _decision(tmp_path):
    db = str(tmp_path / "jobs.db")
    jobs = JobStore(db)
    job = jobs.create_job("test", {"account": "123"}, idempotency_key="test-decision")
    decisions = DecisionStore(db)
    decision = decisions.create(
        job_id=job["id"],
        checkpoint_id="before-send",
        prompt="Send the carrier email?",
        choices=[
            {"label": "Approve", "value": "approve"},
            {"label": "Approve session", "value": "approve_session"},
            {"label": "Deny", "value": "deny"},
            {"label": "Something else", "value": "__other__"},
        ],
        authorized_users=["Carlo@StreetSmart.Insurance"],
        session_scope="space:abc:ezlynx-write",
    )
    return jobs, decisions, job, decision


def test_approval_pauses_then_resumes_exact_job(tmp_path):
    jobs, decisions, job, decision = _decision(tmp_path)
    assert jobs.get_job(job["id"])["status"] == JobStatus.PAUSED
    result = decisions.resolve(decision["id"], actor="carlo@streetsmart.insurance", choice="approve")
    assert result.resumed is True
    assert jobs.get_job(job["id"])["status"] == JobStatus.PENDING
    checkpoint = jobs.get_checkpoint(job["id"], f"decision:{decision['id']}")
    assert checkpoint["choice"] == "approve"


def test_duplicate_click_is_idempotent_and_conflicting_click_is_rejected(tmp_path):
    _, decisions, _, decision = _decision(tmp_path)
    first = decisions.resolve(decision["id"], actor="carlo@streetsmart.insurance", choice="approve")
    duplicate = decisions.resolve(decision["id"], actor="carlo@streetsmart.insurance", choice="approve")
    conflict = decisions.resolve(decision["id"], actor="carlo@streetsmart.insurance", choice="deny")
    assert first.status == duplicate.status == "RESOLVED"
    assert duplicate.resumed is False
    assert conflict.status == "CONFLICT"


def test_unauthorized_and_expired_clicks_do_not_resume(tmp_path):
    jobs, decisions, job, decision = _decision(tmp_path)
    unauthorized = decisions.resolve(decision["id"], actor="intruder@example.com", choice="approve")
    assert unauthorized.status == "UNAUTHORIZED"
    assert jobs.get_job(job["id"])["status"] == JobStatus.PAUSED
    with decisions._connect() as conn:
        conn.execute(
            "UPDATE decisions SET expires_at=? WHERE id=?",
            ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), decision["id"]),
        )
    expired = decisions.resolve(decision["id"], actor="carlo@streetsmart.insurance", choice="approve")
    assert expired.status == "EXPIRED"
    assert jobs.get_job(job["id"])["status"] == JobStatus.PAUSED


def test_deny_stops_job_and_other_requires_text(tmp_path):
    jobs, decisions, job, decision = _decision(tmp_path)
    needs_text = decisions.resolve(decision["id"], actor="carlo@streetsmart.insurance", choice="__other__")
    assert needs_text.status == "NEEDS_TEXT"
    denied = decisions.resolve(decision["id"], actor="carlo@streetsmart.insurance", choice="deny")
    assert denied.status == "RESOLVED"
    assert jobs.get_job(job["id"])["status"] == JobStatus.FAILED


def test_session_approval_and_card_payload(tmp_path):
    _, decisions, _, decision = _decision(tmp_path)
    decisions.resolve(
        decision["id"],
        actor="carlo@streetsmart.insurance",
        choice="approve_session",
        session_scope="space:abc:ezlynx-write",
    )
    assert decisions.session_is_approved("space:abc:ezlynx-write", "carlo@streetsmart.insurance")
    card = decision_card(decision)
    widgets = card["sections"][0]["widgets"]
    labels = [button["text"] for widget in widgets if widget["type"] == "buttons" for button in widget["buttons"]]
    assert labels == ["Approve", "Approve session", "Deny", "Submit"]
    assert labels.count("Something else") == 0
    assert any(widget["type"] == "text_input" for widget in widgets)


def test_dynamic_google_chat_choice_and_free_text_are_audited(tmp_path):
    jobs, _, job, decision = _decision(tmp_path)
    payload = {
        "common": {
            "invokedFunction": "robie_decision",
            "parameters": {
                "decision_id": decision["id"],
                "choice": "__other__",
                "session_scope": "space:abc:ezlynx-write",
            },
            "formInputs": {
                "custom_text": {"stringInputs": {"value": ["Use the active Renewal Manual discussion"]}}
            },
        },
        "user": {"email": "carlo@streetsmart.insurance"},
    }
    interaction = parse_google_chat_interaction(payload)
    assert interaction.custom_text == "Use the active Renewal Manual discussion"
    result = resolve_google_chat_interaction(str(tmp_path / "jobs.db"), payload)
    assert result.status == "RESOLVED" and result.resumed is True
    checkpoint = jobs.get_checkpoint(job["id"], f"decision:{decision['id']}")
    assert checkpoint["custom_text"] == "Use the active Renewal Manual discussion"
