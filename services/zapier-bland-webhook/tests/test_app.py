"""Tests for the webhook payload contract (app.py _parse_payload).

The freeform flow's primary instruction is `note_body`: the Zapier
"New Note" trigger's note text. `note_text` is accepted as an alias and
normalized to `note_body`. Uses Flask request contexts — no dispatch
threads are spawned here.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, _parse_payload


def test_parse_payload_note_body_form():
    with app.test_request_context(
        "/webhook", method="POST",
        data={"applicant_id": "1", "note_body": "call about the renewal"},
    ):
        p = _parse_payload()
        assert p["note_body"] == "call about the renewal"
        assert "note_text" not in p


def test_parse_payload_note_text_alias_form():
    with app.test_request_context(
        "/webhook", method="POST",
        data={"applicant_id": "1", "note_text": "call about the renewal"},
    ):
        p = _parse_payload()
        assert p["note_body"] == "call about the renewal"
        assert "note_text" not in p


def test_parse_payload_note_body_wins_over_alias():
    with app.test_request_context(
        "/webhook", method="POST",
        data={"applicant_id": "1", "note_body": "real", "note_text": "alias"},
    ):
        p = _parse_payload()
        assert p["note_body"] == "real"
        assert "note_text" not in p


def test_parse_payload_note_body_json():
    with app.test_request_context(
        "/webhook", method="POST",
        json={"applicant_id": "1", "note_body": "call about the renewal"},
    ):
        p = _parse_payload()
        assert p["note_body"] == "call about the renewal"


def test_parse_payload_note_text_alias_json():
    with app.test_request_context(
        "/webhook", method="POST",
        json={"applicant_id": "1", "note_text": "call about the renewal"},
    ):
        p = _parse_payload()
        assert p["note_body"] == "call about the renewal"


def test_parse_payload_without_note_body():
    with app.test_request_context(
        "/webhook", method="POST",
        data={"applicant_id": "1", "campaign_id": "robie-call"},
    ):
        p = _parse_payload()
        assert p.get("note_body", "") == ""
