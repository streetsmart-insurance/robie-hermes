"""LOB Orig. Date 1/1/1900 must be a validation miss, filled from job effective date.

Job 677f362f: Save & Continue stayed on Edit Policy. The page showed
"LOB Orig. Date: LOB Orig. Date must be after" with value 1/1/1900, but the
worker reported VALIDATION_ERRORS: []. Mocks only. No live EZLynx.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from robie_job_engine.ezlynx_policy_setup import (
    EzlynxPolicySetupPage,
    PolicyShellInput,
    collect_visible_validation_errors,
    is_lob_orig_date_validation_miss,
    is_sentinel_lob_orig_date,
    job_effective_date_for_ezlynx,
    policy_setup_result_from_formentry_nav,
)
from robie_job_engine.hitl_escalation import HitlResponse
from tests.test_ezlynx_field_widgets import EditPolicyPage, FakeGemini
from tests.test_home_formentry_fill import EDIT_POLICY_URL, REAL_FORMENTRY_URL
from tests.test_mint_formentry_field_fill import THIRTY_SECOND_LIE


JOB_EFFECTIVE = "09/15/2026"


class DateHelperTests(unittest.TestCase):
    def test_job_date_is_not_hardcoded_gold(self) -> None:
        self.assertEqual(job_effective_date_for_ezlynx("09/15/2026"), "09/15/2026")
        self.assertEqual(job_effective_date_for_ezlynx("2026-09-15"), "09/15/2026")
        self.assertEqual(job_effective_date_for_ezlynx(""), "")
        self.assertNotEqual(job_effective_date_for_ezlynx("09/15/2026"), "10/02/2026")

    def test_sentinel_1900_is_a_miss(self) -> None:
        self.assertTrue(is_sentinel_lob_orig_date("1/1/1900"))
        self.assertTrue(is_sentinel_lob_orig_date("01/01/1900"))
        self.assertFalse(is_sentinel_lob_orig_date("09/15/2026"))

    def test_visible_must_be_after_banner_is_not_empty_errors(self) -> None:
        snap = {
            "field_errors": [],
            "summary_errors": [],
            "lob_orig_date": "1/1/1900",
            "body_text_sample": (
                "LOB Orig. Date: LOB Orig. Date must be after\n"
                "Cancel  Save  Save & Continue"
            ),
        }
        errors = collect_visible_validation_errors(snap)
        self.assertTrue(errors)
        self.assertTrue(any("must be after" in item.casefold() for item in errors))
        self.assertTrue(is_lob_orig_date_validation_miss(snap))
        self.assertNotEqual(errors, [])


class _DateField:
    def __init__(self, page: "_EditWithBanner") -> None:
        self._page = page
        self.first = self

    async def count(self) -> int:
        return 1

    async def input_value(self) -> str:
        return self._page.lob_orig_date

    async def fill(self, value: str) -> None:
        self._page.lob_orig_date = value
        self._page.banner = ""


class _SaveBtn:
    def __init__(self, page: "_EditWithBanner") -> None:
        self._page = page
        self.first = self
        self.clicks = 0

    async def count(self) -> int:
        return 1

    async def click(self) -> None:
        self.clicks += 1
        self._page.save_clicks += 1
        if self._page.lob_orig_date == JOB_EFFECTIVE and self._page.mint_after_fill:
            self._page.url = REAL_FORMENTRY_URL
            self._page.banner = ""
            return
        if is_sentinel_lob_orig_date(self._page.lob_orig_date):
            self._page.banner = "LOB Orig. Date: LOB Orig. Date must be after"
            self._page.url = EDIT_POLICY_URL
            return
        if self._page.mint_after_fill:
            self._page.url = REAL_FORMENTRY_URL
            self._page.banner = ""
        else:
            self._page.url = EDIT_POLICY_URL
            self._page.banner = "LOB Orig. Date: LOB Orig. Date must be after"


class _EditWithBanner:
    """Edit Policy page: LOB Orig. Date starts at 1/1/1900 with a red banner."""

    def __init__(self, *, mint_after_fill: bool) -> None:
        self.url = EDIT_POLICY_URL
        self.lob_orig_date = "1/1/1900"
        self.banner = "LOB Orig. Date: LOB Orig. Date must be after"
        self.mint_after_fill = mint_after_fill
        self.save_clicks = 0
        self.button = _SaveBtn(self)
        self.context = MagicMock()
        self.context.pages = [self]
        self.frames = []
        self.goto = AsyncMock()
        self.wait_for_timeout = AsyncMock()
        self.screenshot = AsyncMock()

    async def evaluate(self, _js: str) -> dict:
        errors = [self.banner] if self.banner else []
        return {
            "field_errors": [],
            "summary_errors": [],
            "banners": errors,
            "banner_lines": errors,
            "aria_invalid": [],
            "lob_orig_date": self.lob_orig_date,
            "errors": errors,
            "validation_errors": errors,
            "url": self.url,
            "body_text_sample": self.banner,
        }

    def get_by_role(self, role: str, name=None):
        if role == "button" and name == "Save & Continue Edit":
            return self.button
        missing = _SaveBtn(self)

        async def _zero() -> int:
            return 0

        missing.count = _zero  # type: ignore[method-assign]
        return missing

    def locator(self, selector: str, **_kwargs):
        if selector == "#LOBOriginationDate":
            return _DateField(self)
        return self.get_by_role("none")


def _setup(page: _EditWithBanner) -> EzlynxPolicySetupPage:
    setup = EzlynxPolicySetupPage(
        page,
        job_id="677f362f-test",
        hitl_deps={
            "gemini_client": FakeGemini('{"decision":"unsure"}'),
            "email_sender": lambda **_k: None,
            "chat_sender": lambda _m: True,
        },
    )
    setup.lob = "HOME"
    setup.applicant_id = "220250093"
    setup._job_effective_date = JOB_EFFECTIVE
    return setup


class LobOrigDateMintTests(unittest.TestCase):
    def test_fill_from_job_effective_date_then_save_mints(self) -> None:
        async def _run() -> None:
            page = _EditWithBanner(mint_after_fill=True)
            setup = _setup(page)

            async def _dropdown(_widget, wanted: str):
                return type(
                    "Filled",
                    (),
                    {
                        "to_dict": lambda self: {"wanted": wanted},
                        "live_options": [wanted],
                        "selected": wanted,
                        "via": "exact",
                        "hitl": False,
                    },
                )()

            setup._fill_identified_dropdown = _dropdown  # type: ignore[method-assign]
            nav = await setup._mint_formentry(
                "83669533",
                "220250093",
                effective_date=JOB_EFFECTIVE,
            )
            self.assertEqual(page.lob_orig_date, JOB_EFFECTIVE)
            self.assertNotEqual(page.lob_orig_date, "10/02/2026")
            self.assertGreaterEqual(page.save_clicks, 1)
            self.assertTrue(nav.get("formentry_found"), nav)
            self.assertEqual(nav.get("formentry_url"), REAL_FORMENTRY_URL)
            fill = nav.get("field_fill") or {}
            self.assertEqual(fill.get("lob_orig", {}).get("wanted"), JOB_EFFECTIVE)
            self.assertTrue(fill.get("lob_orig", {}).get("filled"))

        asyncio.run(_run())

    def test_banner_is_quoted_not_empty_validation_errors(self) -> None:
        async def _run() -> None:
            page = _EditWithBanner(mint_after_fill=False)
            setup = _setup(page)

            async def _dropdown(_widget, wanted: str):
                return type(
                    "Filled",
                    (),
                    {
                        "to_dict": lambda self: {"wanted": wanted},
                        "live_options": [wanted],
                        "selected": wanted,
                        "via": "exact",
                        "hitl": False,
                    },
                )()

            setup._fill_identified_dropdown = _dropdown  # type: ignore[method-assign]
            with patch(
                "robie_job_engine.hitl_escalation.escalate",
                return_value=HitlResponse(
                    source="system",
                    suggestion="STOP AND ASK",
                    actionable=False,
                    hitl_posted=True,
                ),
            ) as mock_escalate:
                nav = await setup._mint_formentry(
                    "83669533",
                    "220250093",
                    effective_date=JOB_EFFECTIVE,
                )

            error = nav.get("error") or ""
            self.assertIn(THIRTY_SECOND_LIE, error)
            self.assertIn("must be after", error.casefold())
            self.assertNotIn("VALIDATION_ERRORS: []", error)
            self.assertFalse(nav.get("formentry_found"))
            self.assertEqual(nav.get("landed_url"), EDIT_POLICY_URL)
            mock_escalate.assert_called()
            hitl_req = mock_escalate.call_args[0][0]
            self.assertEqual(hitl_req.phase, "formentry_mint")
            self.assertIn("must be after", hitl_req.error.casefold())
            self.assertFalse(hitl_req.gemini_applied)
            notice_blob = (nav.get("hitl_response") or {}).get("suggestion") or ""
            self.assertNotIn("gemini minted", notice_blob.casefold())

        asyncio.run(_run())

    def test_mint_miss_after_fill_is_honest_hitl_on_setup_path(self) -> None:
        async def _run() -> None:
            page = _EditWithBanner(mint_after_fill=False)
            setup = _setup(page)

            async def _dropdown(_widget, wanted: str):
                return type(
                    "Filled",
                    (),
                    {
                        "to_dict": lambda self: {"wanted": wanted},
                        "live_options": [wanted],
                        "selected": wanted,
                        "via": "exact",
                        "hitl": False,
                    },
                )()

            setup._fill_identified_dropdown = _dropdown  # type: ignore[method-assign]
            with patch(
                "robie_job_engine.hitl_escalation.escalate",
                return_value=HitlResponse(
                    source="system",
                    suggestion="STOP AND ASK. FormEntry does not exist.",
                    actionable=False,
                    hitl_posted=True,
                ),
            ), patch("robie_job_engine.ezlynx_api.EzlynxApiClient"), patch(
                "robie_job_engine.ezlynx_api.load_ezlynx_api_config"
            ), patch(
                "robie_job_engine.policy_setup_proof.search_first_create",
                return_value={
                    "policy_id": "83669533",
                    "verdict": "ALREADY_EXISTS",
                    "read_back": {"policyId": "83669533"},
                },
            ):
                result = await setup.setup_policy_by_lob(
                    PolicyShellInput(
                        applicant_id="220250093",
                        lob="HOME",
                        policy_number="TEST-HO-20260911-E01",
                        effective_date=JOB_EFFECTIVE,
                        expiration_date="09/15/2027",
                    )
                )

            self.assertEqual(page.lob_orig_date, JOB_EFFECTIVE)
            self.assertFalse(result.success)
            self.assertEqual(result.phase_reached, "formentry_mint")
            self.assertIn("must be after", (result.error or "").casefold())
            self.assertNotIn("VALIDATION_ERRORS: []", result.error or "")
            outer = result.to_dict()
            self.assertTrue(outer.get("hitl_response"))
            self.assertFalse(outer.get("success"))
            rebuilt = policy_setup_result_from_formentry_nav(
                applicant_id="220250093",
                policy_number="TEST-HO-20260911-E01",
                lob="HOME",
                nav=outer,
            )
            self.assertFalse(rebuilt.success)
            self.assertEqual(rebuilt.phase_reached, "formentry_mint")

        asyncio.run(_run())

    def test_required_fill_writes_job_effective_date_not_gold(self) -> None:
        async def _run() -> None:
            page = EditPolicyPage(
                department_options=["---Select---", "Personal Lines (P/L)"],
                billing_options=["---Select---", "Direct"],
                lob_orig_date="1/1/1900",
            )
            setup = EzlynxPolicySetupPage(
                page,
                job_id="lob-orig-fill",
                hitl_deps={"gemini_client": FakeGemini('{"decision":"unsure"}')},
            )
            setup.lob = "HOME"
            setup._job_effective_date = JOB_EFFECTIVE

            async def _dropdown(_widget, wanted: str):
                return type(
                    "Filled",
                    (),
                    {
                        "to_dict": lambda self: {},
                        "live_options": [wanted],
                        "selected": "Direct" if "Bill" in wanted or wanted == "Direct Bill" else "Personal Lines (P/L)",
                        "via": "exact",
                        "hitl": False,
                    },
                )()

            setup._fill_identified_dropdown = _dropdown  # type: ignore[method-assign]
            filled = await setup._fill_required_policy_fields()
            self.assertEqual(page.lob_orig_date, JOB_EFFECTIVE)
            self.assertEqual(filled["lob_orig"]["before"], "1/1/1900")
            self.assertEqual(filled["lob_orig"]["after"], JOB_EFFECTIVE)
            self.assertTrue(filled["lob_orig"]["sentinel"])

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
