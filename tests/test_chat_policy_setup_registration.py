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
        self.assertNotIn('toolset="ezlynx"', source)


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
            self.assertIn("STOP AND ASK", posted[0][1])
            self.assertNotIn("still working", posted[0][1].casefold())


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
            self.assertIn("STOP AND ASK", response)
            self.assertNotIn("still working", response.casefold())
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


if __name__ == "__main__":
    unittest.main()
