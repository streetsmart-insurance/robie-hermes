"""The receipt must refuse to say PASS without re-fetched evidence."""

import time
import pytest

from robie_guard.receipt import GATES, JobReceipt


def _full(workflow="manual_renewal"):
    r = JobReceipt(job_id="j1", workflow=workflow,
                   applicant_id="102351931", policy_number="GAT0003099-01")
    for gate in ("G1_SUBJECT", "G2_SOURCE", "G3_FILED", "G4_NOTED", "G5_HANDOFF", "G6_WRITE"):
        r.gate(gate, quoted_value="literal value from EZLynx", source="ezlynx_api:refetch")
    r.mark_acted()
    time.sleep(0.01)
    r.gate("G7_REVERIFIED", quoted_value="pending RWL x1, premium $12,480.00",
           source="ezlynx_api:get_applicant_policies")
    return r


def test_empty_receipt_is_unverified_not_pass():
    outcome, problems = JobReceipt(job_id="j", workflow="manual_renewal").outcome()
    assert outcome == "UNVERIFIED"
    assert len(problems) == len(GATES)


def test_fully_evidenced_receipt_passes():
    outcome, problems = _full().outcome()
    assert outcome == "PASS", problems


@pytest.mark.parametrize("gate", list(GATES))
def test_any_missing_gate_downgrades_to_unverified(gate):
    r = _full()
    del r.gates[gate]
    outcome, problems = r.outcome()
    assert outcome == "UNVERIFIED"
    assert any(gate in p for p in problems)


def test_agent_self_report_is_not_evidence():
    r = _full()
    r.gates["G6_WRITE"].source = "memory"
    outcome, problems = r.outcome()
    assert outcome == "UNVERIFIED"
    assert any("agent's own claim" in p for p in problems)


def test_recheck_must_postdate_the_write():
    r = _full()
    r.gates["G7_REVERIFIED"].refetched_at = "2000-01-01T00:00:00+00:00"
    outcome, problems = r.outcome()
    assert outcome == "UNVERIFIED"
    assert any("stale" in p for p in problems)


def test_empty_quoted_value_is_not_evidence():
    r = _full()
    r.gates["G4_NOTED"].quoted_value = "   "
    assert r.outcome()[0] == "UNVERIFIED"


def test_cardinal_violation_is_catastrophe():
    r = _full()
    r.cardinal_violation = {"rule_id": "CR-1", "reason": "policy vanished"}
    assert r.outcome()[0] == "CATASTROPHE"


def test_blocked_beats_missing_gates():
    r = JobReceipt(job_id="j", workflow="audit_verification")
    r.block("carrier portal 2FA never arrived")
    assert r.outcome()[0] == "BLOCKED"


def test_skipping_a_gate_needs_a_real_reason():
    r = _full()
    with pytest.raises(ValueError):
        r.skip("G5_HANDOFF", "n/a")
    r.skip("G5_HANDOFF", "no human action required; nothing was handed off")
    assert r.outcome()[0] == "PASS"


def test_unknown_workflow_is_flagged_in_the_receipt():
    r = _full(workflow="something_new")
    assert r.to_dict()["g6_definition"] == "UNDEFINED WORKFLOW"


@pytest.mark.parametrize("workflow", [
    "manual_renewal", "audit_verification", "policy_change", "mortgagee_verification",
])
def test_all_four_workflows_have_a_g6_definition(workflow):
    assert "UNDEFINED" not in _full(workflow).to_dict()["g6_definition"]


def test_receipt_writes_to_disk(tmp_path):
    path = _full().write(tmp_path)
    assert path.exists()
    assert "PASS" in path.read_text()
