"""Tests for BoundedEzlynxPolicySetupWorker -- the missing evidence-required
worker for ``ezlynx.policy_setup`` (see job 6f0467db).

These tests do not open a live browser. ``EzlynxPolicySetupPage.setup_policy_by_lob``
is currently a deliberate draft-mode stub (see its own docstring in
``robie_job_engine/ezlynx_policy_setup.py``) that always returns
``success=False`` before touching ``self.page`` at all, so a bare object
stands in for the Playwright page here. That stub behavior is exactly what
these tests pin down: this worker must never report a completed policy setup
while that stub is in place, and it must still enforce the same required-field
and forbidden-applicant checks the other bounded EZLynx actions already have.
"""

from __future__ import annotations

import pytest

from robie_job_engine.ezlynx import BoundedEzlynxPolicySetupWorker, EzlynxDestinationVerifier
from robie_job_engine.models import JobStatus


class _FakePage:
    """Stands in for a Playwright ``page``. The current stub orchestrator
    never calls into it, so it does not need to do anything."""


class _FakePolicySetupBrowser:
    def __init__(self) -> None:
        self.opened = 0

    def open_page(self):
        self.opened += 1
        return _FakePage()


class _FakeReadback:
    """Minimal EzlynxReadback fake: nothing has ever been written."""

    def api_state(self, action_type, expected):
        return None

    def fresh_page_state(self, action_type, expected):
        return {}


ALLOWED_TEST_APPLICANT_ID = "220250093"  # robie_job_engine.ezlynx_write_scope


def _job(**payload_overrides):
    payload = {
        "applicant_id": ALLOWED_TEST_APPLICANT_ID,
        "lob": "commercial_auto",
        "policy_number": "CA-0001",
    }
    payload.update(payload_overrides)
    return {"id": "job-1", "action_type": "ezlynx.policy_setup", "payload": payload}


def test_missing_required_field_holds_for_clarification():
    worker = BoundedEzlynxPolicySetupWorker(_FakePolicySetupBrowser())
    job = _job(policy_number="")
    result = worker.perform(job, idempotency_key="k1")
    assert result.succeeded is False
    assert result.hold_status == JobStatus.NEEDS_CLARIFICATION
    assert "missing fields" in (result.error or "")


def test_stub_orchestrator_never_reports_complete():
    """Pins the current safety stub: a fully-specified request still cannot
    complete, because EzlynxPolicySetupPage.setup_policy_by_lob refuses the
    legacy write path. This worker must surface that refusal, not paper over
    it."""
    browser = _FakePolicySetupBrowser()
    worker = BoundedEzlynxPolicySetupWorker(browser)
    job = _job()
    result = worker.perform(job, idempotency_key="k1")
    assert result.succeeded is False
    assert result.destination == {}
    assert result.hold_status == JobStatus.NEEDS_CLARIFICATION
    assert "does not authorize consequential writes" in (result.error or "")
    # The worker did reach the page-object layer (not a short-circuit before
    # ever trying) -- open_page was called once.
    assert browser.opened == 1


def test_disallowed_applicant_fails_closed_before_opening_a_page():
    browser = _FakePolicySetupBrowser()
    worker = BoundedEzlynxPolicySetupWorker(browser)
    job = _job(applicant_id="221398001")  # PAWIVA -- never a test target
    result = worker.perform(job, idempotency_key="k1")
    assert result.succeeded is False
    assert result.hold_status == JobStatus.FAILED
    # Refused before ever touching a browser page.
    assert browser.opened == 0


def test_destination_verifier_generalizes_to_policy_setup():
    """EzlynxDestinationVerifier already works for policy_setup once
    _ezlynx_destination knows the shape -- no verifier-specific code needed."""
    verifier = EzlynxDestinationVerifier(_FakeReadback())
    job = _job()
    result = verifier.verify(job, {"destination": {
        "resource_id": "12345:CA-0001",
        "applicant_id": "12345",
        "lob": "commercial_auto",
        "policy_number": "CA-0001",
    }})
    # Nothing has actually been written (readback is empty), so this must not
    # verify -- and it must be retryable/WAITING, never a silent pass.
    assert result.verified is False
    assert result.hold_status == JobStatus.WAITING
