"""Every bounded action must have a verifier on the engine the SCHEDULER builds.

engine._verify does `self.verifiers.get(job["action_type"])` and terminates the
job UNVERIFIED with "no independent verifier registered" when there is none. A
bounded action with no verifier can therefore NEVER complete, however well its
worker performed.

Coverage is NOT the same in both environments. build_runtime_engine injects a
MemoryProposalDestination when ROBIE_ENV=TEST, which registers
carrier.proposal — and Production cannot have it, because
forbid_memory_destination refuses memory wiring there. So a Test run can be
green on an action that is unverifiable in Production. These tests pin the
environment explicitly rather than inheriting whatever the runner happens to
set, and assert the Test-only surplus is declared.

Stdlib unittest: runs without pytest.
"""
import os
import unittest
from unittest import mock

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _durable_tmp import durable_tmpdir
from robie_job_engine.request_routing import BOUNDED_ENGINE_ACTIONS, WORKER_FOR_ACTION
from robie_job_engine.store import JobStore
from robie_job_engine.test_runtime import build_runtime_engine

# Bounded actions with no verifier on the PRODUCTION scheduler path. Each needs
# a production read port that does not exist yet. Do NOT close these with a
# test double: a verifier holding a fake read port verifies nothing and is
# worse than one that refuses.
PRODUCTION_GAPS = {
    "carrier.proposal": "no production ProposalDestination; ROBIE_ENV=TEST injects a "
                        "MemoryProposalDestination, Production forbids memory wiring",
    "ezlynx.reassign": "no production EzlynxReadback; EzlynxApiClientReadPort exposes "
                       "policy_by_number/documents_for_applicant, not api_state/fresh_page_state",
    "ezlynx.move_document": "same EzlynxReadback gap as ezlynx.reassign",
    "ezlynx.apply_label": "same EzlynxReadback gap as ezlynx.reassign",
}

# Covered in Test but NOT in Production. Anything here is a trap: a green Test
# milestone says nothing about these actions on the live box.
TEST_ONLY_COVERAGE = {"carrier.proposal"}


def _verifiers_for(testcase, env: str) -> set[str]:
    with mock.patch.dict(os.environ, {"ROBIE_ENV": env}):
        store = JobStore(str(durable_tmpdir(testcase) / "jobs.db"))
        return set(build_runtime_engine(store).verifiers)


class BoundedActionVerifierCoverage(unittest.TestCase):
    def test_every_bounded_action_has_a_worker_mapping(self):
        missing = sorted(a for a in BOUNDED_ENGINE_ACTIONS if a not in WORKER_FOR_ACTION)
        self.assertEqual(missing, [], f"bounded actions with no worker mapping: {missing}")

    def test_production_coverage_has_no_undeclared_gap(self):
        covered = _verifiers_for(self, "PRODUCTION")
        undeclared = sorted(
            a for a in BOUNDED_ENGINE_ACTIONS if a not in covered and a not in PRODUCTION_GAPS
        )
        self.assertEqual(
            undeclared, [],
            "bounded actions with no verifier on the PRODUCTION scheduler path and no "
            f"declared gap: {undeclared}. Register a verifier or add it to "
            "PRODUCTION_GAPS with a reason.",
        )

    def test_declared_gaps_are_still_gaps_in_production(self):
        # Stops PRODUCTION_GAPS rotting into a list of things already fixed,
        # which would hide the next real gap.
        covered = _verifiers_for(self, "PRODUCTION")
        fixed = sorted(a for a in PRODUCTION_GAPS if a in covered)
        self.assertEqual(
            fixed, [], f"no longer gaps, remove from PRODUCTION_GAPS: {fixed}"
        )

    def test_declared_gaps_are_all_actually_bounded(self):
        stale = sorted(a for a in PRODUCTION_GAPS if a not in BOUNDED_ENGINE_ACTIONS)
        self.assertEqual(stale, [], f"PRODUCTION_GAPS names non-bounded actions: {stale}")

    def test_test_only_coverage_is_declared(self):
        # THE TRAP THIS GUARDS: an action verified in Test and unverified in
        # Production makes a green Test milestone misleading. Any such action
        # must be listed, so nobody reads a Test PASS as covering it.
        in_test = _verifiers_for(self, "TEST")
        in_prod = _verifiers_for(self, "PRODUCTION")
        surplus = sorted((in_test - in_prod) & set(BOUNDED_ENGINE_ACTIONS))
        self.assertEqual(
            surplus, sorted(TEST_ONLY_COVERAGE),
            "actions verified in Test but not Production changed. A Test PASS does not "
            f"cover these. Expected {sorted(TEST_ONLY_COVERAGE)}, found {surplus}.",
        )

    def test_browser_read_registers_in_both_environments(self):
        # Regression: the scheduler passes no browser_port, so this verifier
        # class existed but never registered.
        for env in ("PRODUCTION", "TEST"):
            self.assertIn("browser.read", _verifiers_for(self, env), f"missing in {env}")

    def test_appsheet_actions_are_no_longer_bounded(self):
        # Zero jobs all-time in Production and no verifier class ever written.
        for action in ("appsheet.smart_reward", "appsheet.qa_audit"):
            self.assertNotIn(action, BOUNDED_ENGINE_ACTIONS)

    def test_report_coverage(self):
        in_prod = _verifiers_for(self, "PRODUCTION")
        in_test = _verifiers_for(self, "TEST")
        print(f"\n  bounded actions: {len(BOUNDED_ENGINE_ACTIONS)}")
        for a in sorted(BOUNDED_ENGINE_ACTIONS):
            p = "ok " if a in in_prod else "GAP"
            t = "ok " if a in in_test else "GAP"
            print(f"    prod {p}  test {t}   {a}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
