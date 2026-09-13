"""HOME coverage fill must use live FormEntry labels, not Dwelling aliases.

Live miss d5aed721: HITL resume worked, then fill looked for
['Dwelling', 'Other Structures', ...]. HOME FormEntry shows Coverage A–F.
Map stated A/B/C/D/F onto those live labels. Do not invent omitted E.
Mocks only. No live EZLynx. Do not touch job d5aed721.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from robie_job_engine.ezlynx_policy_setup import (
    EzlynxPolicySetupPage,
    HomeownersCoverageItem,
    PolicyShellInput,
    homeowners_amounts_by_letter,
    merge_homeowners_coverage_from_text,
    stated_live_coverage_names,
)
from robie_job_engine.formentry_coverages import (
    HOME_LIVE_COVERAGE_LABELS,
    filter_coverage_candidate_labels,
    is_location_section_labels,
    live_label_for_coverage_letter,
    map_letter_amounts_to_live_labels,
)
from robie_job_engine.hitl_copy import coverage_fill_human_text
from robie_job_engine.hitl_escalation import HitlResponse
from robie_job_engine.policy_setup_dispatch import extract_policy_setup_args
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
FULL_AMOUNTS = HomeownersCoverageItem(
    dwelling_a="1200000",
    other_structures_b="120000",
    personal_property_c="500000",
    loss_of_use_d="500000",
    liability_e="10000",
    med_pay_f="10000",
)
LIVE_HOME_LABELS = list(HOME_LIVE_COVERAGE_LABELS)
LIVE_LOCATION_LABELS = [
    "Name",
    "Address 1",
    "Address 2",
    "City",
    "State",
    "Zip",
    "Country",
    "123 Test St",
    "LOS ANGELES",
    "CA",
    "90001",
    "United States",
    "Actions",
    "Location #",
    "Address",
    "Line Of Business",
]
LETTER_EMAIL = (
    "Please create the homeowners policy TEST-HO-20260911-E01 "
    "on applicant 220250093.\n"
    "A $1,200,000 B $120,000 C $500,000 D $500,000 E $10,000 F $10,000"
)
LIVE_LABEL_MISS = (
    "PLAYWRIGHT_BLOCKED: no coverage labels were filled after Gemini apply + "
    "retry. Looked for ['Address']. "
    "live_labels=['Name', 'Address 1', 'Address 2', 'City', 'State', 'Zip', "
    "'Country', '123 Test St', 'LOS ANGELES', 'CA', '90001', "
    "'United States', 'Actions', 'Location #', 'Address', "
    "'Line Of Business']. HITL posted to the email. STOP AND ASK."
)


class LocationThenCoveragesPage(_MintedPage):
    """FormEntry starts on Location/Address; Coverages tab reveals A–F."""

    def __init__(self) -> None:
        super().__init__()
        self.section = "location"
        self.clicked_tabs: list[str] = []

        async def _evaluate(*_a, **_k):
            if self.section == "location":
                return list(LIVE_LOCATION_LABELS)
            return list(LIVE_HOME_LABELS)

        self.evaluate = _evaluate

    def get_by_role(self, role, name=None, exact=False):
        loc = MagicMock()
        match = role == "tab" and name in ("Coverages", "Coverage") and exact
        loc.count = AsyncMock(return_value=1 if match else 0)

        async def _click(*_a, **_k):
            if match:
                self.section = "coverages"
                self.clicked_tabs.append(str(name))

        loc.click = AsyncMock(side_effect=_click)
        return loc


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

    def test_location_labels_are_not_coverage_candidates(self) -> None:
        self.assertTrue(is_location_section_labels(LIVE_LOCATION_LABELS))
        self.assertFalse(is_location_section_labels(LIVE_HOME_LABELS))
        leftover = filter_coverage_candidate_labels(LIVE_LOCATION_LABELS)
        self.assertEqual(leftover, [])
        self.assertNotIn("Address", leftover)
        self.assertNotIn("Address 1", leftover)
        self.assertNotIn("Name", leftover)
        self.assertNotIn("Line Of Business", leftover)
        self.assertNotIn("City", leftover)
        self.assertNotIn("Location #", leftover)
        mapped = map_letter_amounts_to_live_labels(
            {"A": "1200000", "B": "120000"},
            LIVE_LOCATION_LABELS,
            gemini_client=FakeGemini('{"decision":"apply","option":"Address"}'),
        )
        self.assertEqual(mapped, {})
        self.assertNotIn("Address", mapped)
        self.assertNotIn("Name", mapped)
        self.assertNotIn("Address 1", mapped)
        self.assertNotIn("Line Of Business", mapped)

    def test_retry_leftovers_name_address1_line_of_business_are_not_filled(self) -> None:
        mapped = map_letter_amounts_to_live_labels(
            {
                "A": "1200000",
                "B": "120000",
                "C": "500000",
                "D": "500000",
                "E": "10000",
                "F": "10000",
            },
            LIVE_LOCATION_LABELS,
            gemini_client=FakeGemini(
                '{"decision":"apply","option":"Name"}'
            ),
        )
        self.assertEqual(mapped, {})
        for label in ("Name", "Address 1", "Line Of Business", "Address"):
            self.assertNotIn(label, mapped)


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

    def test_location_tab_navigates_to_coverages_then_fills(self) -> None:
        async def _run() -> None:
            page = LocationThenCoveragesPage()
            setup = EzlynxPolicySetupPage(
                page,
                job_id="44928e33-location-tab",
                hitl_deps={
                    "gemini_client": FakeGemini(
                        '{"decision":"apply","option":"Address"}'
                    ),
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
                        homeowners_coverage=FULL_AMOUNTS,
                    )
                )

            self.assertTrue(result.success)
            self.assertEqual(page.clicked_tabs, ["Coverages"])
            self.assertEqual(page.section, "coverages")
            self.assertEqual(len(captured), 1)
            self.assertEqual(
                captured[0],
                {
                    "Coverage A": "1200000",
                    "Coverage B": "120000",
                    "Coverage C": "500000",
                    "Coverage D": "500000",
                    "Coverage E": "10000",
                    "Coverage F": "10000",
                },
            )
            self.assertNotIn("Address", captured[0])
            self.assertNotIn("#HO_CoverageA", str(captured[0]))

        asyncio.run(_run())

    def test_letter_amounts_on_email_do_not_hitl_for_missing_amounts(self) -> None:
        async def _run() -> None:
            page = LocationThenCoveragesPage()
            setup = EzlynxPolicySetupPage(
                page,
                job_id="44928e33-letter-email",
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
                    "robie_job_engine.hitl_escalation.escalate",
                    side_effect=AssertionError("must not HITL when A-F are on the email"),
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
                        request_text=LETTER_EMAIL,
                    )
                )

            self.assertTrue(result.success)
            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0]["Coverage A"], "1200000")
            self.assertEqual(captured[0]["Coverage E"], "10000")
            self.assertEqual(captured[0]["Coverage F"], "10000")
            self.assertNotIn("Address", captured[0])
            self.assertIsNone(result.error)
            self.assertFalse(result.hitl_posted)

        asyncio.run(_run())


class LetterEmailConsumeTests(unittest.TestCase):
    def test_extract_consumes_letter_form_amounts(self) -> None:
        args = extract_policy_setup_args(LETTER_EMAIL)
        self.assertEqual(args["dwelling"], "1200000")
        self.assertEqual(args["other_structures"], "120000")
        self.assertEqual(args["personal_property"], "500000")
        self.assertEqual(args["loss_of_use"], "500000")
        self.assertEqual(args["personal_liability"], "10000")
        self.assertEqual(args["medical_payments"], "10000")

    def test_policy_number_e01_is_not_coverage_e(self) -> None:
        merged = merge_homeowners_coverage_from_text(
            None,
            "Please create the homeowners policy TEST-HO-20260911-E01 "
            "on applicant 220250093.",
        )
        self.assertEqual(homeowners_amounts_by_letter(merged), {})


class LocationTabHitlCopyTests(unittest.TestCase):
    def test_label_miss_does_not_say_amounts_were_not_on_the_email(self) -> None:
        email = coverage_fill_human_text(channel="email", detail=LIVE_LABEL_MISS)
        chat = coverage_fill_human_text(channel="chat", detail=LIVE_LABEL_MISS)
        for text in (email, chat):
            self.assertNotIn("They were not on the email", text)
            self.assertNotIn("they were not on the email", text.casefold())
            self.assertNotIn("PLAYWRIGHT_BLOCKED", text)
        self.assertIn("could not match the coverage labels", email.casefold())
        self.assertIn("Reply to this email", email)
        self.assertNotIn("Chat thread", email)
        self.assertIn("Reply in this Chat thread", chat)


if __name__ == "__main__":
    unittest.main()
