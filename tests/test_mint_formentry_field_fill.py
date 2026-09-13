"""Field-fill failure must not be overwritten by the 30s FormEntry line.

16th E01 run (job d2640bac): _fill_required_policy_fields raised, the click
and 30s poll never ran, then _mint_formentry always wrote
'Save & Continue Edit clicked; no FormEntry URL after 30s.' HITL never
fired. The outer PolicySetupResult.to_dict() dropped field_fill_error.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from robie_job_engine.ezlynx_policy_setup import (
    CODE_VERSION,
    EzlynxPolicySetupPage,
    PolicyShellInput,
    policy_setup_result_from_formentry_nav,
)
from robie_job_engine.hitl_escalation import HitlResponse


THIRTY_SECOND_LIE = "Save & Continue Edit clicked; no FormEntry URL after 30s"


class _Locator:
    def __init__(self, count: int = 0) -> None:
        self._count = count
        self.first = self
        self.clicked = False

    async def count(self) -> int:
        return self._count

    async def click(self) -> None:
        self.clicked = True
        raise AssertionError(
            "Save & Continue Edit must not be clicked after field-fill failure"
        )

    def nth(self, _index: int) -> "_Locator":
        return self

    async def inner_text(self) -> str:
        return ""


class _Page:
    url = (
        "https://app.ezlynx.com/applicantportal/Policy/Actions/Edit/"
        "220250093/83669533"
    )

    def __init__(self) -> None:
        self.button = _Locator(1)
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
        return _Locator(0)

    def locator(self, *_args, **_kwargs):
        return _Locator(0)


def _hitl_fail_closed() -> HitlResponse:
    return HitlResponse(
        source="system",
        suggestion="Could not send HITL notification to Carlo (test)",
        actionable=False,
    )


class MintFormentryFieldFillTests(unittest.TestCase):
    def test_field_fill_failure_does_not_claim_30s_click_calls_hitl_and_keeps_outer_evidence(
        self,
    ) -> None:
        async def _run() -> None:
            page = _Page()
            setup = EzlynxPolicySetupPage(
                page,
                job_id="d2640bac-test",
                hitl_deps={
                    "email_sender": lambda **_k: None,
                    "chat_sender": lambda _msg: False,
                },
            )

            async def _fill_boom() -> dict:
                raise RuntimeError("Billing Type option 'Direct Bill' not found")

            setup._fill_required_policy_fields = _fill_boom  # type: ignore[method-assign]
            captured_nav: dict = {}

            async def _spy_mint(policy_id: str, applicant_id: str = "") -> dict:
                nav = await EzlynxPolicySetupPage._mint_formentry(
                    setup, policy_id, applicant_id
                )
                captured_nav.update(nav)
                return nav

            setup._mint_formentry = _spy_mint  # type: ignore[method-assign]

            with patch(
                "robie_job_engine.hitl_escalation.escalate",
                return_value=_hitl_fail_closed(),
            ) as mock_escalate, patch(
                "robie_job_engine.ezlynx_api.EzlynxApiClient"
            ), patch(
                "robie_job_engine.ezlynx_api.load_ezlynx_api_config"
            ), patch(
                "robie_job_engine.policy_setup_proof.search_first_create",
                return_value={
                    "policy_id": "83669533",
                    "verdict": "CREATED",
                    "read_back": {"policyId": "83669533"},
                },
            ):
                result = await setup.setup_policy_by_lob(
                    PolicyShellInput(
                        applicant_id="220250093",
                        lob="HOME",
                        policy_number="TEST-HO-20260911-E01",
                        effective_date="10/02/2026",
                        expiration_date="10/02/2027",
                    )
                )

            nav = captured_nav
            self.assertTrue(nav, "_mint_formentry must have run")
            self.assertNotIn(THIRTY_SECOND_LIE, nav.get("error") or "")
            self.assertIn("Failed to fill required fields", nav["error"])
            self.assertIn("skipped Save & Continue Edit click", nav["error"])
            self.assertIn("Billing Type option", nav["field_fill_error"])
            self.assertFalse(nav.get("formentry_found"))
            self.assertFalse(page.button.clicked)
            mock_escalate.assert_called_once()
            hitl_req = mock_escalate.call_args[0][0]
            self.assertEqual(hitl_req.phase, "formentry_mint")
            self.assertIn("Failed to fill required fields", hitl_req.error)
            self.assertIn("HITL posted=false", nav["error"])
            self.assertEqual(nav.get("code_version"), CODE_VERSION)

            outer = result.to_dict()
            self.assertEqual(outer["phase_reached"], "formentry_mint")
            self.assertFalse(outer["success"])
            self.assertNotIn(THIRTY_SECOND_LIE, outer.get("error") or "")
            self.assertIn("field_fill_error", outer)
            self.assertIn("Billing Type option", outer["field_fill_error"])
            self.assertEqual(outer.get("hitl_response", {}).get("source"), "system")
            self.assertEqual(outer.get("code_version"), CODE_VERSION)
            self.assertEqual(outer.get("landed_url"), page.url)
            self.assertIn("validation", outer)

            rebuilt = policy_setup_result_from_formentry_nav(
                applicant_id="220250093",
                policy_number="TEST-HO-20260911-E01",
                lob="HOME",
                nav=nav,
            ).to_dict()
            self.assertEqual(rebuilt["field_fill_error"], outer["field_fill_error"])

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
