"""Chat worker must REGISTER a callable ezlynx_policy_setup handler.

Live failure (job c282de98, Production zip 3554f5ea): Chat printed
FAIL_CLOSED_MESSAGE, playwright_exec count 0, tool_called=false, and the
Robie space showed RUNNING / still working. Name-only schema pins are not
enough. Mocks only. No Chrome. No live EZLynx. Applicant 220250093 only.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_guard import (
    _looks_in_progress,
    build_chat_execution_text,
    chat_hermes_should_run,
    guard_chat_response,
    open_chat_job,
)
from robie_job_engine.hermes_tool_visibility import (
    email_chat_job_schema,
    expose_guarded_browser,
    inject_email_chat_job_schemas,
    install_email_chat_schema_filter,
    register_policy_setup_callable,
)
from robie_job_engine.models import JobStatus
from robie_job_engine.policy_setup_dispatch import (
    FAIL_CLOSED_MESSAGE,
    POLICY_SETUP_REQUIRED_KIND,
    POLICY_SETUP_TOOL,
    PolicySetupToolMissing,
    is_formentry_mint_miss,
)
from robie_job_engine.store import JobStore


E01 = (
    "Please create the homeowners policy TEST-HO-20260911-E01 "
    "on applicant 220250093 with the coverages from the quote."
)
FAIL_CLOSED_WORKER = f"ROBIE_OUTCOME_UNKNOWN: {FAIL_CLOSED_MESSAGE}"
ROOT = Path(__file__).resolve().parents[1]


class _RecordingRegistry:
    def __init__(self):
        self.tools = {}

    def register(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs


class ChatPolicySetupRegistrationTests(unittest.TestCase):
    def test_schema_injects_policy_setup_and_handler_is_callable(self):
        registry = _RecordingRegistry()
        handler = register_policy_setup_callable(registry)
        self.assertTrue(callable(handler))
        recorded = registry.tools[POLICY_SETUP_TOOL]
        self.assertTrue(callable(recorded["handler"]))
        self.assertEqual(recorded["toolset"], "playwright")
        self.assertEqual(recorded["schema"]["name"], POLICY_SETUP_TOOL)
        schemas = inject_email_chat_job_schemas(
            [{"function": {"name": "playwright_exec"}}]
        )
        names = [item["function"]["name"] for item in schemas]
        self.assertIn("playwright_exec", names)
        self.assertIn(POLICY_SETUP_TOOL, names)
        self.assertNotIn("execute_code", names)
        self.assertNotIn("terminal", names)

    def test_chat_core_pin_also_registers_handler(self):
        registry = _RecordingRegistry()
        runtime = SimpleNamespace(_HERMES_CORE_TOOLS=["playwright_exec"])
        expose_guarded_browser(
            runtime,
            action_type="hermes.google_chat_task",
            env={},
            argv=["hermes", "chat"],
        )
        self.assertIn(POLICY_SETUP_TOOL, runtime._HERMES_CORE_TOOLS)
        handler = register_policy_setup_callable(registry)
        self.assertTrue(callable(handler))
        self.assertTrue(callable(registry.tools[POLICY_SETUP_TOOL]["handler"]))

    def test_schema_wrap_adds_callable_name_not_just_filter(self):
        defs = [{"function": {"name": "playwright_exec"}}]
        fake = SimpleNamespace(
            get_tool_definitions=lambda enabled_toolsets=None, quiet_mode=False,
            disabled_toolsets=None: list(defs)
        )
        import sys

        with patch.dict(sys.modules, {"model_tools": fake}):
            install_email_chat_schema_filter()
            with patch(
                "robie_job_engine.hermes_tool_visibility.is_email_or_chat_worker",
                return_value=True,
            ):
                names = [item["function"]["name"] for item in fake.get_tool_definitions()]
        self.assertIn(POLICY_SETUP_TOOL, names)

    def test_tool_file_registers_playwright_toolset(self):
        source = (ROOT / "deploy" / "hermes" / "tools" / "policy_setup_tool.py").read_text()
        self.assertIn('toolset="playwright"', source)
        self.assertIn("\n    toolset=\"playwright\",", source)


class ChatPolicySetupInvokeTests(unittest.TestCase):
    def test_chat_build_invokes_handler_and_sets_tool_called(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(
                db,
                "msg-invoke",
                E01,
                requested_by="Carlo",
                conversation_id="spaces/ROBIE",
            )
            store = JobStore(db)
            seen = {}

            def fake_invoke(args):
                seen.update(args)
                store.checkpoint(
                    job_id,
                    POLICY_SETUP_REQUIRED_KIND,
                    {
                        "policy_number": args["policy_number"],
                        "tool_called": True,
                        "handler_registered": True,
                    },
                )
                return {"success": True, "policy_id": "83669533"}

            with (
                patch(
                    "robie_job_engine.hermes_tool_visibility.register_policy_setup_callable",
                    return_value=lambda args: {"success": True},
                ),
                patch(
                    "robie_job_engine.policy_setup_dispatch.invoke_policy_setup_tool",
                    side_effect=fake_invoke,
                ),
            ):
                execution = build_chat_execution_text(db, job_id, E01)
            marker = store.get_checkpoint(job_id, POLICY_SETUP_REQUIRED_KIND)
            self.assertTrue(marker["tool_called"])
            self.assertEqual(seen["policy_number"], "TEST-HO-20260911-E01")
            self.assertIn("callable", execution.casefold())
            self.assertIn("do not call playwright_exec", execution.casefold())
            self.assertNotIn("still working", execution.casefold())

    def test_missing_handler_parks_hitl_not_running(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(
                db,
                "msg-missing-tool",
                E01,
                requested_by="Carlo",
                conversation_id="spaces/ROBIE",
            )
            posted = []

            def poster(space, message, thread_name=None):
                posted.append((space, message, thread_name))
                return {"name": "ok"}

            with (
                patch(
                    "robie_job_engine.hermes_tool_visibility.register_policy_setup_callable",
                    return_value=None,
                ),
                patch(
                    "robie_job_engine.chat_app_post.post_as_chat_app",
                    side_effect=poster,
                ),
            ):
                execution = build_chat_execution_text(db, job_id, E01)
            store = JobStore(db)
            job = store.get_job(job_id)
            self.assertEqual(job["status"], JobStatus.AWAITING_HUMAN_INPUT.value)
            self.assertFalse(chat_hermes_should_run(db, job_id))
            marker = store.get_checkpoint(job_id, POLICY_SETUP_REQUIRED_KIND)
            self.assertFalse(marker["tool_called"])
            self.assertIn(FAIL_CLOSED_MESSAGE, execution)
            self.assertTrue(posted)
            self.assertIn("setup tool is not available", posted[0][1].casefold())
            self.assertIn("Reply in this Chat thread", posted[0][1])
            self.assertNotIn("STOP AND ASK", posted[0][1])
            self.assertNotIn("accepted the request and is still working", posted[0][1].casefold())


class FailClosedHitlHonestyTests(unittest.TestCase):
    def test_fail_closed_text_is_not_in_progress(self):
        self.assertFalse(_looks_in_progress(FAIL_CLOSED_WORKER))
        self.assertIn("playwright_exec", FAIL_CLOSED_MESSAGE)

    def test_guard_parks_hitl_instead_of_running_bubble(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(
                db,
                "msg-c282de98",
                "Please finish the EZLynx form",
                requested_by="Carlo",
                conversation_id="spaces/ROBIE",
            )
            posted = []

            def poster(space, message, thread_name=None):
                posted.append((space, message, thread_name))
                return {"name": "ok"}

            with patch(
                "robie_job_engine.chat_app_post.post_as_chat_app",
                side_effect=poster,
            ):
                response = guard_chat_response(db, job_id, FAIL_CLOSED_WORKER)
            store = JobStore(db)
            self.assertEqual(
                store.get_job(job_id)["status"],
                JobStatus.AWAITING_HUMAN_INPUT.value,
            )
            self.assertIn("setup tool is not available", response.casefold())
            self.assertIn("Reply in this Chat thread", response)
            self.assertNotIn("STOP AND ASK", response)
            self.assertNotIn("accepted the request and is still working", response.casefold())
            self.assertNotIn("RUNNING", response)
            self.assertTrue(posted)
            self.assertEqual(posted[0][0], "spaces/ROBIE")

    def test_does_not_weaken_fail_closed_into_playwright_exec(self):
        chat_src = (ROOT / "robie_job_engine" / "chat_guard.py").read_text()
        dispatch = (ROOT / "robie_job_engine" / "policy_setup_dispatch.py").read_text()
        playwright = (
            ROOT / "deploy" / "hermes" / "tools" / "playwright_tool.py"
        ).read_text()
        self.assertIn("will not fall through to playwright_exec", dispatch)
        self.assertIn("POLICY_SETUP_ORDER", playwright)
        self.assertIn("call the 'ezlynx_policy_setup' tool first", playwright)
        self.assertIn("park_policy_setup_fail_closed", chat_src)
        self.assertIn("Do not call playwright_exec", chat_src)
        # Missing tool must raise, never return a playwright_exec fallback.
        with patch(
            "robie_job_engine.policy_setup_dispatch.tool_module_path",
            return_value=Path("/nonexistent/policy_setup_tool.py"),
        ):
            with self.assertRaises(PolicySetupToolMissing):
                from robie_job_engine.policy_setup_dispatch import (
                    load_policy_setup_handler,
                )

                load_policy_setup_handler()


MINT_MISS = (
    "PLAYWRIGHT_BLOCKED: Save & Continue Edit clicked; no FormEntry URL after 30s. "
    "VALIDATION_ERRORS: [] LANDED_URL: "
    "https://app.ezlynx.com/applicantportal/Policy/Actions/Edit/220250093/83669533"
)


class EmailAndMintMissTests(unittest.TestCase):
    def test_tool_callable_on_chat_and_email_actions(self):
        registry = _RecordingRegistry()
        handler = register_policy_setup_callable(registry)
        self.assertTrue(callable(handler))
        self.assertTrue(callable(registry.tools[POLICY_SETUP_TOOL]["handler"]))
        for action in ("hermes.google_chat_task", "hermes.email_task"):
            schema = email_chat_job_schema(["playwright_exec"])
            self.assertIn(POLICY_SETUP_TOOL, schema)
            runtime = SimpleNamespace(_HERMES_CORE_TOOLS=["playwright_exec"])
            expose_guarded_browser(
                runtime, action_type=action, env={}, argv=["hermes", "chat"]
            )
            self.assertIn(POLICY_SETUP_TOOL, runtime._HERMES_CORE_TOOLS)

    def test_mint_miss_is_detected_and_not_in_progress(self):
        self.assertTrue(is_formentry_mint_miss(MINT_MISS))
        self.assertFalse(_looks_in_progress(MINT_MISS))
        from robie_job_engine.ezlynx_policy_setup import url_is_minted_formentry

        self.assertFalse(
            url_is_minted_formentry(
                "https://app.ezlynx.com/applicantportal/Policy/Actions/Edit/"
                "220250093/83669533"
            )
        )
        self.assertTrue(
            url_is_minted_formentry(
                "https://app.ezlynx.com/applicantportal/Policy/83669533/"
                "FormEntry/Index/480541001"
            )
        )

    def test_mint_miss_parks_hitl_not_running(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(
                db,
                "msg-c75aab5c",
                "Please finish the EZLynx form",
                requested_by="Carlo",
                conversation_id="spaces/ROBIE",
            )
            posted = []

            def poster(space, message, thread_name=None):
                posted.append((space, message, thread_name))
                return {"name": "ok"}

            with patch(
                "robie_job_engine.chat_app_post.post_as_chat_app",
                side_effect=poster,
            ):
                response = guard_chat_response(db, job_id, MINT_MISS)
            store = JobStore(db)
            self.assertEqual(
                store.get_job(job_id)["status"],
                JobStatus.AWAITING_HUMAN_INPUT.value,
            )
            self.assertFalse(chat_hermes_should_run(db, job_id))
            self.assertIn("coverage page did not open", response.casefold())
            self.assertIn("Reply in this Chat thread", response)
            self.assertNotIn("STOP AND ASK", response)
            self.assertNotIn("FormEntry does not exist", response)
            self.assertNotIn("accepted the request and is still working", response.casefold())
            self.assertNotIn("RUNNING", response)
            self.assertTrue(posted)

    def test_email_worker_parks_mint_miss_not_unverified(self):
        from robie_job_engine.email_guard import HermesEmailWorker
        from robie_job_engine.models import JobStatus as Status

        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "hermes.email_task",
                {
                    "prompt": E01,
                    "gmail_message_id": "c75aab5c",
                    "request_text": E01,
                },
            )
            worker = HermesEmailWorker(lambda _prompt: MINT_MISS, store)
            result = worker.perform(job, idempotency_key=job["idempotency_key"])
            self.assertEqual(result.hold_status, Status.AWAITING_HUMAN_INPUT)
            self.assertFalse(result.succeeded)
            self.assertIn("no FormEntry URL after 30s", result.error)

    def test_email_job_hitl_uses_robie_home_space(self):
        from robie_job_engine.chat_app_post import (
            DEFAULT_ROBIE_HOME_SPACE,
            conversation_target,
            post_hitl_to_originating_thread,
        )

        posted = []

        def poster(space, message, thread_name=None):
            posted.append((space, message, thread_name))
            return {"name": "ok"}

        store = type("S", (), {})()
        store.get_job = lambda _id: {
            "id": "c75aab5c",
            "action_type": "hermes.email_task",
            "payload": {},
        }
        self.assertEqual(
            conversation_target(store.get_job("c75aab5c")),
            (DEFAULT_ROBIE_HOME_SPACE, None),
        )
        ok = post_hitl_to_originating_thread(
            "ROBIE HITL: STOP AND ASK. FormEntry does not exist.",
            job_id="c75aab5c",
            store=store,
            poster=poster,
        )
        self.assertTrue(ok)
        self.assertEqual(posted[0][0], DEFAULT_ROBIE_HOME_SPACE)

    def test_playwright_still_refused_after_tool_called_without_complete(self):
        source = (
            ROOT / "deploy" / "hermes" / "tools" / "playwright_tool.py"
        ).read_text()
        self.assertIn("setup_complete", source)
        self.assertIn("did not complete FormEntry", source)
        self.assertIn("playwright_exec is refused", source)

    def test_mint_miss_notice_does_not_claim_gemini_handled_it(self):
        from robie_job_engine.hitl_escalation import HitlRequest, build_hitl_notice

        notice = build_hitl_notice(
            HitlRequest(
                job_id="c75aab5c",
                phase="formentry_mint",
                error=MINT_MISS,
                page_state={},
                attempted=["save_and_continue_edit"],
                applicant_id="220250093",
                policy_id="83669533",
                formentry_exists=False,
            ),
            None,
        )
        blob = notice["subject"] + notice["body"] + notice["chat"]
        self.assertIn("coverage page did not open", blob.casefold())
        self.assertNotIn("gemini did not handle this", blob.casefold())
        self.assertNotIn("gemini handled", blob.casefold())
        self.assertNotIn("job is continuing", blob.casefold())
        self.assertNotIn("PLAYWRIGHT_BLOCKED", notice["body"])


if __name__ == "__main__":
    unittest.main()
