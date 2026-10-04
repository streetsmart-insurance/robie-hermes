"""Live Task Check-In labels match the full normalized text."""
from __future__ import annotations

from robie_job_engine.call_pickup import classify_call_request


def test_live_lead_label_and_its_aliases_are_the_lead_script():
    for label in (
        "Robie lead follow-up",
        "Robie Lead Follow Up",
        "ROBIE LEAD FOLLOW-UP",
        "Robie_lead_follow_up",
        "Robie Call Follow Up",
        "robie-call-follow-up",
    ):
        pickup = classify_call_request(label, "")
        assert pickup.action == "workflow", label
        assert pickup.workflow_id == "lead_follow_up", label


def test_robie_call_is_free_form_and_does_not_prefix_match():
    assert classify_call_request("Robie Call", "").action == "freeform"
    assert classify_call_request("robie_call", "").action == "freeform"
    follow = classify_call_request("Robie Call Follow Up", "call the lead")
    assert follow.action == "workflow"
    assert follow.workflow_id == "lead_follow_up"
    assert classify_call_request("Robie Caller", "").action == "not_labeled"
    assert classify_call_request("Robie Call Follow Up please", "").action == "not_labeled"
    assert classify_call_request("Robie Call extra", "").action == "not_labeled"


def test_note_text_does_not_dial():
    assert classify_call_request(
        "", "Please do a Robie Call Follow Up today.",
    ).action == "not_labeled"
    assert classify_call_request(
        "", "Robie Call the client about the quote.",
    ).action == "not_labeled"
    assert classify_call_request(
        "", "Do not call or contact anyone.",
    ).action == "not_labeled"
    assert classify_call_request("Robie audit", "Please call the client.").action == "not_labeled"


def test_lead_label_wins_when_both_labels_are_present():
    pickup = classify_call_request("Robie Call, Robie lead follow-up", "")
    assert pickup.action == "workflow"
    assert pickup.workflow_id == "lead_follow_up"
