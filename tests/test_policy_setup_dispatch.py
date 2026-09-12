"""Deterministic routing for the homeowners policy-setup job class.

When an email/Chat asks to create/setup a homeowners policy on applicant
220250093 (policy numbers TEST-HO-*), the runner must invoke ezlynx_policy_setup
as a real tool call before any playwright_exec. If the tool is missing, fail
closed. Never fall through to playwright_exec for this job class.

No live EZLynx. No real customer, policy, coverage, or payment data.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from robie_job_engine.policy_setup_dispatch import (
    FAIL_CLOSED_MESSAGE,
    POLICY_SETUP_REQUIRED_KIND,
    PolicySetupToolMissing,
    detect_policy_setup_request,
    load_policy_setup_handler,
)

ROOT = Path(__file__).resolve().parents[1]

E01_REQUEST = (
    "Please create the homeowners policy TEST-HO-20260911-E01 "
    "on applicant 220250093 with the coverages from the quote."
)


class DetectionTests(unittest.TestCase):
    def test_detects_create_homeowners_with_policy_number(self):
        self.assertEqual(
            detect_policy_setup_request(E01_REQUEST),
            {"policy_number": "TEST-HO-20260911-E01"},
        )

    def test_detects_setup_variant(self):
        text = "Can you set up the HO policy TEST-HO-20260912-D02 for 220250093?"
        self.assertEqual(
            detect_policy_setup_request(text),
            {"policy_number": "TEST-HO-20260912-D02"},
        )

    def test_rejects_without_intent(self):
        self.assertIsNone(
            detect_policy_setup_request(
                "The homeowners policy TEST-HO-20260911-E01 renewed."
            )
        )

    def test_rejects_without_applicant_or_test_policy(self):
        self.assertIsNone(
            detect_policy_setup_request("Please create the homeowners policy.")
        )

    def test_rejects_unrelated_email(self):
        self.assertIsNone(
            detect_policy_setup_request(
                "Please create the Ascend finance agreement for applicant 221398001."
            )
        )

    def test_rejects_empty(self):
        self.assertIsNone(detect_policy_setup_request(""))
        self.assertIsNone(detect_policy_setup_request(None))


class FailClosedTests(unittest.TestCase):
    def test_missing_tool_module_fails_closed(self):
        with patch(
            "robie_job_engine.policy_setup_dispatch.tool_module_path",
            return_value=Path("/nonexistent/policy_setup_tool.py"),
        ):
            with self.assertRaisesRegex(
                PolicySetupToolMissing, "not registered; failing closed"
            ):
                load_policy_setup_handler()

    def test_fail_closed_message_names_the_tool(self):
        self.assertIn("ezlynx_policy_setup", FAIL_CLOSED_MESSAGE)
        self.assertIn("playwright_exec", FAIL_CLOSED_MESSAGE)


class EmailRoutingTests(unittest.TestCase):
    def test_email_agent_invokes_tool_deterministically(self):
        source = (ROOT / "scripts" / "robie_email_agent.py").read_text()
        self.assertIn("invoke_policy_setup_tool(policy_setup_args)", source)
        self.assertIn("policy_setup_deterministic", source)
        # The deterministic branch must come before the generic LLM fallback.
        self.assertLess(
            source.index("policy_setup_deterministic"),
            source.index("For any other request"),
        )

    def test_email_agent_fails_closed_without_fallthrough(self):
        source = (ROOT / "scripts" / "robie_email_agent.py").read_text()
        self.assertIn("except PolicySetupToolMissing as exc", source)
        self.assertIn('return f"ROBIE_OUTCOME_UNKNOWN: {exc}"', source)
        # The surfaced message itself carries the no-fallthrough guarantee.
        dispatch = (ROOT / "robie_job_engine" / "policy_setup_dispatch.py").read_text()
        self.assertIn(
            "will not fall through to playwright_exec for policy setup", dispatch
        )


class ChatRoutingTests(unittest.TestCase):
    def test_chat_contract_routes_tool_first(self):
        from robie_job_engine.chat_guard import _policy_setup_contract

        lines = _policy_setup_contract(E01_REQUEST)
        joined = "\n".join(lines)
        self.assertIn("'ezlynx_policy_setup' tool FIRST", joined)
        self.assertIn("before any", joined)
        self.assertIn("ROBIE_OUTCOME_UNKNOWN", joined)

    def test_chat_contract_empty_for_other_messages(self):
        from robie_job_engine.chat_guard import _policy_setup_contract

        self.assertEqual(_policy_setup_contract("What is the status?"), [])


class PlaywrightGuardTests(unittest.TestCase):
    def test_playwright_tool_refuses_policy_setup_fallthrough(self):
        source = (ROOT / "deploy" / "hermes" / "tools" / "playwright_tool.py").read_text()
        self.assertIn("POLICY_SETUP_ORDER", source)
        self.assertIn("POLICY_SETUP_REQUIRED_KIND", source)
        self.assertIn("call the 'ezlynx_policy_setup' tool first", source)

    def test_policy_setup_tool_marks_tool_called(self):
        source = (
            ROOT / "deploy" / "hermes" / "tools" / "policy_setup_tool.py"
        ).read_text()
        self.assertIn("_mark_policy_setup_tool_called", source)
        self.assertIn('"tool_called"] = True', source)


if __name__ == "__main__":
    unittest.main()
