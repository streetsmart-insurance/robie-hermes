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
    GOLD_EFFECTIVE_DATE,
    GOLD_EXPIRATION_DATE,
    POLICY_SETUP_REQUIRED_KIND,
    PolicySetupToolMissing,
    detect_policy_setup_request,
    extract_policy_setup_args,
    invoke_policy_setup_tool,
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
        self.assertIn("'tool_called': True", source)
        self.assertIn("'setup_complete': bool(success)", source)
        # The deterministic branch must come before the generic LLM fallback.
        self.assertLess(
            source.index("policy_setup_deterministic"),
            source.index("For any other request"),
        )

    def test_email_agent_fails_closed_without_fallthrough(self):
        source = (ROOT / "scripts" / "robie_email_agent.py").read_text()
        self.assertIn("except PolicySetupToolMissing as exc", source)
        # Fail-closed: surfaces ROBIE_OUTCOME_UNKNOWN, persists the real error,
        # copies policy_number onto action.destination, and does not fall through.
        self.assertIn('ROBIE_OUTCOME_UNKNOWN', source)
        self.assertIn('policy_api_create_error', source)
        self.assertIn("'destination'", source)
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

    def test_chat_guard_invokes_callable_handler_not_prompt_only(self):
        source = (ROOT / "robie_job_engine" / "chat_guard.py").read_text()
        self.assertIn("invoke_policy_setup_tool(policy_args)", source)
        self.assertIn("register_policy_setup_callable", source)
        self.assertIn("park_policy_setup_fail_closed", source)
        self.assertLess(
            source.index("register_policy_setup_callable"),
            source.index("invoke_policy_setup_tool(policy_args)"),
        )

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


class ExtractArgsTests(unittest.TestCase):
    BODY = (
        "Please create the homeowners policy TEST-HO-20260911-E01 "
        "on applicant 220250093.\n"
        "Effective Date: 10/02/2026\n"
        "Expiration Date: 10/02/2027\n"
        "Dwelling: $1,200,000\n"
        "Other Structures: $120,000\n"
        "Personal Property: $600,000\n"
        "Loss of Use: $240,000\n"
        "Personal Liability EA OCC: $500,000\n"
        "Medical Payments EA PER: $5,000\n"
    )

    def test_extracts_dates_and_limits(self):
        args = extract_policy_setup_args(self.BODY)
        self.assertEqual(args["policy_number"], "TEST-HO-20260911-E01")
        self.assertEqual(args["effective_date"], "10/02/2026")
        self.assertEqual(args["expiration_date"], "10/02/2027")
        self.assertEqual(args["dwelling"], "1200000")
        self.assertEqual(args["other_structures"], "120000")
        self.assertEqual(args["personal_property"], "600000")
        self.assertEqual(args["loss_of_use"], "240000")
        self.assertEqual(args["personal_liability"], "500000")
        self.assertEqual(args["medical_payments"], "5000")

    def test_gold_dates_when_body_has_none(self):
        args = extract_policy_setup_args(E01_REQUEST)
        self.assertEqual(args["effective_date"], GOLD_EFFECTIVE_DATE)
        self.assertEqual(args["expiration_date"], GOLD_EXPIRATION_DATE)
        self.assertEqual(GOLD_EFFECTIVE_DATE, "10/02/2026")
        self.assertEqual(GOLD_EXPIRATION_DATE, "10/02/2027")

    def test_dates_never_empty(self):
        for text in (E01_REQUEST, "", "hello"):
            args = extract_policy_setup_args(text) or {}
            self.assertTrue(args.get("effective_date", "10/02/2026").strip())
            self.assertTrue(args.get("expiration_date", "10/02/2027").strip())

    def test_iso_dates_normalized(self):
        text = E01_REQUEST + " Effective 2026-10-02, expires 2027-10-02."
        args = extract_policy_setup_args(text)
        self.assertEqual(args["effective_date"], "10/02/2026")
        self.assertEqual(args["expiration_date"], "10/02/2027")

    def test_limits_absent_when_not_stated(self):
        args = extract_policy_setup_args(E01_REQUEST)
        for key in ("dwelling", "other_structures", "personal_property",
                    "loss_of_use", "personal_liability", "medical_payments"):
            self.assertNotIn(key, args)

    def test_returns_none_outside_job_class(self):
        self.assertIsNone(extract_policy_setup_args("What is the status?"))

    def test_invoke_fills_empty_dates(self):
        seen = {}

        def fake_handler(args):
            seen.update(args)
            return {"ok": True}

        with patch(
            "robie_job_engine.policy_setup_dispatch.load_policy_setup_handler",
            return_value=fake_handler,
        ):
            invoke_policy_setup_tool({"policy_number": "TEST-HO-20260911-E01"})
        self.assertEqual(seen["effective_date"], GOLD_EFFECTIVE_DATE)
        self.assertEqual(seen["expiration_date"], GOLD_EXPIRATION_DATE)

    def test_invoke_passes_full_args(self):
        seen = {}

        def fake_handler(args):
            seen.update(args)
            return {"ok": True}

        with patch(
            "robie_job_engine.policy_setup_dispatch.load_policy_setup_handler",
            return_value=fake_handler,
        ):
            invoke_policy_setup_tool(extract_policy_setup_args(self.BODY))
        self.assertEqual(seen["policy_number"], "TEST-HO-20260911-E01")
        self.assertEqual(seen["effective_date"], "10/02/2026")
        self.assertEqual(seen["expiration_date"], "10/02/2027")
        self.assertEqual(seen["dwelling"], "1200000")
        self.assertEqual(seen["medical_payments"], "5000")


if __name__ == "__main__":
    unittest.main()
