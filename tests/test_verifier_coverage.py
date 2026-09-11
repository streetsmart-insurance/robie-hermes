"""Regression test: verifier coverage on the scheduler-path engine.

Stops the silent-UNVERIFIED recurrence. ``build_runtime_engine(store)``
with no optional args is exactly what ``maybe_run_bounded_job`` builds on
the scheduler tick and on chat dispatch. Any BOUNDED_ENGINE_ACTIONS entry
missing from ``engine.verifiers`` can NEVER complete: ``_verify`` terminates
it UNVERIFIED with "no independent verifier registered" no matter how well
its worker performs.

KNOWN_GAPS names the actions with no production input in the tree yet.
Each entry says why. If a gap is fixed, remove it from KNOWN_GAPS — the
test fails until you do, so the fix is deliberate, never accidental.
A verifier constructed without its read port would verify nothing and is
worse than one that refuses: do not close a gap with a test double.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from robie_job_engine.request_routing import BOUNDED_ENGINE_ACTIONS
from robie_job_engine.store import JobStore
from robie_job_engine.test_runtime import build_runtime_engine

KNOWN_GAPS = {
    # No honest readback exists in the tree for these three. The EZLynx
    # API does not expose assignment, document folder/location, or labels,
    # and a CDP readback would be invented selectors with no way to test
    # them from here. A verifier constructed without its read port would
    # verify nothing and is worse than one that refuses — so these stay
    # refused-loudly until a tested readback is built (needs the EZLynx
    # browser service on the scheduler path, which the worker also lacks:
    # it is _UnavailableWorker there today). 3 jobs have ever queued them
    # (1 each, all-time), so this is documented hygiene, not the unlock.
    "ezlynx.reassign": "no honest EzlynxReadback buildable without a tested EZLynx read path",
    "ezlynx.move_document": "no honest EzlynxReadback buildable without a tested EZLynx read path",
    "ezlynx.apply_label": "no honest EzlynxReadback buildable without a tested EZLynx read path",
}


class SchedulerPathVerifierCoverageTest(unittest.TestCase):
    def test_every_bounded_action_either_registered_or_a_known_gap(self):
        # Simulate the scheduler service, which sets no ROBIE_ENV.
        old_env = os.environ.pop("ROBIE_ENV", None)
        try:
            with tempfile.TemporaryDirectory(prefix="verifier-coverage-") as tmp:
                engine = build_runtime_engine(
                    JobStore(os.path.join(tmp, "jobs.db"))
                )
        finally:
            if old_env is not None:
                os.environ["ROBIE_ENV"] = old_env
        for action in sorted(BOUNDED_ENGINE_ACTIONS):
            if action in KNOWN_GAPS:
                continue
            self.assertIn(
                action,
                engine.verifiers,
                f"scheduler-path engine has no verifier for {action}: "
                "that job can NEVER complete",
            )
        for action, reason in KNOWN_GAPS.items():
            self.assertIn(
                action,
                BOUNDED_ENGINE_ACTIONS,
                f"{action} is no longer a bounded action — remove it from "
                "KNOWN_GAPS",
            )
            self.assertNotIn(
                action,
                engine.verifiers,
                f"{action} is now registered ({reason}) — remove it from "
                "KNOWN_GAPS so the fix is deliberate",
            )


if __name__ == "__main__":
    unittest.main()
