#!/usr/bin/env python3
"""Tests for the Job Engine policy-setup wiring (Dusty's blockers on PR 339).

stdlib unittest — no browser, no network, no box. Async engine paths are
exercised only through their pre-browser refusal branches; the live
FormEntry path is Dusty's orchestrated proof, not a unit test.
"""
from __future__ import annotations

import asyncio
import unittest

from robie_job_engine import ezlynx_policy_setup as eps
from robie_job_engine.hermes_tool_visibility import email_chat_job_schema


class FakePage:
    pass


def run(coro):
    return asyncio.run(coro)


class TestSetupPolicyByLobGates(unittest.TestCase):
    def test_wrong_applicant_refuses_before_browser(self):
        setup = eps.EzlynxPolicySetupPage(FakePage())
        shell = eps.PolicyShellInput(
            applicant_id="221398001", lob="HOME", policy_number="TEST-X"
        )
        result = run(setup.setup_policy_by_lob(shell))
        self.assertFalse(result.success)
        self.assertEqual(result.phase_reached, "applicant_write_scope")

    def test_non_home_lob_still_gated(self):
        setup = eps.EzlynxPolicySetupPage(FakePage())
        shell = eps.PolicyShellInput(
            applicant_id="220250093", lob="Commercial Auto", policy_number="TEST-X"
        )
        result = run(setup.setup_policy_by_lob(shell))
        self.assertFalse(result.success)
        self.assertEqual(result.phase_reached, "draft_write_gate")

    def test_home_lob_passes_gate_to_api(self):
        # With no API config / browser, the HOME path must attempt the API
        # phase (and fail there with a reported error) — not refuse at the gate.
        setup = eps.EzlynxPolicySetupPage(FakePage())
        shell = eps.PolicyShellInput(
            applicant_id="220250093",
            lob="HOME",
            policy_number="TEST-HO-20260912-X1",
            effective_date="10/02/2026",
            expiration_date="10/02/2027",
        )
        result = run(setup.setup_policy_by_lob(shell))
        self.assertNotEqual(result.phase_reached, "draft_write_gate")
        # It reached the API phase (or failed inside it with evidence).
        self.assertIn(result.phase_reached, ("api_search_first_create",))


class TestHomeownersLabelMapping(unittest.TestCase):
    def test_already_exists_without_readable_id_gives_honest_diagnostic(self):
        # The policy already existed (no create attempted) but the search row
        # carried no recognizable ID key. The error must say the policy
        # existed — never "Create HTTP None" for a create that never ran.
        from unittest.mock import patch

        import robie_job_engine.ezlynx_api as api_mod
        import robie_job_engine.policy_setup_proof as proof_mod

        row = {"policyNumber": "TEST-HO-20260911-E01", "SomeOtherKey": "x"}
        fake_report = {
            "policy_number": "TEST-HO-20260911-E01",
            "verdict": "ALREADY_EXISTS",
            "read_back": row,
            "create": None,
            "policy_id": None,
            "no_id_diagnostic": None,
        }
        setup = eps.EzlynxPolicySetupPage(FakePage())
        shell = eps.PolicyShellInput(
            applicant_id="220250093",
            lob="HOME",
            policy_number="TEST-HO-20260911-E01",
            effective_date="10/02/2026",
            expiration_date="10/02/2027",
        )
        with (
            patch.object(api_mod, "load_ezlynx_api_config", return_value=object()),
            patch.object(api_mod, "EzlynxApiClient", return_value=object()),
            patch.object(proof_mod, "search_first_create", return_value=fake_report),
        ):
            result = run(setup.setup_policy_by_lob(shell))
        self.assertFalse(result.success)
        self.assertEqual(result.phase_reached, "api_search_first_create")
        self.assertIn("already existed", result.error)
        self.assertNotIn("Create HTTP None", result.error)

    def test_maps_to_carlo_literal_labels(self):
        ho = eps.HomeownersCoverageItem(
            dwelling_a="250000",
            other_structures_b="25000",
            personal_property_c="100000",
            loss_of_use_d="50000",
            liability_e="500000",
            med_pay_f="5000",
        )
        values = eps._homeowners_values_by_label(ho)
        self.assertEqual(values["Dwelling"], "250000")
        self.assertEqual(values["Other Structures"], "25000")
        self.assertEqual(values["Personal Property"], "100000")
        self.assertEqual(values["Loss of Use"], "50000")
        self.assertEqual(values["Personal Liability EA OCC"], "500000")
        self.assertEqual(values["Medical Payments EA PER"], "5000")
        # No invented selectors in the keys.
        for key in values:
            self.assertNotIn("HO_Coverage", key)

    def test_none_coverage_gives_empty(self):
        self.assertEqual(eps._homeowners_values_by_label(None), {})


class TestToIsoDate(unittest.TestCase):
    def test_mm_dd_yyyy(self):
        self.assertEqual(eps._to_iso_date("10/02/2026"), "2026-10-02T00:00:00")

    def test_iso_passthrough(self):
        self.assertEqual(
            eps._to_iso_date("2026-10-02T00:00:00"), "2026-10-02T00:00:00"
        )


class TestToolVisibility(unittest.TestCase):
    def test_email_chat_schema_includes_policy_setup_tool(self):
        names = email_chat_job_schema(["playwright_exec", "some_other"])
        self.assertIn("ezlynx_policy_setup", names)
        self.assertIn("playwright_exec", names)
        self.assertNotIn("execute_code", names)
        self.assertNotIn("terminal", names)


if __name__ == "__main__":
    unittest.main()
