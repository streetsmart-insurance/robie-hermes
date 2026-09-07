"""Tests for the request_routing safety net on EZLynx requests that read as a
write but do not match any bounded action pattern (see job 6f0467db and the
2026-09 evidence-gap discussion).

Before this change, a request like "please set up a commercial auto policy in
ezlynx for this applicant" fell through to hermes.plain_english/
hermes.google_chat_task -- the free-form Hermes/cua-driver path, which has no
Worker/Verifier evidence contract and can report COMPLETE on the model's
say-so alone. These tests pin down that such requests are now held for a
human (NEEDS_CLARIFICATION) instead of running unverified, while requests
that already match a specific bounded pattern, or that don't mention EZLynx
at all, or that are pure reads, are unaffected.
"""

from __future__ import annotations

from robie_job_engine.models import JobStatus
from robie_job_engine.request_routing import classify_request


def test_unbounded_ezlynx_write_is_held_for_a_human():
    result = classify_request(
        "please set up a new commercial auto policy in ezlynx for this applicant"
    )
    assert result.action_type == "hermes.needs_clarification"
    assert result.hold_status == JobStatus.NEEDS_CLARIFICATION.value


def test_known_bounded_ezlynx_patterns_still_route_normally():
    reassign = classify_request("please reassign this ezlynx account to Jake")
    assert reassign.action_type == "ezlynx.reassign"
    assert reassign.hold_status is None

    move_doc = classify_request("move this document in ezlynx to the closed folder")
    assert move_doc.action_type == "ezlynx.move_document"
    assert move_doc.hold_status is None


def test_ezlynx_read_only_request_is_not_held():
    result = classify_request("can you just look up the page for this ezlynx account")
    assert result.action_type == "browser.read"
    assert result.hold_status is None


def test_non_ezlynx_write_shaped_request_is_unaffected():
    # "create"/"generate" are in _MUTATION_WORDS, but this isn't an EZLynx
    # request, so it must not be swept into the new hold -- it still reaches
    # the ordinary plain-english/general chat classification.
    result = classify_request("please create a summary of this week's tasks")
    assert result.action_type != "hermes.needs_clarification"


def test_explicit_prohibition_does_not_trigger_the_hold():
    result = classify_request(
        "do not create anything in ezlynx, just tell me the current status"
    )
    assert result.action_type != "hermes.needs_clarification"
