"""Commercial vs Personal from LOB, not name/LLC. No live Ascend."""

from __future__ import annotations

import unittest

from robie_job_engine.ascend_customer_type import (
    COMMERCIAL,
    COMMERCIAL_LOCATOR,
    CUSTOMER_TYPE_SCENARIO_ID,
    PERSONAL,
    PERSONAL_LOCATOR,
    resolve_customer_type,
    resolve_customer_type_from_lob,
    run_customer_type_lob_scenario,
    unknown_lob_hitl,
)


class CustomerTypeFromLobTests(unittest.TestCase):
    def test_commercial_auto_or_package_is_commercial(self):
        for raw in ("commercial auto", "Commercial Auto", "commercial package"):
            with self.subTest(raw=raw):
                self.assertEqual(resolve_customer_type_from_lob(raw), COMMERCIAL)
                decision = resolve_customer_type({"line_of_business": raw})
                self.assertEqual(decision["resolved"], COMMERCIAL)
                self.assertEqual(decision["locator"], COMMERCIAL_LOCATOR)
                self.assertFalse(decision["hitl_required"])

    def test_homeowners_or_personal_auto_is_personal(self):
        for raw in ("homeowners", "personal auto"):
            with self.subTest(raw=raw):
                self.assertEqual(resolve_customer_type_from_lob(raw), PERSONAL)
                decision = resolve_customer_type({"lob": raw})
                self.assertEqual(decision["resolved"], PERSONAL)
                self.assertEqual(decision["locator"], PERSONAL_LOCATOR)

    def test_missing_lob_is_unknown_hitl(self):
        self.assertIsNone(resolve_customer_type_from_lob(""))
        self.assertIsNone(resolve_customer_type_from_lob(None))
        decision = resolve_customer_type({})
        self.assertTrue(decision["hitl_required"])
        self.assertIsNone(decision["resolved"])
        self.assertIn("PLAYWRIGHT_BLOCKED", decision["hitl_text"])
        self.assertNotIn("Listen up", decision["hitl_text"])
        self.assertIn("PLAYWRIGHT_BLOCKED", unknown_lob_hitl())

    def test_llc_or_person_name_is_not_the_decider(self):
        for name in (
            "PAWIVA INVESTMENT LLC",
            "Joseph's Tree N Landscaping LLC",
            "Jane Smith",
            "Acme",
        ):
            with self.subTest(name=name):
                self.assertIsNone(resolve_customer_type_from_lob(name))
                self.assertIsNone(
                    resolve_customer_type({"insured": name, "name": name}).get("resolved")
                )
        package = resolve_customer_type(
            {
                "line_of_business": "commercial package",
                "insured": "PAWIVA INVESTMENT LLC",
            }
        )
        self.assertEqual(package["resolved"], COMMERCIAL)

    def test_named_scenario_passes(self):
        report = run_customer_type_lob_scenario()
        self.assertEqual(report["id"], CUSTOMER_TYPE_SCENARIO_ID)
        self.assertTrue(report["ok"], report.get("evidence"))


if __name__ == "__main__":
    unittest.main()
