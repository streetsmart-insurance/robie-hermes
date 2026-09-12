"""Hard route for hermes.email_task homeowners-create on 220250093.

The route is code, not prompt text: the email runner classifies the job in
code and checkpoints the marker; playwright_tool calls ezlynx_policy_setup in
code before any playwright_exec browser code runs. Job 1cfd0f3e never invoked
the tool; this route makes invocation unavoidable. Missing tool = fail closed.

No live EZLynx. No real customer, policy, coverage, or payment data.
"""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from robie_job_engine.policy_setup_dispatch import (
    E01_LIMIT_DEFAULTS,
    FAIL_CLOSED_MESSAGE,
    GOLD_EFFECTIVE_DATE,
    GOLD_EXPIRATION_DATE,
    POLICY_SETUP_ACTION,
    POLICY_SETUP_REQUIRED_KIND,
    POLICY_SETUP_TOOL,
    PolicySetupToolMissing,
    detect_policy_setup_request,
    extract_policy_setup_args,
    handler_args_from_marker,
)

ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = ROOT / "deploy" / "hermes" / "tools"

E01_REQUEST = (
    "Please create the homeowners policy TEST-HO-20260911-E01 "
    "on applicant 220250093 with the coverages from the quote."
)


def _stub_tools_registry():
    """playwright_tool imports tools.registry at module level; stub it."""
    tools_pkg = types.ModuleType("tools")
    tools_pkg.__path__ = []
    registry_mod = types.ModuleType("tools.registry")

    class DummyRegistry:
        def register(self, **_kwargs):
            return None

    def tool_error(message):
        return {"ok": False, "error": message}

    def tool_result(payload):
        return {"ok": True, "result": payload}

    registry_mod.registry = DummyRegistry()
    registry_mod.tool_error = tool_error
    registry_mod.tool_result = tool_result
    prev = {n: sys.modules.get(n) for n in ("tools", "tools.registry")}
    sys.modules["tools"] = tools_pkg
    sys.modules["tools.registry"] = registry_mod
    return prev


def _restore(prev):
    for name, mod in prev.items():
        if mod is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = mod


from contextlib import contextmanager


@contextmanager
def _tools_registry_stub():
    """Keep the tools.registry stub installed for module load AND calls."""
    prev = _stub_tools_registry()
    try:
        yield
    finally:
        _restore(prev)


def _load_playwright_tool():
    spec = importlib.util.spec_from_file_location(
        "playwright_tool_under_test", TOOLS_DIR / "playwright_tool.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeStore:
    def __init__(self, job, checkpoints):
        self._job = job
        self._checkpoints = dict(checkpoints)
        self.saved = {}

    def get_job(self, job_id):
        return self._job

    def get_checkpoint(self, job_id, kind):
        return self._checkpoints.get(kind)

    def checkpoint(self, job_id, kind, data):
        self.saved[kind] = data
        self._checkpoints[kind] = data


def _make_route_test(job_action="hermes.email_task", marker=None, handler=None):
    """Run _hard_route_policy_setup with a fake store and fake tool handler."""
    import os

    job = {"action_type": job_action, "status": "RUNNING"}
    checkpoints = {}
    if marker is not None:
        checkpoints[POLICY_SETUP_REQUIRED_KIND] = dict(marker)
    store = FakeStore(job, checkpoints)
    calls = []

    def fake_handler(args):
        calls.append(dict(args))
        if handler is not None:
            return handler(args)
        return {"ok": True, "policy_number": args.get("policy_number")}

    env = {"ROBIE_JOB_ID": "job-test-1", "ROBIE_JOB_DB": "/tmp/fake-jobs.db"}
    with _tools_registry_stub():
        mod = _load_playwright_tool()
        with patch.dict(os.environ, env), patch(
            "robie_job_engine.store.JobStore", return_value=store
        ), patch(
            "robie_job_engine.policy_setup_dispatch.load_policy_setup_handler",
            return_value=fake_handler,
        ):
            result = mod._hard_route_policy_setup({})
    return result, calls, store


class DetectionTests(unittest.TestCase):
    def test_detects_homeowners_create(self):
        self.assertEqual(
            detect_policy_setup_request(E01_REQUEST),
            {"policy_number": "TEST-HO-20260911-E01"},
        )

    def test_detects_setup_variant(self):
        self.assertEqual(
            detect_policy_setup_request(
                "Set up the HO policy TEST-HO-20260912-D02 for 220250093 please."
            ),
            {"policy_number": "TEST-HO-20260912-D02"},
        )

    def test_rejects_without_intent(self):
        self.assertIsNone(
            detect_policy_setup_request(
                "The homeowners policy TEST-HO-20260911-E01 renewed."
            )
        )

    def test_rejects_without_applicant_or_policy(self):
        self.assertIsNone(
            detect_policy_setup_request("Please create the homeowners policy.")
        )

    def test_rejects_unrelated(self):
        self.assertIsNone(
            detect_policy_setup_request(
                "Please create the Ascend finance agreement for 221398001."
            )
        )


class ExtractArgsTests(unittest.TestCase):
    """Dates/limits ride on the checkpoint; create never gets empty dates."""

    E01_BODY = (
        "Please create the homeowners policy TEST-HO-20260911-E01 "
        "on applicant 220250093.\n"
        "Use Homeowners / HO-3, term 10/02/2026 to 10/02/2027. "
        "Dwelling $1,200,000; other structures $120,000; "
        "personal property $600,000; loss of use $360,000; liability $500,000; "
        "medical payments $5,000."
    )

    def test_extracts_dates_and_limits_from_body(self):
        args = extract_policy_setup_args(self.E01_BODY)
        self.assertEqual(args["policy_number"], "TEST-HO-20260911-E01")
        self.assertEqual(args["effective_date"], "10/02/2026")
        self.assertEqual(args["expiration_date"], "10/02/2027")
        self.assertEqual(args["dwelling"], "1200000")
        self.assertEqual(args["other_structures"], "120000")
        self.assertEqual(args["personal_property"], "600000")
        self.assertEqual(args["loss_of_use"], "360000")
        self.assertEqual(args["personal_liability"], "500000")
        self.assertEqual(args["medical_payments"], "5000")

    def test_gold_defaults_when_body_has_none(self):
        args = extract_policy_setup_args(E01_REQUEST)
        self.assertEqual(args["effective_date"], GOLD_EFFECTIVE_DATE)
        self.assertEqual(args["expiration_date"], GOLD_EXPIRATION_DATE)
        self.assertEqual(GOLD_EFFECTIVE_DATE, "10/02/2026")
        self.assertEqual(GOLD_EXPIRATION_DATE, "10/02/2027")
        for key, default in E01_LIMIT_DEFAULTS.items():
            self.assertEqual(args[key], default)

    def test_dates_never_empty(self):
        for text in (E01_REQUEST, self.E01_BODY):
            args = extract_policy_setup_args(text)
            self.assertTrue(args["effective_date"].strip())
            self.assertTrue(args["expiration_date"].strip())

    def test_returns_none_outside_job_class(self):
        self.assertIsNone(extract_policy_setup_args("What is the status?"))

    def test_sparse_marker_gets_gold_defaults(self):
        args = handler_args_from_marker(
            {"policy_number": "TEST-HO-20260911-E01", "tool_called": False}
        )
        self.assertEqual(args["effective_date"], "10/02/2026")
        self.assertEqual(args["expiration_date"], "10/02/2027")
        self.assertEqual(args["dwelling"], "1200000")
        self.assertNotIn("tool_called", args)


class RunnerClassificationTests(unittest.TestCase):
    """The runner classifies in code. It never sets up the policy itself."""

    def test_runner_checkpoints_the_route_marker(self):
        source = (ROOT / "scripts" / "robie_email_agent.py").read_text()
        self.assertIn("POLICY_SETUP_REQUIRED_KIND", source)
        self.assertIn("policy_setup_hard_route", source)
        self.assertIn("extract_policy_setup_args", source)

    def test_runner_never_invokes_the_setup(self):
        source = (ROOT / "scripts" / "robie_email_agent.py").read_text()
        self.assertNotIn("invoke_policy_setup_tool", source)
        self.assertNotIn("_run_policy_setup(", source)
        self.assertNotIn("ezlynx_policy_setup_handler(", source)


class HardRouteTests(unittest.TestCase):
    def test_route_calls_tool_before_playwright(self):
        result, calls, store = _make_route_test(
            marker={"policy_number": "TEST-HO-20260911-E01", "tool_called": False}
        )
        self.assertEqual(len(calls), 1)
        # The hole: the handler used to get policy_number only, so create got
        # empty dates and 400'd. The checkpoint args now ride along.
        self.assertEqual(calls[0]["policy_number"], "TEST-HO-20260911-E01")
        self.assertEqual(calls[0]["effective_date"], "10/02/2026")
        self.assertEqual(calls[0]["expiration_date"], "10/02/2027")
        self.assertEqual(calls[0]["dwelling"], "1200000")
        self.assertEqual(calls[0]["medical_payments"], "5000")
        self.assertNotIn("tool_called", calls[0])
        self.assertTrue(result["ok"])
        saved = store.saved[POLICY_SETUP_REQUIRED_KIND]
        self.assertTrue(saved["tool_called"])
        self.assertEqual(saved["policy_number"], "TEST-HO-20260911-E01")

    def test_route_passes_checkpoint_args_through(self):
        marker = {
            "policy_number": "TEST-HO-20260911-E01",
            "effective_date": "11/01/2026",
            "expiration_date": "11/01/2027",
            "dwelling": "900000",
            "other_structures": "90000",
            "personal_property": "450000",
            "loss_of_use": "180000",
            "personal_liability": "300000",
            "medical_payments": "1000",
            "tool_called": False,
        }
        result, calls, _ = _make_route_test(marker=marker)
        self.assertEqual(len(calls), 1)
        for key in (
            "policy_number", "effective_date", "expiration_date", "dwelling",
            "other_structures", "personal_property", "loss_of_use",
            "personal_liability", "medical_payments",
        ):
            self.assertEqual(calls[0][key], marker[key])
        self.assertTrue(result["ok"])

    def test_route_fires_only_once(self):
        result, calls, _ = _make_route_test(
            marker={"policy_number": "TEST-HO-20260911-E01", "tool_called": True}
        )
        self.assertIsNone(result)
        self.assertEqual(calls, [])

    def test_route_ignores_jobs_without_marker(self):
        result, calls, _ = _make_route_test(marker=None)
        self.assertIsNone(result)
        self.assertEqual(calls, [])

    def test_route_ignores_non_email_tasks(self):
        result, calls, _ = _make_route_test(
            job_action="hermes.google_chat_task",
            marker={"policy_number": "TEST-HO-20260911-E01", "tool_called": False},
        )
        self.assertIsNone(result)
        self.assertEqual(calls, [])

    def test_missing_tool_fails_closed(self):
        import os

        job = {"action_type": POLICY_SETUP_ACTION, "status": "RUNNING"}
        store = FakeStore(job, {
            POLICY_SETUP_REQUIRED_KIND: {
                "policy_number": "TEST-HO-20260911-E01",
                "tool_called": False,
            }
        })
        env = {"ROBIE_JOB_ID": "job-test-1", "ROBIE_JOB_DB": "/tmp/fake-jobs.db"}
        with _tools_registry_stub():
            mod = _load_playwright_tool()
            with patch.dict(os.environ, env), patch(
                "robie_job_engine.store.JobStore", return_value=store
            ), patch(
                "robie_job_engine.policy_setup_dispatch.load_policy_setup_handler",
                side_effect=PolicySetupToolMissing(FAIL_CLOSED_MESSAGE),
            ):
                result = mod._hard_route_policy_setup({})
        self.assertIn("ok", result)
        self.assertFalse(result["ok"])
        self.assertIn("not registered; failing closed", result["error"])
        self.assertIn("playwright_exec", result["error"])

    def test_tool_exception_becomes_tool_error(self):
        def boom(args):
            raise RuntimeError("cdp down")

        result, calls, _ = _make_route_test(
            marker={"policy_number": "TEST-HO-20260911-E01", "tool_called": False},
            handler=boom,
        )
        self.assertEqual(len(calls), 1)
        self.assertFalse(result["ok"])
        self.assertIn("RuntimeError", result["error"])


class NoPromptRouteTests(unittest.TestCase):
    """Prompt text is not a route: the hard route adds no prompt directives."""

    def test_route_has_no_prompt_directives(self):
        source = (TOOLS_DIR / "playwright_tool.py").read_text()
        route_start = source.index("def _hard_route_policy_setup")
        route_end = source.index("def playwright_exec")
        route = source[route_start:route_end]
        for phrase in ("You MUST", "you must call", "instruct the agent", "tell the agent"):
            self.assertNotIn(phrase, route)

    def test_tool_marks_tool_called_in_code(self):
        source = (TOOLS_DIR / "policy_setup_tool.py").read_text()
        self.assertIn("_mark_policy_setup_tool_called", source)
        self.assertIn("POLICY_SETUP_REQUIRED_KIND", source)


if __name__ == "__main__":
    unittest.main()
