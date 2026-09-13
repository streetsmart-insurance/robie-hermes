"""HOME Job Engine fill/mint: #388 live-option helper, no alias maps.

Mocks only. No Chrome. No live EZLynx. Applicant 220250093 / policy 83669533.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import unittest
from pathlib import Path

from robie_job_engine.ezlynx_field_widgets import BILLING_TYPE_WIDGET, DEPARTMENT_WIDGET
from robie_job_engine.ezlynx_account_nav import FORMENTRY_RE
from robie_job_engine.ezlynx_policy_setup import (
    CODE_VERSION,
    EzlynxPolicySetup,
    EzlynxPolicySetupPage,
    is_commercial_lob,
    normalize_lob,
    running_code_version,
    url_is_minted_formentry,
)
from tests.test_ezlynx_field_widgets import EditPolicyPage, FakeGemini


STALE_CODE_VERSION = "c6d216cec99662b945d81a69b2a2c2d55eeee7b3"
MODULE_PATH = Path("robie_job_engine/ezlynx_policy_setup.py")


class SequencedGemini:
    def __init__(self, payloads: list[str]) -> None:
        self.payloads = list(payloads)
        self.prompts: list[str] = []

    def generate_unique_field(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.payloads:
            return '{"decision":"unsure","reason":"no more answers"}'
        return self.payloads.pop(0)

    def generate_content(self, prompt: str) -> str:
        return self.generate_unique_field(prompt)


class HomeLobClassificationTests(unittest.TestCase):
    def test_home_token_is_homeowners_not_passthrough(self) -> None:
        self.assertEqual(normalize_lob("HOME"), "Homeowners")
        self.assertEqual(normalize_lob("home"), "Homeowners")
        self.assertEqual(normalize_lob("HO"), "Homeowners")

    def test_home_is_not_commercial(self) -> None:
        for lob in ("HOME", "home", "HO", "homeowners", "Homeowners"):
            self.assertFalse(is_commercial_lob(lob), lob)

    def test_commercial_auto_is_still_commercial(self) -> None:
        self.assertTrue(is_commercial_lob("commercial_auto"))
        self.assertTrue(is_commercial_lob("Auto (Commercial)"))


class HomeRequiredFieldFillTests(unittest.TestCase):
    def test_home_wants_personal_and_applies_gemini_named_live_options(self) -> None:
        async def _run() -> None:
            page = EditPolicyPage(
                department_options=[
                    "---Select---",
                    "Personal Lines (P/L)",
                    "Commercial Lines (CL)",
                ],
                billing_options=["---Select---", "Agency", "Direct"],
            )
            client = SequencedGemini(
                [
                    json.dumps({"decision": "unique", "option": "Direct"}),
                    json.dumps({"decision": "unique", "option": "Personal Lines (P/L)"}),
                ]
            )
            setup = EzlynxPolicySetupPage(
                page,
                job_id="e01-home-fill",
                hitl_deps={"gemini_client": client},
            )
            setup.lob = "HOME"

            result = await setup._fill_required_policy_fields()

            self.assertEqual(result["department_wanted"], "Personal")
            self.assertEqual(result["billing"], "Direct")
            self.assertEqual(result["department"], "Personal Lines (P/L)")
            self.assertEqual(page.billing_selected, "Direct")
            self.assertEqual(page.dept_selected, "Personal Lines (P/L)")
            self.assertEqual(result["billing_via"], "gemini")
            self.assertEqual(result["department_via"], "gemini")
            self.assertFalse(result["billing_fill"]["hitl"])
            self.assertFalse(result["department_fill"]["hitl"])
            self.assertIn("Direct Bill", client.prompts[0])
            self.assertIn("Personal", client.prompts[1])
            self.assertNotIn("Commercial", result["department_wanted"])

        asyncio.run(_run())

    def test_ezlynx_policy_setup_alias_uses_same_home_fill(self) -> None:
        self.assertIs(EzlynxPolicySetup, EzlynxPolicySetupPage)

        async def _run() -> None:
            page = EditPolicyPage(
                department_options=["---Select---", "Personal Lines (P/L)"],
                billing_options=["---Select---", "Agency", "Direct"],
            )
            setup = EzlynxPolicySetup(
                page,
                hitl_deps={
                    "gemini_client": SequencedGemini(
                        [
                            json.dumps({"decision": "unique", "option": "Direct"}),
                            json.dumps(
                                {"decision": "unique", "option": "Personal Lines (P/L)"}
                            ),
                        ]
                    )
                },
            )
            setup.lob = "HOME"
            result = await setup._fill_required_policy_fields()
            self.assertEqual(result["billing"], "Direct")
            self.assertEqual(result["department"], "Personal Lines (P/L)")

        asyncio.run(_run())

    def test_gemini_unsure_then_named_live_option_on_retry_is_applied(self) -> None:
        async def _run() -> None:
            page = EditPolicyPage(
                department_options=["---Select---", "Personal Lines (P/L)"],
                billing_options=["---Select---", "Agency", "Direct"],
            )

            class _RetryGemini:
                def __init__(self) -> None:
                    self.unique_prompts: list[str] = []
                    self.content_prompts: list[str] = []
                    self._unique = [
                        json.dumps(
                            {
                                "decision": "unsure",
                                "reason": "wanted value is not an exact live option",
                            }
                        ),
                        json.dumps(
                            {
                                "decision": "unsure",
                                "reason": "wanted value is not an exact live option",
                            }
                        ),
                    ]
                    self._content = ["Direct", "Personal Lines (P/L)"]

                def generate_unique_field(self, prompt: str) -> str:
                    self.unique_prompts.append(prompt)
                    return self._unique.pop(0)

                def generate_content(self, prompt: str) -> str:
                    self.content_prompts.append(prompt)
                    return self._content.pop(0)

            client = _RetryGemini()
            setup = EzlynxPolicySetupPage(
                page,
                hitl_deps={"gemini_client": client},
            )
            setup.lob = "HOME"
            result = await setup._fill_required_policy_fields()
            self.assertEqual(result["department_wanted"], "Personal")
            self.assertEqual(result["billing"], "Direct")
            self.assertEqual(result["department"], "Personal Lines (P/L)")
            self.assertEqual(page.billing_selected, "Direct")
            self.assertEqual(page.dept_selected, "Personal Lines (P/L)")
            self.assertEqual(len(client.unique_prompts), 2)
            self.assertEqual(len(client.content_prompts), 2)
            self.assertFalse(result["billing_fill"]["hitl"])
            self.assertFalse(result["department_fill"]["hitl"])

        asyncio.run(_run())

    def test_gemini_still_unsure_is_hitl_no_select(self) -> None:
        async def _run() -> None:
            page = EditPolicyPage(
                department_options=["---Select---", "Personal Lines (P/L)"],
                billing_options=["---Select---", "Agency", "Direct"],
            )
            setup = EzlynxPolicySetupPage(
                page,
                hitl_deps={
                    "gemini_client": FakeGemini(
                        '{"decision":"unsure","reason":"cannot name one"}'
                    )
                },
            )
            setup.lob = "HOME"
            with self.assertRaisesRegex(RuntimeError, "HITL"):
                await setup._fill_required_policy_fields()
            self.assertEqual(page.billing_selected, "")
            self.assertEqual(page.dept_selected, "")

        asyncio.run(_run())

    def test_every_identified_dropdown_uses_fill_identified_widget(self) -> None:
        async def _run() -> None:
            page = EditPolicyPage(
                department_options=["---Select---", "Personal Lines (P/L)"],
                billing_options=["---Select---", "Agency", "Direct"],
            )
            setup = EzlynxPolicySetupPage(
                page,
                hitl_deps={
                    "gemini_client": SequencedGemini(
                        [
                            json.dumps({"decision": "unique", "option": "Direct"}),
                            json.dumps(
                                {"decision": "unique", "option": "Personal Lines (P/L)"}
                            ),
                        ]
                    )
                },
            )
            setup.lob = "HOME"
            called: list[tuple[str, str]] = []
            real = setup._fill_identified_dropdown

            async def _spy(widget, wanted: str):
                called.append((widget.name, wanted))
                return await real(widget, wanted)

            setup._fill_identified_dropdown = _spy  # type: ignore[method-assign]
            await setup._fill_required_policy_fields()
            self.assertEqual(
                called,
                [
                    (BILLING_TYPE_WIDGET.name, "Direct Bill"),
                    (DEPARTMENT_WIDGET.name, "Personal"),
                ],
            )

        asyncio.run(_run())

    def test_policy_setup_source_has_no_alias_labels(self) -> None:
        text = MODULE_PATH.read_text()
        self.assertNotIn("Personal Lines (P/L)", text)
        self.assertNotIn("Commercial Lines (CL)", text)
        self.assertNotIn('ALIASES', text)
        self.assertNotIn(STALE_CODE_VERSION, text)
        self.assertIn("fill_identified_widget", text)
        self.assertIn("Direct Bill", text)
        self.assertIn("Personal", text)


class HomeSetupPolicyByLobTests(unittest.TestCase):
    def test_home_setup_sets_personal_department_before_mint(self) -> None:
        async def _run() -> None:
            page = EditPolicyPage(
                department_options=["---Select---", "Personal Lines (P/L)"],
                billing_options=["---Select---", "Agency", "Direct"],
            )
            client = SequencedGemini(
                [
                    json.dumps({"decision": "unique", "option": "Direct"}),
                    json.dumps({"decision": "unique", "option": "Personal Lines (P/L)"}),
                ]
            )
            setup = EzlynxPolicySetup(
                page,
                job_id="83669533-home",
                hitl_deps={"gemini_client": client},
            )
            captured: dict = {}

            async def _spy_fill() -> dict:
                captured["lob"] = setup.lob
                captured["fill"] = await EzlynxPolicySetupPage._fill_required_policy_fields(
                    setup
                )
                return captured["fill"]

            async def _spy_mint(
                policy_id: str, applicant_id: str = "", **_kwargs
            ) -> dict:
                fill = await setup._fill_required_policy_fields()
                captured["mint_policy_id"] = policy_id
                captured["mint_applicant_id"] = applicant_id
                return {
                    "code_version": CODE_VERSION,
                    "formentry_found": True,
                    "formentry_url": (
                        "https://app.ezlynx.com/applicantportal/Policy/"
                        f"{policy_id}/FormEntry/Index/1"
                    ),
                    "field_fill": fill,
                    "via": "save_and_continue_edit",
                }

            setup._fill_required_policy_fields = _spy_fill  # type: ignore[method-assign]
            setup._mint_formentry = _spy_mint  # type: ignore[method-assign]

            from unittest.mock import patch

            from robie_job_engine.ezlynx_policy_setup import (
                HomeownersCoverageItem,
                PolicyShellInput,
            )

            with patch("robie_job_engine.ezlynx_api.EzlynxApiClient"), patch(
                "robie_job_engine.ezlynx_api.load_ezlynx_api_config"
            ), patch(
                "robie_job_engine.policy_setup_proof.search_first_create",
                return_value={
                    "policy_id": "83669533",
                    "verdict": "ALREADY_EXISTS",
                    "read_back": {"policyId": "83669533"},
                },
            ), patch(
                "robie_job_engine.formentry_coverages.afill_coverages_by_label",
                return_value={"filled_count": 1, "not_found": []},
            ):
                result = await setup.setup_policy_by_lob(
                    PolicyShellInput(
                        applicant_id="220250093",
                        lob="HOME",
                        policy_number="TEST-HO-20260911-E01",
                        effective_date="10/02/2026",
                        expiration_date="10/02/2027",
                        homeowners_coverage=HomeownersCoverageItem(
                            dwelling_a="250000"
                        ),
                    )
                )

            self.assertEqual(setup.lob, "Homeowners")
            self.assertFalse(is_commercial_lob(setup.lob))
            self.assertEqual(captured["mint_policy_id"], "83669533")
            self.assertEqual(captured["mint_applicant_id"], "220250093")
            self.assertEqual(captured["fill"]["department_wanted"], "Personal")
            self.assertEqual(captured["fill"]["billing"], "Direct")
            self.assertEqual(captured["fill"]["department"], "Personal Lines (P/L)")
            self.assertTrue(result.success)
            self.assertEqual(result.phase_reached, "coverage_fill")

        asyncio.run(_run())


REAL_FORMENTRY_URL = (
    "https://app.ezlynx.com/applicantportal/Policy/83669533/FormEntry/Index/"
    "480541001?prevApplied=480541001"
)
EDIT_POLICY_URL = (
    "https://app.ezlynx.com/applicantportal/Policy/Actions/Edit/"
    "220250093/83669533"
)
OLD_ACCOUNT_FORMENTRY_URL = (
    "https://app.ezlynx.com/applicantportal/FormEntry/220250093"
)


class MintedFormEntryUrlTests(unittest.TestCase):
    def test_account_nav_regex_misses_the_live_policy_formentry_url(self) -> None:
        self.assertIsNone(FORMENTRY_RE.search(REAL_FORMENTRY_URL))
        self.assertIsNotNone(FORMENTRY_RE.search(OLD_ACCOUNT_FORMENTRY_URL))

    def test_mint_detector_accepts_policy_index_url(self) -> None:
        self.assertTrue(url_is_minted_formentry(REAL_FORMENTRY_URL))
        self.assertTrue(url_is_minted_formentry(OLD_ACCOUNT_FORMENTRY_URL))
        self.assertFalse(url_is_minted_formentry(EDIT_POLICY_URL))
        self.assertFalse(url_is_minted_formentry(""))
        self.assertFalse(
            url_is_minted_formentry(
                "https://app.ezlynx.com/applicantportal/Policy/83669533/Edit"
            )
        )

    def test_mint_after_save_recognizes_policy_formentry_index_url(self) -> None:
        async def _run() -> None:
            from unittest.mock import AsyncMock, MagicMock

            class _Btn:
                def __init__(self, page: "_MintPage") -> None:
                    self._page = page
                    self.first = self
                    self.clicked = False

                async def count(self) -> int:
                    return 1

                async def click(self) -> None:
                    self.clicked = True
                    self._page.url = REAL_FORMENTRY_URL

            class _MintPage:
                def __init__(self) -> None:
                    self.url = EDIT_POLICY_URL
                    self.button = _Btn(self)
                    self.context = MagicMock()
                    self.context.pages = [self]
                    self.frames = []
                    self.goto = AsyncMock()
                    self.wait_for_timeout = AsyncMock()
                    self.screenshot = AsyncMock()
                    self.evaluate = AsyncMock(return_value={})

                def get_by_role(self, role: str, name=None):
                    if role == "button" and name == "Save & Continue Edit":
                        return self.button
                    missing = _Btn(self)

                    async def _zero() -> int:
                        return 0

                    missing.count = _zero  # type: ignore[method-assign]
                    return missing

                def locator(self, *_args, **_kwargs):
                    return self.get_by_role("none")

            page = _MintPage()
            setup = EzlynxPolicySetupPage(
                page,
                job_id="mint-url",
                hitl_deps={
                    "gemini_client": FakeGemini('{"decision":"unsure"}'),
                    "email_sender": lambda **_k: None,
                    "chat_sender": lambda _m: False,
                },
            )
            setup.lob = "HOME"
            setup.applicant_id = "220250093"

            async def _fill() -> dict:
                return {
                    "billing": "Direct",
                    "department": "Personal Lines (P/L)",
                    "department_wanted": "Personal",
                }

            setup._fill_required_policy_fields = _fill  # type: ignore[method-assign]
            nav = await setup._mint_formentry("83669533", "220250093")
            self.assertTrue(page.button.clicked)
            self.assertTrue(nav.get("formentry_found"), nav)
            self.assertEqual(nav.get("formentry_url"), REAL_FORMENTRY_URL)
            self.assertEqual(nav.get("via"), "save_and_continue_edit")
            self.assertEqual(nav.get("code_version"), CODE_VERSION)

        asyncio.run(_run())


class CodeVersionHonestyTests(unittest.TestCase):
    def test_code_version_is_running_tree_not_leftover_sha(self) -> None:
        self.assertEqual(CODE_VERSION, running_code_version())
        self.assertNotEqual(CODE_VERSION, STALE_CODE_VERSION)
        self.assertNotEqual(CODE_VERSION, "unknown")
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        head = (out.stdout or "").strip()
        if out.returncode == 0 and head:
            self.assertEqual(CODE_VERSION, head)

if __name__ == "__main__":
    unittest.main()
