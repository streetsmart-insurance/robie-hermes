"""Every place that hardcodes a worker name into a job payload must agree with
WORKER_FOR_ACTION.

engine._perform reads the worker from the PAYLOAD, not the registry, so a
hand-written name in an enqueuer is what actually selects the worker. When
.github/workflows/trigger-production-worker.yml emitted {"worker":"audit"} for
action audit_verification, every job it created died with
"unknown worker: audit" while the registry was correct all along. Nothing
failed at build time, so it was found by draining the queue.

Two enqueuers hardcode these strings today. This test pins both to the
registry. Stdlib unittest: runs without pytest.
"""
import re
import unittest
from pathlib import Path

from robie_job_engine.request_routing import WORKER_FOR_ACTION

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "trigger-production-worker.yml"

# ACTION=<action_type>; PAYLOAD='{"worker":"<worker>", ...
_CASE = re.compile(r"ACTION=(?P<action>[a-z_.]+);\s*PAYLOAD='\{\"worker\":\"(?P<worker>[a-z\-]+)\"")


class WorkflowEnqueuerMatchesRegistry(unittest.TestCase):
    def test_workflow_file_exists(self):
        self.assertTrue(WORKFLOW.is_file(), f"missing {WORKFLOW}")

    def test_every_workflow_payload_names_the_registry_worker(self):
        pairs = _CASE.findall(WORKFLOW.read_text())
        self.assertTrue(pairs, "parsed no ACTION/PAYLOAD pairs — has the workflow changed shape?")
        wrong = [
            (action, worker, WORKER_FOR_ACTION.get(action))
            for action, worker in pairs
            if WORKER_FOR_ACTION.get(action) != worker
        ]
        self.assertEqual(
            wrong, [],
            "workflow payloads disagree with WORKER_FOR_ACTION "
            "(action, payload_says, registry_says): " + repr(wrong),
        )

    def test_every_workflow_action_is_a_known_action(self):
        pairs = _CASE.findall(WORKFLOW.read_text())
        unknown = sorted({a for a, _ in pairs if a not in WORKER_FOR_ACTION})
        self.assertEqual(unknown, [], f"workflow triggers unknown action types: {unknown}")


class InstallSchedulesMatchesRegistry(unittest.TestCase):
    def test_every_scheduled_payload_names_the_registry_worker(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "_install_verification_schedules",
            REPO / "scripts" / "install_verification_schedules.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        wrong = []
        for _name, action, payload, _cron in mod.SCHEDULES:
            claimed = payload.get("worker")
            if claimed is not None and WORKER_FOR_ACTION.get(action) != claimed:
                wrong.append((action, claimed, WORKER_FOR_ACTION.get(action)))
        self.assertEqual(
            wrong, [],
            "scheduled payloads disagree with WORKER_FOR_ACTION "
            "(action, payload_says, registry_says): " + repr(wrong),
        )

    def test_every_scheduled_action_is_bounded(self):
        import importlib.util

        from robie_job_engine.request_routing import BOUNDED_ENGINE_ACTIONS

        spec = importlib.util.spec_from_file_location(
            "_install_verification_schedules2",
            REPO / "scripts" / "install_verification_schedules.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        unbounded = sorted(
            {a for _n, a, _p, _c in mod.SCHEDULES if a not in BOUNDED_ENGINE_ACTIONS}
        )
        self.assertEqual(
            unbounded, [],
            f"scheduled actions that are not bounded (they bypass Worker/Verifier): {unbounded}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
