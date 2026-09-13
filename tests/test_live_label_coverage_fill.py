"""HOME coverage fill must use live FormEntry labels, not Dwelling aliases.

Live miss d5aed721: HITL resume worked, then fill looked for
['Dwelling', 'Other Structures', ...]. HOME FormEntry shows Coverage A–F.
Map stated A/B/C/D/F onto those live labels. Do not invent omitted E.
Mocks only. No live EZLynx. Do not touch job d5aed721.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from robie_job_engine.ezlynx_policy_setup import (
    EzlynxPolicySetupPage,
    HomeownersCoverageItem,
    PolicyShellInput,
    homeowners_amounts_by_letter,
    stated_live_coverage_names,
)
from robie_job_engine.formentry_coverages import (
    HOME_LIVE_COVERAGE_LABELS,
    live_label_for_coverage_letter,
    map_letter_amounts_to_live_labels,
)
from robie_job_engine.hitl_escalation import HitlResponse
from tests.test_coverage_fill_hitl import _MintedPage
from tests.test_ezlynx_field_widgets import FakeGemini
from tests.test_home_formentry_fill import REAL_FORMENTRY_URL


CARLO_AMOUNTS = HomeownersCoverageItem(
    dwelling_a="1200000",
    other_structures_b="120000",
    personal_property_c="500000",
    loss_of_use_d="500000",
    med_pay_f="10000",
)
LIVE_HOME_LABELS = list(HOME_LIVE_COVERAGE_LABELS)


class LiveLabelMappingTests(unittest.TestCase):
    def test_reads_coverage_a_f_not_dwelling_aliases(self) -> None:
        self.assertEqual(
            live_label_for_coverage_letter("A", LIVE_HOME_LABELS),
            "Coverage A",
        )
        self.assertEqual(
            live_label_for_coverage_letter("F", LIVE_HOME_LABELS),
            "Coverage F",
        )
        self.assertIsNone(
            live_label_for_coverage_letter(
                "A",
                [
                    "Dwelling",
                    "Other Structures",
                    "Personal Property",
                    "Loss of Use",
                    "Personal Liability EA OCC",
                    "Medical Payments EA PER",
                ],
            )
        )

    def test_maps_stated_letters_and_omits_e(self) -> None:
        amounts = homeowners_amounts_by_letter(CARLO_AMOUNTS)
        self.assertEqual(amounts["A"], "1200000")
        self.assertEqual(amounts["F"], "10000")
        self.assertNotIn("E", amounts)
        mapped = map_letter_amounts_to_live_labels(amounts, LIVE_HOME_LABELS)
        self.assertEqual(
            mapped,
            {
                "Coverage A": "1200000",
                "Coverage B": "120000",
                "Coverage C": "500000",
                "Coverage D": "500000",
                "Coverage F": "10000",
            },
        )
        self.assertNotIn("Coverage E", mapped)
        self.assertNotIn("Dwelling", mapped)
        self.assertNotIn("Personal Liability EA OCC", mapped)
        self.assertEqual(
            stated_live_coverage_names(amounts),
            ["Coverage A", "Coverage B", "Coverage C", "Coverage D", "Coverage F"],
        )

    def test_gemini_picks_among_live_labels_only(self) -> None:
        amounts = {"A": "1200000"}
        live = ["Limit 1", "Coverage A", "Something else"]
        mapped = map_letter_amounts_to_live_labels(
            amounts, live, gemini_client=FakeGemini('{"decision":"unsure"}')
        )
        self.assertEqual(mapped, {"Coverage A": "1200000"})


class LiveLabelFillSetupTests(unittest.TestCase):
    def test_fill_uses_live_coverage_labels_not_dwelling(self) -> None:
        async def _run() -> None:
            page = _MintedPage()
            page.evaluate = AsyncMock(return_value=list(LIVE_HOME_LABELS))
            setup = EzlynxPolicySetupPage(
                page,
                job_id="d5aed721-test",
                hitl_deps={
                    "gemini_client": FakeGemini('{"decision":"unsure"}'),
                    "email_sender": lambda **_k: None,
                    "chat_sender": lambda _m: True,
                },
            )

            async def _already_minted(policy_id, applicant_id="", **_k):
                return {
                    "formentry_found": True,
                    "formentry_url": REAL_FORMENTRY_URL,
                    "policy_id": policy_id,
                }

            setup._mint_formentry = _already_minted  # type: ignore[method-assign]
            captured: list[dict] = []

            async def fake_fill(_page, values, **_k):
                captured.append(dict(values))
                return {
                    "filled_count": len(values),
                    "not_found": [],
                    "labels": {
                        label: {"found": True, "filled": True} for label in values
                    },
                }

            with (
                patch(
                    "robie_job_engine.ezlynx_api.EzlynxApiClient"
                ),
                patch(
                    "robie_job_engine.ezlynx_api.load_ezlynx_api_config"
                ),
                patch(
                    "robie_job_engine.policy_setup_proof.search_first_create",
                    return_value={
                        "policy_id": "83669533",
                        "verdict": "ALREADY_EXISTS",
                        "read_back": {"policyId": "83669533"},
                    },
                ),
                patch(
                    "robie_job_engine.formentry_coverages.afill_coverages_by_label",
                    side_effect=fake_fill,
                ),
            ):
                result = await setup.setup_policy_by_lob(
                    PolicyShellInput(
                        applicant_id="220250093",
                        lob="HOME",
                        policy_number="TEST-HO-20260911-E01",
                        effective_date="10/02/2026",
                        expiration_date="10/02/2027",
                        homeowners_coverage=CARLO_AMOUNTS,
                    )
                )

            self.assertTrue(result.success)
            self.assertEqual(len(captured), 1)
            self.assertEqual(
                captured[0],
                {
                    "Coverage A": "1200000",
                    "Coverage B": "120000",
                    "Coverage C": "500000",
                    "Coverage D": "500000",
                    "Coverage F": "10000",
                },
            )
            self.assertNotIn("Coverage E", captured[0])
            self.assertNotIn("Dwelling", captured[0])
            self.assertNotIn("Other Structures", captured[0])
            self.assertNotIn("Personal Liability EA OCC", captured[0])
            self.assertNotIn("Medical Payments EA PER", captured[0])
            error = result.error or ""
            self.assertNotIn("Dwelling", error)

        asyncio.run(_run())

    def test_empty_live_fill_error_names_coverage_letters_not_dwelling(self) -> None:
        async def _run() -> None:
            page = _MintedPage()
            page.evaluate = AsyncMock(return_value=list(LIVE_HOME_LABELS))
            setup = EzlynxPolicySetupPage(
                page,
                job_id="d5aed721-empty",
                hitl_deps={
                    "email_sender": lambda **_k: None,
                    "chat_sender": lambda _m: True,
                },
            )

            async def _already_minted(policy_id, applicant_id="", **_k):
                return {
                    "formentry_found": True,
                    "formentry_url": REAL_FORMENTRY_URL,
                    "policy_id": policy_id,
                }

            setup._mint_formentry = _already_minted  # type: ignore[method-assign]
            with (
                patch(
                    "robie_job_engine.hitl_escalation.escalate",
                    return_value=HitlResponse(
                        source="system",
                        suggestion="STOP AND ASK",
                        actionable=False,
                        hitl_posted=True,
                    ),
                ),
                patch("robie_job_engine.ezlynx_api.EzlynxApiClient"),
                patch("robie_job_engine.ezlynx_api.load_ezlynx_api_config"),
                patch(
                    "robie_job_engine.policy_setup_proof.search_first_create",
                    return_value={
                        "policy_id": "83669533",
                        "verdict": "ALREADY_EXISTS",
                        "read_back": {"policyId": "83669533"},
                    },
                ),
                patch(
                    "robie_job_engine.formentry_coverages.afill_coverages_by_label",
                    return_value={
                        "filled_count": 0,
                        "not_found": [
                            "Coverage A",
                            "Coverage B",
                            "Coverage C",
                            "Coverage D",
                            "Coverage F",
                        ],
                        "labels": {},
                    },
                ),
            ):
                result = await setup.setup_policy_by_lob(
                    PolicyShellInput(
                        applicant_id="220250093",
                        lob="HOME",
                        policy_number="TEST-HO-20260911-E01",
                        effective_date="10/02/2026",
                        expiration_date="10/02/2027",
                        homeowners_coverage=CARLO_AMOUNTS,
                    )
                )

            error = result.error or ""
            self.assertIn("no coverage labels were filled", error)
            self.assertIn(
                "Looked for ['Coverage A', 'Coverage B', 'Coverage C', "
                "'Coverage D', 'Coverage F']",
                error,
            )
            self.assertNotIn("Looked for ['Dwelling'", error)
            self.assertNotIn("Other Structures", error)
            self.assertNotIn("Personal Liability EA OCC", error)

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
