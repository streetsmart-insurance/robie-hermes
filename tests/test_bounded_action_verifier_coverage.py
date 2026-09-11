"""Every bounded action must have a verifier on the engine the SCHEDULER builds.

engine._verify does `self.verifiers.get(job["action_type"])` and terminates
the job UNVERIFIED with "no independent verifier registered" when there is
none. A bounded action with no verifier can therefore NEVER complete, however
well its worker performed — and until now nothing failed when that happened.

The scheduler calls build_runtime_engine(store) with no optional arguments,
so this test does the same. Gaps must be declared in KNOWN_GAPS with a reason;
an undeclared gap fails. Stdlib unittest: runs without pytest.
"""
import tempfile
import unittest
from pathlib import Path

from robie_job_engine.request_routing import BOUNDED_ENGINE_ACTIONS, WORKER_FOR_ACTION
from robie_job_engine.store import JobStore
from robie_job_engine.test_runtime import build_runtime_engine

# Declared, reviewed gaps. Each needs a production read port that does not
# exist yet. Do NOT close these with a test double: a verifier holding a fake
# read port verifies nothing and is worse than one that refuses.
KNOWN_GAPS = {
    "carrier.proposal": "no production ProposalDestination exists; only test doubles",
    "ezlynx.reassign": "no production EzlynxReadback; EzlynxApiClientReadPort exposes "
                       "policy_by_number/documents_for_applicant, not api_state/fresh_page_state",
    "ezlynx.move_document": "same EzlynxReadback gap as ezlynx.reassign",
    "ezlynx.apply_label": "same EzlynxReadback gap as ezlynx.reassign",
}


def _engine():
    tmp = tempfile.mkdtemp()
    return build_runtime_engine(JobStore(str(Path(tmp) / "jobs.db")))


class BoundedActionVerifierCoverage(unittest.TestCase):
    def test_every_bounded_action_has_a_worker_mapping(self):
        missing = sorted(a for a in BOUNDED_ENGINE_ACTIONS if a not in WORKER_FOR_ACTION)
        self.assertEqual(missing, [], f"bounded actions with no worker mapping: {missing}")

    def test_every_bounded_action_has_a_verifier_or_a_declared_gap(self):
        engine = _engine()
        undeclared = sorted(
            a for a in BOUNDED_ENGINE_ACTIONS
            if a not in engine.verifiers and a not in KNOWN_GAPS
        )
        self.assertEqual(
            undeclared, [],
            "bounded actions with no verifier on the scheduler-built engine and no "
            f"declared gap: {undeclared}. Either register a verifier or add it to "
            "KNOWN_GAPS with a reason.",
        )

    def test_known_gaps_are_still_gaps(self):
        # Stops KNOWN_GAPS rotting into a list of things that were fixed and
        # never removed — which would hide the next real gap.
        engine = _engine()
        fixed = sorted(a for a in KNOWN_GAPS if a in engine.verifiers)
        self.assertEqual(
            fixed, [],
            f"these are no longer gaps and must be removed from KNOWN_GAPS: {fixed}",
        )

    def test_known_gaps_are_all_actually_bounded(self):
        stale = sorted(a for a in KNOWN_GAPS if a not in BOUNDED_ENGINE_ACTIONS)
        self.assertEqual(stale, [], f"KNOWN_GAPS names non-bounded actions: {stale}")

    def test_browser_read_now_registers_on_the_scheduler_path(self):
        # Regression: the scheduler passes no browser_port, so this verifier
        # class existed but never registered.
        self.assertIn("browser.read", _engine().verifiers)

    def test_appsheet_actions_are_no_longer_bounded(self):
        # Zero jobs all-time in Production and no verifier class ever written.
        for action in ("appsheet.smart_reward", "appsheet.qa_audit"):
            self.assertNotIn(action, BOUNDED_ENGINE_ACTIONS)

    def test_report_coverage(self):
        engine = _engine()
        covered = sorted(a for a in BOUNDED_ENGINE_ACTIONS if a in engine.verifiers)
        print(f"\n  bounded actions: {len(BOUNDED_ENGINE_ACTIONS)}")
        print(f"  with a verifier: {len(covered)}")
        print(f"  declared gaps:   {len(KNOWN_GAPS)}")
        for a in sorted(BOUNDED_ENGINE_ACTIONS):
            mark = "ok " if a in engine.verifiers else "GAP"
            print(f"    {mark} {a}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
