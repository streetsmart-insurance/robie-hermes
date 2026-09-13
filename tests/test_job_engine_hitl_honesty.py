"""Job Engine HOME HITL: STOP AND ASK in the same Chat thread.

Mocks only. No Chrome. No live EZLynx. Applicant 220250093 / policy 83669533.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from robie_job_engine.chat_app_post import (
    conversation_target,
    post_hitl_to_originating_thread,
)
from robie_job_engine.ezlynx_field_widgets import DEPARTMENT_WIDGET, fill_identified_widget
from robie_job_engine.ezlynx_policy_setup import (
    EzlynxPolicySetupPage,
    FieldFillHitlError,
    PolicyShellInput,
    policy_setup_hitl_blocks_continue,
)
from robie_job_engine.hitl_escalation import (
    HitlRequest,
    build_hitl_notice,
    escalate,
    gemini_resolved_and_job_continuing,
    live_control_shows_named_option,
)
from tests.test_ezlynx_field_widgets import EditPolicyPage, FakeGemini
from tests.test_mint_formentry_field_fill import _Page


MODULE_PATH = Path("robie_job_engine/ezlynx_policy_setup.py")
HITL_MODULE = Path("robie_job_engine/hitl_escalation.py")
TOOL_PATH = Path("deploy/hermes/tools/policy_setup_tool.py")
CHAT_POST = Path("robie_job_engine/chat_app_post.py")


def _job(*, space="spaces/ROBIE", thread="spaces/ROBIE/threads/abc"):
    return {
        "id": "job-hitl-1",
        "payload": {
            "conversation_id": space,
            "thread_name": thread,
        },
    }


class SameThreadHitlTests(unittest.TestCase):
    def test_conversation_target_is_originating_space_and_thread(self) -> None:
        space, thread = conversation_target(_job())
        self.assertEqual(space, "spaces/ROBIE")
        self.assertEqual(thread, "spaces/ROBIE/threads/abc")

    def test_post_uses_job_thread_not_webhook(self) -> None:
        posted: list[tuple[str, str | None]] = []

        def poster(space, message, thread_name=None):
            posted.append((space, thread_name))
            self.assertIn("ROBIE HITL", message)
            return {"name": "spaces/ROBIE/messages/1"}

        store = MagicMock()
        store.get_job.return_value = _job()
        ok = post_hitl_to_originating_thread(
            "ROBIE HITL: STOP AND ASK",
            job_id="job-hitl-1",
            store=store,
            poster=poster,
        )
        self.assertTrue(ok)
        self.assertEqual(posted, [("spaces/ROBIE", "spaces/ROBIE/threads/abc")])

    def test_missing_conversation_is_fail_closed(self) -> None:
        store = MagicMock()
        store.get_job.return_value = {"id": "job-hitl-1", "payload": {}}
        self.assertFalse(
            post_hitl_to_originating_thread(
                "ROBIE HITL",
                job_id="job-hitl-1",
                store=store,
                poster=lambda *_a, **_k: {"name": "x"},
            )
        )

    def test_default_sender_source_is_not_webhook(self) -> None:
        setup_src = MODULE_PATH.read_text(encoding="utf-8")
        tool_src = TOOL_PATH.read_text(encoding="utf-8")
        self.assertNotIn("send_google_chat_alert", setup_src)
        self.assertIn("post_hitl_to_originating_thread", setup_src)
        self.assertIn("ROBIE_JOB_ID", tool_src)
        self.assertIn("policy_setup_hitl_blocks_continue", tool_src)
        self.assertNotIn("ROBIE_GOOGLE_CHAT_WEBHOOK_URL", CHAT_POST.read_text(encoding="utf-8"))


class FillHonestyTests(unittest.TestCase):
    def test_named_option_not_applied_is_hitl(self) -> None:
        async def _run() -> None:
            page = EditPolicyPage(
                department_options=["---Select---", "Personal Lines (P/L)"]
            )

            async def _no_match(_page, _widget, _option):
                raise RuntimeError("click missed")

            with patch(
                "robie_job_engine.ezlynx_field_widgets._select_live_option",
                new=_no_match,
            ):
                result = await fill_identified_widget(
                    page,
                    DEPARTMENT_WIDGET,
                    "Personal",
                    gemini_client=FakeGemini(
                        json.dumps(
                            {"decision": "unique", "option": "Personal Lines (P/L)"}
                        )
                    ),
                )
            self.assertTrue(result.hitl)
            self.assertFalse(result.gemini_applied)
            self.assertEqual(result.named_option, "Personal Lines (P/L)")
            self.assertEqual(page.dept_selected, "")

        asyncio.run(_run())

    def test_apply_is_not_hitl(self) -> None:
        async def _run() -> None:
            page = EditPolicyPage(
                department_options=["---Select---", "Personal Lines (P/L)"]
            )
            result = await fill_identified_widget(
                page,
                DEPARTMENT_WIDGET,
                "Personal",
                gemini_client=FakeGemini(
                    json.dumps({"decision": "unique", "option": "Personal Lines (P/L)"})
                ),
            )
            self.assertFalse(result.hitl)
            self.assertTrue(result.gemini_applied)
            self.assertEqual(result.live_visible, "Personal Lines (P/L)")
            self.assertTrue(
                live_control_shows_named_option(
                    HitlRequest(
                        job_id="j",
                        phase="formentry_mint",
                        error="x",
                        page_state={},
                        attempted=[],
                        applicant_id="220250093",
                        gemini_applied=True,
                        gemini_named_option=result.named_option,
                        live_control_shows=result.live_visible,
                    )
                )
            )

        asyncio.run(_run())


class MintStopAndAskTests(unittest.TestCase):
    def test_named_not_applied_does_not_click_save_or_continue(self) -> None:
        async def _run() -> None:
            page = _Page()
            chats: list[str] = []
            setup = EzlynxPolicySetupPage(
                page,
                job_id="job-hitl-1",
                hitl_deps={"chat_sender": lambda msg: chats.append(msg) or True},
            )

            async def _hitl_fill() -> dict:
                raise FieldFillHitlError(
                    type(
                        "F",
                        (),
                        {
                            "error": "retry still failed after applying 'Personal Lines (P/L)'",
                            "widget": "Department",
                            "to_dict": lambda self=None: {
                                "widget": "Department",
                                "wanted": "Personal",
                                "named_option": "Personal Lines (P/L)",
                                "live_visible": "",
                                "gemini_asked": True,
                                "gemini_applied": False,
                                "hitl": True,
                                "selected": None,
                            },
                        },
                    )()
                )

            setup._fill_required_policy_fields = _hitl_fill  # type: ignore[method-assign]
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
                side_effect=AssertionError("must not continue to coverages after HITL"),
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
            self.assertFalse(result.success)
            self.assertFalse(result.continue_after_hitl)
            self.assertTrue(result.hitl_posted)
            self.assertFalse(page.button.clicked)
            self.assertTrue(chats)
            blob = chats[0].casefold()
            self.assertIn("named a live option", blob)
            self.assertIn("could not apply", blob)
            self.assertIn("reply in this chat thread", blob)
            self.assertNotIn("resolved", blob)
            self.assertNotIn("continuing", blob)
            self.assertNotIn("playwright_blocked", blob)
            block = policy_setup_hitl_blocks_continue(result.to_dict())
            self.assertIsNotNone(block)
            self.assertIn("PLAYWRIGHT_BLOCKED", block or "")

        asyncio.run(_run())

    def test_chat_post_failure_is_fail_closed_no_continue(self) -> None:
        async def _run() -> None:
            page = _Page()
            setup = EzlynxPolicySetupPage(
                page,
                job_id="job-hitl-1",
                hitl_deps={"chat_sender": lambda _msg: False},
            )

            async def _hitl_fill() -> dict:
                raise FieldFillHitlError(
                    type(
                        "F",
                        (),
                        {
                            "error": "Gemini is unsure; HITL Carlo",
                            "widget": "Department",
                            "to_dict": lambda self=None: {
                                "widget": "Department",
                                "wanted": "Personal",
                                "named_option": None,
                                "live_visible": None,
                                "gemini_asked": True,
                                "gemini_applied": False,
                                "hitl": True,
                                "selected": None,
                            },
                        },
                    )()
                )

            setup._fill_required_policy_fields = _hitl_fill  # type: ignore[method-assign]
            with patch("robie_job_engine.ezlynx_api.EzlynxApiClient"), patch(
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
                        effective_date="10/02/2026",
                        expiration_date="10/02/2027",
                    )
                )
            self.assertFalse(result.success)
            self.assertFalse(result.hitl_posted)
            self.assertFalse(result.continue_after_hitl)
            self.assertFalse(page.button.clicked)
            self.assertIn("HITL posted=false", result.error or "")
            block = policy_setup_hitl_blocks_continue(result.to_dict())
            self.assertIn("PLAYWRIGHT_BLOCKED", block or "")

        asyncio.run(_run())

    def test_successful_apply_is_not_hitl_notice(self) -> None:
        chats: list[str] = []
        notice = build_hitl_notice(
            HitlRequest(
                job_id="job-hitl-1",
                phase="formentry_mint",
                error="should not post on apply",
                page_state={},
                attempted=["field_fill"],
                applicant_id="220250093",
                gemini_applied=True,
                gemini_named_option="Direct",
                live_control_shows="Direct",
                formentry_exists=True,
            )
        )
        escalate(
            HitlRequest(
                job_id="job-hitl-1",
                phase="formentry_mint",
                error="unused",
                page_state={},
                attempted=[],
                applicant_id="220250093",
                notify_carlo=False,
            ),
            {"chat_sender": lambda msg: chats.append(msg) or True},
        )
        self.assertTrue(gemini_resolved_and_job_continuing(
            HitlRequest(
                job_id="job-hitl-1",
                phase="formentry_mint",
                error="x",
                page_state={},
                attempted=[],
                applicant_id="220250093",
                gemini_applied=True,
                gemini_named_option="Direct",
                live_control_shows="Direct",
                formentry_exists=True,
                job_still_running=True,
                script_or_job_stopped=False,
                save_skipped=False,
            )
        ))
        self.assertIn("the page shows", notice["body"].casefold())
        self.assertNotIn("PLAYWRIGHT_BLOCKED", notice["body"])
        self.assertEqual(chats, [])


class SourceContractTests(unittest.TestCase):
    def test_no_alias_map_and_wanted_strings_stay(self) -> None:
        text = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn('"Direct Bill"', text)
        self.assertIn('"Personal"', text)
        self.assertNotIn("Personal Lines (P/L)", text)
        self.assertNotIn("ALIAS", text)
        hitl = HITL_MODULE.read_text(encoding="utf-8")
        self.assertIn("hitl_posted", hitl)
        copy = Path("robie_job_engine/hitl_copy.py").read_text(encoding="utf-8")
        self.assertIn("Reply to this email", copy)
        self.assertIn("Reply in this Chat thread", copy)


if __name__ == "__main__":
    unittest.main()
