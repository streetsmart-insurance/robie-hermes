import pytest
from robie_job_engine.request_routing import (
    BOUNDED_ENGINE_ACTIONS, WORKER_FOR_ACTION,
)
from robie_job_engine.engine import resolve_worker_name


def test_every_bounded_action_has_a_registry_worker():
    missing = sorted(a for a in BOUNDED_ENGINE_ACTIONS
                     if a not in WORKER_FOR_ACTION)
    assert not missing, f"bounded actions with no worker mapping: {missing}"


@pytest.mark.parametrize("action", sorted(BOUNDED_ENGINE_ACTIONS))
def test_bounded_action_ignores_a_wrong_payload_worker(action):
    # A bad payload name must refuse, never select a different worker.
    assert resolve_worker_name(action, {"worker": "definitely-not-a-worker"}) is None


@pytest.mark.parametrize("action", sorted(BOUNDED_ENGINE_ACTIONS))
def test_bounded_action_never_falls_through_to_cua(action):
    # Missing worker key must resolve from the registry, not default to
    # the freeform worker.
    resolved = resolve_worker_name(action, {})
    assert resolved == WORKER_FOR_ACTION[action]
    assert resolved != "hermes-cua" or WORKER_FOR_ACTION[action] == "hermes-cua"


def test_real_payload_shapes_that_failed_in_production():
    # The exact shapes from the 2026-09-11 drain report.
    assert resolve_worker_name("audit_verification", {"worker": "audit"}) is None
    assert resolve_worker_name("mortgagee_verification", {"worker": "mortgagee"}) is None
    assert resolve_worker_name("audit_verification", {}) == "audit-verification"
    assert resolve_worker_name("mortgagee_verification", {}) == "mortgagee-verification"
